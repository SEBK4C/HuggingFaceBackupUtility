"""Gitea REST API client for managing local mirror repositories."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import stat
import subprocess
import tempfile
from pathlib import Path

import httpx

from .errors import GiteaError
from .models import AppConfig

logger = logging.getLogger(__name__)

INI_TEMPLATE = """[database]
DB_TYPE  = sqlite3
PATH     = {data_dir}/gitea.db

[server]
HTTP_PORT        = {port}
ROOT_URL         = {base_url}
LFS_START_SERVER = true
LFS_JWT_SECRET   = {lfs_jwt_secret}

[lfs]
PATH = {data_dir}/lfs

[repository]
ROOT = {data_dir}/repositories

[security]
INSTALL_LOCK = true

[service]
DISABLE_REGISTRATION = true

[log]
ROOT_PATH = {data_dir}/log
MODE      = file
LEVEL     = Warn
"""


class GiteaClient:
    def __init__(self, config: AppConfig):
        self.config = config
        self.base_url = config.gitea_base_url.rstrip("/")
        self.api_url = f"{self.base_url}/api/v1"
        self.token = config.gitea_api_token.get_secret_value() if config.gitea_api_token else None
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {}
            if self.token:
                headers["Authorization"] = f"token {self.token}"
            self._client = httpx.AsyncClient(
                base_url=self.api_url,
                headers=headers,
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # --- Initialization ---

    @staticmethod
    def generate_app_ini(config: AppConfig, data_dir: Path) -> str:
        """Generate Gitea app.ini content."""
        import secrets
        lfs_jwt_secret = secrets.token_urlsafe(32)
        return INI_TEMPLATE.format(
            data_dir=data_dir.resolve(),
            port=config.gitea_port,
            base_url=config.gitea_base_url,
            lfs_jwt_secret=lfs_jwt_secret,
        )

    @classmethod
    async def initialize_gitea(cls, config: AppConfig) -> None:
        """First-run Gitea provisioning: create dirs, config, admin user."""
        data_dir = Path("./gitea-data")
        for subdir in ["repositories", "lfs", "log"]:
            (data_dir / subdir).mkdir(parents=True, exist_ok=True)

        ini_path = data_dir / "app.ini"
        if not ini_path.exists():
            ini_content = cls.generate_app_ini(config, data_dir)
            ini_path.write_text(ini_content)
            logger.info("Generated Gitea config at %s", ini_path)

    @classmethod
    async def create_admin_user(cls, config: AppConfig) -> None:
        """Create the Gitea admin user via CLI."""
        gitea_bin = Path("./bin/gitea")
        if not gitea_bin.exists():
            raise GiteaError("Gitea binary not found at ./bin/gitea")

        password = config.gitea_admin_password.get_secret_value()
        if not password:
            import secrets
            password = secrets.token_urlsafe(16)
            logger.info("Generated Gitea admin password (see .env file)")

        cmd = [
            str(gitea_bin),
            "admin", "user", "create",
            "--username", config.gitea_admin_user,
            "--password", password,
            "--email", "admin@localhost",
            "--admin",
            "--config", "./gitea-data/app.ini",
        ]

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True),
        )
        if result.returncode != 0 and "already exists" not in result.stderr:
            raise GiteaError(f"Failed to create admin user: {result.stderr}")

    # --- Health ---

    async def wait_for_ready(self, timeout: float = 30.0) -> None:
        """Poll Gitea until it responds to health check or timeout."""
        import time
        start = time.time()
        while time.time() - start < timeout:
            try:
                resp = await self.client.get("/version")
                if resp.status_code == 200:
                    logger.info("Gitea is ready")
                    return
            except httpx.ConnectError:
                pass
            await asyncio.sleep(1.0)
        raise GiteaError(f"Gitea did not become ready within {timeout}s")

    async def health_check(self) -> bool:
        """Quick health check - returns True if Gitea responds."""
        try:
            resp = await self.client.get("/version")
            return resp.status_code == 200
        except Exception:
            return False

    # --- Repository Operations ---

    async def create_repo(self, name: str, private: bool = False) -> dict:
        """Create a new repository in Gitea."""
        resp = await self.client.post(
            "/user/repos",
            json={"name": name, "private": private},
        )
        if resp.status_code == 409:
            logger.info("Repo %s already exists in Gitea", name)
            return await self.get_repo(name)
        if resp.status_code not in (200, 201):
            raise GiteaError(f"Failed to create repo {name}: {resp.status_code} {resp.text}")
        return resp.json()

    async def delete_repo(self, name: str) -> None:
        """Delete a repository from Gitea."""
        owner = self.config.gitea_admin_user
        resp = await self.client.delete(f"/repos/{owner}/{name}")
        if resp.status_code not in (204, 404):
            raise GiteaError(f"Failed to delete repo {name}: {resp.status_code} {resp.text}")

    async def get_repo(self, name: str) -> dict:
        """Get repository info from Gitea."""
        owner = self.config.gitea_admin_user
        resp = await self.client.get(f"/repos/{owner}/{name}")
        if resp.status_code == 404:
            raise GiteaError(f"Repo {name} not found in Gitea")
        if resp.status_code != 200:
            raise GiteaError(f"Failed to get repo {name}: {resp.status_code}")
        return resp.json()

    async def list_repos(self) -> list[dict]:
        """List all repositories in Gitea."""
        resp = await self.client.get("/repos/search", params={"limit": 50})
        if resp.status_code != 200:
            raise GiteaError(f"Failed to list repos: {resp.status_code}")
        data = resp.json()
        return data.get("data", [])

    async def create_api_token(self, username: str, password: str) -> str:
        """Create an API token for the given user."""
        async with httpx.AsyncClient(
            base_url=self.api_url, timeout=30.0
        ) as client:
            resp = await client.post(
                f"/users/{username}/tokens",
                json={"name": "hfmirror-auto"},
                auth=(username, password),
            )
            if resp.status_code not in (200, 201):
                raise GiteaError(
                    f"Failed to create API token: {resp.status_code} {resp.text}"
                )
            return resp.json()["sha1"]

    # --- Git Push ---

    async def git_push_repo(
        self,
        work_dir: Path,
        gitea_repo_name: str,
        commit_message: str,
        lfs_patterns: list[str] | None = None,
    ) -> str:
        """Initialize a git repo, commit files, and push to Gitea.

        Returns the commit SHA.
        """
        owner = self.config.gitea_admin_user
        password = self.config.gitea_admin_password.get_secret_value()
        port = self.config.gitea_port
        # Do NOT embed password in URL — it would be stored in .git/config and
        # visible in process listings. Use GIT_ASKPASS instead.
        remote_url = f"http://{owner}@localhost:{port}/{owner}/{gitea_repo_name}.git"

        if lfs_patterns is None:
            lfs_patterns = [
                "*.safetensors", "*.bin", "*.gguf", "*.ot",
                "*.pt", "*.pth", "*.h5",
            ]

        loop = asyncio.get_running_loop()

        # Ensure .bin/ (local git-lfs) is on PATH for subprocess calls
        env = os.environ.copy()
        local_bin = str(Path(".bin").resolve())
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")

        # Pass credentials via GIT_ASKPASS: a temporary executable script that
        # prints the password.  This avoids storing credentials in .git/config
        # or exposing them in the process argument list.
        askpass_fd, askpass_path = tempfile.mkstemp(suffix=".sh")
        try:
            with os.fdopen(askpass_fd, "w") as f:
                f.write(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(password)}\n")
            os.chmod(askpass_path, stat.S_IRWXU)  # 0o700 — owner-only execute
            env["GIT_ASKPASS"] = askpass_path
            env["GIT_TERMINAL_PROMPT"] = "0"  # never fall back to interactive prompt

            async def run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
                cmd = ["git"] + list(args)
                result = await loop.run_in_executor(
                    None,
                    lambda: subprocess.run(
                        cmd, cwd=str(work_dir), capture_output=True, text=True,
                        env=env,
                    ),
                )
                if check and result.returncode != 0:
                    raise GiteaError(f"git {' '.join(args)} failed: {result.stderr}")
                return result

            # Init (safe to re-run)
            await run_git("init", "--initial-branch=main", check=False)
            await run_git("lfs", "install", "--local")

            # Track LFS patterns
            for pattern in lfs_patterns:
                await run_git("lfs", "track", pattern)

            await run_git("add", "-A")

            # Commit (skip if nothing to commit)
            status = await run_git("status", "--porcelain")
            if status.stdout.strip():
                await run_git("commit", "-m", commit_message)
            else:
                # Check if there are any commits at all
                head = await run_git("rev-parse", "HEAD", check=False)
                if head.returncode != 0:
                    await run_git("commit", "--allow-empty", "-m", commit_message)

            # Set remote (update if exists)
            existing = await run_git("remote", "get-url", "origin", check=False)
            if existing.returncode == 0:
                await run_git("remote", "set-url", "origin", remote_url)
            else:
                await run_git("remote", "add", "origin", remote_url)

            await run_git("push", "-u", "origin", "main", "--force")

            # Get commit SHA
            result = await run_git("rev-parse", "HEAD")
            return result.stdout.strip()
        finally:
            os.unlink(askpass_path)
