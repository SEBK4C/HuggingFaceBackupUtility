"""Unit tests for tier routing and symlink management."""

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from src.errors import InsufficientStorageError, MigrationError, SymlinkError
from src.models import AppConfig, HFFileInfo, HFRepoManifest, LFSPointer
from src.storage import (
    check_symlink_health,
    create_symlink,
    evaluate_tier_routing,
    find_orphaned_blobs,
    repo_id_to_dirname,
    verify_sha256,
)


def test_repo_id_to_dirname():
    assert repo_id_to_dirname("meta-llama/Llama-3.1-70B") == "meta-llama--Llama-3.1-70B"
    assert repo_id_to_dirname("single") == "single"


def test_verify_sha256(tmp_path):
    f = tmp_path / "test.bin"
    f.write_bytes(b"hello world")
    import hashlib
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert verify_sha256(f, expected) is True
    assert verify_sha256(f, "wrong") is False


def test_create_symlink(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"data")
    link = tmp_path / "link.bin"
    create_symlink(link, target)
    assert link.is_symlink()
    assert link.resolve() == target.resolve()


def test_create_symlink_missing_target(tmp_path):
    target = tmp_path / "nonexistent"
    link = tmp_path / "link"
    with pytest.raises(SymlinkError):
        create_symlink(link, target)


def test_check_symlink_health_all_ok(tmp_path):
    target = tmp_path / "tier2" / "file.bin"
    target.parent.mkdir()
    target.write_bytes(b"data")

    tier1 = tmp_path / "tier1"
    tier1.mkdir()
    link = tier1 / "file.bin"
    link.symlink_to(target)

    issues = check_symlink_health(tier1)
    assert len(issues) == 0


def test_check_symlink_health_dangling(tmp_path):
    tier1 = tmp_path / "tier1"
    tier1.mkdir()
    link = tier1 / "broken.bin"
    link.symlink_to(tmp_path / "nonexistent")

    issues = check_symlink_health(tier1)
    assert len(issues) == 1
    assert issues[0]["type"] == "dangling"


def test_find_orphaned_blobs(tmp_path):
    tier1 = tmp_path / "tier1"
    tier2 = tmp_path / "tier2"
    tier1.mkdir()
    tier2.mkdir()

    # File on tier2 with no symlink on tier1
    orphan = tier2 / "orphan.bin"
    orphan.write_bytes(b"data")

    # File on tier2 with a symlink on tier1
    linked = tier2 / "linked.bin"
    linked.write_bytes(b"data")
    link = tier1 / "linked.bin"
    link.symlink_to(linked)

    orphans = find_orphaned_blobs(tier1, tier2)
    assert len(orphans) == 1
    assert orphans[0].name == "orphan.bin"


def test_find_orphaned_blobs_no_tier2(tmp_path):
    tier1 = tmp_path / "tier1"
    tier2 = tmp_path / "tier2"
    tier1.mkdir()
    # tier2 doesn't exist
    orphans = find_orphaned_blobs(tier1, tier2)
    assert orphans == []


@pytest.mark.asyncio
async def test_evaluate_tier_routing_no_tier2(app_config):
    app_config.tier2_path = None
    manifest = HFRepoManifest(
        repo_id="org/model", files=[], total_size=0, lfs_size=100_000_000_000,
        fetched_at=datetime.now(),
    )
    result = await evaluate_tier_routing(manifest, app_config)
    assert result == "tier1"


@pytest.mark.asyncio
async def test_evaluate_tier_routing_fits_tier1(app_config):
    manifest = HFRepoManifest(
        repo_id="org/model", files=[], total_size=0, lfs_size=100,
        fetched_at=datetime.now(),
    )
    with patch("src.storage.shutil.disk_usage") as mock_usage:
        mock_usage.return_value = type("Usage", (), {"free": 1_000_000_000, "total": 2_000_000_000, "used": 1_000_000_000})()
        result = await evaluate_tier_routing(manifest, app_config)
    assert result == "tier1"


@pytest.mark.asyncio
async def test_evaluate_tier_routing_overflows_to_tier2(app_config):
    manifest = HFRepoManifest(
        repo_id="org/model", files=[], total_size=0,
        lfs_size=500_000_000,  # 500 MB — exceeds 10% of 1 GB free
        fetched_at=datetime.now(),
    )
    with patch("src.storage.shutil.disk_usage") as mock_usage:
        def side_effect(path):
            return type("Usage", (), {"free": 1_000_000_000, "total": 2_000_000_000, "used": 1_000_000_000})()
        mock_usage.side_effect = side_effect
        result = await evaluate_tier_routing(manifest, app_config)
    assert result == "tier2"


@pytest.mark.asyncio
async def test_evaluate_tier_routing_neither_fits(app_config):
    manifest = HFRepoManifest(
        repo_id="org/model", files=[], total_size=0,
        lfs_size=2_000_000_000,  # 2 GB
        fetched_at=datetime.now(),
    )
    with patch("src.storage.shutil.disk_usage") as mock_usage:
        mock_usage.return_value = type("Usage", (), {"free": 100_000_000, "total": 200_000_000, "used": 100_000_000})()
        with pytest.raises(InsufficientStorageError):
            await evaluate_tier_routing(manifest, app_config)
