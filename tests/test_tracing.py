from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from deepflow_swarm.tracing import RunTrace


def test_observation_failure_does_not_fail_job():
    client = Mock()
    client.create_score.side_effect = RuntimeError("offline")
    client.flush.side_effect = RuntimeError("offline")
    client.start_as_current_observation.side_effect = RuntimeError("offline")
    trace = RunTrace(client, Mock(), "a" * 32)
    trace.score("quality", 1, "passed")
    trace.flush()
    with trace.span(name="worker", input={}, metadata={}) as observation:
        assert observation is None


def test_telemetry_context_cannot_swallow_job_failure():
    @contextmanager
    def suppressing_context(**kwargs):
        try:
            yield SimpleNamespace(update=Mock())
        except RuntimeError:
            pass

    client = SimpleNamespace(start_as_current_observation=suppressing_context)
    with pytest.raises(RuntimeError, match="job failed"):
        with RunTrace(client).span(name="worker", input={}, metadata={}):
            raise RuntimeError("job failed")
