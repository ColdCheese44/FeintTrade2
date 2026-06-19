"""Shared pytest fixtures for the FeintTrade suite."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


@pytest.fixture(autouse=True)
def _hermetic_no_live_orders(monkeypatch):
    """Keep the suite hermetic. validate_order()'s crypto path otherwise calls
    trade._pending_crypto_notional(), which hits the LIVE Alpaca /v2/orders endpoint —
    making tests slow (retry/backoff on a flaky network), offline-fragile, and
    non-deterministic (real pending crypto orders would skew the projected-exposure
    assertions). Pin it to 0 so crypto-exposure tests depend only on the positions they
    pass in. Harmless for tests that never touch trade.
    """
    try:
        import trade
        monkeypatch.setattr(trade, "_pending_crypto_notional", lambda: 0.0)
        # equities_open_now() hits the live Alpaca /v2/clock. Pin it OPEN so equity
        # exit/order paths behave as the suite assumes (it already pins market_phase to
        # REGULAR where needed). A dedicated test overrides this to exercise the closed gate.
        monkeypatch.setattr(trade, "equities_open_now", lambda *a, **k: True)
    except Exception:
        pass
