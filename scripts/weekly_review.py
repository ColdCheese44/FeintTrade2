"""
Weekly review (Monday) — FeintTrade.

Runs and posts the SOP-mandated weekly deep-dive to Discord by chaining the three
read-only analytics engines that are otherwise on-demand only (!intel/!lab/!benchmark):

  1. intel_audit  — decision-intelligence audit (what the agent gets right/wrong)
  2. strategy_lab — evidence-based what-if recommendations to make the book profitable
  3. replay       — realized P&L vs buy-and-hold + no-trade baselines

Each section is isolated so one failure never blocks the others. These are analytics
over the trade log — they do not place trades. Cheap (≈ once/week).

Run:  python scripts/weekly_review.py            # run the analyses + post to Discord
      python scripts/weekly_review.py dry          # run the analyses, do NOT post
"""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# (module, compute callable [dry — runs the analysis, no Discord], post callable [live]).
# Dry mode actually EXERCISES the analysis so it catches analysis bugs, not just imports.
_SECTIONS = (
    ("intel_audit", "audit", "post"),
    ("strategy_lab", "format_report", "post"),
    ("replay", "benchmark_report", "post_report"),
)


def run(do_post: bool = True) -> dict:
    """Run each weekly-review section. do_post=True posts to Discord; do_post=False (dry)
    runs the analysis WITHOUT posting. Returns {module: 'ok' | 'failed: <err>'}. One section
    failing never blocks the others."""
    results = {}
    for mod_name, compute_fn, post_fn in _SECTIONS:
        fn_name = post_fn if do_post else compute_fn
        try:
            mod = importlib.import_module(mod_name)
            getattr(mod, fn_name)()
            results[mod_name] = "ok"
        except Exception as e:
            results[mod_name] = f"failed: {e}"
            print(f"weekly_review: {mod_name}.{fn_name} failed: {e}", file=sys.stderr)
    return results


if __name__ == "__main__":
    do_post = not (len(sys.argv) > 1 and sys.argv[1] == "dry")
    res = run(do_post=do_post)
    print("Weekly review:", ", ".join(f"{k}={v}" for k, v in res.items()))
    sys.exit(0 if all(v == "ok" for v in res.values()) else 1)
