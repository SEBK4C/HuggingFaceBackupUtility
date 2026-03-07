# SPEC.md — Autonomous Hugging Face Dual-Core Downloader & Gitea Mirror

> Single source of truth. Every section answers *what*, *how*, and *what happens when it breaks*.

---

## 1. System Overview

A rootless, dual-interface (CLI + Web UI) application that:

1. Downloads Hugging Face model repositories (including multi-hundred-GB LFS blobs)
2. Mirrors them into a locally-provisioned Gitea instance with full Git history
3. Manages tiered storage (fast SSD ↔ archive RAID) with capacity-aware routing
4. Tracks sync state between upstream HF Hub and local Gitea mirrors

The system treats AI models as first-class Git repositories. Gitea provides native diff viewing, commit history, and branch management. Git LFS handles the heavy weights, with symlink-based tiering across heterogeneous storage.

### 1.1 Non-Goals

- This is NOT a model serving/inference system
- This is NOT a training pipeline — it manages weights at rest
- No multi-user access control beyond Gitea's built-in auth
- No cloud sync (S3, GCS) — strictly local storage tiers

---

## 2. Execution & Deployment

### 2.1 Bootstrap: `run.sh`

The entire system bootstraps from a single `run.sh` script. No other entry point exists.

#### Direct CLI Arguments

```
./run.sh                  # No args → interactive numbered menu
./run.sh web              # Start Web UI + Gitea
./run.sh cli [command]    # CLI mode (clone, list, diff, update, migrate, prune, doctor, setup, open)
./run.sh setup            # Interactive first-run wizard
./run.sh doctor           # Diagnose system health
./run.sh test [args]      # Run test suite
./run.sh stop             # Graceful shutdown of backgrounded services
./run.sh help             # Show usage and examples
```

#### Interactive Numbered Menu

When invoked with no arguments (`./run.sh`), an interactive menu is displayed:

```
╔══════════════════════════════════════════╗
║        HFMirror — Main Menu              ║
╠══════════════════════════════════════════╣
║  1)  Web UI        Start Web UI + Gitea  ║
║  2)  CLI           Enter CLI mode         ║
║  3)  Setup         First-run wizard       ║
║  4)  Doctor        System health checks   ║
║  5)  Test          Run test suite          ║
║  6)  Stop          Stop services           ║
║  7)  Help          Show CLI usage          ║
║  0)  Exit                                  ║
╚══════════════════════════════════════════╝
```

Numbers also work as direct arguments (e.g. `./run.sh 1` is equivalent to `./run.sh web`).

#### 2.1.1 Zero-Privilege Constraints

- No `sudo`, no root, no system package managers (`apt`, `brew`, `pacman`)
- Only external dependencies: `curl`, `tar`, `git` (validated at boot)
- `git-lfs` is auto-bootstrapped via curl if missing (see 2.1.2)
- Python managed exclusively via `uv` (downloaded if missing)
- `chmod +x run.sh` is the single setup prerequisite

#### 2.1.2 Git LFS Bootstrapping (Cross-Platform via curl)

If `git-lfs` is not found on `$PATH`, `run.sh` downloads it locally:

```
1. Create local binary directory: .bin/ (added to PATH and .gitignore)
2. Detect OS:    uname -s | tr upper lower → linux | darwin
3. Detect arch:  uname -m → amd64 | arm64
4. Download from GitHub releases:
     darwin → git-lfs-darwin-{arch}-v{version}.zip  (unzip -j)
     linux  → git-lfs-linux-{arch}-v{version}.tar.gz (tar --strip-components=1)
5. chmod +x .bin/git-lfs
6. git lfs install --skip-repo (user-space init, no root)
```

No user intervention required. The binary lives in `.bin/` (project-local, gitignored).

#### 2.1.3 Platform Detection Sequence

On first run, `run.sh` detects the execution environment for Gitea:

