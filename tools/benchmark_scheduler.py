"""同じ模擬taskで旧バッチ待ちと空き枠を補充するschedulerを比較する。"""

import asyncio
import json
import statistics
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from deepflow_swarm.models import SwarmPlan, TaskOutcome, WorkTask
from deepflow_swarm.orchestrator import SwarmOrchestrator
from deepflow_swarm.settings import Settings
from deepflow_swarm.tracing import RunTrace


async def main():
    delays = {"a": 0.2, "b": 0.02, "c": 0.02, "d": 0.2}
    plan = SwarmPlan(
        tasks=[
            WorkTask(id=name, title=name, objective="benchmark", worker="research")
            for name in delays
        ]
    )
    old_times, new_times = [], []

    async def execute(**kwargs):
        await asyncio.sleep(delays[kwargs["task"].id])
        return TaskOutcome(success=True, output="done")

    engine = SwarmOrchestrator.__new__(SwarmOrchestrator)
    engine.settings = Settings(_env_file=None, swarm_max_concurrency=2)
    engine.store = SimpleNamespace(finish_task=AsyncMock(), add_artifact=AsyncMock())
    engine._execute_task = execute
    for _ in range(5):
        started = time.perf_counter()
        for offset in range(0, len(plan.tasks), 2):
            await asyncio.gather(*(execute(task=task) for task in plan.tasks[offset : offset + 2]))
        old_times.append(time.perf_counter() - started)
        started = time.perf_counter()
        outcomes = await engine._execute_plan(
            run_id="bench",
            objective="benchmark",
            plan=plan,
            repo_path=None,
            trace_id="a" * 32,
            trace=RunTrace(),
        )
        assert len(outcomes) == 4 and all(outcome.success for outcome in outcomes.values())
        new_times.append(time.perf_counter() - started)
    old_mean, new_mean = statistics.mean(old_times), statistics.mean(new_times)
    print(
        json.dumps(
            {
                "iterations": 5,
                "concurrency": 2,
                "task_delays_seconds": delays,
                "old_mean_seconds": round(old_mean, 4),
                "new_mean_seconds": round(new_mean, 4),
                "elapsed_reduction_percent": round((1 - new_mean / old_mean) * 100, 1),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
