"""Unit tests for Gitea client with mocked API."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.errors import GiteaError
from src.gitea_client import GiteaClient


@pytest.fixture
def gitea_client(app_config):
    return GiteaClient(app_config)


def test_generate_app_ini(app_config, tmp_path):
    ini = GiteaClient.generate_app_ini(app_config, tmp_path)
    assert "DB_TYPE  = sqlite3" in ini
    assert str(tmp_path.resolve()) in ini
    assert "INSTALL_LOCK = true" in ini


@pytest.mark.asyncio
async def test_create_repo_success(gitea_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"name": "org--model", "id": 1}

    with patch.object(gitea_client.client, "post", new_callable=AsyncMock, return_value=mock_resp):
        result = await gitea_client.create_repo("org--model")
    assert result["name"] == "org--model"


@pytest.mark.asyncio
async def test_create_repo_already_exists(gitea_client):
    mock_conflict = MagicMock()
    mock_conflict.status_code = 409

    mock_get = MagicMock()
    mock_get.status_code = 200
    mock_get.json.return_value = {"name": "org--model", "id": 1}

    with patch.object(gitea_client.client, "post", new_callable=AsyncMock, return_value=mock_conflict), \
         patch.object(gitea_client.client, "get", new_callable=AsyncMock, return_value=mock_get):
        result = await gitea_client.create_repo("org--model")
    assert result["name"] == "org--model"


@pytest.mark.asyncio
async def test_delete_repo_success(gitea_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 204

    with patch.object(gitea_client.client, "delete", new_callable=AsyncMock, return_value=mock_resp):
        await gitea_client.delete_repo("org--model")  # Should not raise


@pytest.mark.asyncio
async def test_health_check_ok(gitea_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch.object(gitea_client.client, "get", new_callable=AsyncMock, return_value=mock_resp):
        assert await gitea_client.health_check() is True


@pytest.mark.asyncio
async def test_health_check_fail(gitea_client):
    with patch.object(gitea_client.client, "get", new_callable=AsyncMock, side_effect=Exception("conn refused")):
        assert await gitea_client.health_check() is False
