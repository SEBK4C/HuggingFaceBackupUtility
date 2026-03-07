"""Hugging Face Hub API client using the huggingface_hub library."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import AsyncIterator

from huggingface_hub import HfApi, snapshot_download
from huggingface_hub.utils import RepositoryNotFoundError

from .errors import AuthenticationError, RateLimitError
from .models import (
    AppConfig,
    CloneProgress,
    HFFileInfo,
    HFRepoManifest,
    LFSPointer,
)

logger = logging.getLogger(__name__)


class HFClient:
    def __init__(self, config: AppConfig):
        self.config = config
        self.token = config.hf_token.get_secret_value() or None
        self.api = HfApi(token=self.token)

    async def fetch_manifest(
        self, repo_id: str, revision: str = "main", repo_type: str = "model"
    ) -> HFRepoManifest:
        """Fetch complete file listing for a repo from HF Hub."""
        loop = asyncio.get_event_loop()
        try:
            repo_info = await loop.run_in_executor(
                None, lambda: self.api.repo_info(repo_id, revision=revision, repo_type=repo_type)
            )
            siblings = await loop.run_in_executor(
                None,
                lambda: list(
                    self.api.list_repo_tree(
                        repo_id, revision=revision, repo_type=repo_type, recursive=True
                    )
                ),
            )
        except RepositoryNotFoundError:
            raise AuthenticationError(
                f"Repository {repo_id} not found or token lacks access."
            )
        except Exception as e:
            if "rate limit" in str(e).lower():
                raise RateLimitError(str(e))
            raise

        files: list[HFFileInfo] = []
        total_size = 0
        lfs_size = 0

        for entry in siblings:
            # list_repo_tree with recursive=True returns only RepoFile objects
            lfs_pointer = None
            size = entry.size or 0
            if entry.lfs is not None:
                lfs_pointer = LFSPointer(
                    sha256=entry.lfs.sha256, size=entry.lfs.size
                )
                lfs_size += entry.lfs.size
            blob_id = entry.blob_id or ""
            files.append(
                HFFileInfo(
                    rfilename=entry.path,
                    size=size,
                    blob_id=blob_id,
                    lfs=lfs_pointer,
                )
            )
            total_size += size

        from datetime import datetime

        return HFRepoManifest(
            repo_id=repo_id,
            repo_type=repo_type,
            revision=revision,
            files=files,
            total_size=total_size,
            lfs_size=lfs_size,
            fetched_at=datetime.now(),
        )

    async def get_upstream_commit(
        self, repo_id: str, revision: str = "main", repo_type: str = "model"
    ) -> str:
        """Get the latest commit SHA for a repo revision."""
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(
            None,
            lambda: self.api.repo_info(
                repo_id, revision=revision, repo_type=repo_type
            ),
        )
        return info.sha

    async def download_repo_snapshot(
        self,
        repo_id: str,
        download_dir: Path,
        revision: str = "main",
        repo_type: str = "model",
    ) -> Path:
        """Download entire repo using snapshot_download.

        Uses huggingface_hub's built-in parallelism, resume, and progress bars.
        Returns the local directory path.
        """
        loop = asyncio.get_event_loop()
        max_workers = self.config.hf_concurrent_downloads

        def _download() -> str:
            return snapshot_download(
                repo_id=repo_id,
                local_dir=str(download_dir),
                revision=revision,
                repo_type=repo_type,
                token=self.token,
                max_workers=max_workers,
            )

        result_path = await loop.run_in_executor(None, _download)
        return Path(result_path)

    async def download_repo_streaming(
        self,
        repo_id: str,
        manifest: HFRepoManifest,
        download_dir: Path,
        semaphore: asyncio.Semaphore,
        revision: str = "main",
        repo_type: str = "model",
    ) -> AsyncIterator[CloneProgress]:
        """Download all repo files using snapshot_download, yielding progress updates."""
        total_files = len(manifest.files)
        total_bytes = manifest.total_size
        start_time = time.time()

        yield CloneProgress(
            phase="download",
            bytes_downloaded=0,
            bytes_total=total_bytes,
            files_completed=0,
            files_total=total_files,
            message=f"Downloading {repo_id} ({total_files} files, {total_bytes / 1024**3:.1f} GB)...",
        )

        try:
            await self.download_repo_snapshot(
                repo_id, download_dir, revision=revision, repo_type=repo_type
            )
        except Exception as e:
            yield CloneProgress(
                phase="error",
                message=f"Download failed: {e}",
            )
            return

        elapsed = time.time() - start_time
        speed = total_bytes / elapsed if elapsed > 0 else 0

        yield CloneProgress(
            phase="download",
            bytes_downloaded=total_bytes,
            bytes_total=total_bytes,
            files_completed=total_files,
            files_total=total_files,
            speed_bytes_sec=speed,
            message=f"Downloaded {total_files} files in {elapsed:.0f}s ({speed / 1024**2:.1f} MB/s)",
        )
