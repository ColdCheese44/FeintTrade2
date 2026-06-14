"""
Self-diagnostic & auto-heal — FeintTrade.

Runs a full health sweep, auto-fixes what is safe to fix, and posts a report to
Discord. Designed to run on a schedule (e.g. before the open and midday) so the
system repairs itself instead of silently drifting.

Checks:
  • .env keys present
  • Alpaca connectivity (account + clock) and crypto-trading enabled
  • data/ integrity (open_trades.json / performance.json valid JSON)
  • Learning reconciliation (tracked symbols no longer held -> log exits)
  • Risk posture (crypto-concentration cap, cash reserve, daily drawdown)
  • Kill switch state
  • Recent agent.log error rate
  • Stale bot.pid / duplicate bot processes

Auto-fixes (safe only): repair corrupt JSON state, reconcile the learning log,
refresh the performance cache, remove a stale bot.pid. Risk breaches are flagged
loudly (the agent must unwind positions — diagnostics never trades).

CLI:
  python scripts/diagnostics.py run        # check + auto-fix + Discord report
  python scripts/diagnostics.py check       # check only, no fixes, no Discord
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT / "scripts"))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from common import now_mt_str, today_mt, normalize_positions, is_crypto, load_risk  # noqa: E402

try:
    import discord_notify as dn
except Exception:
    dn = None
try:
    import learning
except Exception:
    learning = None

BASE_URL = os.getenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")
HEADERS = {"APCA-API-KEY-ID": os.getenv("APCA_API_KEY_ID"),
           "APCA-API-SECRET-KEY": os.getenv("APCA_API_SECRET_KEY")}
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

REQUIRED_ENV = ["APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "ANTHROPIC_API_KEY"]
OPTIONAL_ENV = ["NEWSAPI_API_KEY", "FRED_API_KEY", "FINNHUB_API_KEY",
                "DISCORD_WEBHOOK_URL", "DISCORD_BOT_TOKEN"]


class Report:
    def __init__(self):
        self.ok, self.warn, self.fail, self.fixed = [], [], [], []
    def good(self, m): self.ok.append(m)
    def warning(self, m): self.warn.append(m)
    def error(self, m): self.fail.append(m)
    def fix(self, m): self.fixed.append(m)
    def healthy(self): return not self.fail


def _check_env(r):
    missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
    if missing:
        r.error(f"Missing required .env keys: {', '.join(missing)}")
    else:
        r.good("All required .env keys present")
    miss_opt = [k for k in OPTIONAL_ENV if not os.getenv(k)]
    if miss_opt:
        r.warning(f"Optional keys not set: {', '.join(miss_opt)}")


def _check_alpaca(r):
    try:
        acct = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS, timeout=10).json()
        eq = float(acct.get("equity", 0))
        r.good(f"Alpaca account reachable — equity ${eq:,.0f}, status {acct.get('status')}")
        if acct.get("crypto_status") != "ACTIVE":
            r.warning(f"Crypto trading status: {acct.get('crypto_status')}")
        if acct.get("trading_blocked") or acct.get("account_blocked"):
            r.error("Account is BLOCKED for trading")
        return acct
    except Exception as e:
        r.error(f"Alpaca account unreachable: {e}")
        return {}


def _check_positions_and_risk(r, acct, fix):
    try:
        positions = normalize_positions(
            requests.get(f"{BASE_URL}/v2/positions", headers=HEADERS, timeout=10).json())
    except Exception as e:
        r.error(f"Positions unreachable: {e}")
        return []
    risk = load_risk()
    eq = float(acct.get("equity", 0) or 0)
    cash = float(acct.get("cash", 0) or 0)
    if eq:
        crypto_mv = sum(float(p.get("market_value", 0) or 0)
                        for p in positions if is_crypto(p.get("symbol", ""), p.get("asset_class")))
        crypto_pct = crypto_mv / eq * 100
        cap = risk.get("max_crypto_exposure_pct", 40)
        if crypto_pct > cap + 0.5:
            r.error(f"Crypto exposure {crypto_pct:.0f}% exceeds {cap}% cap — agent must trim "
                    f"(sells now work post-fix). Diagnostics will not trade.")
        else:
            r.good(f"Crypto exposure {crypto_pct:.0f}% within {cap}% cap")
        cash_pct = cash / eq * 100
        if cash_pct < risk.get("cash_reserve_pct", 5):
            r.warning(f"Cash reserve {cash_pct:.1f}% below {risk.get('cash_reserve_pct')}% target")
        n = len(positions)
        if n > risk.get("max_open_positions", 8):
            r.warning(f"{n} open positions exceeds max {risk.get('max_open_positions')}")
    return positions


def _check_data_integrity(r, fix):
    DATA_DIR.mkdir(exist_ok=True)
    for name in ("open_trades.json", "performance.json"):
        p = DATA_DIR / name
        if not p.exists():
            continue
        try:
            json.loads(p.read_text(encoding="utf-8"))
            r.good(f"data/{name} valid")
        except Exception:
            if fix:
                p.write_text("{}", encoding="utf-8")
                r.fix(f"Repaired corrupt data/{name} (reset to empty)")
            else:
                r.error(f"data/{name} is corrupt JSON")


def _reconcile_learning(r, positions, fix):
    if not learning:
        return
    try:
        open_trades = learning._load_open_trades()
    except Exception:
        return
    held = {p["symbol"] for p in positions}
    stale = [s for s in open_trades if s not in held]
    if stale and fix:
        closed = learning.detect_and_log_exits(positions, "diagnostic_reconcile")
        if closed:
            r.fix(f"Reconciled learning log — recorded exits for: {', '.join(closed)}")
        try:
            learning.update_performance()
            r.fix("Refreshed performance.json cache")
        except Exception:
            pass
    elif stale:
        r.warning(f"Learning log has {len(stale)} tracked symbols no longer held (run --fix)")
    else:
        r.good("Learning log in sync with live positions")


def _check_kill(r):
    if (ROOT / "kill.flag").exists():
        r.warning("KILL SWITCH is ACTIVE — agent will not trade until `!resume`")
    else:
        r.good("Kill switch clear")


def _check_logs(r):
    log = ROOT / "agent.log"
    if not log.exists():
        return
    today = today_mt()
    errs = 0
    try:
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]:
            if line.startswith(today) and (" ERROR " in line or "FATAL" in line):
                errs += 1
    except Exception:
        return
    if errs > 25:
        r.error(f"{errs} ERROR-level log lines today — investigate agent.log")
    elif errs:
        r.warning(f"{errs} ERROR-level log lines today")
    else:
        r.good("No ERROR-level log lines today")


def _check_bot_pid(r, fix):
    pid_file = ROOT / "bot.pid"
    if not pid_file.exists():
        return
    if fix:
        try:
            pid_file.unlink()
            r.fix("Removed stale bot.pid (run_bot.bat manages the bot lifecycle now)")
        except Exception:
            pass


def _check_discord_bot_process(r):
    if not os.getenv("DISCORD_BOT_TOKEN"):
        return
    try:
        task_cmd = "(Get-ScheduledTask -TaskName 'Trading - Discord Bot' -ErrorAction SilentlyContinue).State"
        task_proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", task_cmd],
            capture_output=True,
            text=True,
            timeout=10,
        )
        task_state = (task_proc.stdout or "").strip()
        if task_state == "Running":
            r.good("Discord bot scheduled task running")
            return

        cmd = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'bot.py' } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            timeout=10,
        )
        pids = [line.strip() for line in proc.stdout.splitlines() if line.strip().isdigit()]
        if not pids:
            r.warning("Discord bot process is not running")
        elif len(pids) > 1:
            r.warning(f"Multiple Discord bot.py processes running: {', '.join(pids)}")
        else:
            r.good(f"Discord bot process running (PID {pids[0]})")
    except Exception as e:
        r.warning(f"Discord bot process check failed: {e}")


def _check_egress_ip(r):
    """Report the public egress IP so you can see — especially on HEADLESS runs — whether
    traffic is leaving via the VPN or the bare ISP connection. Informational only."""
    try:
        ip = requests.get("https://api.ipify.org", timeout=8).text.strip()
        r.good(f"Public egress IP: {ip or '(empty)'}")
    except Exception as e:
        r.warning(f"Egress IP check failed: {e}")


def run(fix=True, post=True):
    r = Report()
    _check_env(r)
    acct = _check_alpaca(r)
    _check_egress_ip(r)
    _check_data_integrity(r, fix)
    positions = _check_positions_and_risk(r, acct, fix) if acct else []
    _reconcile_learning(r, positions, fix)
    _check_kill(r)
    _check_logs(r)
    _check_bot_pid(r, fix)
    _check_discord_bot_process(r)

    lines = [f"# FeintTrade Diagnostics — {now_mt_str()}",
             f"Status: {'✅ HEALTHY' if r.healthy() else '❌ ISSUES FOUND'}", ""]
    if r.fail:
        lines += ["## ❌ Errors"] + [f"- {m}" for m in r.fail] + [""]
    if r.warn:
        lines += ["## ⚠️ Warnings"] + [f"- {m}" for m in r.warn] + [""]
    if r.fixed:
        lines += ["## 🔧 Auto-Fixed"] + [f"- {m}" for m in r.fixed] + [""]
    lines += ["## ✅ Healthy"] + [f"- {m}" for m in r.ok]
    text = "\n".join(lines)

    (REPORTS_DIR / f"diagnostics-{today_mt()}.md").write_text(text, encoding="utf-8")
    print(text)

    if post and dn:
        color = 0x2ecc71 if r.healthy() else (0xe74c3c if r.fail else 0xe67e22)
        desc = []
        if r.fail:  desc.append("**❌ " + " | ".join(r.fail[:4]) + "**")
        if r.fixed: desc.append("🔧 Fixed: " + "; ".join(r.fixed[:4]))
        if r.warn:  desc.append("⚠️ " + "; ".join(r.warn[:4]))
        if not desc: desc.append("All systems healthy. ✅")
        try:
            dn.send(f"🩺 Diagnostics — {'HEALTHY' if r.healthy() else 'ISSUES'}",
                    "\n".join(desc)[:4000], color=color)
        except Exception:
            pass
    return 0 if r.healthy() else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "check":
        sys.exit(run(fix=False, post=False))
    else:
        sys.exit(run(fix=True, post=True))
