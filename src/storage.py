"""Tier routing, symlink management, and capacity checks."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Literal

from .errors import InsufficientStorageError, MigrationError, SymlinkError
from .models import AppConfig, HFRepoManifest

logger = logging.getLogger(__name__)

JOURNAL_FILENAME = "migration.journal"


def repo_id_to_dirname(repo_id: str) -> str:
    """Convert HF repo_id (org/name) to directory-safe name (org--name)."""
    return repo_id.replace("/", "--")


def check_tier_accessible(path: Path, tier_name: str) -> None:
    """Verify a tier path is accessible (exists, is a mount or dir, writable)."""
    if not path.exists():
        raise InsufficientStorageError(f"{tier_name} path does not exist: {path}")
    if not os.access(path, os.W_OK):
        raise InsufficientStorageError(f"{tier_name} path is not writable: {path}")


async def evaluate_tier_routing(
    manifest: HFRepoManifest,
    config: AppConfig,
) -> Literal["tier1", "tier2"]:
    """Determine where LFS blobs should land based on capacity thresholds."""
    if config.tier2_path is None:
        return "tier1"

    tier1_free = shutil.disk_usage(config.tier1_path).free
    threshold_bytes = tier1_free * (config.tier_threshold_percent / 100)

    if manifest.lfs_size > threshold_bytes:
        check_tier_accessible(config.tier2_path, "Tier 2")
        tier2_free = shutil.disk_usage(config.tier2_path).free
        if manifest.lfs_size > tier2_free * 0.95:
            raise InsufficientStorageError(
                f"Neither tier has enough space. "
                f"Need {manifest.lfs_size} bytes, "
                f"Tier1 free: {tier1_free}, Tier2 free: {tier2_free}"
            )
        return "tier2"

    return "tier1"


def get_repo_tier_path(
    config: AppConfig, repo_id: str, tier: Literal["tier1", "tier2"]
) -> Path:
    """Get the storage path for a repo on a given tier."""
    dirname = repo_id_to_dirname(repo_id)
    base = config.tier1_path if tier == "tier1" else config.tier2_path
    if base is None:
        raise ValueError("Tier 2 path not configured")
    return base / dirname


def ensure_repo_dirs(
    config: AppConfig, repo_id: str, tier: Literal["tier1", "tier2"]
) -> Path:
    """Create and return the repo directory on the specified tier."""
    path = get_repo_tier_path(config, repo_id, tier)
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_symlink(source: Path, target: Path) -> None:
    """Create a symlink at source pointing to target. Atomic: target must exist first."""
    if not target.exists():
        raise SymlinkError(f"Symlink target does not exist: {target}")
    source.parent.mkdir(parents=True, exist_ok=True)
    # Remove existing file/symlink at source
    if source.exists() or source.is_symlink():
        source.unlink()
    source.symlink_to(target)
    logger.debug("Created symlink %s -> %s", source, target)


def verify_sha256(file_path: Path, expected_sha256: str) -> bool:
    """Verify a file's SHA256 matches the expected hash."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest() == expected_sha256


def check_symlink_health(tier1_path: Path) -> list[dict]:
    """Check all symlinks under tier1_path and return issues found."""
    issues: list[dict] = []
    if not tier1_path.exists():
        return issues

    for root, _dirs, files in os.walk(tier1_path):
        for fname in files:
            fpath = Path(root) / fname
            if fpath.is_symlink():
                target = fpath.resolve()
                if not target.exists():
                    issues.append({
                        "type": "dangling",
                        "symlink": str(fpath),
                        "target": str(os.readlink(fpath)),
                        "message": f"Dangling symlink: {fpath} -> {os.readlink(fpath)}",
                    })
                elif not os.access(target, os.R_OK):
                    issues.append({
                        "type": "unreadable",
                        "symlink": str(fpath),
                        "target": str(target),
                        "message": f"Symlink target not readable: {target}",
                    })

    return issues


def find_orphaned_blobs(tier1_path: Path, tier2_path: Path) -> list[Path]:
    """Find files on Tier 2 that have no corresponding symlink on Tier 1."""
    orphans: list[Path] = []
    if not tier2_path.exists():
        return orphans

    # Collect all symlink targets from tier1
    symlink_targets: set[Path] = set()
    if tier1_path.exists():
        for root, _dirs, files in os.walk(tier1_path):
            for fname in files:
                fpath = Path(root) / fname
                if fpath.is_symlink():
                    symlink_targets.add(fpath.resolve())

    # Check tier2 files
    for root, _dirs, files in os.walk(tier2_path):
        for fname in files:
            fpath = Path(root) / fname
            if fpath not in symlink_targets:
                orphans.append(fpath)

    return orphans


# --- Migration Journal ---

def _journal_path(config: AppConfig) -> Path:
    return Path("./run") / JOURNAL_FILENAME


def write_migration_journal(
    config: AppConfig,
    repo_id: str,
    filename: str,
    source: str,
    target: str,
    operation: str,
) -> None:
    """Write a migration journal entry for crash recovery."""
    journal = _journal_path(config)
    journal.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "repo_id": repo_id,
        "filename": filename,
        "source": source,
        "target": target,
        "operation": operation,
        "started_at": time.time(),
    }
    with open(journal, "w") as f:
        json.dump(entry, f)


def clear_migration_journal(config: AppConfig) -> None:
    """Remove the migration journal after successful completion."""
    journal = _journal_path(config)
    if journal.exists():
        journal.unlink()


def read_migration_journal(config: AppConfig) -> dict | None:
    """Read an incomplete migration journal if present."""
    journal = _journal_path(config)
    if not journal.exists():
        return None
    with open(journal) as f:
        return json.load(f)


async def migrate_file(
    config: AppConfig,
    repo_id: str,
    filename: str,
    target_tier: Literal["tier1", "tier2"],
) -> int:
    """Migrate a single file between tiers. Returns bytes moved."""
    dirname = repo_id_to_dirname(repo_id)
    tier1_base = config.tier1_path / dirname
    if config.tier2_path is None:
        raise MigrationError("Tier 2 path not configured")
    tier2_base = config.tier2_path / dirname

    if target_tier == "tier2":
        # Offload: Tier 1 → Tier 2
        source = tier1_base / filename
        dest = tier2_base / filename
        if source.is_symlink():
            raise MigrationError(f"File already on Tier 2: {filename}")
        if not source.exists():
            raise MigrationError(f"Source file not found: {source}")

        write_migration_journal(
            config, repo_id, filename, str(source), str(dest), "offload"
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = source.stat().st_size
        shutil.copy2(str(source), str(dest))
        # Replace source with symlink
        source.unlink()
        source.symlink_to(dest)
        clear_migration_journal(config)
        return size

    else:
        # Recall: Tier 2 → Tier 1
        link = tier1_base / filename
        if not link.is_symlink():
            raise MigrationError(f"File not symlinked (already on Tier 1): {filename}")
        real_file = link.resolve()
        if not real_file.exists():
            raise MigrationError(f"Symlink target missing: {real_file}")

        write_migration_journal(
            config, repo_id, filename, str(real_file), str(link), "recall"
        )
        size = real_file.stat().st_size
        link.unlink()
        shutil.copy2(str(real_file), str(link))
        clear_migration_journal(config)
        return size
