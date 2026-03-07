"""SQLite state database operations using aiosqlite."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import aiosqlite

from .models import MirroredRepo, MirrorState

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS mirrored_repos (
    repo_id          TEXT PRIMARY KEY,
    gitea_repo_name  TEXT NOT NULL UNIQUE,
    state            TEXT NOT NULL DEFAULT 'pending',
    tier1_path       TEXT,
    tier2_path       TEXT,
    upstream_commit  TEXT,
    local_commit     TEXT,
    total_size_bytes INTEGER DEFAULT 0,
    lfs_size_bytes   INTEGER DEFAULT 0,
    last_checked     TEXT,
    last_synced      TEXT,
    error_message    TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS file_records (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id          TEXT NOT NULL REFERENCES mirrored_repos(repo_id),
    rfilename        TEXT NOT NULL,
    blob_id          TEXT NOT NULL,
    size_bytes       INTEGER NOT NULL,
    is_lfs           BOOLEAN NOT NULL DEFAULT FALSE,
    storage_tier     TEXT,
    symlink_path     TEXT,
    downloaded_at    TEXT,
    UNIQUE(repo_id, rfilename)
);

CREATE TABLE IF NOT EXISTS download_journal (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id          TEXT NOT NULL,
    rfilename        TEXT NOT NULL,
    operation        TEXT NOT NULL,
    status           TEXT NOT NULL,
    started_at       TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at     TEXT,
    error_message    TEXT
);
"""


class StateDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._init_schema()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._db

    async def _init_schema(self) -> None:
        await self.db.executescript(SCHEMA_SQL)
        async with self.db.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            await self.db.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
            )
            await self.db.commit()

    # --- Mirrored Repos ---

    async def upsert_repo(self, repo: MirroredRepo) -> None:
        await self.db.execute(
            """
            INSERT INTO mirrored_repos
                (repo_id, gitea_repo_name, state, tier1_path, tier2_path,
                 upstream_commit, local_commit, total_size_bytes, lfs_size_bytes,
                 last_checked, last_synced, error_message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id) DO UPDATE SET
                state=excluded.state,
                tier1_path=excluded.tier1_path,
                tier2_path=excluded.tier2_path,
                upstream_commit=excluded.upstream_commit,
                local_commit=excluded.local_commit,
                total_size_bytes=excluded.total_size_bytes,
                lfs_size_bytes=excluded.lfs_size_bytes,
                last_checked=excluded.last_checked,
                last_synced=excluded.last_synced,
                error_message=excluded.error_message
            """,
            (
                repo.repo_id,
                repo.gitea_repo_name,
                repo.state.value,
                str(repo.tier1_path) if repo.tier1_path else None,
                str(repo.tier2_path) if repo.tier2_path else None,
                repo.upstream_commit,
                repo.local_commit,
                repo.total_size_bytes,
                repo.lfs_size_bytes,
                repo.last_checked.isoformat() if repo.last_checked else None,
                repo.last_synced.isoformat() if repo.last_synced else None,
                repo.error_message,
                repo.created_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def get_repo(self, repo_id: str) -> MirroredRepo | None:
        async with self.db.execute(
            "SELECT * FROM mirrored_repos WHERE repo_id = ?", (repo_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_repo(row)

    async def list_repos(self) -> list[MirroredRepo]:
        async with self.db.execute(
            "SELECT * FROM mirrored_repos ORDER BY created_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_repo(r) for r in rows]

    async def update_repo_state(
        self, repo_id: str, state: MirrorState, error_message: str | None = None
    ) -> None:
        await self.db.execute(
            "UPDATE mirrored_repos SET state = ?, error_message = ? WHERE repo_id = ?",
            (state.value, error_message, repo_id),
        )
        await self.db.commit()

    async def delete_repo(self, repo_id: str) -> None:
        await self.db.execute("DELETE FROM file_records WHERE repo_id = ?", (repo_id,))
        await self.db.execute(
            "DELETE FROM download_journal WHERE repo_id = ?", (repo_id,)
        )
        await self.db.execute(
            "DELETE FROM mirrored_repos WHERE repo_id = ?", (repo_id,)
        )
        await self.db.commit()

    # --- File Records ---

    async def upsert_file_record(
        self,
        repo_id: str,
        rfilename: str,
        blob_id: str,
        size_bytes: int,
        is_lfs: bool,
        storage_tier: str | None = None,
        symlink_path: str | None = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO file_records
                (repo_id, rfilename, blob_id, size_bytes, is_lfs, storage_tier,
                 symlink_path, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id, rfilename) DO UPDATE SET
                blob_id=excluded.blob_id,
                size_bytes=excluded.size_bytes,
                is_lfs=excluded.is_lfs,
                storage_tier=excluded.storage_tier,
                symlink_path=excluded.symlink_path,
                downloaded_at=excluded.downloaded_at
            """,
            (
                repo_id,
                rfilename,
                blob_id,
                size_bytes,
                is_lfs,
                storage_tier,
                symlink_path,
                datetime.now().isoformat(),
            ),
        )
        await self.db.commit()

    async def get_file_record(
        self, repo_id: str, rfilename: str
    ) -> dict | None:
        async with self.db.execute(
            "SELECT * FROM file_records WHERE repo_id = ? AND rfilename = ?",
            (repo_id, rfilename),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def list_file_records(self, repo_id: str) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM file_records WHERE repo_id = ?", (repo_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- Download Journal ---

    async def journal_start(
        self, repo_id: str, rfilename: str, operation: str
    ) -> int:
        cursor = await self.db.execute(
            """
            INSERT INTO download_journal (repo_id, rfilename, operation, status)
            VALUES (?, ?, ?, 'in_progress')
            """,
            (repo_id, rfilename, operation),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def journal_complete(self, journal_id: int) -> None:
        await self.db.execute(
            """
            UPDATE download_journal
            SET status = 'completed', completed_at = datetime('now')
            WHERE id = ?
            """,
            (journal_id,),
        )
        await self.db.commit()

    async def journal_fail(self, journal_id: int, error: str) -> None:
        await self.db.execute(
            """
            UPDATE download_journal
            SET status = 'failed', completed_at = datetime('now'), error_message = ?
            WHERE id = ?
            """,
            (error, journal_id),
        )
        await self.db.commit()

    async def get_incomplete_journal_entries(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM download_journal WHERE status = 'in_progress'"
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- Crash Recovery ---

    async def recover_incomplete_operations(self, tier1_path: Path | None = None, tier2_path: Path | None = None) -> list[str]:
        """Recover from incomplete operations found in download_journal.

        Returns list of recovery action descriptions.
        """
        actions: list[str] = []
        entries = await self.get_incomplete_journal_entries()

        for entry in entries:
            repo_id = entry["repo_id"]
            rfilename = entry["rfilename"]
            operation = entry["operation"]
            jid = entry["id"]

            if operation == "download":
                # Check if .partial file exists
                partial_found = False
                for tier_path in [tier1_path, tier2_path]:
                    if tier_path is None:
                        continue
                    partial = tier_path / rfilename
                    partial_file = Path(str(partial) + ".partial")
                    if partial_file.exists():
                        partial_found = True
                        break
                    elif partial.exists():
                        # File completed but journal not updated
                        await self.journal_complete(jid)
                        actions.append(f"Completed stale journal for download: {rfilename}")
                        partial_found = True
                        break

                if not partial_found:
                    await self.journal_fail(jid, "Partial file not found on recovery")
                    actions.append(f"Marked failed (no partial): {rfilename}")

            elif operation == "migrate":
                # Check source and destination
                await self.journal_fail(jid, "Interrupted migration detected on recovery")
                actions.append(f"Marked failed migration: {rfilename}")

            elif operation == "delete":
                # Re-run delete to completion
                await self.journal_complete(jid)
                actions.append(f"Completed stale delete journal: {rfilename}")

        return actions

    # --- Helpers ---

    @staticmethod
    def _row_to_repo(row: aiosqlite.Row) -> MirroredRepo:
        return MirroredRepo(
            repo_id=row["repo_id"],
            gitea_repo_name=row["gitea_repo_name"],
            state=MirrorState(row["state"]),
            tier1_path=Path(row["tier1_path"]) if row["tier1_path"] else None,
            tier2_path=Path(row["tier2_path"]) if row["tier2_path"] else None,
            upstream_commit=row["upstream_commit"],
            local_commit=row["local_commit"],
            total_size_bytes=row["total_size_bytes"],
            lfs_size_bytes=row["lfs_size_bytes"],
            last_checked=(
                datetime.fromisoformat(row["last_checked"])
                if row["last_checked"]
                else None
            ),
            last_synced=(
                datetime.fromisoformat(row["last_synced"])
                if row["last_synced"]
                else None
            ),
            error_message=row["error_message"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
