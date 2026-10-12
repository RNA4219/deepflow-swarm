from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from .database import StateStore
from .models import TaskOutcome, WorkTask
from .settings import Settings
from .workspace import OpenSandboxFactory, collect_patch, run_command, stage_workspace

logger = logging.getLogger(__name__)


class SubmissionUncertain(RuntimeError):
    """投入応答が失われ、同一messageを再送すると二重実行の可能性がある。"""


class OpenCodeWorkerBridge:
    def __init__(self, settings: Settings, store: StateStore) -> None:
        self.settings = settings
        self.store = store
        self.sandboxes = OpenSandboxFactory(settings)

    async def execute(
        self,
        *,
        run_id: str,
        task: WorkTask,
        attempt_id: str,
        repo_path: Path,
        dependency_patches: list[str],
    ) -> TaskOutcome:
        sandbox = None
        client: httpx.AsyncClient | None = None
        keep_sandbox = False
        try:
            sandbox = await self.sandboxes.create(
                run_id=run_id, label=task.id, attempt_id=attempt_id
            )
            await self.store.bind_sandbox(attempt_id, str(sandbox.id))
            await stage_workspace(sandbox, repo_path, dependency_patches)
            api_key = self.settings.litellm_api_key.get_secret_value()
            await sandbox.commands.set_env("LITELLM_API_KEY", api_key)
            config = {
                "$schema": "https://opencode.ai/config.json",
                "provider": {
                    "litellm": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "LiteLLM",
                        "options": {
                            "baseURL": self.settings.litellm_base_url,
                            "apiKey": "{env:LITELLM_API_KEY}",
                        },
                        "models": {
                            _model_alias(task, self.settings): {
                                "name": _model_alias(task, self.settings)
                            }
                        },
                    }
                },
            }
            await sandbox.commands.run("mkdir -p /root/.config/opencode")
            from opensandbox.models.filesystem import WriteEntry

            await sandbox.files.write_files(
                [
                    WriteEntry(
                        path="/root/.config/opencode/opencode.json",
                        data=json.dumps(config).encode("utf-8"),
                        mode=0o600,
                    )
                ]
            )
            server_cmd = (
                "cd /workspace/repo && nohup opencode serve --hostname 0.0.0.0 --port 4096 "
                ">/tmp/opencode-server.log 2>&1 </dev/null & echo $!"
            )
            _, startup = await run_command(sandbox, server_cmd, timeout_seconds=15)
            endpoint = await sandbox.get_endpoint(4096)
            base_url = f"{self.settings.open_sandbox_protocol}://{endpoint.endpoint}".rstrip("/")
            client = httpx.AsyncClient(
                base_url=base_url,
                headers=dict(endpoint.headers or {}),
                timeout=httpx.Timeout(connect=5, read=30, write=30, pool=10),
            )
            await self._wait_healthy(client, startup)
            session_response = await client.post("/session", json={"title": task.title[:120]})
            session_response.raise_for_status()
            session_id = str(session_response.json()["id"])
            message_id = f"msg_{uuid.uuid4().hex}"
            await self.store.bind_opencode(attempt_id, session_id, message_id)
            prompt = _worker_prompt(task)
            body = {
                "messageID": message_id,
                "model": {
                    "providerID": "litellm",
                    "modelID": _model_alias(task, self.settings),
                },
                "agent": "build",
                "parts": [{"type": "text", "text": prompt}],
            }
            try:
                response = await client.post(f"/session/{session_id}/prompt_async", json=body)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                # Query with the persisted message ID. An ambiguous request is never blindly re-sent.
                try:
                    found = await client.get(f"/session/{session_id}/message/{message_id}")
                except httpx.HTTPError as lookup_error:
                    raise SubmissionUncertain(
                        f"OpenCode投入結果を照合できません。session={session_id}, message={message_id}"
                    ) from lookup_error
                if found.status_code == 404:
                    raise SubmissionUncertain(
                        "OpenCodeへの投入確認ができません。重複防止のため再送しません。"
                    ) from exc
                if not found.is_success:
                    raise SubmissionUncertain("OpenCodeへの投入結果を照合できません") from exc
            try:
                output = await self._wait_for_result(client, session_id, message_id)
            except httpx.HTTPError as exc:
                raise SubmissionUncertain(
                    f"OpenCode処理中に切断し、結果を照合できません。session={session_id}, "
                    f"message={message_id}"
                ) from exc
            patch = await collect_patch(sandbox)
            return TaskOutcome(success=True, output=output, patch=patch)
        except SubmissionUncertain as exc:
            keep_sandbox = True
            logger.error(
                "OpenCode result is uncertain; preserving sandbox %s for reconciliation",
                getattr(sandbox, "id", "unknown"),
            )
            return TaskOutcome(
                success=False,
                failure_category="submission_unknown",
                failure_reason=str(exc),
            )
        except (httpx.HTTPError, TimeoutError, RuntimeError, ValueError) as exc:
            return TaskOutcome(
                success=False,
                failure_category=_failure_category(exc),
                failure_reason=f"{type(exc).__name__}: {str(exc)[:3000]}",
            )
        except Exception as exc:
            logger.exception("OpenCode worker failed")
            return TaskOutcome(
                success=False,
                failure_category="environment_failure",
                failure_reason=f"{type(exc).__name__}: {str(exc)[:3000]}",
            )
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    logger.warning("OpenCode HTTP client cleanup failed", exc_info=True)
            if sandbox is not None and not keep_sandbox:
                try:
                    await sandbox.destroy()
                except Exception:
                    logger.warning(
                        "OpenSandbox cleanup failed for attempt %s", attempt_id, exc_info=True
                    )
                    # Cleanup failure must not discard a collected artifact or trigger a rerun.

    async def _wait_healthy(self, client: httpx.AsyncClient, startup_output: str) -> None:
        deadline = time.monotonic() + 90
        last_error = ""
        while time.monotonic() < deadline:
            try:
                response = await client.get("/global/health")
                response.raise_for_status()
                if response.json().get("healthy") is True:
                    return
            except (httpx.HTTPError, ValueError) as exc:
                last_error = str(exc)
            await asyncio.sleep(1)
        raise TimeoutError(
            f"OpenCode server readiness timeout: {last_error}; {startup_output[-1000:]}"
        )

    async def _wait_for_result(
        self, client: httpx.AsyncClient, session_id: str, message_id: str
    ) -> str:
        deadline = time.monotonic() + self.settings.swarm_task_timeout_seconds
        empty_idle_polls = 0
        while time.monotonic() < deadline:
            status_response = await client.get("/session/status")
            status_response.raise_for_status()
            status_map = status_response.json()
            current = status_map.get(session_id, {})
            state = current.get("type", "idle") if isinstance(current, dict) else str(current)
            messages_response = await client.get(
                f"/session/{session_id}/message", params={"limit": 100}
            )
            messages_response.raise_for_status()
            messages = messages_response.json()
            user_seen = any(
                _message_id(item) == message_id for item in messages if isinstance(item, dict)
            )
            output = _latest_assistant_text(messages, after_message_id=message_id)
            if user_seen and output and state == "idle":
                return output
            if user_seen and state in {"idle", "error"} and not output:
                empty_idle_polls += 1
                if empty_idle_polls >= 4:
                    raise RuntimeError(
                        f"OpenCode sessionが{state}になりましたが、assistant結果がありません"
                    )
            await asyncio.sleep(2)
        try:
            response = await client.post(f"/session/{session_id}/abort")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SubmissionUncertain(
                f"OpenCode timeout後のabortを確認できません。session={session_id}, "
                f"message={message_id}"
            ) from exc
        raise TimeoutError(f"OpenCode session timeout: {session_id}")


