import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _extra in (REPO_ROOT / "tools" / "simulator", REPO_ROOT / "tools" / "vectorgen"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))


@pytest.fixture()
def sim_state(tmp_path: Path) -> Path:
    d = tmp_path / "phone"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def pc_config(tmp_path: Path) -> Path:
    d = tmp_path / "pc"
    d.mkdir(parents=True, exist_ok=True)
    return d
