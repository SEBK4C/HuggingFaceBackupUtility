"""Typer CLI invocation tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.cli import app
from src.models import (
    DoctorResult,
    HealthCheck,
    MirroredRepo,
    MirrorState,
    MigrateResult,
    PruneResult,
    RepoStatusResponse,
)

runner = CliRunner()


# --- help commands ---

def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "hfmirror" in result.output.lower() or "clone" in result.output.lower()


def test_clone_help():
    result = runner.invoke(app, ["clone", "--help"])
    assert result.exit_code == 0
    assert "repo-id" in result.output.lower() or "repo_id" in result.output.lower()


def test_list_help():
    result = runner.invoke(app, ["list", "--help"])
    assert result.exit_code == 0


def test_doctor_help():
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0


def test_prune_help():
    result = runner.invoke(app, ["prune", "--help"])
    assert result.exit_code == 0
    assert "dry-run" in result.output.lower() or "dry_run" in result.output.lower()


def test_status_help():
    result = runner.invoke(app, ["status", "--help"])
    assert result.exit_code == 0


def test_diff_help():
    result = runner.invoke(app, ["diff", "--help"])
    assert result.exit_code == 0


def test_update_help():
    result = runner.invoke(app, ["update", "--help"])
    assert result.exit_code == 0


def test_migrate_help():
    result = runner.invoke(app, ["migrate", "--help"])
    assert result.exit_code == 0
    assert "--to" in result.output


def test_open_help():
    result = runner.invoke(app, ["open", "--help"])
    assert result.exit_code == 0


# --- helpers ---

def _make_mock_core():
    """Create a minimal mock core with all methods as AsyncMock."""
    core = MagicMock()
    core.close = AsyncMock()
    return core


# --- list command ---

@patch("src.cli.init_core", new_callable=AsyncMock)
@patch("src.cli.load_config")
def test_list_runs_empty(mock_load_config, mock_init_core):
    core = _make_mock_core()
    core.list_repos = AsyncMock(return_value=RepoStatusResponse(repos=[]))
    mock_init_core.return_value = core

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    core.list_repos.assert_called_once()


@patch("src.cli.init_core", new_callable=AsyncMock)
@patch("src.cli.load_config")
def test_list_runs_with_repos(mock_load_config, mock_init_core, tmp_path):
    from pathlib import Path
    core = _make_mock_core()
    repo = MirroredRepo(
        repo_id="org/example",
        gitea_repo_name="org--example",
        state=MirrorState.SYNCED,
        tier1_path=tmp_path / "org--example",
        total_size_bytes=1024,
    )
    core.list_repos = AsyncMock(return_value=RepoStatusResponse(repos=[repo]))
    mock_init_core.return_value = core

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0


# --- doctor command ---

@patch("src.cli.init_core", new_callable=AsyncMock)
@patch("src.cli.load_config")
def test_doctor_passes(mock_load_config, mock_init_core):
    core = _make_mock_core()
    core.doctor = AsyncMock(return_value=DoctorResult(
        checks=[HealthCheck(name="Gitea", passed=True, message="Reachable")],
        all_passed=True,
    ))
    mock_init_core.return_value = core

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    core.doctor.assert_called_once()


@patch("src.cli.init_core", new_callable=AsyncMock)
@patch("src.cli.load_config")
def test_doctor_fails(mock_load_config, mock_init_core):
    core = _make_mock_core()
    core.doctor = AsyncMock(return_value=DoctorResult(
        checks=[HealthCheck(name="Gitea", passed=False, message="Not reachable")],
        all_passed=False,
    ))
    mock_init_core.return_value = core

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1


# --- prune command ---

@patch("src.cli.init_core", new_callable=AsyncMock)
@patch("src.cli.load_config")
def test_prune_dry_run_cli(mock_load_config, mock_init_core):
    core = _make_mock_core()
    core.prune = AsyncMock(return_value=PruneResult(
        repo_id="org/model",
        bytes_reclaimed=2048,
        files_deleted=3,
        gitea_repo_deleted=False,
        tier1_scrubbed=False,
        tier2_scrubbed=False,
        was_dry_run=True,
    ))
    mock_init_core.return_value = core

    result = runner.invoke(app, ["prune", "org/model", "--dry-run"])
    assert result.exit_code == 0
    assert "dry run" in result.output.lower()
    assert "3" in result.output


@patch("src.cli.init_core", new_callable=AsyncMock)
@patch("src.cli.load_config")
def test_prune_live_cli(mock_load_config, mock_init_core):
    core = _make_mock_core()
    core.prune = AsyncMock(return_value=PruneResult(
        repo_id="org/model",
        bytes_reclaimed=1024,
        files_deleted=1,
        gitea_repo_deleted=True,
        tier1_scrubbed=True,
        tier2_scrubbed=False,
        was_dry_run=False,
    ))
    mock_init_core.return_value = core

    result = runner.invoke(app, ["prune", "org/model"])
    assert result.exit_code == 0
    assert "org/model" in result.output


# --- open command ---

@patch("src.cli.init_core", new_callable=AsyncMock)
@patch("src.cli.load_config")
def test_open_prints_url(mock_load_config, mock_init_core):
    core = _make_mock_core()
    core.get_gitea_url = MagicMock(return_value="http://localhost:3000/testadmin/org--model")
    mock_init_core.return_value = core

    result = runner.invoke(app, ["open", "org/model"])
    assert result.exit_code == 0
    assert "localhost:3000" in result.output


# --- migrate command ---

@patch("src.cli.init_core", new_callable=AsyncMock)
@patch("src.cli.load_config")
def test_migrate_cli(mock_load_config, mock_init_core):
    core = _make_mock_core()
    core.migrate = AsyncMock(return_value=MigrateResult(
        repo_id="org/model",
        files_moved=5,
        bytes_moved=10240,
        symlinks_created=5,
        duration_seconds=0.1,
    ))
    mock_init_core.return_value = core

    result = runner.invoke(app, ["migrate", "org/model", "--to", "tier2"])
    assert result.exit_code == 0
    assert "5" in result.output
