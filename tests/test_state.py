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


@pytest.mark.asyncio
async def test_upsert_idempotency(state_db):
    await state_db.upsert_repo(MirroredRepo(
        repo_id="org/model",
        gitea_repo_name="org--model",
        state=MirrorState.PENDING,
    ))
    await state_db.upsert_repo(MirroredRepo(
        repo_id="org/model",
        gitea_repo_name="org--model",
        state=MirrorState.SYNCED,
    ))
    repos = await state_db.list_repos()
    assert len(repos) == 1
    assert repos[0].state == MirrorState.SYNCED


@pytest.mark.asyncio
async def test_list_file_records_multiple(state_db):
    await state_db.upsert_repo(MirroredRepo(
        repo_id="org/model",
        gitea_repo_name="org--model",
        state=MirrorState.SYNCED,
    ))
    for i in range(3):
        await state_db.upsert_file_record(
            repo_id="org/model",
            rfilename=f"file{i}.bin",
            blob_id=f"blob{i}",
            size_bytes=100 * (i + 1),
            is_lfs=True,
            storage_tier="tier1",
        )
    records = await state_db.list_file_records("org/model")
    assert len(records) == 3


@pytest.mark.asyncio
async def test_list_file_records_empty(state_db):
    await state_db.upsert_repo(MirroredRepo(
        repo_id="org/model",
        gitea_repo_name="org--model",
        state=MirrorState.SYNCED,
    ))
    records = await state_db.list_file_records("org/model")
    assert records == []


@pytest.mark.asyncio
async def test_file_record_upsert_updates_existing(state_db):
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
    await state_db.upsert_file_record(
        repo_id="org/model",
        rfilename="weights.bin",
        blob_id="def456",
        size_bytes=2000,
        is_lfs=True,
        storage_tier="tier2",
    )
    records = await state_db.list_file_records("org/model")
    assert len(records) == 1
    assert records[0]["blob_id"] == "def456"


@pytest.mark.asyncio
async def test_recover_download_completed_file(state_db, tmp_dirs):
    tier1 = tmp_dirs["tier1"]
    real_file = tier1 / "weights.bin"
    real_file.write_bytes(b"data")
    await state_db.journal_start("org/model", "weights.bin", "download")

    actions = await state_db.recover_incomplete_operations(tier1_path=tier1)

    assert len(actions) == 1
    entries = await state_db.get_incomplete_journal_entries()
    assert len(entries) == 0


@pytest.mark.asyncio
async def test_recover_download_partial_exists(state_db, tmp_dirs):
    tier1 = tmp_dirs["tier1"]
    partial = tier1 / "weights.bin.partial"
    partial.write_bytes(b"partial")
    await state_db.journal_start("org/model", "weights.bin", "download")

    actions = await state_db.recover_incomplete_operations(tier1_path=tier1)

    assert actions == []
    entries = await state_db.get_incomplete_journal_entries()
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_recover_download_no_file(state_db, tmp_dirs):
    tier1 = tmp_dirs["tier1"]
    await state_db.journal_start("org/model", "weights.bin", "download")

    actions = await state_db.recover_incomplete_operations(tier1_path=tier1)

    assert len(actions) == 1
    entries = await state_db.get_incomplete_journal_entries()
    assert len(entries) == 0


@pytest.mark.asyncio
async def test_recover_migrate(state_db, tmp_dirs):
    await state_db.journal_start("org/model", "weights.bin", "migrate")

    actions = await state_db.recover_incomplete_operations(
        tier1_path=tmp_dirs["tier1"], tier2_path=tmp_dirs["tier2"]
    )

    assert len(actions) == 1
    entries = await state_db.get_incomplete_journal_entries()
    assert len(entries) == 0


@pytest.mark.asyncio
async def test_recover_delete(state_db, tmp_dirs):
    await state_db.journal_start("org/model", "weights.bin", "delete")

    actions = await state_db.recover_incomplete_operations(
        tier1_path=tmp_dirs["tier1"], tier2_path=tmp_dirs["tier2"]
    )

    assert len(actions) == 1
    entries = await state_db.get_incomplete_journal_entries()
    assert len(entries) == 0


@pytest.mark.asyncio
async def test_recover_no_incomplete(state_db, tmp_dirs):
    actions = await state_db.recover_incomplete_operations(
        tier1_path=tmp_dirs["tier1"], tier2_path=tmp_dirs["tier2"]
    )
    assert actions == []
