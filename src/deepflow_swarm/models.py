from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

RunProfile = Literal["research", "implementation", "competition", "dependency"]
WorkerKind = Literal["research", "code"]


class WorkTask(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=12000)
    worker: WorkerKind
    dependencies: list[str] = Field(default_factory=list)
    acceptance_commands: list[str] = Field(default_factory=list, max_length=20)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=30)
    model_role: str = Field(default="worker", max_length=80)


class SwarmPlan(BaseModel):
    tasks: list[WorkTask] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_graph(self) -> SwarmPlan:
        by_id = {task.id: task for task in self.tasks}
        if len(by_id) != len(self.tasks):
            raise ValueError("task ID が重複しています")
        for task in self.tasks:
            missing = set(task.dependencies) - by_id.keys()
            if missing:
                raise ValueError(f"{task.id} に未定義の依存先があります: {sorted(missing)}")
            if task.id in task.dependencies:
                raise ValueError(f"{task.id} は自分自身に依存できません")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("task dependency に循環があります")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in by_id[task_id].dependencies:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task in self.tasks:
            visit(task.id)
        return self


class TaskOutcome(BaseModel):
    success: bool
    output: str = ""
    patch: str | None = None
    failure_category: str | None = None
    failure_reason: str | None = None


class PatchCandidate(BaseModel):
    task_id: str
    title: str
    patch: str
    dependencies: list[str] = Field(default_factory=list)
    acceptance_commands: list[str] = Field(default_factory=list)
