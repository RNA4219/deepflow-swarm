from __future__ import annotations

import io
import os
import tarfile
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .settings import Settings


class OpenSandboxFactory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def create(self, *, run_id: str, label: str, attempt_id: str | None = None) -> Any:
        from opensandbox import Sandbox
        from opensandbox.config import ConnectionConfig
        from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule

        timeout = timedelta(seconds=self.settings.open_sandbox_timeout_seconds)
        api_key = (
            self.settings.open_sandbox_api_key.get_secret_value()
            if self.settings.open_sandbox_api_key
            and self.settings.open_sandbox_api_key.get_secret_value().strip()
            else None
        )
        connection = ConnectionConfig(
            domain=self.settings.open_sandbox_domain,
            api_key=api_key,
            protocol=self.settings.open_sandbox_protocol,
            request_timeout=timedelta(seconds=30),
        )
        egress_domains = set(
            filter(
                None,
                (part.strip() for part in self.settings.open_sandbox_egress_domains.split(",")),
            )
        )
        gateway_host = urlparse(self.settings.litellm_base_url).hostname
        if gateway_host:
            egress_domains.add(gateway_host)
        policy = NetworkPolicy(
            defaultAction="deny",
            egress=[
                NetworkRule(action="allow", target=domain) for domain in sorted(egress_domains)
            ],
        )
        return await Sandbox.create(
            self.settings.open_sandbox_worker_image,
            connection_config=connection,
            timeout=timeout,
            ready_timeout=timedelta(seconds=90),
            secure_access=True,
            network_policy=policy,
            metadata={
                "project": "deepflow-swarm",
                "run_id": run_id,
                "label": label[:120],
                "attempt_id": attempt_id or "integration",
            },
        )


async def stage_workspace(
    sandbox: Any, repo_path: Path, dependency_patches: list[str] | None = None
) -> None:
    archive = _archive_workspace(repo_path)
    from opensandbox.models.filesystem import WriteEntry

    await sandbox.commands.run("mkdir -p /workspace/repo")
    await sandbox.files.write_files(
        [WriteEntry(path="/tmp/deepflow-swarm-workspace.tar.gz", data=archive, mode=0o600)]
    )
    exit_code, output = await run_command(
        sandbox,
        "tar -xzf /tmp/deepflow-swarm-workspace.tar.gz -C /workspace/repo "
        "&& cd /workspace/repo "
        "&& git init -q "
        "&& git config user.email 'deepflow-swarm@localhost' "
        "&& git config user.name 'deepflow-swarm' "
        "&& git add -A "
        "&& git commit -q --allow-empty -m 'Isolated task baseline'",
    )
    if exit_code != 0:
        raise RuntimeError(f"workspace初期化に失敗しました: {output}")
    for index, patch in enumerate(dependency_patches or []):
        if not patch.strip():
            continue
        path = f"/tmp/dependency-{index}.patch"
        await sandbox.files.write_files(
            [WriteEntry(path=path, data=patch.encode("utf-8"), mode=0o600)]
        )
        exit_code, output = await run_command(sandbox, f"cd /workspace/repo && git apply {path}")
        if exit_code != 0:
            raise RuntimeError(f"dependency patchの適用に失敗しました: {output}")
    if dependency_patches:
        exit_code, output = await run_command(
            sandbox,
            "cd /workspace/repo && git add -A && git commit -q --allow-empty -m 'Dependency results'",
        )
        if exit_code != 0:
            raise RuntimeError(f"dependency patchの確定に失敗しました: {output}")


async def run_command(sandbox: Any, command: str, *, timeout_seconds: int = 600) -> tuple[int, str]:
    from opensandbox.models.execd import RunCommandOpts

    result = await sandbox.commands.run(
        command,
        opts=RunCommandOpts(timeout=timedelta(seconds=timeout_seconds)),
    )
    stdout = "".join(message.text for message in (result.logs.stdout or []))
    stderr = "".join(message.text for message in (result.logs.stderr or []))
    exit_code = int(result.exit_code) if result.exit_code is not None else 1
    return exit_code, (stdout + stderr)[-30000:]


async def collect_patch(sandbox: Any) -> str:
    exit_code, output = await run_command(
        sandbox,
        "cd /workspace/repo && git add -N . >/dev/null 2>&1 || true; "
        "git -C /workspace/repo diff --binary HEAD > /tmp/deepflow-swarm.patch",
    )
    if exit_code != 0:
        raise RuntimeError(f"worker patchの回収に失敗しました: {output}")
    return await sandbox.files.read_file("/tmp/deepflow-swarm.patch")


def _archive_workspace(root: Path) -> bytes:
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"repo path はdirectoryではありません: {root}")
    excluded_dirs = {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        "coverage",
        ".next",
    }
    excluded_names = {".env", ".env.local", ".env.production", "secrets.json", "credentials.json"}
    memory = io.BytesIO()
    total_file_bytes = 0
    with tarfile.open(fileobj=memory, mode="w:gz", dereference=False) as archive:
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted(
                name
                for name in directories
                if name not in excluded_dirs and not (current_path / name).is_symlink()
            )
            for filename in sorted(files):
                path = current_path / filename
                lower_name = filename.lower()
                if (
                    path.is_symlink()
                    or lower_name in excluded_names
                    or lower_name.startswith(".env.")
                    or lower_name.endswith((".pem", ".key", ".p12", ".pfx"))
                    or lower_name.startswith(("id_rsa", "id_ed25519"))
                ):
                    continue
                total_file_bytes += path.stat().st_size
                if total_file_bytes > 250 * 1024 * 1024:
                    raise ValueError("workspace内のfile総量が250 MiBを超えています")
                relative = path.relative_to(root).as_posix()
                archive.add(path, arcname=relative, recursive=False)
    return memory.getvalue()
