import json
from unittest.mock import AsyncMock

import httpx
import pytest

from deepflow_swarm.deerflow import DeerFlowClient, SwarmPlanner


async def client_with_stream(settings, payload):
    client = DeerFlowClient(settings)
    await client.close()

    def handler(request):
        if request.url.path.endswith("/threads"):
            return httpx.Response(200, json={"thread_id": "thread1"})
        assert request.url.path == "/api/langgraph/threads/thread1/runs/stream"
        assert request.headers["Idempotency-Key"] == "stable-request"
        return httpx.Response(
            200, text=payload, headers={"Content-Location": "/threads/thread1/runs/run1"}
        )

    client.client = httpx.AsyncClient(
        base_url=settings.deerflow_langgraph_url, transport=httpx.MockTransport(handler)
    )
    return client


async def test_gateway_refs_are_persisted_before_stream_completion(settings):
    payload = 'event: values\ndata: {"messages":[{"type":"ai","content":"answer"}]}\n\n'
    client = await client_with_stream(settings, payload)
    save_thread, save_run = AsyncMock(), AsyncMock()
    try:
        result = await client.run_prompt(
            "prompt",
            model="worker",
            idempotency_key="stable-request",
            on_thread_created=save_thread,
            on_run_started=save_run,
        )
        assert result.text == "answer"
        save_thread.assert_awaited_once_with("thread1")
        save_run.assert_awaited_once_with("thread1", "run1")
    finally:
        await client.close()


async def test_stream_error_after_partial_output_is_not_success(settings):
    client = await client_with_stream(
        settings,
        'event: messages-tuple\ndata: [{"type":"ai","content":"partial"},{}]\n\n'
        'event: error\ndata: {"error":"model unavailable"}\n\n',
    )
    try:
        with pytest.raises(RuntimeError, match="model unavailable"):
            await client.run_prompt("prompt", model="worker", idempotency_key="stable-request")
    finally:
        await client.close()


@pytest.mark.parametrize(
    "tasks",
    [
        [{"id": "a", "dependencies": []}],
        [{"id": "a", "dependencies": []}, {"id": "b", "dependencies": ["a"]}],
    ],
)
async def test_competition_requires_independent_candidates(settings, tasks):
    for task in tasks:
        task.update(title=task["id"], objective="implement", worker="code")
    client = AsyncMock()
    client.run_prompt.return_value.text = json.dumps({"tasks": tasks})
    with pytest.raises(ValueError):
        await SwarmPlanner(client, settings).plan(
            "objective", "competition", idempotency_key="plan"
        )


async def test_reviewer_never_approves_a_silently_truncated_patch(settings):
    client = AsyncMock()
    with pytest.raises(ValueError, match="30000"):
        await SwarmPlanner(client, settings).review(
            "objective", "x" * 30001, [], idempotency_key="review"
        )
    client.run_prompt.assert_not_awaited()
