"""Error taxonomy for the HFMirror application."""

from __future__ import annotations


class HFMirrorError(Exception):
    """Base exception for all application errors."""


class AuthenticationError(HFMirrorError):
    """HF token invalid or expired."""


class RateLimitError(HFMirrorError):
    """HF API rate limit hit."""

    def __init__(self, message: str, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


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
