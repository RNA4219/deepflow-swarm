from pathlib import Path

import pytest

from deepflow_swarm.database import StateStore
from deepflow_swarm.settings import Settings


@pytest.fixture
def settings():
    return Settings(_env_file=None, swarm_max_replans=0)


@pytest.fixture
async def store(tmp_path: Path):
    state = StateStore(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    await state.initialize()
    yield state
    await state.close()
