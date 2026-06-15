"""
Nightly state backup — FeintTrade.

Zips the local-only, gitignored state that holds the system's *memory* — data/ (trade
log, performance, decision logs, peaks, session/daily state) and journal/ — into
backups/state_backup_<ts>.zip, then prunes to the most recent N. This is the compounding
edge: lose data/trade_log.jsonl and the learning loop forgets every trade. Pure local I/O
— no network, no API cost.

Run:  python scripts/backup_state.py            # make a backup, prune to KEEP
      python scripts/backup_state.py --list      # list existing backups
"""

import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIRNAME = "backups"
SOURCES = ("data", "journal")
KEEP = 14                       # retain ~2 weeks of nightly backups
_SKIP_PARTS = {"__pycache__", BACKUP_DIRNAME, "cache"}


def _iter_files(root: Path):
    """Yield backup-worthy files under the source dirs (skips caches and prior zips)."""
    for src in SOURCES:
        base = root / src
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if _SKIP_PARTS & set(p.parts):
                continue
            if p.suffix == ".zip":
                continue
            yield p


def make_backup(root: Path = ROOT, keep: int = KEEP) -> Path:
    """Create a timestamped zip of data/ + journal/ and prune to `keep` most recent.
    Returns the backup path. Returns None only if there was nothing to back up."""
    backup_dir = root / BACKUP_DIRNAME
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"state_backup_{stamp}.zip"
    count = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in _iter_files(root):
            z.write(p, p.relative_to(root))
            count += 1
    if count == 0:
        dest.unlink(missing_ok=True)
        return None
    prune(backup_dir, keep)
    return dest


def prune(backup_dir: Path, keep: int = KEEP) -> list:
    """Delete all but the `keep` most recent state_backup_*.zip. Returns removed paths."""
    backups = sorted(backup_dir.glob("state_backup_*.zip"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for old in backups[max(0, keep):]:
        try:
            old.unlink()
            removed.append(old)
        except OSError:
            pass
    return removed


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        bd = ROOT / BACKUP_DIRNAME
        for p in sorted(bd.glob("state_backup_*.zip")) if bd.exists() else []:
            print(f"{p.name}  {p.stat().st_size/1024:.0f} KB")
        sys.exit(0)
    out = make_backup()
    if out:
        print(f"State backup written: {out.relative_to(ROOT)} ({out.stat().st_size/1024:.0f} KB)")
        sys.exit(0)
    print("Nothing to back up (data/ and journal/ are empty or missing).")
    sys.exit(1)
