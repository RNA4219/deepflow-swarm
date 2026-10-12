from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .models import SwarmPlan


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RunRecord(Base):
    __tablename__ = "swarm_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    profile: Mapped[str] = mapped_column(String(32))
    objective: Mapped[str] = mapped_column(Text)
    repo_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    integrated_patch: Mapped[str | None] = mapped_column(Text, nullable=True)
    integration_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    tasks: Mapped[list[WorkItemRecord]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class WorkItemRecord(Base):
    __tablename__ = "swarm_work_items"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("swarm_runs.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(160))
    objective: Mapped[str] = mapped_column(Text)
    worker: Mapped[str] = mapped_column(String(16))
    dependencies: Mapped[list[str]] = mapped_column(JSON, default=list)
    acceptance_commands: Mapped[list[str]] = mapped_column(JSON, default=list)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    patch: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deerflow_thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deerflow_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    run: Mapped[RunRecord] = relationship(back_populates="tasks")
    attempts: Mapped[list[AttemptRecord]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class AttemptRecord(Base):
    __tablename__ = "swarm_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("swarm_work_items.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="admitted", index=True)
    sandbox_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    opencode_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    opencode_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deerflow_thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deerflow_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_alias: Mapped[str] = mapped_column(String(80))
    failure_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    task: Mapped[WorkItemRecord] = relationship(back_populates="attempts")


class ArtifactRecord(Base):
    __tablename__ = "swarm_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("swarm_runs.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StateStore:
    def __init__(self, database_url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    async def create_run(
        self, profile: str, objective: str, repo_path: str | None, trace_id: str
    ) -> str:
        run_id = str(uuid4())
        async with self.sessions.begin() as session:
            session.add(
                RunRecord(
                    id=run_id,
                    trace_id=trace_id,
                    profile=profile,
                    objective=objective,
                    repo_path=repo_path,
                    status="planning",
                )
            )
        return run_id

    async def save_plan(self, run_id: str, plan: SwarmPlan) -> None:
        async with self.sessions.begin() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                raise KeyError(run_id)
            run.plan = plan.model_dump(mode="json")
            run.status = "running"
            run.updated_at = utcnow()
            for task in plan.tasks:
                session.add(
                    WorkItemRecord(
                        id=f"{run_id}:{task.id}",
                        run_id=run_id,
                        title=task.title,
                        objective=task.objective,
                        worker=task.worker,
                        dependencies=task.dependencies,
                        acceptance_commands=task.acceptance_commands,
                        acceptance_criteria=task.acceptance_criteria,
                    )
                )

    async def create_attempt(self, task_id: str, number: int, model_alias: str) -> str:
        attempt_id = str(uuid4())
        async with self.sessions.begin() as session:
            task = await session.get(WorkItemRecord, task_id)
            if task is None:
                raise KeyError(task_id)
            task.status = "running"
            task.updated_at = utcnow()
            session.add(
                AttemptRecord(
                    id=attempt_id, task_id=task_id, number=number, model_alias=model_alias
                )
            )
        return attempt_id

    async def bind_sandbox(self, attempt_id: str, sandbox_id: str) -> None:
        async with self.sessions.begin() as session:
            attempt = await session.get(AttemptRecord, attempt_id)
            if attempt is None:
                raise KeyError(attempt_id)
            attempt.sandbox_id = sandbox_id
            attempt.status = "sandbox_ready"

    async def bind_opencode(self, attempt_id: str, session_id: str, message_id: str) -> None:
        async with self.sessions.begin() as session:
            attempt = await session.get(AttemptRecord, attempt_id)
            if attempt is None:
                raise KeyError(attempt_id)
            attempt.opencode_session_id = session_id
            attempt.opencode_message_id = message_id
            attempt.status = "submitted"

    async def bind_deerflow(
        self, attempt_id: str, thread_id: str, run_id: str | None = None
    ) -> None:
        async with self.sessions.begin() as session:
            attempt = await session.get(AttemptRecord, attempt_id)
            if attempt is None:
                raise KeyError(attempt_id)
            attempt.status = "running"
            attempt.deerflow_thread_id = thread_id
            attempt.deerflow_run_id = run_id
            task = await session.get(WorkItemRecord, attempt.task_id)
            if task is not None:
                task.deerflow_thread_id = thread_id
                task.deerflow_run_id = run_id

    async def finish_attempt(
        self,
        attempt_id: str,
        status: str,
        failure_category: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        async with self.sessions.begin() as session:
            attempt = await session.get(AttemptRecord, attempt_id)
            if attempt is None:
                raise KeyError(attempt_id)
            attempt.status = status
            attempt.failure_category = failure_category
            attempt.failure_reason = failure_reason
            attempt.finished_at = utcnow()

    async def finish_task(
        self,
        task_id: str,
        status: str,
        output: str = "",
        patch: str | None = None,
        failure_category: str | None = None,
    ) -> None:
        async with self.sessions.begin() as session:
            task = await session.get(WorkItemRecord, task_id)
            if task is None:
                raise KeyError(task_id)
            task.status = status
            task.result = output
            task.patch = patch
            task.failure_category = failure_category
            task.updated_at = utcnow()

    async def finish_run(
        self,
        run_id: str,
        status: str,
        patch: str | None = None,
        report: dict[str, Any] | None = None,
    ) -> None:
        async with self.sessions.begin() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                raise KeyError(run_id)
            run.status = status
            run.integrated_patch = patch
            run.integration_report = report
            run.updated_at = utcnow()

    async def set_run_status(self, run_id: str, status: str) -> None:
        async with self.sessions.begin() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                raise KeyError(run_id)
            run.status = status
            run.updated_at = utcnow()

    async def add_artifact(
        self, run_id: str, artifact_type: str, payload: str, task_id: str | None = None
    ) -> None:
        async with self.sessions.begin() as session:
            session.add(
                ArtifactRecord(
                    run_id=run_id, task_id=task_id, artifact_type=artifact_type, payload=payload
                )
            )

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self.sessions() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                return None
            tasks = (
                await session.scalars(
                    select(WorkItemRecord)
                    .where(WorkItemRecord.run_id == run_id)
                    .order_by(WorkItemRecord.created_at)
                )
            ).all()
            task_ids = [task.id for task in tasks]
            attempt_rows = (
                (
                    await session.scalars(
                        select(AttemptRecord)
                        .where(AttemptRecord.task_id.in_(task_ids))
                        .order_by(AttemptRecord.number)
                    )
                ).all()
                if task_ids
                else []
            )
            artifacts = (
                await session.scalars(
                    select(ArtifactRecord)
                    .where(ArtifactRecord.run_id == run_id)
                    .order_by(ArtifactRecord.created_at)
                )
            ).all()
            attempts_by_task: dict[str, list[AttemptRecord]] = {}
            for attempt in attempt_rows:
                attempts_by_task.setdefault(attempt.task_id, []).append(attempt)
            return {
                "id": run.id,
                "trace_id": run.trace_id,
                "profile": run.profile,
                "objective": run.objective,
                "repo_path": run.repo_path,
                "status": run.status,
                "created_at": run.created_at.isoformat(),
                "updated_at": run.updated_at.isoformat(),
                "plan": run.plan,
                "integration_report": run.integration_report,
                "integrated_patch": run.integrated_patch,
                "artifacts": [
                    {
                        "id": artifact.id,
                        "task_id": artifact.task_id,
                        "type": artifact.artifact_type,
                        "payload": artifact.payload,
                        "created_at": artifact.created_at.isoformat(),
                    }
                    for artifact in artifacts
                ],
                "tasks": [
                    {
                        "id": task.id,
                        "title": task.title,
                        "worker": task.worker,
                        "dependencies": task.dependencies,
                        "status": task.status,
                        "failure_category": task.failure_category,
                        "result": task.result,
                        "patch": task.patch,
                        "objective": task.objective,
                        "acceptance_commands": task.acceptance_commands,
                        "attempts": [
                            {
                                "id": attempt.id,
                                "number": attempt.number,
                                "status": attempt.status,
                                "sandbox_id": attempt.sandbox_id,
                                "opencode_session_id": attempt.opencode_session_id,
                                "opencode_message_id": attempt.opencode_message_id,
                                "deerflow_thread_id": attempt.deerflow_thread_id,
                                "deerflow_run_id": attempt.deerflow_run_id,
                                "failure_category": attempt.failure_category,
                                "failure_reason": attempt.failure_reason,
                            }
                            for attempt in attempts_by_task.get(task.id, [])
                        ],
                    }
                    for task in tasks
                ],
            }

    async def health(self) -> None:
        async with self.sessions() as session:
            await session.execute(select(1))
