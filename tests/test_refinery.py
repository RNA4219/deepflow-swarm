import asyncio
import os
import re
import shlex
import shutil
from types import SimpleNamespace

from deepflow_swarm.models import PatchCandidate
from deepflow_swarm.refinery import RefineryIntegrator
from deepflow_swarm.workspace import collect_patch, stage_workspace


class LocalGitWorkspace:
    """Test-only filesystem fixture: executes trusted test commands, never agent output."""

    def __init__(self, root):
        self.root = root
        root.mkdir()
        (root / "tmp").mkdir()
        self.commands = SimpleNamespace(run=self.run)
        self.files = SimpleNamespace(write_files=self.write_files, read_file=self.read_file)
        self.destroyed = False

    def path(self, path):
        return self.root / path.lstrip("/")

    async def write_files(self, entries):
        for entry in entries:
            destination = self.path(entry.path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(entry.data)

    async def read_file(self, path):
        return self.path(path).read_text(encoding="utf-8")

    async def run(self, command, opts=None):
        def shell_path(path):
            value = path.as_posix()
            if os.name == "nt":
                value = "/" + value[0].lower() + value[2:]
            return shlex.quote(value)

        # Replace original sandbox paths in one pass; inserted /tmp paths must not be remapped.
        command = re.sub(
            r"/workspace/repo|/tmp(?=/)",
            lambda match: shell_path(self.path(match.group())),
            command,
        )
        bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
        process = await asyncio.create_subprocess_exec(
            bash, "-c", command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        return SimpleNamespace(
            exit_code=process.returncode,
            logs=SimpleNamespace(
                stdout=[SimpleNamespace(text=stdout.decode("utf-8", errors="replace"))],
                stderr=[SimpleNamespace(text=stderr.decode("utf-8", errors="replace"))],
            ),
        )

    async def destroy(self):
        self.destroyed = True


class LocalFactory:
    def __init__(self, root):
        self.root = root
        self.created = []

    async def create(self, **kwargs):
        workspace = LocalGitWorkspace(self.root / f"trial-{len(self.created)}")
        self.created.append(workspace)
        return workspace


def candidate(identifier, filename, content, dependencies=()):
    return PatchCandidate(
        task_id=identifier,
        title=identifier,
        dependencies=list(dependencies),
        patch=f"diff --git a/{filename} b/{filename}\nnew file mode 100644\n"
        f"--- /dev/null\n+++ b/{filename}\n@@ -0,0 +1 @@\n+{content}\n",
        acceptance_commands=["this_model_command_must_never_run"],
    )


def integrator(settings, tmp_path):
    engine = RefineryIntegrator(settings)
    engine.sandboxes = LocalFactory(tmp_path)
    return engine


async def test_real_git_collects_new_and_binary_files(tmp_path):
    repo = tmp_path / "input"
    repo.mkdir()
    (repo / "baseline.txt").write_text("baseline\n")
    workspace = LocalGitWorkspace(tmp_path / "worker")
    await stage_workspace(workspace, repo)
    await workspace.write_files(
        [SimpleNamespace(path="/workspace/repo/image.bin", data=b"\x00\x01\x02")]
    )
    patch = await collect_patch(workspace)
    assert "GIT binary patch" in patch
    destination = LocalGitWorkspace(tmp_path / "destination")
    await stage_workspace(destination, repo, [patch])
    assert destination.path("/workspace/repo/image.bin").read_bytes() == b"\x00\x01\x02"


async def test_real_git_integrates_independent_changes_and_fixed_checks(settings, tmp_path):
    repo = tmp_path / "input"
    repo.mkdir()
    engine = integrator(settings, tmp_path)
    result = await engine.integrate(
        run_id="run",
        repo_path=repo,
        candidates=[candidate("a", "a.txt", "good"), candidate("b", "b.txt", "good")],
        global_checks=["test -f a.txt && test -f b.txt"],
    )
    assert result["verified"]
    assert result["accepted_task_ids"] == ["a", "b"]
    assert len(result["trials"]) == 1
    assert all(workspace.destroyed for workspace in engine.sandboxes.created)


async def test_false_worker_success_is_rejected_and_other_change_salvaged(settings, tmp_path):
    repo = tmp_path / "input"
    repo.mkdir()
    engine = integrator(settings, tmp_path)
    result = await engine.integrate(
        run_id="run",
        repo_path=repo,
        candidates=[candidate("bad", "bad.txt", "bad"), candidate("good", "good.txt", "good")],
        global_checks=["test ! -f bad.txt"],
    )
    assert result["accepted_task_ids"] == ["good"]
    assert result["rejected_task_ids"] == ["bad"]
    assert result["verified"]
    assert sum(trial["task_ids"] == ["bad", "good"] for trial in result["trials"]) == 1


async def test_competing_edits_are_evaluated_separately(settings, tmp_path):
    repo = tmp_path / "input"
    repo.mkdir()
    engine = integrator(settings, tmp_path)
    result = await engine.integrate(
        run_id="run",
        repo_path=repo,
        candidates=[
            candidate("first", "choice.txt", "one"),
            candidate("second", "choice.txt", "two"),
        ],
        global_checks=["test -s choice.txt"],
        competition=True,
    )
    assert result["accepted_task_ids"] == ["first"]
    assert result["unselected_task_ids"] == ["second"]
    assert "\n+one\n" in result["patch"] and "\n+two\n" not in result["patch"]
    assert len(engine.sandboxes.created) == 2


async def test_no_acceptance_never_creates_sandbox(settings, tmp_path):
    engine = integrator(settings, tmp_path)
    result = await engine.integrate(
        run_id="run", repo_path=tmp_path, candidates=[candidate("a", "a", "good")], global_checks=[]
    )
    assert not result["verified"]
    assert not engine.sandboxes.created


async def test_empty_diff_can_still_be_verified(settings, tmp_path):
    repo = tmp_path / "input"
    repo.mkdir()
    engine = integrator(settings, tmp_path)
    result = await engine.integrate(
        run_id="run",
        repo_path=repo,
        candidates=[PatchCandidate(task_id="a", title="a", patch="")],
        global_checks=["git status --porcelain"],
    )
    assert result["verified"]
    assert result["patch"] == ""
