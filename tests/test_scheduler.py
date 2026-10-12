import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_contracts import task

from deepflow_swarm.models import SwarmPlan, TaskOutcome
from deepflow_swarm.orchestrator import SwarmOrchestrator
from deepflow_swarm.tracing import RunTrace


def orchestrator(settings, store):
    engine = SwarmOrchestrator.__new__(SwarmOrchestrator)
    engine.settings = settings
    engine.store = store
    return engine


async def run_plan(engine, plan, store, repo=None):
    run_id = await store.create_run("dependency", "objective", None, "a" * 32)
    await store.save_plan(run_id, plan)
    result = await engine._execute_plan(
        run_id=run_id,
        objective="objective",
        plan=plan,
        repo_path=repo,
        trace_id="a" * 32,
        trace=RunTrace(),
    )
    return result, await store.get_run(run_id)


async def test_free_slot_starts_next_task_before_slow_peer_finishes(settings, store):
    settings.swarm_max_concurrency = 2
    engine = orchestrator(settings, store)
    slow_release = asyncio.Event()
    third_started = asyncio.Event()

    async def execute(**kwargs):
        if kwargs["task"].id == "slow":
            await slow_release.wait()
        if kwargs["task"].id == "third":
            third_started.set()
        return TaskOutcome(success=True, output="done")

    engine._execute_task = execute
    execution = asyncio.create_task(
        run_plan(
            engine,
            SwarmPlan(
                tasks=[
                    task("slow", worker="research"),
                    task("fast", worker="research"),
                    task("third", worker="research"),
                ]
            ),
            store,
        )
    )
    try:
        await asyncio.wait_for(third_started.wait(), 1)
    finally:
        slow_release.set()
        await execution


async def test_transitive_dependencies_are_ordered_and_deduplicated(settings, store):
    engine = orchestrator(settings, store)
    seen = {}

    async def execute(**kwargs):
        identifier = kwargs["task"].id
        seen[identifier] = list(kwargs["dependency_outcomes"])
        return TaskOutcome(success=True, output=identifier, patch=identifier)

    engine._execute_task = execute
    plan = SwarmPlan(tasks=[task("a"), task("b", ["a"]), task("c", ["a"]), task("d", ["b", "c"])])
    await run_plan(engine, plan, store)
    assert seen["d"] == ["a", "b", "c"]


async def test_failure_blocks_entire_dependency_chain(settings, store):
    engine = orchestrator(settings, store)
    engine._execute_task = AsyncMock(
        return_value=TaskOutcome(success=False, failure_category="model_capacity")
    )
    outcomes, record = await run_plan(
        engine, SwarmPlan(tasks=[task("a"), task("b", ["a"]), task("c", ["b"])]), store
    )
    assert engine._execute_task.await_count == 1
    assert outcomes["c"].failure_category == "dependency_failed"
    assert [item["status"] for item in record["tasks"]] == ["failed", "blocked", "blocked"]


async def test_unknown_submission_never_creates_second_attempt(settings, store, tmp_path):
    settings.swarm_max_replans = 3
    engine = orchestrator(settings, store)
    engine.opencode = SimpleNamespace(
        execute=AsyncMock(
            return_value=TaskOutcome(success=False, failure_category="submission_unknown")
        )
    )
    engine.planner = SimpleNamespace(replan=AsyncMock())
    outcomes, record = await run_plan(engine, SwarmPlan(tasks=[task("a")]), store, tmp_path)
    assert not outcomes["a"].success
    assert len(record["tasks"][0]["attempts"]) == 1
    assert record["tasks"][0]["attempts"][0]["status"] == "submission_unknown"
    engine.planner.replan.assert_not_awaited()
