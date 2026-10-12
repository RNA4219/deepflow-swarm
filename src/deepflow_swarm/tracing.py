from __future__ import annotations

import logging
import sys
from contextlib import contextmanager, nullcontext
from typing import Any, ContextManager, Iterator

from .settings import Settings

logger = logging.getLogger(__name__)


def _observe(action):
    try:
        return action()
    except Exception:
        logger.warning("Langfuse observation failed", exc_info=True)
        return None


class _Observation:
    def __init__(self, observation):
        self.observation = observation

    def update(self, **kwargs):
        _observe(lambda: self.observation.update(**kwargs))


@contextmanager
def _safe_context(factory, *, wrap=False):
    context = None
    value = None
    try:
        candidate = factory()
        value = candidate.__enter__()
        context = candidate
    except Exception:
        logger.warning("Langfuse context unavailable", exc_info=True)
    try:
        yield _Observation(value) if wrap and value is not None else value
    finally:
        if context is not None:
            failure = sys.exc_info()
            _observe(lambda: context.__exit__(*failure))


class RunTrace:
    def __init__(
        self, client: Any = None, observation: Any = None, trace_id: str | None = None
    ) -> None:
        self.client = client
        self.observation = observation
        self.trace_id = trace_id

    def score(self, name: str, value: float, comment: str) -> None:
        if self.client is not None and self.trace_id is not None:
            _observe(
                lambda: self.client.create_score(
                    name=name,
                    value=float(value),
                    trace_id=self.trace_id,
                    data_type="NUMERIC",
                    comment=comment,
                )
            )

    def update(self, output: dict[str, Any]) -> None:
        if self.observation is not None:
            _observe(lambda: self.observation.update(output=output))

    def span(
        self, *, name: str, input: dict[str, Any], metadata: dict[str, str]
    ) -> ContextManager[Any]:
        if self.client is None:
            return nullcontext(None)
        return _safe_context(
            lambda: self.client.start_as_current_observation(
                as_type="span", name=name, input=input, metadata=metadata
            ),
            wrap=True,
        )

    def flush(self) -> None:
        if self.client is not None:
            _observe(self.client.flush)


@contextmanager
def trace_run(
    settings: Settings, *, trace_id: str, run_id: str, profile: str, objective: str
) -> Iterator[RunTrace]:
    if not settings.langfuse_enabled:
        yield RunTrace()
        return

    from langfuse import Langfuse, propagate_attributes

    client = _observe(
        lambda: Langfuse(
            public_key=settings.langfuse_public_key.get_secret_value(),
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            host=settings.langfuse_base_url,
        )
    )
    if client is None:
        yield RunTrace()
        return
    try:
        with _safe_context(
            lambda: client.start_as_current_observation(
                as_type="span",
                name="deepflow-swarm.run",
                input={"objective": objective, "profile": profile, "run_id": run_id},
                trace_context={"trace_id": trace_id},
            ),
            wrap=True,
        ) as observation:
            with _safe_context(
                lambda: propagate_attributes(session_id=run_id, tags=["deepflow-swarm", profile])
            ):
                yield RunTrace(client, observation, trace_id)
    finally:
        _observe(client.flush)


def send_trace_score(
    settings: Settings, *, trace_id: str, name: str, value: float, comment: str
) -> bool:
    if not settings.langfuse_enabled:
        return False
    from langfuse import Langfuse

    client = Langfuse(
        public_key=settings.langfuse_public_key.get_secret_value(),
        secret_key=settings.langfuse_secret_key.get_secret_value(),
        host=settings.langfuse_base_url,
    )
    client.create_score(
        name=name,
        value=float(value),
        trace_id=trace_id,
        data_type="NUMERIC",
        comment=comment,
    )
    client.flush()
    return True
