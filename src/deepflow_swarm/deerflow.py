from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

from .models import RunProfile, SwarmPlan
from .settings import Settings


@dataclass
class DeerFlowResult:
    text: str
    thread_id: str
    run_id: str | None


class DeerFlowClient:
    """DeerFlow GatewayのLangGraph互換API client。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        headers = {"Accept": "text/event-stream"}
        if settings.deerflow_api_token and settings.deerflow_api_token.get_secret_value().strip():
            headers["Authorization"] = f"Bearer {settings.deerflow_api_token.get_secret_value()}"
        self.client = httpx.AsyncClient(
            base_url=settings.deerflow_langgraph_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(
                connect=10, read=settings.swarm_task_timeout_seconds, write=30, pool=10
            ),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def health(self) -> None:
        base = self.settings.deerflow_langgraph_url.rstrip("/")
        gateway = re.sub(r"/api/langgraph/?$", "", base)
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(urljoin(gateway + "/", "health"))
            response.raise_for_status()

    async def run_prompt(
        self,
        prompt: str,
        *,
        model: str,
        idempotency_key: str,
        correlation: dict[str, str] | None = None,
        on_thread_created: Callable[[str], Awaitable[None]] | None = None,
        on_run_started: Callable[[str, str | None], Awaitable[None]] | None = None,
    ) -> DeerFlowResult:
        thread_response = await self.client.post("/threads", json={"metadata": correlation or {}})
        thread_response.raise_for_status()
        thread_id = str(thread_response.json()["thread_id"])
        if on_thread_created is not None:
            await on_thread_created(thread_id)
        body = {
            "assistant_id": "lead_agent",
            "input": {"messages": [{"role": "user", "content": prompt}]},
            "stream_mode": ["values", "messages-tuple"],
            "stream_subgraphs": False,
            "config": {
                "recursion_limit": 100,
                "configurable": {"thread_id": thread_id, "model_name": model},
                "metadata": correlation or {},
            },
        }
        headers = {"Idempotency-Key": idempotency_key}
        async with self.client.stream(
            "POST", f"/threads/{thread_id}/runs/stream", json=body, headers=headers
        ) as response:
            response.raise_for_status()
            run_id = _run_id_from_location(response.headers.get("Content-Location"))
            if on_run_started is not None:
                await on_run_started(thread_id, run_id)
            chunks: list[str] = []
            final_text = ""
            event_name = "message"
            frame_data: list[str] = []

            async def consume_frame(name: str, payload_text: str) -> None:
                nonlocal final_text
                if not payload_text or payload_text == "[DONE]":
                    return
                try:
                    payload = json.loads(payload_text)
                except json.JSONDecodeError:
                    return
                if name == "error":
                    raise RuntimeError(f"DeerFlow run failed: {payload}")
                text = _message_text(payload)
                if not text:
                    return
                if "value" in name or isinstance(payload, dict) and "messages" in payload:
                    final_text = text
                elif "message" in name or "tuple" in name:
                    chunks.append(text)

            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event_name = line.partition(":")[2].strip()
                elif line.startswith("data:"):
                    frame_data.append(line.partition(":")[2].lstrip())
                elif not line and frame_data:
                    await consume_frame(event_name, "\n".join(frame_data))
                    event_name = "message"
                    frame_data.clear()
            if frame_data:
                await consume_frame(event_name, "\n".join(frame_data))

        output = final_text or "".join(chunks)
        if not output:
            state_response = await self.client.get(f"/threads/{thread_id}/state")
            state_response.raise_for_status()
            output = _message_text(state_response.json().get("values", {}).get("messages", []))
        if not output:
            raise RuntimeError("DeerFlow run は完了しましたが、assistant出力を取得できませんでした")
        return DeerFlowResult(text=output, thread_id=thread_id, run_id=run_id)


class SwarmPlanner:
    def __init__(self, client: DeerFlowClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.last_result: DeerFlowResult | None = None

    async def plan(
        self,
        objective: str,
        profile: RunProfile,
        *,
        idempotency_key: str,
        correlation: dict[str, str] | None = None,
    ) -> SwarmPlan:
        profile_instruction = {
            "research": "調査を観点別に分担し、各結論にURLやファイル行などの根拠を含める。workerはresearch。",
            "implementation": "独立して実装できる単位に分割し、テスト・レビューと統合後検証を考慮する。コード変更はworker=code。",
            "competition": "同じ課題を2〜4個の独立候補に分け、相互依存なしで共通条件を比較できる計画にする。worker=code。",
            "dependency": "前提関係をDAGで表現し、先行タスク完了後に実行できる作業だけを並列にする。",
        }[profile]
        prompt = f"""あなたはDeerFlowを使うオーケストレーションのplannerです。
目的: {objective}
実行profile: {profile}
profile方針: {profile_instruction}

自己完結したtaskを最大{self.settings.swarm_max_tasks}件まで作り、必ず次のJSONだけを返してください。
{{"tasks":[{{"id":"短い一意ID","title":"短い題名","objective":"担当が単独で実行できる指示",
"worker":"researchまたはcode","dependencies":[],"acceptance_commands":[],
"acceptance_criteria":[],"model_role":"worker"}}]}}

