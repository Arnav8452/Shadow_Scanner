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