```
1. Detect OS:    uname -s → Linux | Darwin
2. Detect arch:  uname -m → x86_64 | aarch64 | arm64
3. Map to Gitea release asset name:
     Linux  + x86_64  → gitea-{version}-linux-amd64
     Linux  + aarch64 → gitea-{version}-linux-arm64
     Darwin + arm64   → gitea-{version}-darwin-arm64
     Darwin + x86_64  → gitea-{version}-darwin-amd64
4. Download Gitea binary to ./bin/gitea
5. Verify SHA256 checksum against published checksums
```

#### 2.1.4 `uv` Bootstrap

```
1. Check: command -v uv
2. If missing: curl -LsSf https://astral.sh/uv/install.sh | sh
3. Create venv:  uv venv .venv
4. Sync deps:    uv pip sync requirements.lock
5. Activate:     source .venv/bin/activate
```

The `requirements.lock` is committed to the repo. `uv pip compile requirements.in -o requirements.lock` generates it.

#### 2.1.5 Process Management (Web Mode)

When `./run.sh web` is invoked:

```
1. Start Gitea:   ./bin/gitea web --config ./gitea-data/app.ini &
                   Store PID in ./run/gitea.pid
2. Wait for Gitea: Poll localhost:3000/api/v1/version (max 30s, 1s interval)
3. Start Gradio:   python -m src.web &
                   Store PID in ./run/gradio.pid
4. Trap SIGINT/SIGTERM → kill both PIDs, rm pidfiles
5. Wait on both PIDs
```

Stale PID files are detected on startup: if `./run/gitea.pid` exists but the process is dead, the file is removed and startup proceeds.

#### 2.1.6 `.gitignore` Management

On every invocation, `run.sh` ensures these entries exist in `.gitignore` (append-only, idempotent):

```
.env
.venv/
gitea-data/
bin/
run/
downloads/
*.log
```

### 2.2 Configuration: `.env`

All runtime configuration lives in a single `.env` file. The schema:

```ini
# --- Required ---
HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxx

# --- Storage Tiers ---
TIER1_PATH=/mnt/nvme/models           # Fast SSD. Default: ./downloads
TIER2_PATH=/mnt/raid/models-archive   # Archive HDD/RAID. Default: (none, tiering disabled)

# --- Capacity Routing ---
TIER_THRESHOLD_PERCENT=10             # Route to Tier 2 if payload > N% of Tier 1 free space

# --- Gitea ---
GITEA_PORT=3000
GITEA_ADMIN_USER=hfmirror
GITEA_ADMIN_PASSWORD=                 # Auto-generated on first run if blank
GITEA_BASE_URL=http://localhost:3000

# --- Gradio ---
GRADIO_PORT=7860
GRADIO_SHARE=false                    # Set true for Gradio public URL

# --- Networking ---
HF_CONCURRENT_DOWNLOADS=4            # Max parallel file downloads per repo
HF_CHUNK_SIZE_MB=64                   # Streaming chunk size for large files
HF_RETRY_ATTEMPTS=5                   # Per-file retry count
HF_RETRY_BACKOFF_BASE=2              # Exponential backoff base (seconds)

# --- Logging ---
LOG_LEVEL=INFO                        # DEBUG | INFO | WARNING | ERROR
LOG_FILE=./hfmirror.log               # Rotated at 50MB, keep 3
```

Missing required keys cause a startup error with a message pointing to `./run.sh setup`.

---

## 3. Architecture: The Rule of Three

### 3.1 Module Boundaries

```
src/
├── core.py          # Business logic. NO imports from cli.py or web.py.
├── cli.py           # Typer app. Imports core. Formats with Rich.
├── web.py           # Gradio app. Imports core. Renders to browser.
├── models.py        # All Pydantic models (shared across all three).
├── gitea_client.py  # Gitea REST API client (used by core.py).
├── hf_client.py     # Hugging Face Hub API client (used by core.py).
├── storage.py       # Tier routing, symlink management, capacity checks.
└── state.py         # SQLite state database operations.
```

The invariant: **`cli.py` and `web.py` are pure presentation layers.** They construct Pydantic request models, pass them to `core.py` functions, and format the Pydantic response models for display. Zero business logic leaks into the presentation layer.

### 3.2 Data Flow

