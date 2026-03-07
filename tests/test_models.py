"""Pydantic model validation edge cases."""

from datetime import datetime

import pytest

from src.models import (
    AppConfig,
    CloneRequest,
    HFFileInfo,
    HFRepoManifest,
    LFSPointer,
    MirroredRepo,
    MirrorState,
)


def test_app_config_defaults():
    cfg = AppConfig(hf_token="hf_test")
    assert cfg.tier1_path.name == "downloads"
    assert cfg.tier2_path is None
    assert cfg.tier_threshold_percent == 10
    assert cfg.hf_concurrent_downloads == 4


def test_app_config_validation_bounds():
    with pytest.raises(Exception):
        AppConfig(hf_token="hf_test", tier_threshold_percent=0)
    with pytest.raises(Exception):
        AppConfig(hf_token="hf_test", tier_threshold_percent=91)


def test_clone_request_defaults():
    req = CloneRequest(repo_id="org/model")
    assert req.revision == "main"
    assert req.force_tier is None


def test_hf_file_info_with_lfs():
    lfs = LFSPointer(sha256="abc123", size=1000)
    f = HFFileInfo(rfilename="weights.bin", size=1000, blob_id="xyz", lfs=lfs)
    assert f.lfs is not None
    assert f.lfs.sha256 == "abc123"


def test_hf_file_info_without_lfs():
    f = HFFileInfo(rfilename="config.json", size=100, blob_id="xyz")
    assert f.lfs is None


def test_mirror_state_enum():
    assert MirrorState.SYNCED.value == "synced"
    assert MirrorState("error") == MirrorState.ERROR


def test_mirrored_repo_defaults():
    repo = MirroredRepo(
        repo_id="org/model",
        gitea_repo_name="org--model",
        state=MirrorState.PENDING,
    )
    assert repo.total_size_bytes == 0
    assert repo.last_synced is None
    assert isinstance(repo.created_at, datetime)


def test_hf_repo_manifest():
    manifest = HFRepoManifest(
        repo_id="org/model",
        files=[
            HFFileInfo(rfilename="a.txt", size=10, blob_id="aaa"),
            HFFileInfo(
                rfilename="b.bin", size=1000, blob_id="bbb",
                lfs=LFSPointer(sha256="ccc", size=1000),
            ),
        ],
        total_size=1010,
        lfs_size=1000,
        fetched_at=datetime.now(),
    )
    assert len(manifest.files) == 2
    assert manifest.total_size == 1010
