"""Regression tests for issues found in the 9 Nov 2026 system review."""

import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from engine.core.calendar import TradingCalendar
from engine.core.config import ConfigError, load_settings, sebi_ops_threshold
from engine.core.rules import RuleBook
from engine.sidecars.event_arb.report import _safe_url
from engine.sidecars.event_arb.sources import EventFileError, event_from_mapping


@pytest.fixture
def cfg_root(tmp_path: Path, repo_root: Path) -> Path:
    shutil.copytree(repo_root / "config", tmp_path / "config")
    return tmp_path


def _patch(path: Path, section: str, key: str, value: object) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc[section][key] = value
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")


def test_ops_threshold_comes_from_rules(repo_root: Path, cfg_root: Path) -> None:
    assert sebi_ops_threshold(repo_root, date(2026, 11, 9)) == 10
    # Lower the threshold in the table → the same config must now be rejected.
    p = cfg_root / "config" / "rules" / "sebi_algo_rules.csv"
    p.write_text(p.read_text(encoding="utf-8").replace(",10,orders", ",5,orders"), encoding="utf-8")
    with pytest.raises(ConfigError, match="< 5"):
        load_settings(root=cfg_root, environ={}, today=date(2026, 11, 9))


@pytest.mark.parametrize(
    ("key", "value", "msg"),
    [
        ("directional_no_entry_after", "2pm", "HH:MM"),
        ("cooloff_after_loss_min", -5, ">= 0"),
        ("daily_loss_limit_pct", 9.0, "cannot exceed"),
    ],
)
def test_risk_validation(cfg_root: Path, key: str, value: object, msg: str) -> None:
    _patch(cfg_root / "config" / "settings.yaml", "risk", key, value)
    with pytest.raises(ConfigError, match=msg):
        load_settings(root=cfg_root, environ={}, today=date(2026, 11, 9))


def test_muhurat_special_session_from_table(repo_root: Path) -> None:
    cal = TradingCalendar.from_rulebook(RuleBook.load(repo_root / "config" / "rules"))
    assert cal.is_trading_day(date(2026, 11, 8))  # Sunday Muhurat session (NSE/CMTR/71775)
    assert not cal.is_trading_day(date(2026, 11, 15))  # ordinary Sunday


def test_holidays_are_primary(repo_root: Path) -> None:
    book = RuleBook.load(repo_root / "config" / "rules")
    assert all(r.status.value == "primary" for r in book["holidays"])


def test_integer_fields_reject_fractions() -> None:
    rec = {
        "id": "x",
        "type": "tender_buyback",
        "symbol": "X",
        "company": "X",
        "announced_on": "2026-11-01",
        "compiled_on": "2026-11-09",
        "filings": [{"url": "https://example.org"}],
        "terms": {"price": 10, "shares": "1,000.5"},
    }
    with pytest.raises(EventFileError, match="integer"):
        event_from_mapping(rec)
    rec["terms"] = {"price": 10, "shares": "11,59,199"}  # Indian digit grouping
    assert event_from_mapping(rec).terms.shares == 1_159_199  # type: ignore[union-attr]


def test_report_drops_non_http_links() -> None:
    assert _safe_url("javascript:alert(1)") == "#"
    assert _safe_url("https://x.org/a?b=1&c=2") == "https://x.org/a?b=1&amp;c=2"


def test_backup_copies_new_files_and_never_deletes(tmp_path: Path) -> None:
    from engine.data.backup import run_backup

    root, target = tmp_path / "repo", tmp_path / "bk"
    (root / "data" / "state").mkdir(parents=True)
    (root / "data" / "raw").mkdir(parents=True)
    (root / "data" / "state" / "j.jsonl").write_text("a\n", encoding="utf-8")
    (root / "data" / "raw" / "big.zip").write_text("x", encoding="utf-8")
    r1 = run_backup(root, ["data/state"], target, min_free_gb=0)
    assert r1.copied == 1 and (target / "data" / "state" / "j.jsonl").exists()
    assert not (target / "data" / "raw").exists()  # re-downloadable data is skipped
    r2 = run_backup(root, ["data/state"], target, min_free_gb=0)
    assert r2.copied == 0 and r2.skipped == 1  # unchanged: not copied again
    (root / "data" / "state" / "j.jsonl").unlink()  # an accidental delete on the source...
    run_backup(root, ["data/state"], target, min_free_gb=0)
    assert (target / "data" / "state" / "j.jsonl").exists()  # ...does not reach the backup


def test_audit_flags_rule_breaches_in_journals(tmp_path: Path) -> None:
    import json as _json

    from engine.cockpit.audit import audit_journals

    def w(name: str, recs: list[dict]) -> None:
        (tmp_path / f"paper_{name}.jsonl").write_text(
            "".join(_json.dumps(r) + "\n" for r in recs), encoding="utf-8"
        )

    w("breakthrough", [
        {"type": "open", "t": "2026-11-13T09:37:11+05:30", "symbol": "NSE_FO|1", "qty": 65,
         "price": 204.0, "stop": 163.0},
        {"type": "close", "t": "2026-11-13T10:34:58+05:30", "symbol": "NSE_FO|1",
         "opened_at": "2026-11-13T09:37:11+05:30", "gross_inr": -2700.0, "charges_inr": 101.0,
         "net_inr": -2801.0, "reason": "stop on premium"},
    ])  # fmt: skip
    w("oi_credit", [
        {"type": "open", "t": "2026-11-13T09:20:00+05:30", "id": "X", "qty": 65, "max_loss_inr": 12000,
         "legs": [{"side": "SELL", "kind": "PE", "strike": 25000.0, "qty": 65},
                  {"side": "BUY", "kind": "PE", "strike": 24800.0, "qty": 65}]},
    ])  # fmt: skip
    finds, stats = audit_journals(tmp_path, capital_inr=200000, directional_pct=2.5,
                                  spread_pct=5, daily_loss_pct=2, max_trades=5, max_lots=2,
                                  lot_of=lambda s: 65)  # fmt: skip
    rules = sorted({f.rule for f in finds})
    assert "R8 scalp discipline" in rules  # a losing scalp held 58 min
    assert "R2 per-trade risk" in rules  # a Rs 12,000 spread max loss > the Rs 10,000 limit
    assert "R4 entry window" in rules  # the 09:20 spread entry
    assert "R1 defined risk" in rules  # the short was filled before its wing
    assert stats["opens"] == 2 and stats["closes"] == 1


def test_band_widths_from_minute_candles_match_the_live_tape() -> None:
    from engine.cockpit.backfill import widths_from_candles

    candles = [[f"2026-11-12T{9 + (15 + i) // 60:02d}:{(15 + i) % 60:02d}:00+05:30",
                25000 + i, 25001 + i, 24999 + i, 25000.5 + i, 0, 0] for i in range(120)]  # fmt: skip
    ws = widths_from_candles("NIFTY", candles)
    assert len(ws) > 10 and all(w > 0 for w in ws)
