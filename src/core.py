"""Business logic orchestration. NO imports from cli or web modules."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from .errors import GiteaError, HFMirrorError
from .gitea_client import GiteaClient
from .hf_client import HFClient
from .models import (
    AppConfig,
    CloneProgress,
    CloneRequest,
    CloneResult,
    DiffRequest,
    DiffResult,
    DoctorResult,
    FileDiff,
    HealthCheck,
    MigrateRequest,
    MigrateResult,
    MirroredRepo,
    MirrorState,
    PruneRequest,
    PruneResult,
    RepoStatusResponse,
)
from .state import StateDB
from .storage import (
    check_symlink_health,
    ensure_repo_dirs,
    evaluate_tier_routing,
    find_orphaned_blobs,
    migrate_file,
    repo_id_to_dirname,
)

logger = logging.getLogger(__name__)


class HFMirrorCore:
    def __init__(self, config: AppConfig, state_db: StateDB):
        self.config = config
        self.state_db = state_db
        self.hf = HFClient(config)
        self.gitea = GiteaClient(config)
        self._semaphore = asyncio.Semaphore(config.hf_concurrent_downloads)

    async def close(self) -> None:
        await self.gitea.close()
        await self.state_db.close()

    # --- Clone ---

    async def clone(
        self, request: CloneRequest
    ) -> AsyncIterator[CloneProgress]:
        """Clone a HF repo, downloading files and pushing to Gitea."""
        start_time = time.time()
        repo_id = request.repo_id
        gitea_name = repo_id_to_dirname(repo_id)
        warnings: list[str] = []

        yield CloneProgress(
            phase="manifest",
            message=f"Fetching manifest for {repo_id}",
        )

        try:
            manifest = await self.hf.fetch_manifest(
                repo_id, revision=request.revision
            )
            upstream_commit = await self.hf.get_upstream_commit(
                repo_id, revision=request.revision
            )
        except Exception as e:
            yield CloneProgress(phase="error", message=str(e))
            return

        # Determine tier routing
        if request.force_tier:
            tier = request.force_tier
        else:
            try:
                tier = await evaluate_tier_routing(manifest, self.config)
            except Exception as e:
                yield CloneProgress(phase="error", message=str(e))
                return

        # Create state record
        tier1_path = ensure_repo_dirs(self.config, repo_id, "tier1")
        tier2_path = (
            ensure_repo_dirs(self.config, repo_id, "tier2")
            if tier == "tier2" and self.config.tier2_path
            else None
        )

        repo_record = MirroredRepo(
            repo_id=repo_id,
            gitea_repo_name=gitea_name,
            state=MirrorState.CLONING,
            tier1_path=tier1_path,
            tier2_path=tier2_path,
            upstream_commit=upstream_commit,
            total_size_bytes=manifest.total_size,
            lfs_size_bytes=manifest.lfs_size,
        )
        await self.state_db.upsert_repo(repo_record)

        # Download files
        download_dir = tier1_path
        async for progress in self.hf.download_repo_streaming(
            repo_id,
            manifest,
            download_dir,
            self._semaphore,
            revision=request.revision,
        ):
            yield progress

        # Push to Gitea
        yield CloneProgress(
            phase="gitea_push",
            message=f"Pushing to Gitea as {gitea_name}",
        )

        try:
            await self.gitea.create_repo(gitea_name)
            commit_sha = await self.gitea.git_push_repo(
                download_dir,
                gitea_name,
                f"Mirror {repo_id} @ {upstream_commit}",
            )
        except Exception as e:
            logger.warning("Gitea push failed: %s", e)
            warnings.append(f"Gitea push failed: {e}")
            commit_sha = None

        # Update state
        now = datetime.now()
        repo_record.state = MirrorState.SYNCED
        repo_record.local_commit = commit_sha
        repo_record.last_synced = now
        repo_record.last_checked = now
        await self.state_db.upsert_repo(repo_record)

        duration = time.time() - start_time
        gitea_url = f"{self.config.gitea_base_url}/{self.config.gitea_admin_user}/{gitea_name}"

        yield CloneProgress(
            phase="complete",
            files_completed=len(manifest.files),
            files_total=len(manifest.files),
            bytes_downloaded=manifest.total_size,
            bytes_total=manifest.total_size,
            message=f"Clone complete in {duration:.1f}s",
        )

    async def clone_to_result(self, request: CloneRequest) -> CloneResult:
        """Run a full clone and return the final result."""
        start_time = time.time()
        warnings: list[str] = []
        last_progress: CloneProgress | None = None

        async for progress in self.clone(request):
            last_progress = progress
            if progress.phase == "error":
                raise HFMirrorError(progress.message)

        repo = await self.state_db.get_repo(request.repo_id)
        gitea_name = repo_id_to_dirname(request.repo_id)
        gitea_url = f"{self.config.gitea_base_url}/{self.config.gitea_admin_user}/{gitea_name}"
        tier = "tier2" if repo and repo.tier2_path else "tier1"

        return CloneResult(
            repo_id=request.repo_id,
            gitea_url=gitea_url,
            state=repo.state if repo else MirrorState.ERROR,
            total_downloaded=last_progress.bytes_downloaded if last_progress else 0,
            lfs_routed_to=tier,
            duration_seconds=time.time() - start_time,
            warnings=warnings,
        )

    # --- Status / List ---

    async def list_repos(self) -> RepoStatusResponse:
        repos = await self.state_db.list_repos()
        return RepoStatusResponse(repos=repos)

    async def get_repo_status(self, repo_id: str) -> MirroredRepo | None:
        return await self.state_db.get_repo(repo_id)

    # --- Diff ---

    async def diff(self, request: DiffRequest) -> DiffResult:
        """Compare local mirror state with upstream HF Hub."""
        repo = await self.state_db.get_repo(request.repo_id)
        if repo is None:
            raise HFMirrorError(f"Repo {request.repo_id} not found in state DB")

        upstream_commit = await self.hf.get_upstream_commit(request.repo_id)
        manifest = await self.hf.fetch_manifest(request.repo_id)

        local_files = await self.state_db.list_file_records(request.repo_id)
        local_map = {f["rfilename"]: f for f in local_files}

        changes: list[FileDiff] = []
        upstream_filenames = set()

        for hf_file in manifest.files:
            upstream_filenames.add(hf_file.rfilename)
            local = local_map.get(hf_file.rfilename)
            if local is None:
                changes.append(FileDiff(
                    filename=hf_file.rfilename,
                    change_type="added",
                    is_lfs=hf_file.lfs is not None,
                    upstream_size=hf_file.size,
                ))
            elif local["blob_id"] != hf_file.blob_id:
                changes.append(FileDiff(
                    filename=hf_file.rfilename,
                    change_type="modified",
                    is_lfs=hf_file.lfs is not None,
                    local_size=local["size_bytes"],
                    upstream_size=hf_file.size,
                ))
            else:
                changes.append(FileDiff(
                    filename=hf_file.rfilename,
                    change_type="unchanged",
                    is_lfs=hf_file.lfs is not None,
                    local_size=local["size_bytes"],
                    upstream_size=hf_file.size,
                ))

        # Check for deleted files
        for fname in local_map:
            if fname not in upstream_filenames:
                local = local_map[fname]
                changes.append(FileDiff(
                    filename=fname,
                    change_type="deleted",
                    is_lfs=bool(local["is_lfs"]),
                    local_size=local["size_bytes"],
                ))

        is_up_to_date = repo.upstream_commit == upstream_commit

        return DiffResult(
            repo_id=request.repo_id,
            local_commit=repo.local_commit or "",
            upstream_commit=upstream_commit,
            is_up_to_date=is_up_to_date,
            changes=changes,
            upstream_total_size=manifest.total_size,
        )

    # --- Update ---

    async def update(self, repo_id: str) -> AsyncIterator[CloneProgress]:
        """Update a previously cloned repo by pulling upstream changes."""
        repo = await self.state_db.get_repo(repo_id)
        if repo is None:
            raise HFMirrorError(f"Repo {repo_id} not found")

        await self.state_db.update_repo_state(repo_id, MirrorState.UPDATING)

        # Re-clone logic (simplified: re-download changed files)
        request = CloneRequest(repo_id=repo_id)
        async for progress in self.clone(request):
            yield progress

    async def update_all(self) -> AsyncIterator[CloneProgress]:
        """Update all stale repos."""
        repos = await self.state_db.list_repos()
        for repo in repos:
            if repo.state in (MirrorState.STALE, MirrorState.SYNCED):
                async for progress in self.update(repo.repo_id):
                    yield progress

    # --- Migrate ---

    async def migrate(self, request: MigrateRequest) -> MigrateResult:
        """Migrate files between storage tiers.

        Scans the filesystem directly so it works even when file_records
        table is empty (e.g. after snapshot_download).
        """
        start_time = time.time()
        repo = await self.state_db.get_repo(request.repo_id)
        if repo is None:
            raise HFMirrorError(f"Repo {request.repo_id} not found")

        if not repo.tier1_path or not repo.tier1_path.exists():
            raise HFMirrorError(f"Tier 1 path not found: {repo.tier1_path}")

        # Scan filesystem for real files to migrate (skip symlinks, .git, .cache)
        skip_dirs = {".git", ".cache"}
        files_on_disk: list[str] = []
        for fpath in repo.tier1_path.rglob("*"):
            if fpath.is_file() and not fpath.is_symlink():
                # Skip files inside .git / .cache directories
                rel = fpath.relative_to(repo.tier1_path)
                if any(part in skip_dirs for part in rel.parts):
                    continue
                files_on_disk.append(str(rel))

        # Filter if specific files requested
        if request.files:
            import fnmatch
            files_to_migrate = [
                f for f in files_on_disk
                if any(fnmatch.fnmatch(f, pat) for pat in request.files)
            ]
        else:
            files_to_migrate = files_on_disk

        total_moved = 0
        files_moved = 0
        symlinks_created = 0

        for filename in files_to_migrate:
            try:
                bytes_moved = await migrate_file(
                    self.config,
                    request.repo_id,
                    filename,
                    request.target_tier,
                )
                total_moved += bytes_moved
                files_moved += 1
                if request.target_tier == "tier2":
                    symlinks_created += 1
            except Exception as e:
                logger.warning("Failed to migrate %s: %s", filename, e)

        # Update the repo record to reflect the new tier2 path
        if files_moved > 0 and request.target_tier == "tier2":
            tier2_path = ensure_repo_dirs(self.config, request.repo_id, "tier2")
            repo.tier2_path = tier2_path
            await self.state_db.upsert_repo(repo)
        elif files_moved > 0 and request.target_tier == "tier1":
            # Check if any symlinks remain; if not, clear tier2_path
            has_symlinks = any(
                f.is_symlink()
                for f in repo.tier1_path.rglob("*")
                if f.is_file()
            )
            if not has_symlinks:
                repo.tier2_path = None
                await self.state_db.upsert_repo(repo)

        return MigrateResult(
            repo_id=request.repo_id,
            files_moved=files_moved,
            bytes_moved=total_moved,
            symlinks_created=symlinks_created,
            duration_seconds=time.time() - start_time,
        )

    async def copy_to_drive2(self, repo_id: str) -> MigrateResult:
        """Copy files to Drive 2 without replacing originals with symlinks.

        Keeps real files on both drives for redundancy.
        """
        start_time = time.time()
        repo = await self.state_db.get_repo(repo_id)
        if repo is None:
            raise HFMirrorError(f"Repo {repo_id} not found")
        if not repo.tier1_path or not repo.tier1_path.exists():
            raise HFMirrorError(f"Drive 1 path not found: {repo.tier1_path}")
        if self.config.tier2_path is None:
            raise HFMirrorError("Drive 2 path not configured")

        tier2_dir = ensure_repo_dirs(self.config, repo_id, "tier2")
        skip_dirs = {".git", ".cache"}
        total_copied = 0
        files_copied = 0

        for fpath in repo.tier1_path.rglob("*"):
            if not fpath.is_file():
                continue
            rel = fpath.relative_to(repo.tier1_path)
            if any(part in skip_dirs for part in rel.parts):
                continue

            # Get the real file (follow symlinks if needed)
            if fpath.is_symlink():
                real_file = fpath.resolve()
                if not real_file.exists():
                    logger.warning("Dangling symlink, skipping: %s", fpath)
                    continue
            else:
                real_file = fpath

            dest = tier2_dir / rel
            if dest.exists() and dest.stat().st_size == real_file.stat().st_size:
                continue  # Already on Drive 2

            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(real_file), str(dest))
                total_copied += real_file.stat().st_size
                files_copied += 1
            except Exception as e:
                logger.warning("Failed to copy %s to Drive 2: %s", rel, e)

        # Update DB
        if files_copied > 0:
            repo.tier2_path = tier2_dir
            await self.state_db.upsert_repo(repo)

        return MigrateResult(
            repo_id=repo_id,
            files_moved=files_copied,
            bytes_moved=total_copied,
            symlinks_created=0,
            duration_seconds=time.time() - start_time,
        )

    # --- Prune ---

    async def prune(self, request: PruneRequest) -> PruneResult:
        """Remove a mirrored repo and optionally scrub storage."""
        repo = await self.state_db.get_repo(request.repo_id)
        if repo is None:
            raise HFMirrorError(f"Repo {request.repo_id} not found")

        bytes_reclaimed = 0
        files_deleted = 0
        gitea_deleted = False
        tier1_scrubbed = False
        tier2_scrubbed = False

        if request.dry_run:
            # Calculate what would be deleted
            if repo.tier1_path and repo.tier1_path.exists():
                for f in repo.tier1_path.rglob("*"):
                    if f.is_file() and not f.is_symlink():
                        bytes_reclaimed += f.stat().st_size
                        files_deleted += 1
            return PruneResult(
                repo_id=request.repo_id,
                bytes_reclaimed=bytes_reclaimed,
                files_deleted=files_deleted,
                gitea_repo_deleted=False,
                tier1_scrubbed=False,
                tier2_scrubbed=False,
                was_dry_run=True,
            )

        # Delete from Gitea
        if request.delete_from_gitea:
            try:
                await self.gitea.delete_repo(repo.gitea_repo_name)
                gitea_deleted = True
            except GiteaError as e:
                logger.warning("Failed to delete Gitea repo: %s", e)

        # Scrub storage
        if request.scrub_lfs_blobs:
            if repo.tier1_path and repo.tier1_path.exists():
                for f in repo.tier1_path.rglob("*"):
                    if f.is_file() and not f.is_symlink():
                        bytes_reclaimed += f.stat().st_size
                        files_deleted += 1
                shutil.rmtree(repo.tier1_path)
                tier1_scrubbed = True

            if repo.tier2_path and repo.tier2_path.exists():
                for f in repo.tier2_path.rglob("*"):
                    if f.is_file():
                        bytes_reclaimed += f.stat().st_size
                        files_deleted += 1
                shutil.rmtree(repo.tier2_path)
                tier2_scrubbed = True

        # Update state
        await self.state_db.update_repo_state(
            request.repo_id, MirrorState.PRUNED
        )

        return PruneResult(
            repo_id=request.repo_id,
            bytes_reclaimed=bytes_reclaimed,
            files_deleted=files_deleted,
            gitea_repo_deleted=gitea_deleted,
            tier1_scrubbed=tier1_scrubbed,
            tier2_scrubbed=tier2_scrubbed,
            was_dry_run=False,
        )

    # --- Doctor ---

    async def doctor(self) -> DoctorResult:
        """Run health checks on the system."""
        checks: list[HealthCheck] = []

        # Check Gitea connectivity
        gitea_ok = await self.gitea.health_check()
        checks.append(HealthCheck(
            name="Gitea Connectivity",
            passed=gitea_ok,
            message="Gitea is reachable" if gitea_ok else "Gitea is not reachable",
        ))

        # Check Drive 1 storage
        self.config.tier1_path.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self.config.tier1_path)
        pct_used = (usage.used / usage.total) * 100
        checks.append(HealthCheck(
            name="Drive 1 Storage",
            passed=True,
            message=f"Drive 1 OK: {pct_used:.1f}% used, {usage.free / (1024**3):.1f} GB free",
        ))

        # Check Drive 2 storage
        if self.config.tier2_path:
            self.config.tier2_path.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(self.config.tier2_path)
            pct_used = (usage.used / usage.total) * 100
            checks.append(HealthCheck(
                name="Drive 2 Storage",
                passed=True,
                message=f"Drive 2 OK: {pct_used:.1f}% used, {usage.free / (1024**3):.1f} GB free",
            ))

        # Check symlink health
        issues = check_symlink_health(self.config.tier1_path)
        symlink_ok = len(issues) == 0
        checks.append(HealthCheck(
            name="Symlink Health",
            passed=symlink_ok,
            message=f"All symlinks OK" if symlink_ok else f"{len(issues)} symlink issue(s) found",
            details=[i["message"] for i in issues],
        ))

        # Check orphaned blobs
        if self.config.tier2_path:
            orphans = find_orphaned_blobs(
                self.config.tier1_path, self.config.tier2_path
            )
            checks.append(HealthCheck(
                name="Orphaned Blobs",
                passed=len(orphans) == 0,
                message=(
                    "No orphaned blobs"
                    if len(orphans) == 0
                    else f"{len(orphans)} orphaned blob(s) on Drive 2"
                ),
                details=[str(p) for p in orphans],
            ))

        # Check repos in error state
        repos = await self.state_db.list_repos()
        error_repos = [r for r in repos if r.state == MirrorState.ERROR]
        checks.append(HealthCheck(
            name="Repository States",
            passed=len(error_repos) == 0,
            message=(
                f"All {len(repos)} repos healthy"
                if len(error_repos) == 0
                else f"{len(error_repos)} repo(s) in error state"
            ),
            details=[f"{r.repo_id}: {r.error_message}" for r in error_repos],
        ))

        return DoctorResult(
            checks=checks,
            all_passed=all(c.passed for c in checks),
        )

    # --- Open URL ---

    def get_gitea_url(self, repo_id: str) -> str:
        gitea_name = repo_id_to_dirname(repo_id)
        return f"{self.config.gitea_base_url}/{self.config.gitea_admin_user}/{gitea_name}"
