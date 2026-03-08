"""Configuration loading and core initialization."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

from .core import HFMirrorCore
from .models import AppConfig
from .state import StateDB

logger = logging.getLogger(__name__)


def load_config() -> AppConfig:
    """Load configuration from .env file."""
    load_dotenv()

    env = os.environ

    hf_token = env.get("HF_TOKEN", "")
    if not hf_token:
        logger.warning(
            "HF_TOKEN is not set. Hugging Face operations will fail. "
            "Run setup or add HF_TOKEN to .env"
        )

    tier2 = env.get("TIER2_PATH")
    return AppConfig(
        hf_token=hf_token,
        tier1_path=Path(env.get("TIER1_PATH", "./downloads")).expanduser().resolve(),
        tier2_path=Path(tier2).expanduser().resolve() if tier2 else None,
        tier_threshold_percent=int(env.get("TIER_THRESHOLD_PERCENT", "10")),
        gitea_port=int(env.get("GITEA_PORT", "3000")),
        gitea_admin_user=env.get("GITEA_ADMIN_USER", "hfmirror"),
        gitea_admin_password=env.get("GITEA_ADMIN_PASSWORD", ""),
        gitea_base_url=env.get("GITEA_BASE_URL", "http://localhost:3000"),
        gitea_api_token=env.get("GITEA_API_TOKEN") or None,
        gradio_port=int(env.get("GRADIO_PORT", "7860")),
        gradio_share=env.get("GRADIO_SHARE", "false").lower() == "true",
        hf_concurrent_downloads=int(env.get("HF_CONCURRENT_DOWNLOADS", "4")),
        hf_chunk_size_mb=int(env.get("HF_CHUNK_SIZE_MB", "64")),
        hf_retry_attempts=int(env.get("HF_RETRY_ATTEMPTS", "5")),
        hf_retry_backoff_base=float(env.get("HF_RETRY_BACKOFF_BASE", "2.0")),
        log_level=env.get("LOG_LEVEL", "INFO"),
        log_file=env.get("LOG_FILE", "./hfmirror.log"),
    )


def setup_logging(config: AppConfig) -> None:
    """Configure structured logging with file rotation and console output."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.log_level))

    # File handler with rotation
    file_handler = RotatingFileHandler(
        config.log_file,
        maxBytes=50 * 1024 * 1024,  # 50 MB
        backupCount=3,
    )
    file_handler.setFormatter(
        logging.Formatter(
            '{"ts": "%(asctime)s", "level": "%(levelname)s", '
            '"module": "%(name)s", "msg": "%(message)s"}'
        )
    )
    root.addHandler(file_handler)

    # Console handler (Rich-friendly)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s", datefmt="%H:%M:%S")
    )
    root.addHandler(console_handler)


async def init_core(config: AppConfig) -> HFMirrorCore:
    """Initialize the core with state DB connection and crash recovery."""
    setup_logging(config)
    db_path = Path("./gitea-data/hfmirror.db")
    state_db = StateDB(db_path)
    await state_db.connect()

    # Run crash recovery for incomplete journal entries
    actions = await state_db.recover_incomplete_operations(
        tier1_path=config.tier1_path,
        tier2_path=config.tier2_path,
    )
    if actions:
        logger.info("Crash recovery: %d action(s) taken", len(actions))
        for action in actions:
            logger.info("  Recovery: %s", action)

    return HFMirrorCore(config, state_db)
