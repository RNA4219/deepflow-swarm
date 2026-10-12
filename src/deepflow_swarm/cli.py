from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import typer

from .database import StateStore
from .deerflow import DeerFlowClient
from .orchestrator import SwarmOrchestrator
from .settings import get_settings
from .tracing import send_trace_score

app = typer.Typer(
    name="deepflow-swarm",
    help="DeerFlowを中核にしたCLI-firstのagent orchestration",
    no_args_is_help=True,
)


@app.command("init-db")
def init_db() -> None:
    """PostgreSQL schemaを作成する。"""
    settings = get_settings()
    store = StateStore(settings.database_url)

    async def execute() -> None:
        try:
            await store.initialize()
        finally:
            await store.close()

    _run(execute())
    typer.echo("database schema を作成しました")


@app.command()
def doctor() -> None:
    """PostgreSQLとDeerFlow Gatewayへの接続を確認する。"""
    settings = get_settings()
    store = StateStore(settings.database_url)
    client = DeerFlowClient(settings)

    async def execute() -> dict[str, str]:
        result: dict[str, str] = {}
        try:
            await store.health()
            result["postgres"] = "ok"
        except Exception as exc:
            result["postgres"] = f"error: {type(exc).__name__}: {exc}"
        try:
            await client.health()
            result["deerflow"] = "ok"
        except Exception as exc:
            result["deerflow"] = f"error: {type(exc).__name__}: {exc}"
        await client.close()
        await store.close()
        return result

    results = _run(execute())
    typer.echo(json.dumps(results, ensure_ascii=False, indent=2))
    if any(value != "ok" for value in results.values()):
        raise typer.Exit(code=1)


@app.command()
def run(
    objective: str = typer.Argument(..., help="達成したい目的"),
    profile: str = typer.Option(
        "implementation",
        "--profile",
        "-p",
        help="research / implementation / competition / dependency",
    ),
    repo: Optional[Path] = typer.Option(None, "--repo", help="コード作業対象のlocal repository"),
    check: list[str] = typer.Option(
        [], "--check", help="統合sandboxで実行する受入コマンド。複数指定可"
    ),
) -> None:
    """DeerFlowが計画したtask群を実行し、コード成果物を統合検証する。"""
    allowed = {"research", "implementation", "competition", "dependency"}
    if profile not in allowed:
        raise typer.BadParameter(f"profileは {', '.join(sorted(allowed))} のいずれかです")
    settings = get_settings()
    store = StateStore(settings.database_url)
    orchestrator = SwarmOrchestrator(settings, store)

    async def execute() -> dict:
        await store.initialize()
        try:
            return await orchestrator.run(
                objective=objective,
                profile=profile,  # type: ignore[arg-type]
                repo_path=repo.expanduser().resolve() if repo else None,
                global_checks=check,
            )
        finally:
            await orchestrator.close()
            await store.close()

    try:
        result = _run(execute())
    except Exception as exc:
        typer.echo(f"実行失敗: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "completed":
        raise typer.Exit(code=2)


@app.command()
def status(run_id: str = typer.Argument(..., help="run UUID")) -> None:
    """永続化されたrun/task/attempt状態を表示する。"""
    settings = get_settings()
    store = StateStore(settings.database_url)

    async def execute() -> dict | None:
        await store.initialize()
        try:
            return await store.get_run(run_id)
        finally:
            await store.close()

    result = _run(execute())
    if result is None:
        typer.echo(f"runが見つかりません: {run_id}", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command()
def evaluate(
    run_id: str = typer.Argument(..., help="run UUID"),
    value: float = typer.Option(..., min=0.0, max=1.0, help="0.0から1.0の評価値"),
    comment: str = typer.Option(..., "--comment", help="評価理由"),
    name: str = typer.Option("human_quality", "--name", help="Langfuse score名"),
) -> None:
    """人手評価をPostgreSQLとLangfuseへ記録する。"""
    settings = get_settings()
    store = StateStore(settings.database_url)

    async def execute() -> dict | None:
        await store.initialize()
        try:
            record = await store.get_run(run_id)
            if record is None:
                return None
            await store.add_artifact(
                run_id,
                "human_evaluation",
                json.dumps({"name": name, "value": value, "comment": comment}, ensure_ascii=False),
            )
            return record
        finally:
            await store.close()

    record = _run(execute())
    if record is None:
        typer.echo(f"runが見つかりません: {run_id}", err=True)
        raise typer.Exit(code=1)
    try:
        sent_to_langfuse = send_trace_score(
            settings,
            trace_id=record["trace_id"],
            name=name,
            value=value,
            comment=comment,
        )
    except Exception as exc:
        typer.echo(f"PostgreSQLには保存しましたがLangfuse送信に失敗しました: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if sent_to_langfuse:
        typer.echo(f"{run_id}: {name}={value:.2f} をPostgreSQLとLangfuseに記録しました")
    else:
        typer.echo(f"{run_id}: {name}={value:.2f} をPostgreSQLに記録しました（Langfuse未設定）")


def _run(coroutine):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    raise RuntimeError("CLIは通常のPythonプロセスから実行してください")


if __name__ == "__main__":
    app()
