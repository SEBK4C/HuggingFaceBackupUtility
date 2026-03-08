"""All Pydantic models shared across core, cli, and web modules."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, SecretStr, field_validator


_REPO_ID_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+(/[a-zA-Z0-9._-]+)?$")


def _validate_repo_id(v: str) -> str:
    """Validate repo_id matches HF Hub naming conventions."""
    v = v.strip()
    if not v:
        raise ValueError("repo_id must not be empty")
    if len(v) > 200:
        raise ValueError("repo_id too long")
    if not _REPO_ID_PATTERN.match(v):
        raise ValueError(
            f"Invalid repo_id '{v}'. Expected format: 'org/name' with "
            "alphanumeric characters, dots, hyphens, and underscores only."
        )
    return v


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class AppConfig(BaseModel):
    """Loaded from .env at startup. Validated, not guessed."""

    hf_token: SecretStr
    tier1_path: Path = Path("./downloads")
    tier2_path: Path | None = None
    tier_threshold_percent: int = Field(default=10, ge=1, le=90)
    gitea_port: int = Field(default=3000, ge=1024, le=65535)
    gitea_admin_user: str = "hfmirror"
    gitea_admin_password: SecretStr = SecretStr("")
    gitea_base_url: str = "http://localhost:3000"
    gitea_api_token: SecretStr | None = None
    gradio_port: int = Field(default=7860, ge=1024, le=65535)
    gradio_share: bool = False
    hf_concurrent_downloads: int = Field(default=4, ge=1, le=16)
    hf_chunk_size_mb: int = Field(default=64, ge=1, le=512)
    hf_retry_attempts: int = Field(default=5, ge=1, le=20)
    hf_retry_backoff_base: float = Field(default=2.0, ge=1.0, le=10.0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_file: str = "./hfmirror.log"


# ---------------------------------------------------------------------------
# HF Repository Metadata
# ---------------------------------------------------------------------------

class LFSPointer(BaseModel):
    sha256: str
    size: int


class HFFileInfo(BaseModel):
    """Single file in an HF repo, as returned by the Hub API."""

    rfilename: str
    size: int
    blob_id: str
    lfs: LFSPointer | None = None


class HFRepoManifest(BaseModel):
    """Complete snapshot of an HF repo's file tree at a specific revision."""

    repo_id: str
    repo_type: Literal["model", "dataset", "space"] = "model"
    revision: str = "main"
    files: list[HFFileInfo]
    total_size: int
    lfs_size: int
    fetched_at: datetime


# ---------------------------------------------------------------------------
# Sync State
# ---------------------------------------------------------------------------

class MirrorState(str, Enum):
    PENDING = "pending"
    CLONING = "cloning"
    SYNCED = "synced"
    STALE = "stale"
    UPDATING = "updating"
    ERROR = "error"
    PRUNED = "pruned"


class MirroredRepo(BaseModel):
    """Persistent record of a mirrored repository. Stored in SQLite."""

    repo_id: str
    gitea_repo_name: str
    state: MirrorState
    tier1_path: Path | None = None
    tier2_path: Path | None = None
    upstream_commit: str | None = None
    local_commit: str | None = None
    total_size_bytes: int = 0
    lfs_size_bytes: int = 0
    last_checked: datetime | None = None
    last_synced: datetime | None = None
    error_message: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

# --- Clone ---

class CloneRequest(BaseModel):
    repo_id: str
    revision: str = "main"
    force_tier: Literal["tier1", "tier2"] | None = None

    @field_validator("repo_id")
    @classmethod
    def check_repo_id(cls, v: str) -> str:
        return _validate_repo_id(v)


class CloneProgress(BaseModel):
    """Yielded during streaming clone operations."""

    phase: Literal["manifest", "download", "lfs", "gitea_push", "complete", "error"]
    file_name: str | None = None
    bytes_downloaded: int = 0
    bytes_total: int = 0
    files_completed: int = 0
    files_total: int = 0
    speed_bytes_sec: float = 0
    eta_seconds: float | None = None
    message: str = ""


class CloneResult(BaseModel):
    repo_id: str
    gitea_url: str
    state: MirrorState
    total_downloaded: int
    lfs_routed_to: Literal["tier1", "tier2"]
    duration_seconds: float
    warnings: list[str] = []


# --- Status / Diff ---

class DiffRequest(BaseModel):
    repo_id: str

    @field_validator("repo_id")
    @classmethod
    def check_repo_id(cls, v: str) -> str:
        return _validate_repo_id(v)


class FileDiff(BaseModel):
    filename: str
    change_type: Literal["added", "modified", "deleted", "unchanged"]
    is_lfs: bool
    local_size: int | None = None
    upstream_size: int | None = None
    text_diff: str | None = None


class DiffResult(BaseModel):
    repo_id: str
    local_commit: str
    upstream_commit: str
    is_up_to_date: bool
    changes: list[FileDiff]
    upstream_total_size: int


# --- Storage Migration ---

class MigrateRequest(BaseModel):
    repo_id: str
    target_tier: Literal["tier1", "tier2"]
    files: list[str] | None = None

    @field_validator("repo_id")
    @classmethod
    def check_repo_id(cls, v: str) -> str:
        return _validate_repo_id(v)


class MigrateResult(BaseModel):
    repo_id: str
    files_moved: int
    bytes_moved: int
    symlinks_created: int
    duration_seconds: float


# --- Prune ---

class PruneRequest(BaseModel):
    repo_id: str
    delete_from_gitea: bool = True
    scrub_lfs_blobs: bool = True
    dry_run: bool = False

    @field_validator("repo_id")
    @classmethod
    def check_repo_id(cls, v: str) -> str:
        return _validate_repo_id(v)


class PruneResult(BaseModel):
    repo_id: str
    bytes_reclaimed: int
    files_deleted: int
    gitea_repo_deleted: bool
    tier1_scrubbed: bool
    tier2_scrubbed: bool
    was_dry_run: bool


# --- List / Status ---

class RepoStatusRequest(BaseModel):
    repo_id: str | None = None


class RepoStatusResponse(BaseModel):
    repos: list[MirroredRepo]


# --- Doctor ---

class HealthCheck(BaseModel):
    name: str
    passed: bool
    message: str
    details: list[str] = []


class DoctorResult(BaseModel):
    checks: list[HealthCheck]
    all_passed: bool