```
User Action
    │
    ▼
cli.py / web.py
    │  construct RequestModel
    ▼
core.py
    │  orchestrates calls to:
    ├─→ hf_client.py     (upstream HF Hub)
    ├─→ gitea_client.py  (local Gitea API)
    ├─→ storage.py       (tier routing, disk ops)
    └─→ state.py         (sync state DB)
    │
    │  returns ResponseModel
    ▼
cli.py / web.py
    │  format + display
    ▼
User Output
```

### 3.3 Async Strategy

`core.py` is fully async (`async def`). The HTTP client is `httpx.AsyncClient` (connection pooling, HTTP/2 support).

- **CLI invocation:** `asyncio.run(core.some_function(request))`
- **Web invocation:** Gradio's native async support calls `core.py` directly
- **Download concurrency:** `asyncio.Semaphore(HF_CONCURRENT_DOWNLOADS)` gates parallel file fetches within a single repo clone

---

## 4. Pydantic Models (`models.py`)

### 4.1 Configuration

```python
class AppConfig(BaseModel):
    """Loaded from .env at startup. Validated, not guessed."""
    hf_token: SecretStr
    tier1_path: Path
    tier2_path: Path | None = None
    tier_threshold_percent: int = Field(default=10, ge=1, le=90)
    gitea_port: int = Field(default=3000, ge=1024, le=65535)
    gitea_admin_user: str = "hfmirror"
    gitea_admin_password: SecretStr
    gitea_base_url: HttpUrl = "http://localhost:3000"
    gradio_port: int = Field(default=7860, ge=1024, le=65535)
    hf_concurrent_downloads: int = Field(default=4, ge=1, le=16)
    hf_chunk_size_mb: int = Field(default=64, ge=1, le=512)
    hf_retry_attempts: int = Field(default=5, ge=1, le=20)
    hf_retry_backoff_base: float = Field(default=2.0, ge=1.0, le=10.0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
```

### 4.2 HF Repository Metadata

```python
class HFFileInfo(BaseModel):
    """Single file in an HF repo, as returned by the Hub API."""
    rfilename: str                    # Relative path within repo
    size: int                         # Bytes
    blob_id: str                      # SHA256 of content
    lfs: LFSPointer | None = None     # Present if file is LFS-managed

class LFSPointer(BaseModel):
    sha256: str
    size: int

class HFRepoManifest(BaseModel):
    """Complete snapshot of an HF repo's file tree at a specific revision."""
    repo_id: str                      # e.g. "meta-llama/Llama-3.1-70B"
    repo_type: Literal["model", "dataset", "space"] = "model"
    revision: str = "main"            # Branch/tag/commit
    files: list[HFFileInfo]
    total_size: int                   # Sum of all file sizes
    lfs_size: int                     # Sum of LFS file sizes only
    fetched_at: datetime
```

### 4.3 Sync State

```python
class MirrorState(str, Enum):
    """Lifecycle states for a mirrored repository."""
    PENDING = "pending"               # Queued, not yet started
    CLONING = "cloning"               # Initial download in progress
    SYNCED = "synced"                 # Up to date with upstream
    STALE = "stale"                   # Upstream has newer commits
    UPDATING = "updating"             # Pulling new changes
    ERROR = "error"                   # Last operation failed
    PRUNED = "pruned"                 # Soft-deleted, data may remain

class MirroredRepo(BaseModel):
    """Persistent record of a mirrored repository. Stored in SQLite."""
    repo_id: str                      # HF repo identifier
    gitea_repo_name: str              # Sanitized name in Gitea
    state: MirrorState
    tier1_path: Path | None           # Where lightweight files live
    tier2_path: Path | None           # Where LFS blobs were routed
    upstream_commit: str | None       # Latest known HF commit SHA
    local_commit: str | None          # Latest local Gitea commit SHA
    total_size_bytes: int = 0
    lfs_size_bytes: int = 0
    last_checked: datetime | None
    last_synced: datetime | None
    error_message: str | None = None
    created_at: datetime
```

### 4.4 Request / Response Models

