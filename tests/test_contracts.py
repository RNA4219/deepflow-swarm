import io
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from pydantic import ValidationError

from deepflow_swarm.deerflow import _extract_json, _message_text
from deepflow_swarm.models import SwarmPlan, WorkTask
from deepflow_swarm.workspace import _archive_workspace, run_command


def task(task_id, dependencies=(), worker="code"):
    return WorkTask(
        id=task_id,
        title=task_id,
        objective="Implement requested behavior",
        worker=worker,
        dependencies=list(dependencies),
    )


@pytest.mark.parametrize(
    "tasks",
    [
        [task("a"), task("a")],
        [task("a", ["missing"])],
        [task("a", ["a"])],
        [task("a", ["b"]), task("b", ["a"])],
    ],
)
def test_invalid_dag_rejected(tasks):
    with pytest.raises(ValidationError):
        SwarmPlan(tasks=tasks)


def test_dag_accepts_diamond():
    SwarmPlan(tasks=[task("a"), task("b", ["a"]), task("c", ["a"]), task("d", ["b", "c"])])


@pytest.mark.parametrize("identifier", ["../a", "a/b", "", "a" * 65])
def test_task_id_cannot_escape_artifact_namespace(identifier):
    with pytest.raises(ValidationError):
        task(identifier)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "messages": [
                {"role": "user", "content": "secret"},
                {"role": "assistant", "content": "answer"},
            ]
        },
        [{"type": "ai", "content": [{"type": "text", "text": "answer"}]}],
        [{"type": "AIMessageChunk", "content": "answer"}, {"metadata": "ignored"}],
    ],
)
def test_deerflow_only_extracts_assistant_text(payload):
    assert _message_text(payload) == "answer"


def test_json_plan_can_be_fenced_but_must_be_valid():
    assert _extract_json('```json\n{"tasks": []}\n```') == {"tasks": []}
    with pytest.raises(ValueError):
        _extract_json("not a plan")


def test_final_state_uses_final_assistant_not_intermediate_reasoning():
    assert (
        _message_text(
            {
                "messages": [
                    {"type": "ai", "content": "Preparing the plan"},
                    {"type": "tool", "content": "tool result"},
                    {"type": "ai", "content": '{"tasks": []}'},
                ]
            }
        )
        == '{"tasks": []}'
    )


def test_workspace_excludes_secrets_and_dependencies(tmp_path):
    for name in [".env", ".env.backup", "private.pem", "id_rsa", "secrets.json"]:
        (tmp_path / name).write_text("secret")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dependency.js").write_text("dependency")
    (tmp_path / "main.py").write_text("print('hello')")
    with tarfile.open(fileobj=io.BytesIO(_archive_workspace(tmp_path))) as archive:
        assert archive.getnames() == ["main.py"]


@pytest.mark.parametrize("exit_code,expected", [(0, 0), (2, 2), (None, 1)])
async def test_command_exit_status_is_not_inferred_from_text(exit_code, expected):
    execution = SimpleNamespace(
        exit_code=exit_code,
        logs=SimpleNamespace(stdout=[SimpleNamespace(text="success")], stderr=[]),
    )
    sandbox = SimpleNamespace(commands=SimpleNamespace(run=AsyncMock(return_value=execution)))
    status, output = await run_command(sandbox, "echo success")
    assert status == expected
    assert output == "success"


def test_private_alias_has_no_cloud_fallback():
    from pathlib import Path

    config = yaml.safe_load(Path("config/litellm.yaml").read_text())
    assert not config.get("router_settings", {}).get("fallbacks")
    private = next(
        model for model in config["model_list"] if model["model_name"] == "local_private"
    )
    assert private["litellm_params"]["api_base"] == "os.environ/LOCAL_PRIVATE_API_BASE"