task間で会話履歴を共有せず、依存taskの確定結果だけを必要に応じて参照させます。
コード作業のacceptance_commandsは、repository内で実行できる既知のコマンドだけを指定してください。
不明なコマンドを作らず、コマンドが分からない場合はacceptance_criteriaに記述してください。
"""
        result = await self.client.run_prompt(
            prompt,
            model=self.settings.deerflow_planner_model,
            idempotency_key=idempotency_key,
            correlation=correlation,
        )
        self.last_result = result
        plan = SwarmPlan.model_validate(_extract_json(result.text))
        if len(plan.tasks) > self.settings.swarm_max_tasks:
            raise ValueError(
                f"plannerが上限{self.settings.swarm_max_tasks}件を超えるtaskを返しました"
            )
        if profile == "research" and any(task.worker != "research" for task in plan.tasks):
            raise ValueError("research profileでは全taskのworkerをresearchにしてください")
        if profile in {"implementation", "competition"} and any(
            task.worker != "code" for task in plan.tasks
        ):
            raise ValueError(f"{profile} profileでは全taskのworkerをcodeにしてください")
        if profile == "competition" and (
            not 2 <= len(plan.tasks) <= 4 or any(task.dependencies for task in plan.tasks)
        ):
            raise ValueError("competition profileには相互依存のない2〜4候補が必要です")
        return plan

    async def replan(
        self,
        objective: str,
        task_title: str,
        task_objective: str,
        failure_category: str,
        failure_reason: str,
        *,
        idempotency_key: str,
        correlation: dict[str, str] | None = None,
    ) -> str | None:
        prompt = f"""次のtaskは失敗しました。同じ指示のまま再送せず、原因を踏まえて再計画してください。
全体目的: {objective}
失敗task: {task_title}
元の指示: {task_objective}
分類: {failure_category}
失敗情報: {failure_reason[:3000]}

再実行で改善可能なら、修正したtask指示をJSON {{"retry":true,"objective":"...","reason":"..."}} で返してください。
環境を直せない、情報が不足、または再試行の価値がない場合は {{"retry":false,"reason":"..."}} としてください。
"""
        try:
            result = await self.client.run_prompt(
                prompt,
                model=self.settings.deerflow_planner_model,
                idempotency_key=idempotency_key,
                correlation=correlation,
            )
            decision = _extract_json(result.text)
            if decision.get("retry") is True and isinstance(decision.get("objective"), str):
                return decision["objective"].strip() or None
        except (ValueError, RuntimeError, httpx.HTTPError):
            return None
        return None

    async def review(
        self,
        objective: str,
        patch: str,
        checks: list[str],
        *,
        idempotency_key: str,
        correlation: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if len(patch) > 30000:
            raise ValueError("差分がReviewerの上限30000文字を超えています。全差分の確認が必要です")
        prompt = f"""あなたは実装担当とは別threadの独立Reviewerです。コードを実行したとは推測しないでください。
目的: {objective}
統合後に通過した検証: {json.dumps(checks, ensure_ascii=False)}
差分:
```diff
{patch}
```
JSONだけで {{"decision":"approveまたはrevise","findings":[{{"severity":"...","file":"...","reason":"..."}}],
"summary":"..."}} を返してください。根拠のない指摘は含めないでください。
"""
        result = await self.client.run_prompt(
            prompt,
            model=self.settings.deerflow_reviewer_model,
            idempotency_key=idempotency_key,
            correlation=correlation,
        )
        parsed = _extract_json(result.text)
        if parsed.get("decision") not in {"approve", "revise"}:
            raise ValueError("reviewer response の decision が不正です")
        parsed["thread_id"] = result.thread_id
        parsed["run_id"] = result.run_id
        return parsed


def _run_id_from_location(location: str | None) -> str | None:
    if not location:
        return None
    match = re.search(r"/runs/([^/?#]+)", location)
    return match.group(1) if match else None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


def _message_text(value: Any) -> str:
    if isinstance(value, dict):
        if "messages" in value:
            return _message_text(value["messages"])
        if "message" in value:
            return _message_text(value["message"])
        if "content" in value:
            role = str(value.get("role") or value.get("type") or "").lower()
            if role not in {
                "assistant",
                "ai",
                "aimessage",
                "aimessagechunk",
                "ai_message",
                "ai_message_chunk",
            }:
                return ""
            return _content_text(value["content"])
        return ""
    if isinstance(value, list):
        messages = []
        for item in value:
            if isinstance(item, list) and item:
                item = item[0]
            if isinstance(item, dict):
                info = item.get("info", {})
                role = (
                    item.get("role")
                    or (info.get("role") if isinstance(info, dict) else None)
                    or item.get("type")
                )
                if str(role).lower() not in {
                    "assistant",
                    "ai",
                    "aimessage",
                    "aimessagechunk",
                    "ai_message",
                    "ai_message_chunk",
                }:
                    continue
                content = item.get("content")
                if content is None and isinstance(item.get("parts"), list):
                    content = item["parts"]
                text = _content_text(content)
                if text:
                    messages.append(text)
        return messages[-1] if messages else ""
    return ""


def _extract_json(output: str) -> dict[str, Any]:
    text = output.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("DeerFlow出力にJSON objectがありません")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("DeerFlow出力のJSON rootはobjectである必要があります")
    return value
