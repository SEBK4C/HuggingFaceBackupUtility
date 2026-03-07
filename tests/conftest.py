"""Shared test fixtures: tmp dirs, mock configs, mock clients."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from src.models import AppConfig
from src.state import StateDB


@pytest.fixture
def tmp_dirs(tmp_path: Path):
    """Create tier1, tier2, and gitea-data directories."""
    tier1 = tmp_path / "tier1"
    tier2 = tmp_path / "tier2"
    gitea_data = tmp_path / "gitea-data"
    for d in (tier1, tier2, gitea_data):
        d.mkdir()
    return {"tier1": tier1, "tier2": tier2, "gitea_data": gitea_data, "root": tmp_path}


@pytest.fixture
def app_config(tmp_dirs) -> AppConfig:
    """AppConfig pointing at tmp directories."""
    return AppConfig(
        hf_token="hf_test_token_fake",
        tier1_path=tmp_dirs["tier1"],
        tier2_path=tmp_dirs["tier2"],
        tier_threshold_percent=10,
        gitea_port=3000,
        gitea_admin_user="testadmin",
        gitea_admin_password="testpass",
        gitea_base_url="http://localhost:3000",
    )


@pytest_asyncio.fixture
async def state_db(tmp_dirs) -> StateDB:
    """Connected StateDB using a temp SQLite file."""
    db_path = tmp_dirs["gitea_data"] / "hfmirror.db"
    db = StateDB(db_path)
    await db.connect()
    yield db
    await db.close()
