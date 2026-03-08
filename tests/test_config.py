"""Tests for src/config.py: load_config, setup_logging, init_core."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import load_config, setup_logging
from src.models import AppConfig


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def test_load_config_defaults(monkeypatch, tmp_path):
    """load_config uses sensible defaults when env vars are absent."""
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("TIER1_PATH", raising=False)
    monkeypatch.delenv("TIER2_PATH", raising=False)
    monkeypatch.delenv("GITEA_PORT", raising=False)
    monkeypatch.delenv("HF_CONCURRENT_DOWNLOADS", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    # load_config reads dotenv too; patch load_dotenv to be a no-op
    with patch("src.config.load_dotenv"):
        cfg = load_config()

    assert cfg.hf_token.get_secret_value() == ""
    assert cfg.tier1_path.name == "downloads"
    assert cfg.tier2_path is None
    assert cfg.gitea_port == 3000
    assert cfg.hf_concurrent_downloads == 4
    assert cfg.log_level == "INFO"


def test_load_config_from_env(monkeypatch, tmp_path):
    """load_config picks up values from environment variables."""
    tier1 = str(tmp_path / "ssd")
    tier2 = str(tmp_path / "raid")
    monkeypatch.setenv("HF_TOKEN", "hf_mytoken")
    monkeypatch.setenv("TIER1_PATH", tier1)
    monkeypatch.setenv("TIER2_PATH", tier2)
    monkeypatch.setenv("GITEA_PORT", "4000")
    monkeypatch.setenv("HF_CONCURRENT_DOWNLOADS", "8")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("GRADIO_PORT", "9000")

    with patch("src.config.load_dotenv"):
        cfg = load_config()

    assert cfg.hf_token.get_secret_value() == "hf_mytoken"
    assert cfg.tier1_path == Path(tier1).expanduser().resolve()
    assert cfg.tier2_path == Path(tier2).expanduser().resolve()
    assert cfg.gitea_port == 4000
    assert cfg.hf_concurrent_downloads == 8
    assert cfg.log_level == "DEBUG"
    assert cfg.gradio_port == 9000


def test_load_config_no_tier2(monkeypatch, tmp_path):
    """When TIER2_PATH is unset, tier2_path is None."""
    monkeypatch.delenv("TIER2_PATH", raising=False)

    with patch("src.config.load_dotenv"):
        cfg = load_config()

    assert cfg.tier2_path is None


def test_load_config_gradio_share_true(monkeypatch):
    """GRADIO_SHARE=true maps to gradio_share=True."""
    monkeypatch.setenv("GRADIO_SHARE", "true")
    with patch("src.config.load_dotenv"):
        cfg = load_config()
    assert cfg.gradio_share is True


def test_load_config_gradio_share_false(monkeypatch):
    """GRADIO_SHARE=false maps to gradio_share=False."""
    monkeypatch.setenv("GRADIO_SHARE", "false")
    with patch("src.config.load_dotenv"):
        cfg = load_config()
    assert cfg.gradio_share is False


def test_load_config_retry_params(monkeypatch):
    """Retry and chunk env vars are parsed correctly."""
    monkeypatch.setenv("HF_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("HF_RETRY_BACKOFF_BASE", "1.5")
    monkeypatch.setenv("HF_CHUNK_SIZE_MB", "128")
    with patch("src.config.load_dotenv"):
        cfg = load_config()
    assert cfg.hf_retry_attempts == 3
    assert cfg.hf_retry_backoff_base == 1.5
    assert cfg.hf_chunk_size_mb == 128


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------

def test_setup_logging_sets_level(tmp_path, app_config):
    """setup_logging configures root logger at the configured level."""
    log_file = tmp_path / "test.log"
    cfg = app_config.model_copy(update={"log_level": "WARNING", "log_file": str(log_file)})

    # Grab root logger and clear it so test is isolated
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    try:
        root.handlers.clear()
        setup_logging(cfg)
        assert root.level == logging.WARNING
    finally:
        root.handlers = original_handlers
        root.setLevel(logging.WARNING)


def test_setup_logging_creates_log_file(tmp_path, app_config):
    """setup_logging creates the log file via RotatingFileHandler."""
    log_file = tmp_path / "hfmirror.log"
    cfg = app_config.model_copy(update={"log_file": str(log_file)})

    root = logging.getLogger()
    original_handlers = root.handlers[:]
    try:
        root.handlers.clear()
        setup_logging(cfg)
        root.info("test message")
        assert log_file.exists()
    finally:
        # Close handlers we added so the file isn't held open
        for h in root.handlers[:]:
            h.close()
            root.removeHandler(h)
        root.handlers = original_handlers


# ---------------------------------------------------------------------------
# init_core
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_init_core_returns_core(tmp_dirs):
    """init_core returns a HFMirrorCore with a connected state DB."""
    from src.config import init_core
    from src.core import HFMirrorCore
    from src.models import AppConfig

    db_path = tmp_dirs["gitea_data"] / "hfmirror.db"
    cfg = AppConfig(
        hf_token="hf_test",
        tier1_path=tmp_dirs["tier1"],
        tier2_path=tmp_dirs["tier2"],
        log_file=str(tmp_dirs["root"] / "test.log"),
    )

    with patch("src.config.StateDB") as MockStateDB:
        mock_db = AsyncMock()
        mock_db.recover_incomplete_operations = AsyncMock(return_value=[])
        MockStateDB.return_value = mock_db
        with patch("src.config.setup_logging"):
            core = await init_core(cfg)

    assert isinstance(core, HFMirrorCore)
    mock_db.connect.assert_called_once()


@pytest.mark.asyncio
async def test_init_core_runs_crash_recovery(tmp_dirs):
    """init_core logs crash recovery actions when they exist."""
    from src.config import init_core
    from src.models import AppConfig

    cfg = AppConfig(
        hf_token="hf_test",
        tier1_path=tmp_dirs["tier1"],
        log_file=str(tmp_dirs["root"] / "test.log"),
    )

    recovery_actions = ["restored file foo.bin", "removed partial bar.bin"]
    with patch("src.config.StateDB") as MockStateDB:
        mock_db = AsyncMock()
        mock_db.recover_incomplete_operations = AsyncMock(return_value=recovery_actions)
        MockStateDB.return_value = mock_db
        with patch("src.config.setup_logging"):
            with patch("src.config.logger") as mock_logger:
                await init_core(cfg)

    # Should log the count and each action
    assert mock_logger.info.call_count >= 3
