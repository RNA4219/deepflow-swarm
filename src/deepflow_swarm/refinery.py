from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from opensandbox.models.filesystem import WriteEntry

from .models import PatchCandidate
from .settings import Settings
from .workspace import OpenSandboxFactory, collect_patch, run_command, stage_workspace

logger = logging.getLogger(__name__)


class RefineryIntegrator:
    """隔離sandbox上で変更群を直列適用し、失敗時に群を分割して切り分ける。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sandboxes = OpenSandboxFactory(settings)
        self.trials: list[dict[str, Any]] = []

    async def integrate(
        self,
        *,
        run_id: str,
        repo_path: Path,
        candidates: list[PatchCandidate],
        global_checks: list[str],
        competition: bool = False,
    ) -> dict[str, Any]:
        self.trials = []
        candidates = _topological_candidates(candidates)
        accepted: list[PatchCandidate] = []
        rejected: list[PatchCandidate] = []
        # Commands from a model-generated plan are informational. Only caller-supplied
        # CLI checks are executed in the integration sandbox.
        all_checks = _unique(global_checks)
        if not all_checks:
            return {
                "accepted_task_ids": [],
                "rejected_task_ids": [item.task_id for item in candidates],
                "checks": [],
                "checks_passed": False,
                "verified": False,
                "patch": None,
                "trials": [],
            }

        if competition:
            return await self._compare(run_id, repo_path, candidates, all_checks)
        initial = await self._trial(run_id, repo_path, candidates, all_checks, label="batch")
        if initial[0]:
            accepted = list(candidates)
            final_patch, report = initial[1], initial[2]
        else:

            async def admit(group: list[PatchCandidate], *, known_failed: bool = False) -> None:
                nonlocal accepted
                if not group:
                    return
                attempt = (
                    initial
                    if known_failed
                    else await self._trial(
                        run_id, repo_path, accepted + group, all_checks, label="split"
                    )
                )
                if attempt[0]:
                    accepted.extend(group)
                    return
                if len(group) == 1:
                    rejected.append(group[0])
                    return
                midpoint = len(group) // 2
                await admit(group[:midpoint])
                await admit(group[midpoint:])

            await admit(list(candidates), known_failed=True)
            final = await self._trial(run_id, repo_path, accepted, all_checks, label="final")
            if final[0]:
                final_patch, report = final[1], final[2]
            else:
                # A group that passed before but not in the final combination is not publishable.
                report = final[2]
                rejected.extend(item for item in accepted if item not in rejected)
                accepted = []
                final_patch = None

        verified = bool(accepted) and bool(all_checks) and report.get("checks_passed") is True
        return {
            "accepted_task_ids": [item.task_id for item in accepted],
            "rejected_task_ids": [item.task_id for item in rejected],
            "checks": report.get("checks", []),
            "checks_passed": report.get("checks_passed", False),
            "verified": verified,
            "patch": final_patch,
            "trials": self.trials,
        }

    async def _compare(
        self, run_id: str, repo_path: Path, candidates: list[PatchCandidate], checks: list[str]
    ) -> dict[str, Any]:
        if any(item.dependencies for item in candidates):
            raise ValueError("競合候補は相互依存できません")
        passing = []
        rejected = []
        for candidate in candidates:
            trial = await self._trial(run_id, repo_path, [candidate], checks, label="candidate")
            if trial[0]:
                passing.append((candidate, trial))
            else:
                rejected.append(candidate.task_id)
        # Fixed acceptance is the comparison rule; planner order breaks ties.
        chosen = passing[0] if passing else None
        return {
            "accepted_task_ids": [chosen[0].task_id] if chosen else [],
            "rejected_task_ids": rejected,
            "unselected_task_ids": [item.task_id for item, _ in passing[1:]],
            "selection_rule": "fixed_acceptance_then_plan_order",
            "checks": chosen[1][2].get("checks", []) if chosen else [],
            "checks_passed": chosen is not None,
            "verified": chosen is not None,
            "patch": chosen[1][1] if chosen else None,
            "trials": self.trials,
        }

    async def _trial(
        self,
        run_id: str,
        repo_path: Path,
        candidates: list[PatchCandidate],
        checks: list[str],
        *,
        label: str,
    ) -> tuple[bool, str | None, dict[str, Any]]:
        sandbox = None
        check_results = []
        try:
            sandbox = await self.sandboxes.create(
                run_id=run_id, label=f"refinery-{label}-{len(self.trials) + 1}"
            )
            await stage_workspace(sandbox, repo_path)
            applied_ids: set[str] = set()
            for item in candidates:
                missing = set(item.dependencies) - applied_ids
                if missing:
                    raise RuntimeError(
                        f"{item.task_id} の依存成果物が統合されていません: {sorted(missing)}"
                    )
                if not item.patch.strip():
                    applied_ids.add(item.task_id)
                    continue
                path = f"/tmp/refinery-{len(applied_ids)}.patch"
                await sandbox.files.write_files(
                    [WriteEntry(path=path, data=item.patch.encode("utf-8"), mode=0o600)]
                )
                code, output = await run_command(
                    sandbox,
                    f"cd /workspace/repo && git apply --whitespace=nowarn {path}",
                    timeout_seconds=120,
                )
                if code != 0:
                    raise RuntimeError(f"git apply failed for {item.task_id}: {output}")
                applied_ids.add(item.task_id)

            for command in checks:
                exit_code, output = await run_command(
                    sandbox,
                    f"cd /workspace/repo && {command}",
                    timeout_seconds=self.settings.swarm_task_timeout_seconds,
                )
                check_results.append({"command": command, "exit_code": exit_code, "output": output})
                if exit_code != 0:
                    break
            checks_passed = (
                bool(checks)
                and len(check_results) == len(checks)
                and all(result["exit_code"] == 0 for result in check_results)
            )
            patch = await collect_patch(sandbox) if checks_passed else None
            report = {"checks": check_results, "checks_passed": checks_passed}
            self.trials.append(
                {
                    "label": label,
                    "task_ids": [item.task_id for item in candidates],
                    "passed": checks_passed,
                    "check_count": len(check_results),
                }
            )
            return checks_passed, patch, report
        except Exception as exc:
            logger.info("Refinery trial failed: %s", type(exc).__name__)
            report = {"checks": check_results, "checks_passed": False, "error": str(exc)[:2000]}
            self.trials.append(
                {
                    "label": label,
                    "task_ids": [item.task_id for item in candidates],
                    "passed": False,
                    "check_count": len(check_results),
                    "error": str(exc)[:1000],
                }
            )
            return False, None, report
        finally:
            if sandbox is not None:
                try:
                    await sandbox.destroy()
                except Exception:
                    logger.warning("Refinery sandbox cleanup failed", exc_info=True)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _topological_candidates(candidates: list[PatchCandidate]) -> list[PatchCandidate]:
    by_id = {candidate.task_id: candidate for candidate in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("統合候補のtask IDが重複しています")
    missing = {
        dependency
        for candidate in candidates
        for dependency in candidate.dependencies
        if dependency not in by_id
    }
    if missing:
        raise ValueError(f"統合候補に未定義の依存先があります: {sorted(missing)}")

    result: list[PatchCandidate] = []
    pending = list(candidates)
    completed: set[str] = set()
    while pending:
        ready = [candidate for candidate in pending if set(candidate.dependencies) <= completed]
        if not ready:
            raise ValueError("統合候補の依存関係に循環があります")
        for candidate in ready:
            result.append(candidate)
            completed.add(candidate.task_id)
            pending.remove(candidate)
    return result
