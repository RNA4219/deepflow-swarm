"""Credential-free PostgreSQL/OpenCode container smoke checks; uses temporary containers."""

import asyncio
import json
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from uuid import uuid4

import httpx

from deepflow_swarm.database import StateStore
from deepflow_swarm.models import SwarmPlan, WorkTask
from deepflow_swarm.opencode import OpenCodeWorkerBridge
from deepflow_swarm.settings import Settings


def docker(*args):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


async def check_postgres(name):
    docker(
        "run",
        "-d",
        "--name",
        name,
        "-e",
        "POSTGRES_PASSWORD=smoke-only",
        "-p",
        "127.0.0.1::5432",
        "postgres:17",
    )
    port = docker("port", name, "5432/tcp").rsplit(":", 1)[1]
    state = StateStore(f"postgresql+asyncpg://postgres:smoke-only@localhost:{port}/postgres")
    try:
        for attempt in range(60):
            try:
                await state.health()
                break
            except (OSError, Exception):
                if attempt == 59:
                    raise
                await asyncio.sleep(0.5)
        await state.initialize()
        run_id = await state.create_run("implementation", "smoke", None, uuid4().hex)
        await state.save_plan(
            run_id,
            SwarmPlan(
                tasks=[WorkTask(id="worker", title="worker", objective="smoke", worker="code")]
            ),
        )
        attempt_id = await state.create_attempt(f"{run_id}:worker", 1, "worker")
        await state.bind_sandbox(attempt_id, "sandbox1")
        await state.bind_opencode(attempt_id, "session1", "message1")
        await state.finish_attempt(attempt_id, "succeeded")
        await state.finish_task(f"{run_id}:worker", "succeeded", patch="example")
        await state.add_artifact(run_id, "worker_patch", "example", "worker")
        await state.finish_run(run_id, "completed", "example", {"verified": True})
        record = await state.get_run(run_id)
        assert record["status"] == "completed"
        assert record["tasks"][0]["attempts"][0]["opencode_message_id"] == "message1"
        assert record["artifacts"][0]["payload"] == "example"
        return "passed"
    finally:
        await state.close()


async def check_opencode(name):
    class ModelStub(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in [
                ({"role": "assistant", "content": "smoke passed"}, None),
                ({}, "stop"),
            ]:
                payload = {
                    "id": "chatcmpl-smoke",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "worker",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", 0), ModelStub)
    Thread(target=server.serve_forever, daemon=True).start()
    config = json.dumps(
        {
            "provider": {
                "litellm": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Smoke model",
                    "options": {
                        "baseURL": f"http://host.docker.internal:{server.server_port}/v1",
                        "apiKey": "smoke-only",
                    },
                    "models": {"worker": {"name": "worker"}},
                }
            }
        }
    )
    try:
        return await _check_opencode(name, config)
    except Exception:
        print(docker("logs", "--tail", "60", name))
        print(docker("exec", name, "sh", "-lc", "tail -60 /root/.local/share/opencode/log/*.log"))
        raise
    finally:
        server.shutdown()
        server.server_close()


async def _check_opencode(name, config):
    docker(
        "run",
        "-d",
        "--name",
        name,
        "-e",
        "OPENCODE_DISABLE_MODELS_FETCH=true",
        "-e",
        "OPENCODE_CONFIG_CONTENT=" + config,
        "--add-host",
        "host.docker.internal:host-gateway",
        "-p",
        "127.0.0.1::4096",
        "deepflow-swarm/opencode-worker:dev",
        "sh",
        "-lc",
        "mkdir -p /workspace/repo && cd /workspace/repo && opencode serve --hostname 0.0.0.0 --port 4096",
    )
    port = docker("port", name, "4096/tcp").rsplit(":", 1)[1]
    async with httpx.AsyncClient(base_url=f"http://localhost:{port}", timeout=10) as client:
        for attempt in range(60):
            try:
                response = await client.get("/global/health")
                response.raise_for_status()
                assert response.json()["healthy"]
                break
            except (httpx.HTTPError, AssertionError):
                if attempt == 59:
                    raise
                await asyncio.sleep(0.5)
        response = await client.post("/session", json={"title": "deepflow smoke"})
        response.raise_for_status()
        session_id = response.json()["id"]
        success_id = "msg_" + uuid4().hex
        response = await client.post(
            f"/session/{session_id}/prompt_async",
            json={
                "messageID": success_id,
                "model": {"providerID": "litellm", "modelID": "worker"},
                "agent": "build",
                "parts": [{"type": "text", "text": "smoke"}],
            },
        )
        response.raise_for_status()
        bridge = OpenCodeWorkerBridge(Settings(_env_file=None, swarm_task_timeout_seconds=30), None)
        output = await bridge._wait_for_result(client, session_id, success_id)
        assert output == "smoke passed", output
        response = await client.post("/session", json={"title": "failure smoke"})
        response.raise_for_status()
        session_id = response.json()["id"]
        message_id = "msg_" + uuid4().hex
        response = await client.post(
            f"/session/{session_id}/prompt_async",
            json={
                "messageID": message_id,
                "model": {"providerID": "missing", "modelID": "missing"},
                "agent": "build",
                "parts": [{"type": "text", "text": "smoke"}],
            },
        )
        response.raise_for_status()
        # No model credentials: verify submission and explicit failure, never a paid model call.
        bridge = OpenCodeWorkerBridge(Settings(_env_file=None, swarm_task_timeout_seconds=30), None)
        try:
            await bridge._wait_for_result(client, session_id, message_id)
        except RuntimeError:
            # OpenCode may publish the provider error only as an SSE session.error.
            # An idle session without an assistant result must also be rejected.
            response = await client.get(f"/session/{session_id}/message/{message_id}")
            response.raise_for_status()
        else:
            raise RuntimeError("OpenCode incorrectly reported success without a model")
        await client.post(f"/session/{session_id}/abort")
        return {
            "version": docker("exec", name, "opencode", "--version"),
            "health_session_prompt_success_and_failure": "passed",
        }


async def main():
    names = ["deepflow-smoke-pg-" + uuid4().hex[:8], "deepflow-smoke-worker-" + uuid4().hex[:8]]
    started = time.monotonic()
    try:
        postgres = await check_postgres(names[0])
        opencode = await check_opencode(names[1])
        print(
            json.dumps(
                {
                    "postgres": postgres,
                    "opencode": opencode,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                },
                indent=2,
            )
        )
    finally:
        for name in names:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)


if __name__ == "__main__":
    asyncio.run(main())