```python
# --- Clone ---
class CloneRequest(BaseModel):
    repo_id: str                                 # e.g. "meta-llama/Llama-3.1-70B"
    revision: str = "main"
    force_tier: Literal["tier1", "tier2"] | None = None  # Override auto-routing

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

class FileDiff(BaseModel):
    filename: str
    change_type: Literal["added", "modified", "deleted", "unchanged"]
    is_lfs: bool
    local_size: int | None
    upstream_size: int | None
    text_diff: str | None = None      # Only for non-LFS text files

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
    files: list[str] | None = None    # None = migrate all LFS blobs

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
    scrub_lfs_blobs: bool = True      # Remove from ALL tiers
    dry_run: bool = False

class PruneResult(BaseModel):
    repo_id: str
    bytes_reclaimed: int
    files_deleted: int
    gitea_repo_deleted: bool
    tier1_scrubbed: bool
    tier2_scrubbed: bool
    was_dry_run: bool
```

---

## 5. Gitea Provisioning (`gitea_client.py`)

### 5.1 First-Run Initialization

On first boot (detected by absence of `./gitea-data/app.ini`):

```
1. Create directory structure:
     ./gitea-data/
     ├── app.ini          # Generated config
     ├── gitea.db          # SQLite database
     ├── repositories/     # Bare Git repos
     ├── lfs/              # LFS object storage
     └── log/

2. Generate app.ini with these overrides:
     [database]
     DB_TYPE  = sqlite3
     PATH     = ./gitea-data/gitea.db

     [server]
     HTTP_PORT        = {GITEA_PORT}
     ROOT_URL         = {GITEA_BASE_URL}
     LFS_START_SERVER = true
     LFS_CONTENT_PATH = ./gitea-data/lfs

     [repository]
     ROOT = ./gitea-data/repositories

     [security]
     INSTALL_LOCK = true

     [service]
     DISABLE_REGISTRATION = true

     [log]
     ROOT_PATH = ./gitea-data/log
     MODE      = file
     LEVEL     = Warn

3. Start Gitea, wait for HTTP readiness
4. Create admin user via CLI:
     ./bin/gitea admin user create \
       --username {GITEA_ADMIN_USER} \
       --password {GITEA_ADMIN_PASSWORD} \
       --email admin@localhost \
       --admin \
       --config ./gitea-data/app.ini
5. Generate API token via REST API:
     POST /api/v1/users/{user}/tokens
     Store token in .env as GITEA_API_TOKEN
```

### 5.2 Gitea API Surface (Used Endpoints)

All interactions go through Gitea's REST API v1, authenticated with the generated token.

| Operation | Method | Endpoint |
|-----------|--------|----------|
| Health check | GET | `/api/v1/version` |
| Create repo | POST | `/api/v1/user/repos` |
| Delete repo | DELETE | `/api/v1/repos/{owner}/{repo}` |
| Get repo info | GET | `/api/v1/repos/{owner}/{repo}` |
| List repos | GET | `/api/v1/repos/search?limit=50` |
| Get commits | GET | `/api/v1/repos/{owner}/{repo}/git/refs` |

Git push operations use the standard Git CLI with HTTP basic auth against `http://{user}:{password}@localhost:{port}/{owner}/{repo}.git`.

### 5.3 Repository Naming Convention

HF repo IDs contain `/` which is invalid for Gitea repo names. Mapping:

```
meta-llama/Llama-3.1-70B  →  meta-llama--Llama-3.1-70B
stabilityai/stable-diffusion-xl-base-1.0  →  stabilityai--stable-diffusion-xl-base-1.0
```

The separator `--` is used (double hyphen). The mapping is bijective and stored in `MirroredRepo.gitea_repo_name`.

---

## 6. Storage Tiering (`storage.py`)

### 6.1 Capacity Check

Before any download:

