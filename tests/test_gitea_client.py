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


@pytest.mark.asyncio
async def test_list_repos(gitea_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": [{"name": "repo1"}, {"name": "repo2"}]}

    with patch.object(gitea_client.client, "get", new_callable=AsyncMock, return_value=mock_resp):
        repos = await gitea_client.list_repos()
    assert len(repos) == 2


@pytest.mark.asyncio
async def test_list_repos_error(gitea_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 500

    with patch.object(gitea_client.client, "get", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(GiteaError):
            await gitea_client.list_repos()


@pytest.mark.asyncio
async def test_get_repo_success(gitea_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"name": "org--model", "id": 42}

    with patch.object(gitea_client.client, "get", new_callable=AsyncMock, return_value=mock_resp):
        result = await gitea_client.get_repo("org--model")
    assert result["id"] == 42


@pytest.mark.asyncio
async def test_get_repo_not_found(gitea_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch.object(gitea_client.client, "get", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(GiteaError):
            await gitea_client.get_repo("org--model")


@pytest.mark.asyncio
async def test_create_api_token(gitea_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"sha1": "token123"}

    mock_http_client = AsyncMock()
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=None)
    mock_http_client.post = AsyncMock(return_value=mock_resp)

    with patch("src.gitea_client.httpx.AsyncClient", return_value=mock_http_client):
        token = await gitea_client.create_api_token("testadmin", "testpass")
    assert token == "token123"


@pytest.mark.asyncio
async def test_create_api_token_error(gitea_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "Unauthorized"

    mock_http_client = AsyncMock()
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=None)
    mock_http_client.post = AsyncMock(return_value=mock_resp)

    with patch("src.gitea_client.httpx.AsyncClient", return_value=mock_http_client):
        with pytest.raises(GiteaError):
            await gitea_client.create_api_token("testadmin", "wrongpass")


@pytest.mark.asyncio
async def test_wait_for_ready_success(gitea_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch.object(gitea_client.client, "get", new_callable=AsyncMock, return_value=mock_resp):
        await gitea_client.wait_for_ready(timeout=5.0)  # Should return without raising


@pytest.mark.asyncio
async def test_wait_for_ready_timeout(gitea_client):
    with patch("time.time", side_effect=[0.0, 1.0]), \
         patch.object(gitea_client.client, "get", new_callable=AsyncMock, side_effect=httpx.ConnectError("refused")), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(GiteaError, match="not become ready"):
            await gitea_client.wait_for_ready(timeout=0.5)


@pytest.mark.asyncio
async def test_wait_for_ready_eventual(gitea_client):
    mock_ok = MagicMock()
    mock_ok.status_code = 200

    with patch.object(
        gitea_client.client,
        "get",
        new_callable=AsyncMock,
        side_effect=[httpx.ConnectError("refused"), mock_ok],
    ), patch("asyncio.sleep", new_callable=AsyncMock):
        await gitea_client.wait_for_ready(timeout=5.0)  # Should not raise


@pytest.mark.asyncio
async def test_git_push_repo(gitea_client, tmp_path):
    mock_result = MagicMock(returncode=0, stdout="abc123\n", stderr="")
    with patch("subprocess.run", return_value=mock_result):
        sha = await gitea_client.git_push_repo(tmp_path, "org--model", "commit msg")
    assert sha == "abc123"


@pytest.mark.asyncio
async def test_delete_repo_404(gitea_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch.object(gitea_client.client, "delete", new_callable=AsyncMock, return_value=mock_resp):
        await gitea_client.delete_repo("org--model")  # Should not raise


@pytest.mark.asyncio
async def test_create_repo_failure(gitea_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    with patch.object(gitea_client.client, "post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(GiteaError):
            await gitea_client.create_repo("org--model")


@pytest.mark.asyncio
async def test_initialize_gitea(app_config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    await GiteaClient.initialize_gitea(app_config)
    data_dir = tmp_path / "gitea-data"
    assert (data_dir / "repositories").exists()
    assert (data_dir / "lfs").exists()
    assert (data_dir / "log").exists()
    assert (data_dir / "app.ini").exists()


def test_client_property(gitea_client):
    assert isinstance(gitea_client.client, httpx.AsyncClient)


@pytest.mark.asyncio
async def test_close(gitea_client):
    _ = gitea_client.client  # Force client creation
    await gitea_client.close()
    assert gitea_client._client.is_closed
