"""Integration tests for core orchestration (with mocked external services)."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core import HFMirrorCore
from src.models import (
    AppConfig,
    CloneRequest,
    DiffRequest,
    HFFileInfo,
    HFRepoManifest,
    LFSPointer,
    MirroredRepo,
    MirrorState,
    PruneRequest,
)


@pytest.fixture
def mock_core(app_config, state_db):
    core = HFMirrorCore(app_config, state_db)
    return core


@pytest.mark.asyncio
async def test_list_repos_empty(mock_core):
    result = await mock_core.list_repos()
    assert result.repos == []


@pytest.mark.asyncio
async def test_get_repo_status_not_found(mock_core):
    result = await mock_core.get_repo_status("nonexistent/repo")
    assert result is None


@pytest.mark.asyncio
async def test_prune_not_found(mock_core):
    from src.errors import HFMirrorError
    with pytest.raises(HFMirrorError, match="not found"):
        await mock_core.prune(PruneRequest(repo_id="nonexistent/repo"))


@pytest.mark.asyncio
async def test_prune_dry_run(mock_core, tmp_dirs):
    # Create a repo with some files
    repo_dir = tmp_dirs["tier1"] / "org--model"
    repo_dir.mkdir()
    (repo_dir / "file.bin").write_bytes(b"x" * 100)

    repo = MirroredRepo(
        repo_id="org/model",
        gitea_repo_name="org--model",
        state=MirrorState.SYNCED,
        tier1_path=repo_dir,
    )
    await mock_core.state_db.upsert_repo(repo)

    result = await mock_core.prune(
        PruneRequest(repo_id="org/model", dry_run=True)
    )
    assert result.was_dry_run is True
    assert result.bytes_reclaimed == 100
    assert result.files_deleted == 1
    # Files should still exist
    assert (repo_dir / "file.bin").exists()


@pytest.mark.asyncio
async def test_doctor_runs(mock_core):
    with patch.object(mock_core.gitea, "health_check", new_callable=AsyncMock, return_value=False):
        result = await mock_core.doctor()
    assert len(result.checks) > 0
    # Gitea should fail since we mocked it as unreachable
    gitea_check = next(c for c in result.checks if c.name == "Gitea Connectivity")
    assert gitea_check.passed is False


@pytest.mark.asyncio
async def test_get_gitea_url(mock_core):
    url = mock_core.get_gitea_url("meta-llama/Llama-3.1-70B")
    assert "meta-llama--Llama-3.1-70B" in url
    assert "localhost:3000" in url


@pytest.mark.asyncio
async def test_diff_not_found(mock_core):
    from src.errors import HFMirrorError
    with pytest.raises(HFMirrorError, match="not found"):
        await mock_core.diff(DiffRequest(repo_id="nonexistent/repo"))