```python
async def evaluate_tier_routing(
    manifest: HFRepoManifest,
    config: AppConfig,
) -> Literal["tier1", "tier2"]:
    """
    Determine where LFS blobs should land.

    Returns "tier1" if:
      - Tier 2 is not configured, OR
      - LFS payload fits within the threshold

    Returns "tier2" if:
      - LFS payload exceeds threshold % of Tier 1 free space
    """
    if config.tier2_path is None:
        return "tier1"

    tier1_free = shutil.disk_usage(config.tier1_path).free
    threshold_bytes = tier1_free * (config.tier_threshold_percent / 100)

    if manifest.lfs_size > threshold_bytes:
        # Verify Tier 2 has enough space too
        tier2_free = shutil.disk_usage(config.tier2_path).free
        if manifest.lfs_size > tier2_free * 0.95:  # 5% safety margin
            raise InsufficientStorageError(
                f"Neither tier has enough space. "
                f"Need {manifest.lfs_size} bytes, "
                f"Tier1 free: {tier1_free}, Tier2 free: {tier2_free}"
            )
        return "tier2"

    return "tier1"
```

### 6.2 Symlink Architecture

When LFS blobs route to Tier 2, the directory structure mirrors Tier 1:

```
Tier 1 (SSD):
  /mnt/nvme/models/meta-llama--Llama-3.1-70B/
  ├── README.md                          # Real file (small)
  ├── config.json                        # Real file (small)
  ├── tokenizer.json                     # Real file (small)
  └── model-00001-of-00030.safetensors   # → SYMLINK

Tier 2 (RAID):
  /mnt/raid/models-archive/meta-llama--Llama-3.1-70B/
  └── model-00001-of-00030.safetensors   # Real file (large)
```

Symlink creation is atomic: write the real file to Tier 2 first, verify SHA256, then create the symlink on Tier 1.

### 6.3 Symlink Health Checks

`./run.sh doctor` and the Web UI "Health" tab verify:

- All symlinks resolve (target exists and is readable)
- Symlink targets match expected SHA256
- No orphaned blobs on Tier 2 (blobs with no corresponding symlink)
- No dangling symlinks on Tier 1 (symlinks pointing to missing targets)

Broken symlinks are reported with actionable remediation steps (re-download, re-migrate, or prune).

### 6.4 Migration Operations

Manual migration between tiers:

```
Tier 1 → Tier 2 (offload):
  1. Copy file to Tier 2 path
  2. Verify SHA256 matches
  3. Replace Tier 1 file with symlink → Tier 2
  4. Update MirroredRepo record

Tier 2 → Tier 1 (recall):
  1. Copy file from Tier 2 to Tier 1
  2. Verify SHA256 matches
  3. Remove symlink, file is now real on Tier 1
  4. Optionally delete Tier 2 copy (flag: --keep-archive)
  5. Update MirroredRepo record
```

Both operations are crash-safe: a journal file (`./run/migration.journal`) tracks the in-progress operation. On startup, incomplete migrations are detected and rolled back or completed.

---

## 7. Download Engine (`hf_client.py`)

### 7.1 HF Hub Interaction

Uses the `huggingface_hub` Python library (not raw API calls) for:

- `HfApi.repo_info()` — fetch repo metadata + commit SHA
- `HfApi.list_repo_tree()` — enumerate all files with LFS pointers
- Streaming downloads via `hf_hub_download()` with `resume_download=True`

Authentication: `HF_TOKEN` set as environment variable, picked up automatically by `huggingface_hub`.

### 7.2 Download Lifecycle

For each file in a clone/update operation:

```
1. Check state DB: was this file already downloaded at this blob_id?
   → Yes + file exists + SHA matches → Skip (idempotent)
   → Otherwise → Continue

2. Determine target path:
   → LFS file + routed to Tier 2 → download to Tier 2 path
   → Everything else → download to Tier 1 path

3. Stream download:
   → Chunk size: HF_CHUNK_SIZE_MB
   → Write to {target}.partial
   → Track bytes for progress reporting

4. Verify integrity:
   → Compare SHA256 of completed file against manifest blob_id
   → Mismatch → delete .partial, increment retry counter, retry
   → Match → rename .partial → final filename (atomic on POSIX)

5. If LFS routed to Tier 2 → create symlink on Tier 1

6. Update state DB: record blob_id, size, tier, timestamp
```

