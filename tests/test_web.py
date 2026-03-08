"""Tests for src/web.py handler functions (no Gradio server required)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models import (
    DiffResult,
    DoctorResult,
    FileDiff,
    HealthCheck,
    MigrateResult,
    MirroredRepo,
    MirrorState,
    RepoStatusResponse,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_repo(repo_id: str, tier1_path: Path | None = None, **kwargs) -> MirroredRepo:
    return MirroredRepo(
        repo_id=repo_id,
        gitea_repo_name=repo_id.replace("/", "--"),
        state=MirrorState.SYNCED,
        tier1_path=tier1_path,
        total_size_bytes=kwargs.get("total_size_bytes", 2 * 1024**3),
        **{k: v for k, v in kwargs.items() if k != "total_size_bytes"},
    )


@pytest.fixture
def mock_core():
    return AsyncMock()


@pytest.fixture(autouse=True)
def patch_get_core(mock_core):
    """Patch web.get_core so all handler calls use our mock."""
    with patch("src.web.get_core", return_value=mock_core):
        yield mock_core


# ---------------------------------------------------------------------------
# _drive_label
# ---------------------------------------------------------------------------

def test_drive_label_tier1():
    from src.web import _drive_label
    assert _drive_label("tier1") == "Drive 1"


def test_drive_label_tier2():
    from src.web import _drive_label
    assert _drive_label("tier2") == "Drive 2"


def test_drive_label_unknown():
    from src.web import _drive_label
    assert _drive_label("nvme") == "nvme"


# ---------------------------------------------------------------------------
# _fmt_size
# ---------------------------------------------------------------------------

def test_fmt_size_bytes():
    from src.web import _fmt_size
    assert _fmt_size(500) == "500.0 B"


def test_fmt_size_kilobytes():
    from src.web import _fmt_size
    assert _fmt_size(2048) == "2.0 KB"


def test_fmt_size_megabytes():
    from src.web import _fmt_size
    assert _fmt_size(5 * 1024**2) == "5.0 MB"


def test_fmt_size_gigabytes():
    from src.web import _fmt_size
    assert _fmt_size(3 * 1024**3) == "3.0 GB"


# ---------------------------------------------------------------------------
# _describe_location
# ---------------------------------------------------------------------------

def test_describe_location_drive1_only(tmp_path):
    from src.web import _describe_location
    repo_dir = tmp_path / "myrepo"
    repo_dir.mkdir()
    (repo_dir / "model.bin").write_bytes(b"x" * 100)

    repo = _make_repo("org/model", tier1_path=repo_dir)
    assert _describe_location(repo) == "Drive 1"


def test_describe_location_drive2_symlinked(tmp_path):
    from src.web import _describe_location
    repo_dir = tmp_path / "myrepo"
    repo_dir.mkdir()
    real_file = tmp_path / "real.bin"
    real_file.write_bytes(b"y" * 100)
    (repo_dir / "model.bin").symlink_to(real_file)

    repo = _make_repo("org/model", tier1_path=repo_dir)
    assert _describe_location(repo) == "Drive 2 (symlinked)"


def test_describe_location_mixed(tmp_path):
    from src.web import _describe_location
    repo_dir = tmp_path / "myrepo"
    repo_dir.mkdir()
    (repo_dir / "config.json").write_bytes(b"z" * 10)
    real_file = tmp_path / "real.bin"
    real_file.write_bytes(b"y" * 100)
    (repo_dir / "model.bin").symlink_to(real_file)

    repo = _make_repo("org/model", tier1_path=repo_dir)
    assert _describe_location(repo) == "Drive 1 + Drive 2"


def test_describe_location_no_tier1_path():
    from src.web import _describe_location
    repo = _make_repo("org/model", tier1_path=None)
    assert _describe_location(repo) == "Drive 1"


def test_describe_location_skips_git_dir(tmp_path):
    from src.web import _describe_location
    repo_dir = tmp_path / "myrepo"
    (repo_dir / ".git").mkdir(parents=True)
    git_file = repo_dir / ".git" / "HEAD"
    git_file.write_bytes(b"ref: refs/heads/main\n")

    repo = _make_repo("org/model", tier1_path=repo_dir)
    # .git contents should be skipped, so no files counted → "Drive 1"
    assert _describe_location(repo) == "Drive 1"


# ---------------------------------------------------------------------------
# refresh_dashboard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_dashboard_empty(mock_core):
    from src.web import refresh_dashboard
    mock_core.list_repos = AsyncMock(return_value=RepoStatusResponse(repos=[]))
    result = await refresh_dashboard()
    assert result == []


@pytest.mark.asyncio
async def test_refresh_dashboard_with_repos(mock_core, tmp_path):
    from src.web import refresh_dashboard
    repo_dir = tmp_path / "myrepo"
    repo_dir.mkdir()
    (repo_dir / "weights.bin").write_bytes(b"a" * 10)

    repo = _make_repo("org/model", tier1_path=repo_dir, total_size_bytes=1024**3)
    mock_core.list_repos = AsyncMock(return_value=RepoStatusResponse(repos=[repo]))

    rows = await refresh_dashboard()
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "org/model"
    assert row[1] == "synced"
    assert "GB" in row[2]


# ---------------------------------------------------------------------------
# clone_repo
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clone_repo_empty_input(mock_core):
    from src.web import clone_repo
    outputs = []
    async for out in clone_repo("", "main", "auto"):
        outputs.append(out)
    assert any("Please enter" in o for o in outputs)
    mock_core.clone.assert_not_called()


@pytest.mark.asyncio
async def test_clone_repo_progress(mock_core):
    from src.models import CloneProgress
    from src.web import clone_repo

    progress_events = [
        CloneProgress(phase="manifest", message="Fetching manifest", bytes_total=0),
        CloneProgress(phase="download", message="weights.bin", bytes_downloaded=500, bytes_total=1000),
    ]

    async def _fake_clone(req):
        for p in progress_events:
            yield p

    mock_core.clone = _fake_clone

    outputs = []
    async for out in clone_repo("org/model", "main", "auto"):
        outputs.append(out)

    assert any("manifest" in o for o in outputs)
    assert any("download" in o for o in outputs)
    final = outputs[-1]
    assert "Clone complete" in final


@pytest.mark.asyncio
async def test_clone_repo_error_phase(mock_core):
    from src.models import CloneProgress
    from src.web import clone_repo

    async def _fake_clone(req):
        yield CloneProgress(phase="error", message="Connection refused")

    mock_core.clone = _fake_clone

    outputs = []
    async for out in clone_repo("org/model", "main", "auto"):
        outputs.append(out)

    assert any("ERROR" in o for o in outputs)


@pytest.mark.asyncio
async def test_clone_repo_force_drive_passed(mock_core):
    """force_drive != 'auto' is forwarded as force_tier to CloneRequest."""
    from src.models import CloneProgress
    from src.web import clone_repo

    captured = []

    async def _fake_clone(req):
        captured.append(req)
        yield CloneProgress(phase="complete", message="done")

    mock_core.clone = _fake_clone

    async for _ in clone_repo("org/model", "main", "tier2"):
        pass

    assert len(captured) == 1
    assert captured[0].force_tier == "tier2"


# ---------------------------------------------------------------------------
# check_diff
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_diff_empty_input(mock_core):
    from src.web import check_diff
    result = await check_diff("   ")
    assert "Please enter" in result
    mock_core.diff.assert_not_called()


@pytest.mark.asyncio
async def test_check_diff_up_to_date(mock_core):
    from src.web import check_diff
    mock_core.diff = AsyncMock(return_value=DiffResult(
        repo_id="org/model",
        local_commit="abc123",
        upstream_commit="abc123",
        is_up_to_date=True,
        changes=[],
        upstream_total_size=0,
    ))
    result = await check_diff("org/model")
    assert "up to date" in result


@pytest.mark.asyncio
async def test_check_diff_has_changes(mock_core):
    from src.web import check_diff
    mock_core.diff = AsyncMock(return_value=DiffResult(
        repo_id="org/model",
        local_commit="aaa",
        upstream_commit="bbb",
        is_up_to_date=False,
        changes=[
            FileDiff(filename="weights.bin", change_type="modified", is_lfs=True),
        ],
        upstream_total_size=1000,
    ))
    result = await check_diff("org/model")
    assert "upstream changes" in result
    assert "weights.bin" in result
    assert "[LFS]" in result


@pytest.mark.asyncio
async def test_check_diff_unchanged_files_hidden(mock_core):
    from src.web import check_diff
    mock_core.diff = AsyncMock(return_value=DiffResult(
        repo_id="org/model",
        local_commit="aaa",
        upstream_commit="bbb",
        is_up_to_date=False,
        changes=[
            FileDiff(filename="config.json", change_type="unchanged", is_lfs=False),
        ],
        upstream_total_size=100,
    ))
    result = await check_diff("org/model")
    # unchanged files should not appear
    assert "config.json" not in result


# ---------------------------------------------------------------------------
# run_doctor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_doctor_all_pass(mock_core):
    from src.web import run_doctor
    mock_core.doctor = AsyncMock(return_value=DoctorResult(
        checks=[HealthCheck(name="Storage", passed=True, message="OK")],
        all_passed=True,
    ))
    result = await run_doctor()
    assert "PASS" in result
    assert "All checks passed" in result


@pytest.mark.asyncio
async def test_run_doctor_some_fail(mock_core):
    from src.web import run_doctor
    mock_core.doctor = AsyncMock(return_value=DoctorResult(
        checks=[
            HealthCheck(name="Gitea", passed=False, message="unreachable", details=["port 3000 refused"]),
        ],
        all_passed=False,
    ))
    result = await run_doctor()
    assert "FAIL" in result
    assert "Some checks failed" in result
    assert "port 3000 refused" in result


# ---------------------------------------------------------------------------
# do_migrate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_do_migrate_no_repo(mock_core):
    from src.web import do_migrate
    result = await do_migrate("", "Move to Drive 2 (symlink on Drive 1)")
    assert "select" in result.lower() or "Please" in result


@pytest.mark.asyncio
async def test_do_migrate_to_drive2(mock_core):
    from src.web import do_migrate
    mock_core.migrate = AsyncMock(return_value=MigrateResult(
        repo_id="org/model", files_moved=5, bytes_moved=1024**3,
        symlinks_created=5, duration_seconds=2.5,
    ))
    result = await do_migrate("org/model", "Move to Drive 2 (symlink on Drive 1)")
    assert "5" in result
    assert "Drive 2" in result or "Moved" in result


@pytest.mark.asyncio
async def test_do_migrate_to_drive1(mock_core):
    from src.web import do_migrate
    mock_core.migrate = AsyncMock(return_value=MigrateResult(
        repo_id="org/model", files_moved=3, bytes_moved=500 * 1024**2,
        symlinks_created=0, duration_seconds=1.0,
    ))
    result = await do_migrate("org/model", "Move to Drive 1 (remove symlinks)")
    assert "Recalled" in result or "Drive 1" in result


@pytest.mark.asyncio
async def test_do_migrate_copy_to_drive2(mock_core):
    from src.web import do_migrate
    mock_core.copy_to_drive2 = AsyncMock(return_value=MigrateResult(
        repo_id="org/model", files_moved=2, bytes_moved=200 * 1024**2,
        symlinks_created=0, duration_seconds=0.5,
    ))
    result = await do_migrate("org/model", "Copy to Drive 2 (keep on both)")
    assert "Copied" in result or "both drives" in result.lower()


@pytest.mark.asyncio
async def test_do_migrate_unknown_action(mock_core):
    from src.web import do_migrate
    result = await do_migrate("org/model", "do something weird")
    assert "Unknown" in result


# ---------------------------------------------------------------------------
# save_settings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_settings_creates_env(tmp_path, monkeypatch):
    """save_settings writes merged key=value lines to .env."""
    from src.web import save_settings

    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING_KEY=existing_val\n")

    # save_settings does `from pathlib import Path; Path(".env")` internally.
    # Redirect cwd so ".env" resolves to our temp file.
    monkeypatch.chdir(tmp_path)

    result = await save_settings(
        "hf_token_val",
        str(tmp_path / "tier1"),
        str(tmp_path / "tier2"),
        3000,
        4,
        "INFO",
    )

    assert "saved" in result.lower()
    content = env_file.read_text()
    assert "HF_TOKEN=hf_token_val" in content
    assert "EXISTING_KEY=existing_val" in content


# ---------------------------------------------------------------------------
# get_repo_choices
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_repo_choices(mock_core):
    from src.web import get_repo_choices
    repos = [_make_repo("org/a"), _make_repo("org/b")]
    mock_core.list_repos = AsyncMock(return_value=RepoStatusResponse(repos=repos))

    result = await get_repo_choices()
    # Returns a gr.update dict-like object; check choices field
    assert hasattr(result, "__class__")
    # Gradio gr.update returns an object; just verify it ran without error
    # and the underlying call was made
    mock_core.list_repos.assert_called_once()


# ---------------------------------------------------------------------------
# create_app
# ---------------------------------------------------------------------------

def test_create_app_returns_blocks():
    """create_app should construct a Gradio Blocks without errors."""
    import gradio as gr
    from src.web import create_app
    with patch("src.web.load_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(
            hf_token=MagicMock(get_secret_value=lambda: ""),
            tier1_path=Path("/tmp/tier1"),
            tier2_path=None,
            gitea_port=3000,
            hf_concurrent_downloads=4,
            log_level="INFO",
            gradio_port=7860,
            gradio_share=False,
        )
        # Reload config at module level is already patched at import time;
        # just verify create_app produces a Blocks instance
        app = create_app()
    assert isinstance(app, gr.Blocks)
