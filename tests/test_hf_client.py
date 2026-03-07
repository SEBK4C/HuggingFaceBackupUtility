"""Unit tests for HF client with mocked Hub responses."""

from datetime import datetime
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
