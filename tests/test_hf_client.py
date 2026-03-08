"""Unit tests for HF client with mocked Hub responses."""

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.hf_client import HFClient
from src.models import AppConfig, HFFileInfo, HFRepoManifest, LFSPointer


@pytest.fixture
def hf_client(app_config):
    return HFClient(app_config)


@pytest.mark.asyncio
async def test_fetch_manifest(hf_client):
    mock_entry = MagicMock()
    mock_entry.path = "config.json"
    mock_entry.size = 500
    mock_entry.blob_id = "abc"
    mock_entry.lfs = None

    mock_lfs_entry = MagicMock()
    mock_lfs_entry.path = "model.safetensors"
    mock_lfs_entry.size = 5_000_000_000
    mock_lfs_entry.blob_id = "def"
    mock_lfs_entry.lfs = MagicMock(sha256="sha256hash", size=5_000_000_000)

    with patch.object(hf_client.api, "repo_info", return_value=MagicMock(sha="abc123")), \
         patch.object(hf_client.api, "list_repo_tree", return_value=[mock_entry, mock_lfs_entry]):
        manifest = await hf_client.fetch_manifest("org/model")

    assert manifest.repo_id == "org/model"
    assert len(manifest.files) == 2
    assert manifest.total_size == 5_000_000_500
    assert manifest.lfs_size == 5_000_000_000
    assert manifest.files[1].lfs is not None


@pytest.mark.asyncio
async def test_get_upstream_commit(hf_client):
    with patch.object(hf_client.api, "repo_info", return_value=MagicMock(sha="commit123")):
        sha = await hf_client.get_upstream_commit("org/model")
    assert sha == "commit123"


@pytest.mark.asyncio
async def test_fetch_manifest_not_found(hf_client):
    from huggingface_hub.utils import RepositoryNotFoundError

    from src.errors import AuthenticationError

    mock_response = MagicMock()
    mock_response.status_code = 404
    with patch.object(hf_client.api, "repo_info", side_effect=RepositoryNotFoundError("not found", response=mock_response)):
        with pytest.raises(AuthenticationError):
            await hf_client.fetch_manifest("org/missing")


@pytest.mark.asyncio
async def test_fetch_manifest_rate_limit(hf_client):
    from src.errors import RateLimitError

    with patch.object(hf_client.api, "repo_info", side_effect=Exception("rate limit exceeded")):
        with pytest.raises(RateLimitError):
            await hf_client.fetch_manifest("org/model")


@pytest.mark.asyncio
async def test_download_repo_snapshot(hf_client, tmp_path):
    with patch("src.hf_client.snapshot_download", return_value="/tmp/result"):
        result = await hf_client.download_repo_snapshot("org/model", tmp_path)
    assert result == Path("/tmp/result")


@pytest.mark.asyncio
async def test_download_repo_streaming_success(hf_client, tmp_path):
    manifest = HFRepoManifest(
        repo_id="org/model",
        repo_type="model",
        revision="main",
        files=[HFFileInfo(rfilename="file.txt", size=1000, blob_id="abc", lfs=None)],
        total_size=1000,
        lfs_size=0,
        fetched_at=datetime.now(),
    )
    sem = asyncio.Semaphore(1)

    with patch.object(hf_client, "download_repo_snapshot", new_callable=AsyncMock):
        items = []
        async for p in hf_client.download_repo_streaming("org/model", manifest, tmp_path, sem):
            items.append(p)

    assert items[0].bytes_downloaded == 0
    assert items[-1].bytes_downloaded == 1000


@pytest.mark.asyncio
async def test_download_repo_streaming_error(hf_client, tmp_path):
    manifest = HFRepoManifest(
        repo_id="org/model",
        repo_type="model",
        revision="main",
        files=[],
        total_size=0,
        lfs_size=0,
        fetched_at=datetime.now(),
    )
    sem = asyncio.Semaphore(1)

    with patch.object(
        hf_client, "download_repo_snapshot", new_callable=AsyncMock, side_effect=Exception("fail")
    ):
        items = []
        async for p in hf_client.download_repo_streaming("org/model", manifest, tmp_path, sem):
            items.append(p)

    error_items = [p for p in items if p.phase == "error"]
    assert len(error_items) > 0


@pytest.mark.asyncio
async def test_fetch_manifest_empty_repo(hf_client):
    with patch.object(hf_client.api, "repo_info", return_value=MagicMock(sha="abc")), \
         patch.object(hf_client.api, "list_repo_tree", return_value=[]):
        manifest = await hf_client.fetch_manifest("org/empty")

    assert len(manifest.files) == 0
    assert manifest.total_size == 0
    assert manifest.lfs_size == 0


@pytest.mark.asyncio
async def test_fetch_manifest_mixed_files(hf_client):
    plain = MagicMock()
    plain.path = "config.json"
    plain.size = 100
    plain.blob_id = "abc"
    plain.lfs = None

    lfs_file = MagicMock()
    lfs_file.path = "model.bin"
    lfs_file.size = 2_000_000_000
    lfs_file.blob_id = "def"
    lfs_file.lfs = MagicMock(sha256="sha256hash", size=2_000_000_000)

    with patch.object(hf_client.api, "repo_info", return_value=MagicMock(sha="abc")), \
         patch.object(hf_client.api, "list_repo_tree", return_value=[plain, lfs_file]):
        manifest = await hf_client.fetch_manifest("org/mixed")

    assert manifest.lfs_size == 2_000_000_000
    assert manifest.total_size == 2_000_000_100
    assert manifest.files[0].lfs is None
    assert manifest.files[1].lfs is not None