### 7.3 Retry & Backoff

Per-file retries with exponential backoff:

```
Attempt 1: immediate
Attempt 2: wait 2s
Attempt 3: wait 4s
Attempt 4: wait 8s
Attempt 5: wait 16s
```

Base and max attempts are configurable via `.env`. If all retries exhausted, the file is marked as failed in the state DB, the clone continues with remaining files, and the final `CloneResult` includes the failure in `warnings`.

### 7.4 Resumable Downloads

Partial downloads are preserved across crashes. On restart:

1. Scan for `*.partial` files in both tier paths
2. For each, check the state DB for the expected total size
3. Resume download with `Range: bytes={partial_size}-` header
4. `huggingface_hub` handles this natively with `resume_download=True`

---

## 8. State Database (`state.py`)

### 8.1 Schema

SQLite database at `./gitea-data/hfmirror.db`. Migrations managed with a simple version table.

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS mirrored_repos (
    repo_id          TEXT PRIMARY KEY,
    gitea_repo_name  TEXT NOT NULL UNIQUE,
    state            TEXT NOT NULL DEFAULT 'pending',
    tier1_path       TEXT,
    tier2_path       TEXT,
    upstream_commit  TEXT,
    local_commit     TEXT,
    total_size_bytes INTEGER DEFAULT 0,
    lfs_size_bytes   INTEGER DEFAULT 0,
    last_checked     TEXT,   -- ISO 8601
    last_synced      TEXT,   -- ISO 8601
    error_message    TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS file_records (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id          TEXT NOT NULL REFERENCES mirrored_repos(repo_id),
    rfilename        TEXT NOT NULL,
    blob_id          TEXT NOT NULL,
    size_bytes       INTEGER NOT NULL,
    is_lfs           BOOLEAN NOT NULL DEFAULT FALSE,
    storage_tier     TEXT,           -- 'tier1' | 'tier2'
    symlink_path     TEXT,           -- Tier 1 symlink if routed to Tier 2
    downloaded_at    TEXT,
    UNIQUE(repo_id, rfilename)
);

CREATE TABLE IF NOT EXISTS download_journal (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id          TEXT NOT NULL,
    rfilename        TEXT NOT NULL,
    operation        TEXT NOT NULL,  -- 'download' | 'migrate' | 'delete'
    status           TEXT NOT NULL,  -- 'in_progress' | 'completed' | 'failed'
    started_at       TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at     TEXT,
    error_message    TEXT
);
```

### 8.2 Concurrency

SQLite in WAL mode (`PRAGMA journal_mode=WAL`) allows concurrent reads during writes. All writes go through a single async connection managed by `aiosqlite`. The download journal enables crash recovery: on startup, any `in_progress` entries are inspected and either completed or rolled back.

---

## 9. Gitea Push Pipeline

After downloading files from HF Hub, the system pushes them into the local Gitea instance:

```
1. Create Gitea repo (if first clone):
     POST /api/v1/user/repos
     { "name": "{gitea_repo_name}", "private": false }

2. Initialize local bare work tree:
     git init --initial-branch=main ./tmp/{gitea_repo_name}
     cd ./tmp/{gitea_repo_name}

3. Configure Git LFS:
     git lfs install --local
     git lfs track "*.safetensors" "*.bin" "*.gguf" "*.ot" "*.pt" "*.pth" "*.h5"
     git add .gitattributes

4. Copy/symlink downloaded files into work tree

5. Stage and commit:
     git add -A
     git commit -m "Mirror {repo_id} @ {upstream_commit}"

6. Push to local Gitea:
     git remote add origin http://{user}:{pass}@localhost:{port}/{user}/{repo}.git
     git push -u origin main

7. Cleanup tmp work tree (keep only the Gitea bare repo)

8. Update state DB: set state=synced, local_commit={sha}
```

For **updates** (state=stale), the process diffs the manifest, downloads only changed files, and creates a new commit.

---

## 10. CLI Interface (`cli.py`)

Built with Typer + Rich. Commands mirror core operations exactly.

```
hfmirror clone <repo_id> [--revision main] [--force-tier tier2]
hfmirror status [repo_id]          # All repos or specific one
hfmirror diff <repo_id>            # Show upstream vs local changes
hfmirror update <repo_id>          # Pull upstream changes
hfmirror update --all              # Update all stale repos
hfmirror migrate <repo_id> --to tier2 [--files "*.safetensors"]
hfmirror prune <repo_id> [--dry-run] [--keep-gitea]
hfmirror list                      # Table of all mirrored repos
hfmirror doctor                    # Health checks
hfmirror setup                     # Interactive config wizard
hfmirror open <repo_id>            # Print Gitea URL for the repo
```

### 10.1 Progress Display

Clone/update operations use Rich's `Progress` with multiple tasks:

```
Cloning meta-llama/Llama-3.1-70B
━━━━━━━━━━━━━━━━━━━━ 47% │ 12/30 files │ 156.2 GB / 332.1 GB │ 485 MB/s │ ETA 6m 12s
  ↳ model-00013-of-00030.safetensors  ━━━━━━━━━━━━━━ 72% │ 3.2/4.5 GB │ 512 MB/s
  ↳ model-00014-of-00030.safetensors  ━━━━━━━━━━━━━━ 31% │ 1.4/4.5 GB │ 478 MB/s
  ↳ model-00015-of-00030.safetensors  ━━━━━━━━━━━━━━  8% │ 0.4/4.5 GB │ 501 MB/s
  ↳ model-00016-of-00030.safetensors  ━━━━━━━━━━━━━━  2% │ 0.1/4.5 GB │ 489 MB/s
```

### 10.2 Status Table

```
┌──────────────────────────────────┬──────────┬───────────┬──────────────┬─────────────┐
│ Repository                       │ State    │ Size      │ LFS Tier     │ Last Synced │
├──────────────────────────────────┼──────────┼───────────┼──────────────┼─────────────┤
│ meta-llama/Llama-3.1-70B         │ ✓ synced │ 332.1 GB  │ tier2 (RAID) │ 2h ago      │
│ mistralai/Mistral-7B-v0.3        │ ✓ synced │  14.5 GB  │ tier1 (SSD)  │ 1d ago      │
│ stabilityai/sdxl-base-1.0        │ ⚠ stale  │   6.8 GB  │ tier1 (SSD)  │ 14d ago     │
│ openai/whisper-large-v3          │ ✗ error  │   3.1 GB  │ tier1 (SSD)  │ never       │
└──────────────────────────────────┴──────────┴───────────┴──────────────┴─────────────┘
```

---

## 11. Web Interface (`web.py`)

Built with Gradio Blocks. Layout:

### 11.1 Tabs

1. **Dashboard** — Overview table of all mirrored repos (mirrors CLI `list`), with inline action buttons (update, diff, prune, open in Gitea).
2. **Clone** — Input field for repo ID, revision selector, tier override toggle. Live progress bar during clone.
3. **Storage** — Visual bar chart of Tier 1 and Tier 2 usage. Per-repo breakdown. Migration controls (drag-and-drop repos between tiers).
4. **Health** — Output of `doctor` checks. Symlink status. Broken link remediation buttons.
5. **Settings** — Edit `.env` values through form fields. Save triggers config reload (no restart needed for most settings).

### 11.2 Real-Time Updates

Clone/update operations stream `CloneProgress` objects to the Web UI via Gradio's generator pattern:

```python
async def clone_streaming(repo_id: str):
    async for progress in core.clone(CloneRequest(repo_id=repo_id)):
        yield format_progress(progress)  # Returns Gradio update dict
```

---

## 12. Error Handling

### 12.1 Error Taxonomy

```python
class HFMirrorError(Exception):
    """Base exception for all application errors."""

class AuthenticationError(HFMirrorError):
    """HF token invalid or expired."""

class RateLimitError(HFMirrorError):
    """HF API rate limit hit. Includes retry_after_seconds."""

class InsufficientStorageError(HFMirrorError):
    """Neither tier has enough free space for the operation."""

class IntegrityError(HFMirrorError):
    """SHA256 mismatch after download."""

class GiteaError(HFMirrorError):
    """Gitea API returned an error or is unreachable."""

class SymlinkError(HFMirrorError):
    """Symlink target missing, dangling, or permission denied."""

class MigrationError(HFMirrorError):
    """Storage migration failed mid-operation."""
```

### 12.2 Error Recovery Patterns

| Scenario | Behavior |
|----------|----------|
| Network timeout during download | Retry with backoff, resume from partial |
| SHA256 mismatch | Delete corrupt file, retry download |
| Gitea unreachable during push | Queue push, retry on next `doctor` or manual trigger |
| RAID unmounted mid-operation | Detect via `os.path.ismount()` before any Tier 2 write, fail fast with clear message |
| Stale PID file | Check if process alive, clean up if dead, proceed |
| Interrupted migration | Journal-based recovery on next startup |
| Disk full during download | Catch `OSError`, report remaining space, suggest prune |

---

## 13. Logging & Observability

### 13.1 Logging Strategy

Structured logging via Python's `logging` module with a JSON formatter for machine parsing and a Rich handler for console output.

```python
# Log format (file):
{"ts": "2025-03-07T14:23:01Z", "level": "INFO", "module": "hf_client",
 "msg": "Download complete", "repo": "meta-llama/Llama-3.1-70B",
 "file": "model-00013.safetensors", "bytes": 4831838208, "duration_s": 9.7}

# Log format (console):
14:23:01 INFO  [hf_client] Download complete: model-00013.safetensors (4.5 GB in 9.7s)
```

Log rotation: 50 MB max size, keep 3 rotated files.

### 13.2 Metrics (In-Memory)

The `doctor` command and Web UI "Health" tab expose:

- Total storage used per tier (with percentage of capacity)
- Number of repos by state (synced, stale, error)
- Download history (last 100 operations with speed/duration)
- Symlink health summary

---

## 14. Testing Strategy

### 14.1 Test Structure

```
tests/
├── conftest.py              # Shared fixtures (tmp dirs, mock Gitea, mock HF)
├── test_core.py             # Integration tests for core orchestration
├── test_storage.py          # Unit tests for tier routing and symlinks
├── test_hf_client.py        # Unit tests with mocked HF Hub responses
├── test_gitea_client.py     # Unit tests with mocked Gitea API
├── test_state.py            # SQLite state operations
├── test_cli.py              # Typer CLI invocation tests
└── test_models.py           # Pydantic validation edge cases
```

### 14.2 Key Test Scenarios

- **Tier routing:** Verify threshold calculation with various free-space scenarios
- **Symlink integrity:** Create, break, detect, and repair symlinks
- **Resumable download:** Simulate interrupted download, verify resume from partial
- **Crash recovery:** Kill mid-migration, verify journal-based recovery
- **Gitea push:** Mock Gitea API, verify correct repo creation and push sequence
- **Idempotency:** Run clone twice for same repo, verify no duplicate work
- **Edge cases:** Repo with zero LFS files, repo with only LFS files, empty repo, repo name with special characters

### 14.3 Running Tests

```bash
./run.sh test              # Run all tests
./run.sh test --coverage   # With coverage report
```

Internally: `uv run pytest tests/ -v --tb=short`

---

## 15. Future Considerations (Out of Scope for v1)

These are explicitly deferred but architecturally accounted for:

- **Scheduled sync:** Cron-like periodic `update --all` (the state DB supports this; just needs a scheduler loop)
- **Webhook triggers:** Gitea webhooks for downstream CI/CD when a model updates
- **Multi-user Gitea:** Currently single-admin; Gitea supports it natively
- **S3-compatible Tier 3:** The `storage.py` tier abstraction can extend to object storage
- **Bandwidth throttling:** `HF_MAX_BANDWIDTH_MBPS` config key (plumbing exists in chunk-based download)
- **Deduplication:** Cross-repo LFS dedup via content-addressable storage (Gitea LFS already does this internally)
