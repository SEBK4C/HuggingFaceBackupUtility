# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

HFMirror is a dual-interface (CLI + Web UI) Python application that downloads Hugging Face model repositories (including multi-hundred-GB LFS blobs), mirrors them into a locally-provisioned Gitea instance, and manages tiered storage (SSD / RAID) with capacity-aware routing. The full specification lives in `SPEC.md`.

## Bootstrap & Running

Everything runs through `run.sh`. No system package managers (`apt`, `brew`, `pip`) — only `curl` and `uv`.

```bash
./run.sh cli [command]    # CLI mode
./run.sh web              # Web UI + Gitea (backgrounded)
./run.sh setup            # Interactive first-run wizard
./run.sh doctor           # Diagnose system health
./run.sh stop             # Graceful shutdown
./run.sh test             # Run all tests (uv run pytest tests/ -v --tb=short)
./run.sh test --coverage  # With coverage report
```

Dependencies are managed via `uv`. To add a pip package, add it to the `DEPENDENCIES` array in `run.sh`. To add a required env var, add it to the `REQUIRED_KEYS` array.

## Architecture: The Rule of Three

**This is the central architectural constraint.** Every feature must be exposed via both CLI and Web UI, with all business logic in `core.py`.

```
src/
├── core.py          # ALL business logic. Fully async. NO imports from cli/web.
├── cli.py           # Typer + Rich. Constructs Pydantic request → awaits core → prints result.
├── web.py           # Gradio Blocks. Maps UI inputs → Pydantic request → awaits core → renders.
├── models.py        # All Pydantic models (shared across all three modules).
├── gitea_client.py  # Gitea REST API client (used by core.py).
├── hf_client.py     # HF Hub API client using huggingface_hub library (used by core.py).
├── storage.py       # Tier routing, symlink management, capacity checks.
└── state.py         # SQLite (WAL mode, aiosqlite) state database operations.
```

**Data flow:** `cli.py`/`web.py` → construct `RequestModel` → `core.py` orchestrates → returns `ResponseModel` → `cli.py`/`web.py` formats for display.

## Key Conventions

- **Async throughout:** `core.py` functions are `async def`. CLI uses `asyncio.run()`, Gradio calls async directly.
- **HTTP client:** `httpx.AsyncClient` with connection pooling.
- **HF downloads:** Use `huggingface_hub` library (`hf_hub_download` with `resume_download=True`), not raw API calls.
- **Concurrency:** `asyncio.Semaphore(HF_CONCURRENT_DOWNLOADS)` gates parallel downloads per repo.
- **Pydantic everywhere:** All function inputs/outputs crossing module boundaries use Pydantic models defined in `models.py`.
- **Gitea repo naming:** `org/repo` → `org--repo` (double hyphen, bijective mapping).
- **Storage symlinks:** LFS blobs routed to Tier 2 get a symlink on Tier 1. Write real file first, verify SHA256, then create symlink (atomic).
- **Crash safety:** Download uses `.partial` files with atomic rename. Migrations use a journal file (`./run/migration.journal`) for recovery.

## Configuration

All config in `.env` file. Key required variable: `HF_TOKEN`. See `SPEC.md` section 2.2 for full schema. Config is loaded into `AppConfig` Pydantic model at startup.

## State Database

SQLite at `./gitea-data/hfmirror.db`. Three tables: `mirrored_repos`, `file_records`, `download_journal`. Schema in `SPEC.md` section 8.1.

## Adding a New Feature

1. Define `FeatureRequest` and `FeatureResponse` Pydantic models in `models.py`
2. Implement `async def feature(req: FeatureRequest) -> FeatureResponse` in `core.py`
3. Add a Typer command in `cli.py` that builds the request, awaits core, prints with Rich
4. Add a Gradio tab/component in `web.py` that maps UI inputs to the request, awaits core, renders

## Directories excluded from git

`.env`, `.venv/`, `gitea-data/`, `bin/`, `run/`, `downloads/`, `*.log` — managed by `run.sh` which ensures `.gitignore` entries exist.

## Tests

Tests live in `tests/` with `conftest.py` providing shared fixtures (tmp dirs, mock Gitea, mock HF). Mock external services — don't hit real HF or Gitea APIs in tests.
