"""
Weekly review (Monday) — FeintTrade.

Runs and posts the SOP-mandated weekly deep-dive to Discord by chaining the three
read-only analytics engines that are otherwise on-demand only (!intel/!lab/!benchmark):

  1. intel_audit  — decision-intelligence audit (what the agent gets right/wrong)
  2. strategy_lab — evidence-based what-if recommendations to make the book profitable
  3. replay       — realized P&L vs buy-and-hold + no-trade baselines

Each section is isolated so one failure never blocks the others. These are analytics
over the trade log — they do not place trades. Cheap (≈ once/week).

Run:  python scripts/weekly_review.py            # run + post to Discord
      python scripts/weekly_review.py dry          # run, do NOT post (smoke test)
"""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# (module name, name of its Discord-posting callable)
_SECTIONS = (
    ("intel_audit", "post"),
    ("strategy_lab", "post"),
    ("replay", "post_report"),
)


def run(do_post: bool = True) -> dict:
    """Run each weekly-review section. Returns {module: 'ok' | 'failed: <err>'}."""
    results = {}
    for mod_name, fn_name in _SECTIONS:
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name)
            if do_post:
                fn()
            results[mod_name] = "ok"
        except Exception as e:                       # one section failing must not block the rest
            results[mod_name] = f"failed: {e}"
            print(f"weekly_review: {mod_name}.{fn_name} failed: {e}", file=sys.stderr)
    return results


if __name__ == "__main__":
    do_post = not (len(sys.argv) > 1 and sys.argv[1] == "dry")
    res = run(do_post=do_post)
    print("Weekly review:", ", ".join(f"{k}={v}" for k, v in res.items()))
    sys.exit(0 if all(v == "ok" for v in res.values()) else 1)
