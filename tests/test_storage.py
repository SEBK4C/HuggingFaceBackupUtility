"""Unit tests for tier routing and symlink management."""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from src.errors import InsufficientStorageError, MigrationError, SymlinkError
from src.models import AppConfig, HFFileInfo, HFRepoManifest, LFSPointer
from src.storage import (
    check_symlink_health,
    check_tier_accessible,
    clear_migration_journal,
    create_symlink,
    ensure_repo_dirs,
    evaluate_tier_routing,
    find_orphaned_blobs,
    get_repo_tier_path,
    migrate_file,
    read_migration_journal,
    repo_id_to_dirname,
    verify_sha256,
    write_migration_journal,
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


# --- check_tier_accessible ---

def test_check_tier_accessible_ok(tmp_path):
    d = tmp_path / "writable"
    d.mkdir()
    check_tier_accessible(d, "tier1")  # no exception


def test_check_tier_accessible_not_exists(tmp_path):
    with pytest.raises(InsufficientStorageError):
        check_tier_accessible(tmp_path / "nonexistent", "tier1")


def test_check_tier_accessible_not_writable(tmp_path):
    d = tmp_path / "readonly"
    d.mkdir()
    d.chmod(0o444)
    try:
        with pytest.raises(InsufficientStorageError):
            check_tier_accessible(d, "tier1")
    finally:
        d.chmod(0o755)


# --- get_repo_tier_path ---

def test_get_repo_tier_path_tier1(app_config):
    result = get_repo_tier_path(app_config, "org/model", "tier1")
    assert result == app_config.tier1_path / "org--model"


def test_get_repo_tier_path_tier2(app_config):
    result = get_repo_tier_path(app_config, "org/model", "tier2")
    assert result == app_config.tier2_path / "org--model"


def test_get_repo_tier_path_tier2_not_configured(app_config):
    app_config.tier2_path = None
    with pytest.raises(ValueError):
        get_repo_tier_path(app_config, "org/model", "tier2")


# --- ensure_repo_dirs ---

def test_ensure_repo_dirs_creates(app_config):
    path = ensure_repo_dirs(app_config, "org/model", "tier1")
    assert path.exists()
    assert path.is_dir()


def test_ensure_repo_dirs_idempotent(app_config):
    ensure_repo_dirs(app_config, "org/model", "tier1")
    ensure_repo_dirs(app_config, "org/model", "tier1")  # no error


# --- migrate_file ---

@pytest.fixture
def migrate_config(tmp_path):
    t1 = tmp_path / "tier1"
    t2 = tmp_path / "tier2"
    t1.mkdir()
    t2.mkdir()
    return AppConfig(hf_token="hf_test", tier1_path=t1, tier2_path=t2)


@pytest.mark.asyncio
async def test_migrate_file_tier1_to_tier2(migrate_config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    repo_id = "org/model"
    dirname = "org--model"
    filename = "weights.bin"
    t1_dir = migrate_config.tier1_path / dirname
    t1_dir.mkdir()
    (t1_dir / filename).write_bytes(b"data")

    size = await migrate_file(migrate_config, repo_id, filename, "tier2")

    t2_file = migrate_config.tier2_path / dirname / filename
    assert t2_file.exists()
    assert (t1_dir / filename).is_symlink()
    assert size == 4


@pytest.mark.asyncio
async def test_migrate_file_tier2_to_tier1(migrate_config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    repo_id = "org/model"
    dirname = "org--model"
    filename = "weights.bin"
    t2_dir = migrate_config.tier2_path / dirname
    t2_dir.mkdir()
    t2_file = t2_dir / filename
    t2_file.write_bytes(b"data")
    t1_dir = migrate_config.tier1_path / dirname
    t1_dir.mkdir()
    (t1_dir / filename).symlink_to(t2_file.resolve())

    size = await migrate_file(migrate_config, repo_id, filename, "tier1")

    t1_result = t1_dir / filename
    assert t1_result.is_file()
    assert not t1_result.is_symlink()
    assert size == 4


@pytest.mark.asyncio
async def test_migrate_file_already_on_tier2(migrate_config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    repo_id = "org/model"
    dirname = "org--model"
    filename = "weights.bin"
    t2_dir = migrate_config.tier2_path / dirname
    t2_dir.mkdir()
    t2_file = t2_dir / filename
    t2_file.write_bytes(b"data")
    t1_dir = migrate_config.tier1_path / dirname
    t1_dir.mkdir()
    (t1_dir / filename).symlink_to(t2_file.resolve())

    with pytest.raises(MigrationError):
        await migrate_file(migrate_config, repo_id, filename, "tier2")


@pytest.mark.asyncio
async def test_migrate_file_not_symlinked(migrate_config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    repo_id = "org/model"
    dirname = "org--model"
    filename = "weights.bin"
    t1_dir = migrate_config.tier1_path / dirname
    t1_dir.mkdir()
    (t1_dir / filename).write_bytes(b"data")

    with pytest.raises(MigrationError):
        await migrate_file(migrate_config, repo_id, filename, "tier1")


@pytest.mark.asyncio
async def test_migrate_file_no_tier2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    t1 = tmp_path / "tier1"
    t1.mkdir()
    config = AppConfig(hf_token="hf_test", tier1_path=t1, tier2_path=None)
    with pytest.raises(MigrationError):
        await migrate_file(config, "org/model", "weights.bin", "tier2")


# --- migration journal ---

def test_write_migration_journal(tmp_path, app_config, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_migration_journal(app_config, "org/model", "weights.bin", "/src", "/dst", "offload")
    journal_file = tmp_path / "run" / "migration.journal"
    assert journal_file.exists()
    data = json.loads(journal_file.read_text())
    assert data["repo_id"] == "org/model"
    assert data["filename"] == "weights.bin"
    assert data["operation"] == "offload"


def test_read_migration_journal(tmp_path, app_config, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_migration_journal(app_config, "org/model", "weights.bin", "/src", "/dst", "offload")
    result = read_migration_journal(app_config)
    assert isinstance(result, dict)
    assert result["repo_id"] == "org/model"


def test_read_migration_journal_no_file(tmp_path, app_config, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = read_migration_journal(app_config)
    assert result is None


def test_clear_migration_journal(tmp_path, app_config, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_migration_journal(app_config, "org/model", "weights.bin", "/src", "/dst", "offload")
    assert (tmp_path / "run" / "migration.journal").exists()
    clear_migration_journal(app_config)
    assert not (tmp_path / "run" / "migration.journal").exists()


def test_clear_migration_journal_no_file(tmp_path, app_config, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clear_migration_journal(app_config)  # no error