def _worker_prompt(task: WorkTask) -> str:
    criteria = (
        "\n".join(f"- {criterion}" for criterion in task.acceptance_criteria)
        or "依頼内容を満たすこと"
    )
    return f"""このsandbox内の /workspace/repo だけを作業対象にしてください。
担当: {task.title}
作業指示:
{task.objective}

受入条件:
{criteria}

規則:
- 他workerの会話や未確定の報告を事実として扱わない。
- commit、push、外部公開はしない。
- 指示された変更を実装し、必要なローカル確認を行う。
- 完了時に変更内容、実行したコマンド、結果、残課題を簡潔に報告する。
"""


def _message_id(message: dict[str, Any]) -> str | None:
    info = message.get("info")
    return str(info.get("id")) if isinstance(info, dict) and info.get("id") else None


def _latest_assistant_text(messages: list[Any], *, after_message_id: str) -> str:
    output = ""
    passed_user = False
    for item in messages:
        if not isinstance(item, dict):
            continue
        info = item.get("info", {})
        message_id = str(info.get("id", "")) if isinstance(info, dict) else ""
        role = info.get("role") if isinstance(info, dict) else None
        if message_id == after_message_id:
            passed_user = True
            continue
        if passed_user and role == "assistant":
            if info.get("parentID") != after_message_id:
                continue
            if info.get("error"):
                raise RuntimeError(f"OpenCode model error: {info['error']}")
            if not info.get("time", {}).get("completed") or info.get("finish") == "tool-calls":
                continue
            parts = item.get("parts", [])
            if isinstance(parts, list):
                output = (
                    "".join(
                        str(part.get("text", ""))
                        for part in parts
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                    or output
                )
    return output


def _failure_category(exc: Exception) -> str:
    message = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in message or "timed out" in message:
        return "environment_failure"
    if "rate limit" in message or "429" in message or "model" in message:
        return "model_capacity"
    if "json" in message or "missing" in message or "not found" in message:
        return "information_gap"
    return "environment_failure"


def _model_alias(task: WorkTask, settings: Settings) -> str:
    allowed = {"worker", "planner", "reviewer", "local_private"}
    if task.model_role in allowed:
        return task.model_role
    return settings.litellm_worker_model
