"""SQLite state database operation tests."""

import pytest

from src.models import MirroredRepo, MirrorState


@pytest.mark.asyncio
async def test_upsert_and_get_repo(state_db):
    repo = MirroredRepo(
        repo_id="org/model",
        gitea_repo_name="org--model",
        state=MirrorState.PENDING,
    )
    await state_db.upsert_repo(repo)

    fetched = await state_db.get_repo("org/model")
    assert fetched is not None
    assert fetched.repo_id == "org/model"
    assert fetched.state == MirrorState.PENDING


@pytest.mark.asyncio
async def test_list_repos_empty(state_db):
    repos = await state_db.list_repos()
    assert repos == []


@pytest.mark.asyncio
async def test_list_repos_multiple(state_db):
    for name in ["org/a", "org/b"]:
        await state_db.upsert_repo(MirroredRepo(
            repo_id=name,
            gitea_repo_name=name.replace("/", "--"),
            state=MirrorState.SYNCED,
        ))

    repos = await state_db.list_repos()
    assert len(repos) == 2


@pytest.mark.asyncio
async def test_update_repo_state(state_db):
    await state_db.upsert_repo(MirroredRepo(
        repo_id="org/model",
        gitea_repo_name="org--model",
        state=MirrorState.CLONING,
    ))

    await state_db.update_repo_state("org/model", MirrorState.ERROR, "download failed")

    repo = await state_db.get_repo("org/model")
    assert repo.state == MirrorState.ERROR
    assert repo.error_message == "download failed"


@pytest.mark.asyncio
async def test_delete_repo(state_db):
    await state_db.upsert_repo(MirroredRepo(
        repo_id="org/model",
        gitea_repo_name="org--model",
        state=MirrorState.SYNCED,
    ))
    await state_db.delete_repo("org/model")
    assert await state_db.get_repo("org/model") is None


@pytest.mark.asyncio
async def test_file_record_upsert_and_get(state_db):
    await state_db.upsert_repo(MirroredRepo(
        repo_id="org/model",
        gitea_repo_name="org--model",
        state=MirrorState.SYNCED,
    ))

    await state_db.upsert_file_record(
        repo_id="org/model",
        rfilename="weights.bin",
        blob_id="abc123",
        size_bytes=1000,
        is_lfs=True,
        storage_tier="tier1",
    )

    record = await state_db.get_file_record("org/model", "weights.bin")
    assert record is not None
    assert record["blob_id"] == "abc123"
    assert record["is_lfs"] == 1


@pytest.mark.asyncio
async def test_journal_lifecycle(state_db):
    jid = await state_db.journal_start("org/model", "weights.bin", "download")
    assert jid is not None

    entries = await state_db.get_incomplete_journal_entries()
    assert len(entries) == 1

    await state_db.journal_complete(jid)
    entries = await state_db.get_incomplete_journal_entries()
    assert len(entries) == 0


@pytest.mark.asyncio
async def test_journal_fail(state_db):
    jid = await state_db.journal_start("org/model", "weights.bin", "download")
    await state_db.journal_fail(jid, "network timeout")

    entries = await state_db.get_incomplete_journal_entries()
    assert len(entries) == 0
