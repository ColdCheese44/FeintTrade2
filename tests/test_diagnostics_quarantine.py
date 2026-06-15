"""
Diagnostics corrupt-JSON quarantine — FeintTrade.
Run: python -m pytest tests/test_diagnostics_quarantine.py -v

_check_data_integrity() self-heals a corrupt state file by resetting it to {}. It now
QUARANTINES the original bytes (data/quarantine/<name>.corrupt.<ts>) BEFORE resetting, so
forensic evidence is never silently destroyed, and only resets if the backup succeeded.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import diagnostics


def _setup(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(diagnostics, "DATA_DIR", data)
    monkeypatch.setattr(diagnostics, "ROOT", tmp_path)   # for backup.relative_to(ROOT)
    return data


def test_corrupt_json_quarantined_before_reset(tmp_path, monkeypatch):
    data = _setup(tmp_path, monkeypatch)
    corrupt = data / "open_trades.json"
    corrupt.write_text("{not valid json,,,", encoding="utf-8")

    r = diagnostics.Report()
    diagnostics._check_data_integrity(r, fix=True)

    # 1. File reset to empty object.
    assert corrupt.read_text(encoding="utf-8") == "{}"
    # 2. A timestamped quarantine copy preserves the ORIGINAL bytes verbatim.
    qfiles = list((data / "quarantine").glob("open_trades.json.corrupt.*"))
    assert len(qfiles) == 1
    assert qfiles[0].read_text(encoding="utf-8") == "{not valid json,,,"
    # 3. The fix is reported and references the quarantine path.
    assert any("quarantin" in m.lower() for m in r.fixed)


def test_check_only_does_not_reset_or_quarantine(tmp_path, monkeypatch):
    data = _setup(tmp_path, monkeypatch)
    corrupt = data / "performance.json"
    corrupt.write_text("xxx-not-json", encoding="utf-8")

    r = diagnostics.Report()
    diagnostics._check_data_integrity(r, fix=False)

    assert corrupt.read_text(encoding="utf-8") == "xxx-not-json"   # untouched
    assert any("corrupt" in m.lower() for m in r.fail)
    assert not (data / "quarantine").exists()


def test_valid_json_untouched(tmp_path, monkeypatch):
    data = _setup(tmp_path, monkeypatch)
    good = data / "open_trades.json"
    good.write_text('{"BTC/USD": {"qty": 1}}', encoding="utf-8")

    r = diagnostics.Report()
    diagnostics._check_data_integrity(r, fix=True)

    assert good.read_text(encoding="utf-8") == '{"BTC/USD": {"qty": 1}}'
    assert not (data / "quarantine").exists()
    assert any("valid" in m.lower() for m in r.ok)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
