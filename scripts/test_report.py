"""
Run the test suite and post a per-test ✅/❌ report to #ft-dev-log.

This is the canonical "run tests + report" tool: every run posts a card listing every
test grouped by file with a ✅ (passed) / ❌ (failed) / ⏭️ (skipped) next to it, so the
operator sees green/red at a glance. Exit code mirrors pytest (0 = all passed).

    python scripts/test_report.py            # run suite + post to #ft-dev-log
    python scripts/test_report.py --no-post   # run + print only

Also exposed via the !tests Discord command and used by diagnostics.
"""

import re
import subprocess
import sys
from collections import defaultdict
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


def build_embed(files: dict) -> tuple[dict, bool, int, int, int]:
    total = sum(len(v) for v in files.values())
    passed = sum(1 for v in files.values() for _, s in v if s == "PASSED")
    failed = sum(1 for v in files.values() for _, s in v if s in ("FAILED", "ERROR"))
    ok = failed == 0 and total > 0
    fields = []
    for fname in sorted(files):
        tests = files[fname]
        fp = sum(1 for _, s in tests if s == "PASSED")
        lines = [f"{_ICON.get(s, '❓')} {t}" for t, s in tests]
        val = "\n".join(lines)
        if len(val) > 1024:
            val = val[:1000] + "\n…(truncated)"
        all_pass = fp == len(tests)
        fields.append({"name": f"{'✅' if all_pass else '❌'} {fname} ({fp}/{len(tests)})",
                       "value": val or "—", "inline": False})
    title = f"{'✅' if ok else '❌'} Test Run — {passed}/{total} passed"
    if failed:
        title += f"  ·  {failed} FAILED"
    embed = {"title": title, "color": 0x2ecc71 if ok else 0xe74c3c,
             "fields": fields[:25], "footer": {"text": f"{total} tests · pytest -v"}}
    return embed, ok, total, passed, failed


def post_report(do_post: bool = True) -> int:
    output, _code = run_tests()
    files = parse(output)
    embed, ok, total, passed, failed = build_embed(files)
    delivered = None
    if do_post:
        try:
            import discord_channels as dch
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
