"""Typer CLI invocation tests."""

from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from src.cli import app

runner = CliRunner()


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
