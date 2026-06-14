"""
Pure, testable helpers for the Streamlit dashboard (NO streamlit import here, so they
can be unit-tested without launching the UI). Keep display logic that derives from
config/state in here rather than hardcoded in dashboard.py.
"""


def format_research_banner(rm: dict) -> str:
    """
    Caps line for the research-mode banner, derived from the ACTUAL research_mode config
    (fixes the old hardcoded 'positions 15 · crypto 60% · buy score ≥4' drift).
    """
    rm = rm or {}
    pos = rm.get("max_open_positions", "?")
    crypto = rm.get("max_crypto_exposure_pct", "?")
    score = rm.get("min_buy_score", "?")
    relaxed = []
    if rm.get("disable_loss_streak_lockout"):
        relaxed.append("lockout off")
    if rm.get("disable_validation_mode"):
        relaxed.append("validation caps off")
    if rm.get("relax_dedup"):
        relaxed.append("dedup relaxed")
    if rm.get("disable_force_autobuy") is False:
        relaxed.append("force-autobuy ON")
    relaxed_txt = " · ".join(relaxed) if relaxed else "standard caps"
    return f"{relaxed_txt} · positions {pos} · crypto {crypto}% · buy score ≥{score}"


def freshness_label(age_seconds, stale_after=120):
    """('🟢 live' | '🟡 stale' | '🔴 unavailable', color) for a data-fetch age in seconds.
    age_seconds None => unavailable."""
    if age_seconds is None:
        return ("🔴 unavailable", "#ff4d6d")
    if age_seconds <= stale_after:
        return ("🟢 live", "#00d4aa")
    if age_seconds <= stale_after * 5:
        return ("🟡 stale", "#f59e0b")
    return ("🔴 old", "#ff4d6d")
