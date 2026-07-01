"""
Run the test suite and post a per-test ✅/❌ report to #ft-dev-log.

This is the canonical "run tests + report" tool. Every run posts to #ft-dev-log:
  • a SUMMARY embed — one ✅/❌ line per test file (pass count), so the operator sees
    green/red at a glance even as the suite grows; failed tests are listed inline.
  • a FULL .md attachment — every test grouped by file with a ✅/❌/⏭️ next to it (the
    complete `pytest -v` checklist). The embed alone can't hold a large suite (Discord
    caps embeds at 25 fields / 6000 chars), so the attachment is the source of truth.

Exit code mirrors pytest (0 = all passed).

    python scripts/test_report.py            # run suite + post to #ft-dev-log
    python scripts/test_report.py --no-post   # run + print only

Also exposed via the !tests Discord command and used by diagnostics/maintenance tasks.
"""

import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_LINE = re.compile(r"^(tests[/\\][\w./\\-]+\.py)::([\w\[\].:/-]+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)")
_ICON = {"PASSED": "✅", "FAILED": "❌", "ERROR": "❌", "SKIPPED": "⏭️", "XFAIL": "⚠️", "XPASS": "⚠️"}


def run_tests() -> tuple[str, int]:
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-v", "-p", "no:cacheprovider", "--no-header"],
        capture_output=True, text=True, cwd=ROOT, timeout=900)
    return (r.stdout or "") + "\n" + (r.stderr or ""), r.returncode


def parse(output: str) -> dict:
    """{filename: [(test_name, status), ...]} from pytest -v output."""
    files: dict = defaultdict(list)
    for raw in output.splitlines():
        m = _LINE.match(raw.strip())
        if m:
            fname = m.group(1).replace("\\", "/").split("/")[-1]
            files[fname].append((m.group(2), m.group(3)))
    return dict(files)


def _tally(files: dict) -> tuple[int, int, int]:
    total = sum(len(v) for v in files.values())
    passed = sum(1 for v in files.values() for _, s in v if s == "PASSED")
    failed = sum(1 for v in files.values() for _, s in v if s in ("FAILED", "ERROR"))
    return total, passed, failed


def build_embed(files: dict) -> tuple[dict, bool, int, int, int]:
    """Compact SUMMARY embed: one ✅/❌ line per file (+ inline failed-test list).
    Stays under Discord's embed caps regardless of suite size — the full per-test
    checklist rides along as the .md attachment built by build_full_report()."""
    total, passed, failed = _tally(files)
    ok = failed == 0 and total > 0
    summary, fails = [], []
    for fname in sorted(files):
        tests = files[fname]
        fp = sum(1 for _, s in tests if s == "PASSED")
        summary.append(f"{'✅' if fp == len(tests) else '❌'} {fname} ({fp}/{len(tests)})")
        fails += [f"❌ {fname}::{t}" for t, s in tests if s in ("FAILED", "ERROR")]
    desc = "\n".join(summary) or "_no tests collected_"
    if fails:
        desc += "\n\n**Failed:**\n" + "\n".join(fails)
    title = f"{'✅' if ok else '❌'} Test Run — {passed}/{total} passed"
    if failed:
        title += f"  ·  {failed} FAILED"
    embed = {"title": title, "color": 0x2ecc71 if ok else 0xe74c3c,
             "description": desc[:4096], "footer": {"text": f"{total} tests · pytest -v"}}
    return embed, ok, total, passed, failed


def build_full_report(files: dict, raw_output: str = "") -> str:
    """The complete per-test checklist (Markdown) for the .md attachment — every test
    grouped by file with its icon, mirroring the inline `pytest -v` view. When there
    are failures (or nothing parsed), the raw pytest tail is appended for debugging."""
    total, passed, failed = _tally(files)
    ok = failed == 0 and total > 0
    out = [f"{'✅' if ok else '❌'} Test Run — {passed}/{total} passed"
           + (f"  ·  {failed} FAILED" if failed else ""), ""]
    for fname in sorted(files):
        tests = files[fname]
        fp = sum(1 for _, s in tests if s == "PASSED")
        out.append(f"{'✅' if fp == len(tests) else '❌'} {fname} ({fp}/{len(tests)})")
        out += [f"{_ICON.get(s, '❓')} {t}" for t, s in tests]
        out.append("")
    out.append(f"{total} tests · pytest -v")
    if (failed or total == 0) and raw_output:
        out += ["", "--- raw pytest tail ---", *raw_output.strip().splitlines()[-40:]]
    return "\n".join(out)


def post_report(do_post: bool = True) -> int:
    output, _code = run_tests()
    files = parse(output)
    embed, ok, total, passed, failed = build_embed(files)
    full = build_full_report(files, output)
    fname = f"test_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    delivered = None
    if do_post:
        try:
            import discord_channels as dch
            # Summary embed + full per-test checklist attached (scales past embed caps).
            delivered = dch.post_file("dev_log", fname, full, embed=embed)
            if not delivered:                       # last-ditch: at least get the summary out
                delivered = dch.post("dev_log", embed=embed)
        except Exception as e:
            print(f"post failed: {e}")
        try:
            import activity
            activity.log("test_run", f"{passed}/{total} passed, {failed} failed",
                         delivered=delivered, ok=ok)
        except Exception:
            pass
    print(f"{passed}/{total} passed, {failed} failed" + (f" · posted={delivered}" if do_post else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(post_report(do_post="--no-post" not in sys.argv))
