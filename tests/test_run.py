from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_contracts import task
from test_scheduler import orchestrator
from typer.testing import CliRunner

from deepflow_swarm.cli import app
from deepflow_swarm.models import SwarmPlan, TaskOutcome


@pytest.mark.parametrize("patch,expected", [(None, "partial"), ("", "completed")])
async def test_code_report_needs_artifact_and_acceptance(
    settings, store, tmp_path, monkeypatch, patch, expected
):
    engine = orchestrator(settings, store)
    plan = SwarmPlan(tasks=[task("a")])
    engine.planner = SimpleNamespace(
        plan=AsyncMock(return_value=plan),
        last_result=None,
        review=AsyncMock(return_value={"decision": "approve"}),
    )
    engine._execute_task = AsyncMock(return_value=TaskOutcome(success=True, patch=patch))
    monkeypatch.setattr(
        "deepflow_swarm.orchestrator.RefineryIntegrator",
        lambda _: SimpleNamespace(
            integrate=AsyncMock(
                return_value={
                    "patch": "",
                    "verified": True,
                    "accepted_task_ids": ["a"],
                    "checks": [],
                }
            )
        ),
    )
    result = await engine.run(
        objective="objective",
        profile="implementation",
        repo_path=tmp_path,
        global_checks=["fixed-check"],
    )
    assert result["status"] == expected
    persisted = await store.get_run(result["run_id"])
    assert persisted["status"] == expected
    if patch is None:
        assert persisted["tasks"][0]["failure_category"] == "artifact_missing"
        engine.planner.review.assert_not_awaited()
    else:
        engine.planner.review.assert_awaited_once()


def test_cli_name_and_profiles():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "deepflow-swarm" in result.output
    assert runner.invoke(app, ["run", "objective", "--profile", "invalid"]).exit_code == 2


async def test_integration_keeps_code_dependencies_across_research_task(
    settings, store, tmp_path, monkeypatch
):
    engine = orchestrator(settings, store)
    plan = SwarmPlan(tasks=[task("c", ["b"]), task("b", ["a"], worker="research"), task("a")])
    engine.planner = SimpleNamespace(
        plan=AsyncMock(return_value=plan),
        last_result=None,
        review=AsyncMock(return_value={"decision": "approve"}),
    )

    async def execute(**kwargs):
        item = kwargs["task"]
        return TaskOutcome(
            success=True, output=item.id, patch=item.id if item.worker == "code" else None
        )

    engine._execute_task = execute
    integrate = AsyncMock(
        return_value={
            "patch": "result",
            "verified": True,
            "accepted_task_ids": ["a", "c"],
            "checks": [],
        }
    )
    monkeypatch.setattr(
        "deepflow_swarm.orchestrator.RefineryIntegrator",
        lambda _: SimpleNamespace(integrate=integrate),
    )
    await engine.run(
        objective="objective",
        profile="dependency",
        repo_path=tmp_path,
        global_checks=["fixed-check"],
    )
    candidates = integrate.call_args.kwargs["candidates"]
    assert next(item for item in candidates if item.task_id == "c").dependencies == ["a"]
