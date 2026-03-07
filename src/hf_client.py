"""Hugging Face Hub API client using the huggingface_hub library."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import AsyncIterator

from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.utils import EntryNotFoundError, RepositoryNotFoundError

from .errors import AuthenticationError, IntegrityError, RateLimitError
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
        self.token = config.hf_token.get_secret_value()
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

    async def download_file(
        self,
        repo_id: str,
        filename: str,
        local_dir: Path,
        revision: str = "main",
        repo_type: str = "model",
    ) -> Path:
        """Download a single file from HF Hub with resume support.

        Returns the path to the downloaded file.
        """
        loop = asyncio.get_event_loop()

        def _download() -> str:
            return hf_hub_download(
                repo_id=repo_id,
                filename=filename,
                local_dir=str(local_dir),
                revision=revision,
                repo_type=repo_type,
                token=self.token,
                resume_download=True,
                local_dir_use_symlinks=False,
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
        """Download all files in a manifest, yielding progress updates."""
        total_files = len(manifest.files)
        completed = 0
        total_bytes = manifest.total_size
        downloaded_bytes = 0
        start_time = time.time()
        warnings: list[str] = []

        for file_info in manifest.files:
            async with semaphore:
                yield CloneProgress(
                    phase="download",
                    file_name=file_info.rfilename,
                    bytes_downloaded=downloaded_bytes,
                    bytes_total=total_bytes,
                    files_completed=completed,
                    files_total=total_files,
                    message=f"Downloading {file_info.rfilename}",
                )

                retries = self.config.hf_retry_attempts
                for attempt in range(retries):
                    try:
                        await self.download_file(
                            repo_id,
                            file_info.rfilename,
                            download_dir,
                            revision=revision,
                            repo_type=repo_type,
                        )
                        break
                    except Exception as e:
                        if attempt < retries - 1:
                            wait = self.config.hf_retry_backoff_base ** attempt
                            logger.warning(
                                "Retry %d/%d for %s: %s (waiting %.1fs)",
                                attempt + 1,
                                retries,
                                file_info.rfilename,
                                e,
                                wait,
                            )
                            await asyncio.sleep(wait)
                        else:
                            msg = f"Failed to download {file_info.rfilename} after {retries} attempts: {e}"
                            logger.error(msg)
                            warnings.append(msg)

                downloaded_bytes += file_info.size
                completed += 1
                elapsed = time.time() - start_time
                speed = downloaded_bytes / elapsed if elapsed > 0 else 0
                remaining = total_bytes - downloaded_bytes
                eta = remaining / speed if speed > 0 else None

                yield CloneProgress(
                    phase="download",
                    file_name=file_info.rfilename,
                    bytes_downloaded=downloaded_bytes,
                    bytes_total=total_bytes,
                    files_completed=completed,
                    files_total=total_files,
                    speed_bytes_sec=speed,
                    eta_seconds=eta,
                    message=f"Completed {file_info.rfilename}",
                )
