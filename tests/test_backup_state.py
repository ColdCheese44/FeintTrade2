"""
Nightly state backup — FeintTrade.
Run: python -m pytest tests/test_backup_state.py -v

backup_state.make_backup() zips data/ + journal/ (the local-only, gitignored learning
history) and prunes to the most recent N. Pure local I/O — no network.
"""

import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import backup_state


def _seed(root: Path):
    (root / "data").mkdir()
    (root / "journal").mkdir()
    (root / "data" / "trade_log.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    (root / "data" / "performance.json").write_text("{}", encoding="utf-8")
    (root / "data" / "__pycache__").mkdir()
    (root / "data" / "__pycache__" / "x.pyc").write_text("junk", encoding="utf-8")
    (root / "data" / "cache").mkdir()
    (root / "data" / "cache" / "c.json").write_text("transient", encoding="utf-8")
    (root / "journal" / "2026-06-15.md").write_text("# notes", encoding="utf-8")


def test_make_backup_includes_state_excludes_caches(tmp_path):
    _seed(tmp_path)
    dest = backup_state.make_backup(root=tmp_path, keep=14)
    assert dest is not None and dest.exists()
    with zipfile.ZipFile(dest) as z:
        names = set(z.namelist())
    assert any(n.endswith("trade_log.jsonl") for n in names)
    assert any(n.endswith("2026-06-15.md") for n in names)
    assert not any("__pycache__" in n for n in names)       # caches excluded
    assert not any("cache" in n.split("/") for n in names)


def test_make_backup_empty_returns_none(tmp_path):
    # No data/ or journal/ -> nothing to back up, and no stray empty zip left behind.
    assert backup_state.make_backup(root=tmp_path, keep=14) is None
    assert not list((tmp_path / "backups").glob("*.zip")) if (tmp_path / "backups").exists() else True


def test_prune_keeps_only_n_most_recent(tmp_path):
    bd = tmp_path / "backups"
    bd.mkdir()
    for i in range(5):
        p = bd / f"state_backup_2026010{i}_000000.zip"
        p.write_text("z", encoding="utf-8")
        os.utime(p, (1000 + i, 1000 + i))               # distinct, increasing mtimes
    removed = backup_state.prune(bd, keep=2)
    remaining = sorted(bd.glob("state_backup_*.zip"))
    assert len(remaining) == 2 and len(removed) == 3
    # The two newest survive.
    assert (bd / "state_backup_20260104_000000.zip") in remaining
    assert (bd / "state_backup_20260103_000000.zip") in remaining


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
