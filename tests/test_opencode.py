from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from deepflow_swarm.opencode import OpenCodeWorkerBridge, SubmissionUncertain


def messages(completed=True, error=None, text="answer"):
    info = {
        "id": "answer1",
        "role": "assistant",
        "parentID": "msg_request",
        "time": {"created": 1},
        "finish": "stop",
    }
    if completed:
        info["time"]["completed"] = 2
    if error:
        info["error"] = {"name": "APIError", "data": {"message": error}}
    return [
        {"info": {"id": "msg_request", "role": "user"}, "parts": []},
        {"info": info, "parts": [{"type": "text", "text": text}]},
    ]


async def poll(settings, monkeypatch, states, outputs):
    monkeypatch.setattr("deepflow_swarm.opencode.asyncio.sleep", AsyncMock())
    count = 0

    def handler(request):
        nonlocal count
        if request.url.path == "/session/status":
            state = states[min(count, len(states) - 1)]
            return httpx.Response(200, json={} if state is None else {"ses1": {"type": state}})
        index = min(count, len(outputs) - 1)
        count += 1
        return httpx.Response(200, json=outputs[index])

    bridge = OpenCodeWorkerBridge(settings, None)
    async with httpx.AsyncClient(
        base_url="http://worker", transport=httpx.MockTransport(handler)
    ) as client:
        return await bridge._wait_for_result(client, "ses1", "msg_request"), count


async def test_retry_with_partial_text_is_not_finished(settings, monkeypatch):
    output, count = await poll(
        settings,
        monkeypatch,
        ["retry", "idle"],
        [messages(False, text="partial"), messages(text="finished")],
    )
    assert output == "finished"
    assert count == 2


async def test_completed_model_error_is_failure(settings, monkeypatch):
    with pytest.raises(RuntimeError):
        await poll(settings, monkeypatch, [None], [messages(error="model unavailable")])


async def test_absent_status_is_idle_only_for_completed_message(settings, monkeypatch):
    output, count = await poll(
        settings,
        monkeypatch,
        [None, None],
        [messages(False, text="partial"), messages(text="finished")],
    )
    assert output == "finished"
    assert count == 2


async def test_timeout_without_confirmed_abort_is_uncertain(settings, monkeypatch):
    ticks = iter([0, 2])
    settings.swarm_task_timeout_seconds = 1
    monkeypatch.setattr(
        "deepflow_swarm.opencode.time", SimpleNamespace(monotonic=lambda: next(ticks))
    )
    bridge = OpenCodeWorkerBridge(settings, None)
    async with httpx.AsyncClient(
        base_url="http://worker", transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as client:
        with pytest.raises(SubmissionUncertain):
            await bridge._wait_for_result(client, "ses1", "msg_request")


@pytest.mark.parametrize(
    "submission", ["confirmed", "lost_found", "lost_missing", "lost_unavailable"]
)
async def test_bridge_persists_ids_and_never_resends_ambiguous_submission(
    settings, store, tmp_path, monkeypatch, submission
):
    from test_contracts import task

    from deepflow_swarm.models import SwarmPlan

    work = task("a")
    run_id = await store.create_run("implementation", "objective", None, "b" * 32)
    await store.save_plan(run_id, SwarmPlan(tasks=[work]))
    attempt_id = await store.create_attempt(f"{run_id}:a", 1, "worker")
    sandbox = SimpleNamespace(
        id="sandbox1",
        commands=SimpleNamespace(run=AsyncMock(), set_env=AsyncMock()),
        files=SimpleNamespace(write_files=AsyncMock()),
        get_endpoint=AsyncMock(return_value=SimpleNamespace(endpoint="worker:4096", headers={})),
        destroy=AsyncMock(),
    )
    bridge = OpenCodeWorkerBridge(settings, store)
    bridge.sandboxes.create = AsyncMock(return_value=sandbox)
    monkeypatch.setattr("deepflow_swarm.opencode.stage_workspace", AsyncMock())
    monkeypatch.setattr(
        "deepflow_swarm.opencode.run_command", AsyncMock(return_value=(0, "started"))
    )
    monkeypatch.setattr("deepflow_swarm.opencode.collect_patch", AsyncMock(return_value="diff"))
    submitted = []

    def handler(request):
        import json

        path = request.url.path
        if path == "/global/health":
            return httpx.Response(200, json={"healthy": True})
        if path == "/session":
            return httpx.Response(200, json={"id": "ses1"})
        if path.endswith("/prompt_async"):
            submitted.append(json.loads(request.content)["messageID"])
            if submission != "confirmed":
                raise httpx.ReadError("response lost", request=request)
            return httpx.Response(204)
        if path == "/session/status":
            return httpx.Response(200, json={})
        if path.endswith("/message"):
            response = messages()
            response[0]["info"]["id"] = submitted[0]
            response[1]["info"]["parentID"] = submitted[0]
            return httpx.Response(200, json=response)
        status = {"lost_found": 200, "lost_missing": 404, "lost_unavailable": 503}[submission]
        return httpx.Response(status, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "deepflow_swarm.opencode.httpx",
        SimpleNamespace(
            AsyncClient=lambda **kwargs: real_client(
                **kwargs, transport=httpx.MockTransport(handler)
            ),
            Timeout=httpx.Timeout,
            HTTPError=httpx.HTTPError,
        ),
    )
    result = await bridge.execute(
        run_id=run_id, task=work, attempt_id=attempt_id, repo_path=tmp_path, dependency_patches=[]
    )
    assert len(submitted) == 1
    record = await store.get_run(run_id)
    persisted = record["tasks"][0]["attempts"][0]
    assert persisted["sandbox_id"] == "sandbox1"
    assert persisted["opencode_session_id"] == "ses1"
    assert persisted["opencode_message_id"] == submitted[0]
    if submission in {"confirmed", "lost_found"}:
        assert result.success and result.patch == "diff"
        sandbox.destroy.assert_awaited_once()
    else:
        assert result.failure_category == "submission_unknown"
        sandbox.destroy.assert_not_awaited()
