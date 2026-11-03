from pathlib import Path
from types import SimpleNamespace
import pytest
from scripts.storage_preflight import check


def test_low_physical_space_blocks_without_docker_or_cleanup(tmp_path):
    result=check([tmp_path],usage=lambda _:SimpleNamespace(free=512*1024**2))
    assert not result['allowed'] and not result['automatic_cleanup']
    assert result['checked_volumes'][0]['free_gib']==.5


def test_missing_future_disk_path_checks_existing_volume_and_sufficient_space_passes(tmp_path):
    visited=[]
    def usage(path):visited.append(path);return SimpleNamespace(free=20*1024**3)
    result=check([tmp_path/'future'/'docker_data.vhdx'],usage=usage)
    assert result['allowed'] and visited==[tmp_path]


def test_zero_threshold_cannot_disable_safety(tmp_path):
    with pytest.raises(ValueError):check([tmp_path],minimum_gib=0)
