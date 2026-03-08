"""Integration tests for core orchestration (with mocked external services)."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core import HFMirrorCore
from src.models import (
    AppConfig,
    CloneProgress,
    CloneRequest,
    DiffRequest,
    HFFileInfo,
    HFRepoManifest,
    LFSPointer,
    MigrateRequest,
    MirroredRepo,
    MirrorState,
    PruneRequest,
)


@pytest.fixture
def mock_core(app_config, state_db):
    core = HFMirrorCore(app_config, state_db)
    return core


# --- list_repos / get_repo_status ---

@pytest.mark.asyncio
async def test_list_repos_empty(mock_core):
    result = await mock_core.list_repos()
    assert result.repos == []


@pytest.mark.asyncio
async def test_list_repos_with_data(mock_core, tmp_dirs):
    repo = MirroredRepo(
        repo_id="org/model",
        gitea_repo_name="org--model",
        state=MirrorState.SYNCED,
        tier1_path=tmp_dirs["tier1"] / "org--model",
    )
    await mock_core.state_db.upsert_repo(repo)
    result = await mock_core.list_repos()
    assert len(result.repos) == 1
    assert result.repos[0].repo_id == "org/model"


@pytest.mark.asyncio
async def test_get_repo_status_not_found(mock_core):
    result = await mock_core.get_repo_status("nonexistent/repo")
    assert result is None


@pytest.mark.asyncio
async def test_get_repo_status_found(mock_core, tmp_dirs):
    repo = MirroredRepo(
        repo_id="org/found-model",
        gitea_repo_name="org--found-model",
        state=MirrorState.SYNCED,
        tier1_path=tmp_dirs["tier1"] / "org--found-model",
        upstream_commit="deadbeef",
    )
    await mock_core.state_db.upsert_repo(repo)
    result = await mock_core.get_repo_status("org/found-model")
    assert result is not None
    assert result.repo_id == "org/found-model"
    assert result.state == MirrorState.SYNCED
    assert result.upstream_commit == "deadbeef"


# --- clone ---

@pytest.mark.asyncio
async def test_clone_full_flow(mock_core, tmp_dirs):
    manifest = HFRepoManifest(
        repo_id="org/model",
        revision="main",
        files=[HFFileInfo(rfilename="config.json", size=100, blob_id="blob1")],
        total_size=100,
        lfs_size=0,
        fetched_at=datetime.now(),
    )

    async def fake_streaming(*args, **kwargs):
        yield CloneProgress(
            phase="download",
            message="downloading",
            files_completed=1,
            files_total=1,
            bytes_downloaded=100,
            bytes_total=100,
        )

    with (
        patch.object(mock_core.hf, "fetch_manifest", new_callable=AsyncMock, return_value=manifest),
        patch.object(mock_core.hf, "get_upstream_commit", new_callable=AsyncMock, return_value="abc123"),
        patch.object(mock_core.hf, "download_repo_streaming", side_effect=fake_streaming),
        patch.object(mock_core.gitea, "create_repo", new_callable=AsyncMock),
        patch.object(mock_core.gitea, "git_push_repo", new_callable=AsyncMock, return_value="commit_sha"),
        patch("src.core.evaluate_tier_routing", new_callable=AsyncMock, return_value="tier1"),
    ):
        phases = []
        async for progress in mock_core.clone(CloneRequest(repo_id="org/model")):
            phases.append(progress.phase)

    assert "manifest" in phases
    assert "gitea_push" in phases
    assert "complete" in phases

    repo = await mock_core.state_db.get_repo("org/model")
    assert repo is not None
    assert repo.state == MirrorState.SYNCED
    assert repo.upstream_commit == "abc123"
    assert repo.local_commit == "commit_sha"


@pytest.mark.asyncio
async def test_clone_hf_error_yields_error_phase(mock_core):
    with patch.object(mock_core.hf, "fetch_manifest", new_callable=AsyncMock, side_effect=Exception("HF down")):
        phases = []
        messages = []
        async for progress in mock_core.clone(CloneRequest(repo_id="org/model")):
            phases.append(progress.phase)
            messages.append(progress.message)

    assert "error" in phases
    assert any("HF down" in m for m in messages)


# --- prune ---

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
async def test_prune_actual_scrub(mock_core, tmp_dirs):
    repo_dir = tmp_dirs["tier1"] / "org--scrub"
    repo_dir.mkdir()
    (repo_dir / "model.bin").write_bytes(b"x" * 200)
    (repo_dir / "config.json").write_bytes(b"y" * 50)

    repo = MirroredRepo(
        repo_id="org/scrub",
        gitea_repo_name="org--scrub",
        state=MirrorState.SYNCED,
        tier1_path=repo_dir,
    )
    await mock_core.state_db.upsert_repo(repo)

    with patch.object(mock_core.gitea, "delete_repo", new_callable=AsyncMock):
        result = await mock_core.prune(
            PruneRequest(repo_id="org/scrub", dry_run=False, scrub_lfs_blobs=True, delete_from_gitea=True)
        )

    assert result.was_dry_run is False
    assert result.files_deleted == 2
    assert result.bytes_reclaimed == 250
    assert result.tier1_scrubbed is True
    assert result.gitea_repo_deleted is True
    assert not repo_dir.exists()


@pytest.mark.asyncio
async def test_prune_keep_gitea(mock_core, tmp_dirs):
    repo_dir = tmp_dirs["tier1"] / "org--keep"
    repo_dir.mkdir()
    (repo_dir / "weights.bin").write_bytes(b"w" * 100)

    repo = MirroredRepo(
        repo_id="org/keep",
        gitea_repo_name="org--keep",
        state=MirrorState.SYNCED,
        tier1_path=repo_dir,
    )
    await mock_core.state_db.upsert_repo(repo)

    result = await mock_core.prune(
        PruneRequest(repo_id="org/keep", dry_run=False, scrub_lfs_blobs=True, delete_from_gitea=False)
    )
    assert result.gitea_repo_deleted is False
    assert result.tier1_scrubbed is True


# --- doctor ---

@pytest.mark.asyncio
async def test_doctor_runs(mock_core):
    with patch.object(mock_core.gitea, "health_check", new_callable=AsyncMock, return_value=False):
        result = await mock_core.doctor()
    assert len(result.checks) > 0
    # Gitea should fail since we mocked it as unreachable
    gitea_check = next(c for c in result.checks if c.name == "Gitea Connectivity")
    assert gitea_check.passed is False


@pytest.mark.asyncio
async def test_doctor_gitea_reachable(mock_core):
    with patch.object(mock_core.gitea, "health_check", new_callable=AsyncMock, return_value=True):
        result = await mock_core.doctor()
    gitea_check = next(c for c in result.checks if c.name == "Gitea Connectivity")
    assert gitea_check.passed is True
    assert result.all_passed is True


@pytest.mark.asyncio
async def test_doctor_error_repos_detected(mock_core, tmp_dirs):
    repo = MirroredRepo(
        repo_id="org/broken",
        gitea_repo_name="org--broken",
        state=MirrorState.ERROR,
        tier1_path=tmp_dirs["tier1"] / "org--broken",
        error_message="download failed",
    )
    await mock_core.state_db.upsert_repo(repo)

    with patch.object(mock_core.gitea, "health_check", new_callable=AsyncMock, return_value=True):
        result = await mock_core.doctor()

    repo_check = next(c for c in result.checks if c.name == "Repository States")
    assert repo_check.passed is False
    assert any("org/broken" in d for d in repo_check.details)


# --- get_gitea_url ---

@pytest.mark.asyncio
async def test_get_gitea_url(mock_core):
    url = mock_core.get_gitea_url("meta-llama/Llama-3.1-70B")
    assert "meta-llama--Llama-3.1-70B" in url
    assert "localhost:3000" in url


@pytest.mark.asyncio
async def test_get_gitea_url_simple(mock_core):
    url = mock_core.get_gitea_url("org/repo")
    assert "org--repo" in url


# --- diff ---

@pytest.mark.asyncio
async def test_diff_not_found(mock_core):
    from src.errors import HFMirrorError
    with pytest.raises(HFMirrorError, match="not found"):
        await mock_core.diff(DiffRequest(repo_id="nonexistent/repo"))


@pytest.mark.asyncio
async def test_diff_up_to_date(mock_core, tmp_dirs):
    repo = MirroredRepo(
        repo_id="org/uptodate",
        gitea_repo_name="org--uptodate",
        state=MirrorState.SYNCED,
        tier1_path=tmp_dirs["tier1"] / "org--uptodate",
        upstream_commit="abc123",
        local_commit="abc123",
    )
    await mock_core.state_db.upsert_repo(repo)
    await mock_core.state_db.upsert_file_record(
        repo_id="org/uptodate",
        rfilename="config.json",
        blob_id="blob1",
        size_bytes=100,
        is_lfs=False,
        storage_tier="tier1",
    )

    manifest = HFRepoManifest(
        repo_id="org/uptodate",
        revision="main",
        files=[HFFileInfo(rfilename="config.json", size=100, blob_id="blob1")],
        total_size=100,
        lfs_size=0,
        fetched_at=datetime.now(),
    )

    with (
        patch.object(mock_core.hf, "get_upstream_commit", new_callable=AsyncMock, return_value="abc123"),
        patch.object(mock_core.hf, "fetch_manifest", new_callable=AsyncMock, return_value=manifest),
    ):
        result = await mock_core.diff(DiffRequest(repo_id="org/uptodate"))

    assert result.is_up_to_date is True
    assert all(c.change_type == "unchanged" for c in result.changes)


@pytest.mark.asyncio
async def test_diff_with_upstream_changes(mock_core, tmp_dirs):
    repo = MirroredRepo(
        repo_id="org/stale",
        gitea_repo_name="org--stale",
        state=MirrorState.STALE,
        tier1_path=tmp_dirs["tier1"] / "org--stale",
        upstream_commit="old_sha",
        local_commit="old_sha",
    )
    await mock_core.state_db.upsert_repo(repo)
    # No file records → all upstream files will appear as "added"

    manifest = HFRepoManifest(
        repo_id="org/stale",
        revision="main",
        files=[
            HFFileInfo(rfilename="config.json", size=100, blob_id="new_blob"),
            HFFileInfo(rfilename="model.bin", size=5000, blob_id="lfs_blob",
                       lfs=LFSPointer(sha256="abc" * 20 + "ab", size=5000)),
        ],
        total_size=5100,
        lfs_size=5000,
        fetched_at=datetime.now(),
    )

    with (
        patch.object(mock_core.hf, "get_upstream_commit", new_callable=AsyncMock, return_value="new_sha"),
        patch.object(mock_core.hf, "fetch_manifest", new_callable=AsyncMock, return_value=manifest),
    ):
        result = await mock_core.diff(DiffRequest(repo_id="org/stale"))

    assert result.is_up_to_date is False
    assert result.upstream_commit == "new_sha"
    added = [c for c in result.changes if c.change_type == "added"]
    assert len(added) == 2


@pytest.mark.asyncio
async def test_diff_detects_deleted_files(mock_core, tmp_dirs):
    repo = MirroredRepo(
        repo_id="org/deleted",
        gitea_repo_name="org--deleted",
        state=MirrorState.SYNCED,
        tier1_path=tmp_dirs["tier1"] / "org--deleted",
        upstream_commit="sha1",
        local_commit="sha1",
    )
    await mock_core.state_db.upsert_repo(repo)
    # Local has a file that's no longer upstream
    await mock_core.state_db.upsert_file_record(
        repo_id="org/deleted",
        rfilename="old_file.bin",
        blob_id="old_blob",
        size_bytes=300,
        is_lfs=False,
        storage_tier="tier1",
    )

    manifest = HFRepoManifest(
        repo_id="org/deleted",
        revision="main",
        files=[],  # Upstream has no files
        total_size=0,
        lfs_size=0,
        fetched_at=datetime.now(),
    )

    with (
        patch.object(mock_core.hf, "get_upstream_commit", new_callable=AsyncMock, return_value="sha1"),
        patch.object(mock_core.hf, "fetch_manifest", new_callable=AsyncMock, return_value=manifest),
    ):
        result = await mock_core.diff(DiffRequest(repo_id="org/deleted"))

    deleted = [c for c in result.changes if c.change_type == "deleted"]
    assert len(deleted) == 1
    assert deleted[0].filename == "old_file.bin"


# --- migrate ---

@pytest.mark.asyncio
async def test_migrate_not_found(mock_core):
    from src.errors import HFMirrorError
    with pytest.raises(HFMirrorError, match="not found"):
        await mock_core.migrate(MigrateRequest(repo_id="nonexistent/repo", target_tier="tier2"))


@pytest.mark.asyncio
async def test_migrate_to_tier2(mock_core, tmp_dirs):
    repo_dir = tmp_dirs["tier1"] / "org--migrate"
    repo_dir.mkdir()
    (repo_dir / "model.bin").write_bytes(b"y" * 512)

    repo = MirroredRepo(
        repo_id="org/migrate",
        gitea_repo_name="org--migrate",
        state=MirrorState.SYNCED,
        tier1_path=repo_dir,
    )
    await mock_core.state_db.upsert_repo(repo)

    result = await mock_core.migrate(MigrateRequest(repo_id="org/migrate", target_tier="tier2"))

    assert result.files_moved == 1
    assert result.bytes_moved == 512
    assert result.symlinks_created == 1
    # Source on tier1 should now be a symlink
    assert (repo_dir / "model.bin").is_symlink()
    # Real file should exist on tier2
    tier2_repo_dir = tmp_dirs["tier2"] / "org--migrate"
    assert (tier2_repo_dir / "model.bin").exists()


@pytest.mark.asyncio
async def test_migrate_recall_to_tier1(mock_core, tmp_dirs):
    """After offloading to tier2, tier1 has symlinks.
    core.migrate scans only real files (not symlinks), so a recall attempt
    returns 0 files moved — the symlink state is preserved on tier1.
    """
    repo_dir = tmp_dirs["tier1"] / "org--recall"
    repo_dir.mkdir()
    (repo_dir / "weights.bin").write_bytes(b"z" * 256)

    repo = MirroredRepo(
        repo_id="org/recall",
        gitea_repo_name="org--recall",
        state=MirrorState.SYNCED,
        tier1_path=repo_dir,
    )
    await mock_core.state_db.upsert_repo(repo)

    # Offload to tier2: tier1 gets a symlink, tier2 gets the real file
    await mock_core.migrate(MigrateRequest(repo_id="org/recall", target_tier="tier2"))
    assert (repo_dir / "weights.bin").is_symlink()
    tier2_file = tmp_dirs["tier2"] / "org--recall" / "weights.bin"
    assert tier2_file.exists()

    # core.migrate scanner skips symlinks → recall finds 0 files to move
    result = await mock_core.migrate(MigrateRequest(repo_id="org/recall", target_tier="tier1"))
    assert result.files_moved == 0
    # Symlink on tier1 still intact
    assert (repo_dir / "weights.bin").is_symlink()
