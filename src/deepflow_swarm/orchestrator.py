from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from .database import StateStore
from .deerflow import DeerFlowClient, SwarmPlanner
from .models import PatchCandidate, RunProfile, SwarmPlan, TaskOutcome, WorkTask
from .opencode import OpenCodeWorkerBridge
from .refinery import RefineryIntegrator
from .settings import Settings
from .tracing import RunTrace, trace_run

logger = logging.getLogger(__name__)


class SwarmOrchestrator:
    def __init__(self, settings: Settings, store: StateStore) -> None:
        self.settings = settings
        self.store = store
        self.deerflow = DeerFlowClient(settings)
        self.planner = SwarmPlanner(self.deerflow, settings)
        self.opencode = OpenCodeWorkerBridge(settings, store)

    async def close(self) -> None:
        await self.deerflow.close()

    async def run(
        self,
        *,
        objective: str,
        profile: RunProfile,
        repo_path: Path | None,
        global_checks: list[str],
    ) -> dict[str, Any]:
        if profile in {"implementation", "competition", "dependency"} and repo_path is None:
            raise ValueError(f"{profile} profileでは--repoが必要です")
        if repo_path is not None and not repo_path.expanduser().is_dir():
            raise ValueError(f"repo pathが見つかりません: {repo_path}")

        trace_id = uuid4().hex
        run_id = await self.store.create_run(
            profile, objective, str(repo_path.resolve()) if repo_path else None, trace_id
        )
        try:
            with trace_run(
                self.settings,
                trace_id=trace_id,
                run_id=run_id,
                profile=profile,
                objective=objective,
            ) as trace:
                plan = await self.planner.plan(
                    objective,
                    profile,
                    idempotency_key=f"{run_id}:plan",
                    correlation={"run_id": run_id, "trace_id": trace_id, "role": "planner"},
                )
                await self.store.save_plan(run_id, plan)
                plan_call = self.planner.last_result
                if plan_call is not None:
                    await self.store.add_artifact(
                        run_id,
                        "deerflow_plan_reference",
                        json.dumps(
                            {
                                "thread_id": plan_call.thread_id,
                                "run_id": plan_call.run_id,
                                "trace_id": trace_id,
                            },
                            ensure_ascii=False,
                        ),
                    )
                outcomes = await self._execute_plan(
                    run_id=run_id,
                    trace_id=trace_id,
                    objective=objective,
                    plan=plan,
                    repo_path=repo_path,
                    trace=trace,
                )

                code_candidates = [
                    PatchCandidate(
                        task_id=task.id,
                        title=task.title,
                        patch=outcomes[task.id].patch or "",
                        dependencies=[
                            dep
                            for dep in _dependency_ids(plan, task)
                            if _task(plan, dep).worker == "code"
                        ],
                        acceptance_commands=task.acceptance_commands,
                    )
                    for task in plan.tasks
                    if task.worker == "code"
                    and outcomes[task.id].success
                    and outcomes[task.id].patch is not None
                ]
                integration: dict[str, Any] | None = None
                integrated_patch: str | None = None
                review: dict[str, Any] | None = None
                if code_candidates and repo_path is not None:
                    await self.store.set_run_status(run_id, "integrating")
                    integrator = RefineryIntegrator(self.settings)
                    integration = await integrator.integrate(
                        run_id=run_id,
                        repo_path=repo_path,
                        candidates=code_candidates,
                        global_checks=global_checks,
                        competition=profile == "competition",
                    )
                    integrated_patch = integration.pop("patch", None)
                    if integrated_patch is not None:
                        await self.store.add_artifact(run_id, "integrated_patch", integrated_patch)
                    if integration.get("verified") and integrated_patch is not None:
                        try:
                            await self.store.set_run_status(run_id, "reviewing")
                            review = await self.planner.review(
                                objective,
                                integrated_patch,
                                [item["command"] for item in integration.get("checks", [])],
                                idempotency_key=f"{run_id}:review",
                                correlation={
                                    "run_id": run_id,
                                    "trace_id": trace_id,
                                    "role": "reviewer",
                                },
                            )
                            await self.store.add_artifact(
                                run_id,
                                "independent_review",
                                json.dumps(review, ensure_ascii=False),
                            )
                        except Exception as exc:
                            logger.exception("Independent review failed")
                            review = {"decision": "unavailable", "reason": str(exc)[:2000]}

                task_failures = [
                    task_id for task_id, outcome in outcomes.items() if not outcome.success
                ]
                all_tasks_succeeded = not task_failures
                all_code_integrated = (
                    not code_candidates
                    or integration is not None
                    and (
                        profile == "competition"
                        or len(integration.get("accepted_task_ids", [])) == len(code_candidates)
                    )
                    and integration.get("verified") is True
                )
                review_approved = (
                    not code_candidates
                    or review is not None
                    and review.get("decision") == "approve"
                )
                if (
                    (all_tasks_succeeded or profile == "competition")
                    and all_code_integrated
                    and review_approved
                    and (profile != "competition" or bool(code_candidates))
                ):
                    status = "completed"
                elif integration is not None and not integration.get("verified"):
                    status = "awaiting_acceptance"
                else:
                    status = "partial"

                if integration is None:
                    trace.score(
                        "tasks_succeeded",
                        1.0 if all_tasks_succeeded else 0.0,
                        "全research taskが正常終了したか",
                    )
                else:
                    acceptance_passed = 1.0 if integration.get("verified") else 0.0
                    trace.score(
                        "acceptance_passed",
                        acceptance_passed,
                        "統合sandboxで固定した受入コマンドが全件成功したか",
                    )
                if code_candidates and review is not None:
                    trace.score(
                        "independent_review_approved",
                        1.0 if review.get("decision") == "approve" else 0.0,
                        str(review.get("summary") or review.get("reason") or "review結果"),
                    )
                result = {
                    "run_id": run_id,
                    "trace_id": trace_id,
                    "status": status,
                    "profile": profile,
                    "task_count": len(plan.tasks),
                    "failed_tasks": task_failures,
                    "integration": integration,
                    "review": review,
                }
                await self.store.finish_run(run_id, status, integrated_patch, integration)
                trace.update(result)
                return result
        except Exception as exc:
            logger.exception("Swarm run failed: %s", run_id)
            await self.store.finish_run(run_id, "failed", report={"error": str(exc)[:3000]})
            raise

    async def _execute_plan(
        self,
        *,
        run_id: str,
        objective: str,
        plan: SwarmPlan,
        repo_path: Path | None,
        trace_id: str,
        trace: RunTrace,
    ) -> dict[str, TaskOutcome]:
        pending = {task.id: task for task in plan.tasks}
        outcomes: dict[str, TaskOutcome] = {}
        db_ids = {task.id: f"{run_id}:{task.id}" for task in plan.tasks}
        running: dict[asyncio.Task, WorkTask] = {}
        try:
            while pending or running:
                # Propagate failed dependencies to a fixed point before scheduling.
                while True:
                    blocked = [
                        task
                        for task in pending.values()
                        if any(
                            dep in outcomes and not outcomes[dep].success
                            for dep in _dependency_ids(plan, task)
                        )
                    ]
                    if not blocked:
                        break
                    for task in blocked:
                        outcomes[task.id] = TaskOutcome(
                            success=False,
                            failure_category="dependency_failed",
                            failure_reason="先行taskが失敗したため実行を停止しました",
                        )
                        pending.pop(task.id)
                        await self.store.finish_task(
                            db_ids[task.id], "blocked", failure_category="dependency_failed"
                        )
                ready = [
                    task
                    for task in pending.values()
                    if all(dep in outcomes and outcomes[dep].success for dep in task.dependencies)
                ]
                for task in ready[: self.settings.swarm_max_concurrency - len(running)]:
                    pending.pop(task.id)
                    future = asyncio.create_task(
                        self._execute_task(
                            run_id=run_id,
                            trace_id=trace_id,
                            objective=objective,
                            task=task,
                            repo_path=repo_path,
                            dependency_outcomes={
                                dep: outcomes[dep] for dep in _dependency_ids(plan, task)
                            },
                            db_task_id=db_ids[task.id],
                            trace=trace,
                        )
                    )
                    running[future] = task
                if not running:
                    if pending:
                        raise RuntimeError(f"実行可能taskがありません: {sorted(pending)}")
                    break
                completed, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for future in completed:
                    task = running.pop(future)
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        outcome = TaskOutcome(
                            success=False,
                            failure_category="environment_failure",
                            failure_reason=f"{type(exc).__name__}: {str(exc)[:3000]}",
                        )
                    if task.worker == "code" and outcome.success and outcome.patch is None:
                        outcome = TaskOutcome(
                            success=False,
                            failure_category="artifact_missing",
                            failure_reason="コード成果物を取得できませんでした",
                        )
                    await self.store.finish_task(
                        db_ids[task.id],
                        "succeeded" if outcome.success else "failed",
                        output=outcome.output,
                        patch=outcome.patch,
                        failure_category=outcome.failure_category,
                    )
                    artifact = outcome.patch if task.worker == "code" else outcome.output
                    if artifact:
                        await self.store.add_artifact(
                            run_id,
                            "worker_patch" if task.worker == "code" else "research_result",
                            artifact,
                            task.id,
                        )
                    outcomes[task.id] = outcome
        finally:
            for future in running:
                future.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
        return outcomes

    async def _execute_task(
        self,
        *,
        run_id: str,
        trace_id: str,
        objective: str,
        task: WorkTask,
        repo_path: Path | None,
        dependency_outcomes: dict[str, TaskOutcome],
        db_task_id: str,
        trace: RunTrace,
    ) -> TaskOutcome:
        revised_objective = task.objective
        dependency_patches = [
            result.patch for result in dependency_outcomes.values() if result.patch
        ]
        dependency_context = "\n\n".join(
            f"受入前の先行task報告 {task_id}:\n{result.output[:5000]}"
            for task_id, result in dependency_outcomes.items()
            if result.success and result.output
        )
        previous_failure: TaskOutcome | None = None
        max_attempts = self.settings.swarm_max_replans + 1
        for attempt_number in range(1, max_attempts + 1):
            model_alias = (
                task.model_role
                if task.model_role in {"worker", "planner", "reviewer", "local_private"}
                else self.settings.litellm_worker_model
            )
            attempt_id = await self.store.create_attempt(db_task_id, attempt_number, model_alias)
            try:
                if task.worker == "research":
                    prompt = (
                        f"全体目的: {objective}\n担当: {task.title}\n\n"
                        f"作業指示:\n{revised_objective}\n\n"
                        f"受入条件:\n{task.acceptance_criteria}\n\n"
                        f"{dependency_context}\n\n"
                        "結論、根拠URL/資料箇所、未解決事項を分けて報告してください。"
                    )

                    async def save_thread(thread_id: str) -> None:
                        await self.store.bind_deerflow(attempt_id, thread_id)

                    async def save_run(thread_id: str, deerflow_run_id: str | None) -> None:
                        await self.store.bind_deerflow(attempt_id, thread_id, deerflow_run_id)

                    with trace.span(
                        name="deepflow-swarm.research-worker",
                        input={"task_id": task.id, "attempt_id": attempt_id, "prompt": prompt},
                        metadata={
                            "run_id": run_id,
                            "task_id": task.id,
                            "attempt_id": attempt_id,
                            "role": "research",
                        },
                    ) as worker_span:
                        result = await self.deerflow.run_prompt(
                            prompt,
                            model=_deerflow_model(task, self.settings),
                            idempotency_key=f"{attempt_id}:research",
                            on_thread_created=save_thread,
                            on_run_started=save_run,
                            correlation={
                                "run_id": run_id,
                                "trace_id": trace_id,
                                "task_id": task.id,
                                "attempt_id": attempt_id,
                                "role": "research",
                            },
                        )
                        if worker_span is not None:
                            worker_span.update(
                                output={
                                    "thread_id": result.thread_id,
                                    "deerflow_run_id": result.run_id,
                                    "result": result.text,
                                }
                            )
                    outcome = TaskOutcome(success=True, output=result.text)
                elif repo_path is not None:
                    code_task = task.model_copy(update={"objective": revised_objective})
                    if dependency_context:
                        code_task.objective += (
                            "\n\n受入前の先行task報告の結果:\n" + dependency_context
                        )
                    with trace.span(
                        name="deepflow-swarm.opencode-worker",
                        input={
                            "task_id": task.id,
                            "attempt_id": attempt_id,
                            "objective": code_task.objective,
                        },
                        metadata={
                            "run_id": run_id,
                            "task_id": task.id,
                            "attempt_id": attempt_id,
                            "role": "code",
                        },
                    ) as worker_span:
                        outcome = await self.opencode.execute(
                            run_id=run_id,
                            task=code_task,
                            attempt_id=attempt_id,
                            repo_path=repo_path,
                            dependency_patches=dependency_patches,
                        )
                        if worker_span is not None:
                            worker_span.update(
                                output={
                                    "success": outcome.success,
                                    "failure_category": outcome.failure_category,
                                    "patch_bytes": len(outcome.patch or ""),
                                }
                            )
                else:
                    outcome = TaskOutcome(
                        success=False,
                        failure_category="information_gap",
                        failure_reason="code workerにはrepositoryが必要です",
                    )
                if outcome.success:
                    await self.store.finish_attempt(attempt_id, "succeeded")
                    return outcome
                await self.store.finish_attempt(
                    attempt_id,
                    "submission_unknown"
                    if outcome.failure_category == "submission_unknown"
                    else "failed",
                    outcome.failure_category,
                    outcome.failure_reason,
                )
            except Exception as exc:
                outcome = TaskOutcome(
                    success=False,
                    failure_category="environment_failure",
                    failure_reason=f"{type(exc).__name__}: {str(exc)[:3000]}",
                )
                await self.store.finish_attempt(
                    attempt_id, "failed", outcome.failure_category, outcome.failure_reason
                )

            previous_failure = outcome
            if attempt_number >= max_attempts or outcome.failure_category == "submission_unknown":
                return outcome
            updated_objective = await self.planner.replan(
                objective,
                task.title,
                revised_objective,
                outcome.failure_category or "unknown",
                outcome.failure_reason or "worker failed",
                idempotency_key=f"{run_id}:{task.id}:replan:{attempt_number}",
                correlation={
                    "run_id": run_id,
                    "trace_id": trace_id,
                    "task_id": task.id,
                    "attempt_id": attempt_id,
                    "role": "replanner",
                },
            )
            if not updated_objective or updated_objective == revised_objective:
                return outcome
            revised_objective = updated_objective
        return previous_failure or TaskOutcome(success=False, failure_category="unknown")


def _task(plan: SwarmPlan, task_id: str) -> WorkTask:
    return next(task for task in plan.tasks if task.id == task_id)


def _deerflow_model(task: WorkTask, settings: Settings) -> str:
    models = {
        "planner": settings.deerflow_planner_model,
        "worker": settings.deerflow_worker_model,
        "reviewer": settings.deerflow_reviewer_model,
        "local_private": settings.deerflow_local_private_model,
    }
    return models.get(task.model_role, settings.deerflow_worker_model)


def _dependency_ids(plan: SwarmPlan, task: WorkTask) -> list[str]:
    by_id = {item.id: item for item in plan.tasks}
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in seen:
            return
        seen.add(identifier)
        for parent in by_id[identifier].dependencies:
            visit(parent)
        ordered.append(identifier)

    for dependency in task.dependencies:
        visit(dependency)
    return ordered
