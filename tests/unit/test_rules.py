from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from engine.core.rules import (
    RuleBook,
    RuleNotFoundError,
    RuleValidationError,
    Status,
    UnverifiedRuleError,
    load_csv_table,
    load_yaml_table,
    to_ist_date,
)

HEADER = "segment,value,unit,effective_from,effective_to,source_url,verified_on,status,notes\n"
URL = "https://example.org/circular"


def write(tmp_path: Path, body: str, name: str = "stt.csv", header: str = HEADER) -> Path:
    p = tmp_path / name
    p.write_text(header + body, encoding="utf-8")
    return p


# ---------- lookup by date ----------


def test_lookup_picks_row_in_force_and_boundaries_are_half_open(tmp_path: Path) -> None:
    t = load_csv_table(
        write(
            tmp_path,
            f"options,0.1,pct,2024-10-01,2026-04-01,{URL},2026-11-09,secondary,\n"
            f"options,0.15,pct,2026-04-01,,{URL},2026-11-09,secondary,\n",
        )
    )
    assert t.key_columns == ("segment",)
    assert t.lookup(date(2025, 6, 1), segment="options").as_float() == 0.1
    assert t.lookup(date(2026, 3, 31), segment="options").as_float() == 0.1
    assert t.lookup(date(2026, 4, 1), segment="options").as_float() == 0.15  # to is exclusive
    assert t.lookup(date(2030, 1, 1), segment="options").as_float() == 0.15  # open-ended
    with pytest.raises(RuleNotFoundError):
        t.lookup(date(2024, 9, 30), segment="options")


def test_lookup_datetime_uses_ist_calendar_date(tmp_path: Path) -> None:
    t = load_csv_table(
        write(
            tmp_path,
            f"options,0.1,pct,2024-10-01,2026-04-01,{URL},2026-11-09,secondary,\n"
            f"options,0.15,pct,2026-04-01,,{URL},2026-11-09,secondary,\n",
        )
    )
    # 2026-03-31 19:00 UTC is 2026-04-01 00:30 IST -> new rate
    assert t.lookup(datetime(2026, 3, 31, 19, 0, tzinfo=UTC), segment="options").value == "0.15"
    with pytest.raises(ValueError, match="timezone-aware"):
        to_ist_date(datetime(2026, 4, 1, 10, 0))  # noqa: DTZ001


def test_unknown_or_missing_keys_rejected(tmp_path: Path) -> None:
    t = load_csv_table(
        write(tmp_path, f"options,0.1,pct,2024-10-01,,{URL},2026-11-09,secondary,\n")
    )
    with pytest.raises(KeyError):
        t.lookup(date(2025, 1, 1), segment="options", side="buy")
    with pytest.raises(KeyError):
        t.lookup(date(2025, 1, 1))
    with pytest.raises(RuleNotFoundError):
        t.lookup(date(2025, 1, 1), segment="futures")


def test_unverified_refused_unless_allowed(tmp_path: Path) -> None:
    t = load_csv_table(write(tmp_path, "options,0.125,pct,2024-10-01,,,,UNVERIFIED,check\n"))
    with pytest.raises(UnverifiedRuleError, match="UNVERIFIED"):
        t.lookup(date(2025, 1, 1), segment="options")
    row = t.lookup(date(2025, 1, 1), segment="options", allow_unverified=True)
    assert row.status is Status.UNVERIFIED and not row.verified
    assert t.unverified() == [row]


def test_history_sorted(tmp_path: Path) -> None:
    t = load_csv_table(
        write(
            tmp_path,
            f"options,0.15,pct,2026-04-01,,{URL},2026-11-09,secondary,\n"
            f"options,0.1,pct,2024-10-01,2026-04-01,{URL},2026-11-09,secondary,\n",
        )
    )
    assert [r.value for r in t.history(segment="options")] == ["0.1", "0.15"]


# ---------- validation ----------


@pytest.mark.parametrize(
    ("body", "msg"),
    [
        (
            f"options,0.1,pct,2024-10-01,2026-04-01,{URL},2026-11-09,secondary,\n"
            f"options,0.15,pct,2026-03-01,,{URL},2026-11-09,secondary,\n",
            "overlapping",
        ),
        (
            f"options,0.1,pct,2024-10-01,,{URL},2026-11-09,secondary,\n"
            f"options,0.15,pct,2026-04-01,,{URL},2026-11-09,secondary,\n",
            "overlapping",
        ),
        (f"options,0.1,pct,2026-04-01,2026-04-01,{URL},2026-11-09,secondary,\n", "effective_to"),
        ("options,0.1,pct,2024-10-01,,,2026-11-09,secondary,\n", "source_url"),
        (f"options,0.1,pct,2024-10-01,,{URL},,primary,\n", "verified_on"),
        (f"options,0.1,pct,2024-10-01,,{URL},2026-11-09,maybe,\n", "status"),
        (f"options,0.1,pct,01/10/2024,,{URL},2026-11-09,secondary,\n", "YYYY-MM-DD"),
        (f"options,0.1,pct,,,{URL},2026-11-09,secondary,\n", "effective_from is required"),
        (f",0.1,pct,2024-10-01,,{URL},2026-11-09,secondary,\n", "empty key"),
    ],
)
def test_validation_errors(tmp_path: Path, body: str, msg: str) -> None:
    with pytest.raises(RuleValidationError, match=msg):
        load_csv_table(write(tmp_path, body))


def test_different_keys_may_share_dates(tmp_path: Path) -> None:
    t = load_csv_table(
        write(
            tmp_path,
            f"options,0.15,pct,2026-04-01,,{URL},2026-11-09,secondary,\n"
            f"futures,0.05,pct,2026-04-01,,{URL},2026-11-09,secondary,\n",
        )
    )
    assert len(t) == 2


def test_missing_standard_column(tmp_path: Path) -> None:
    with pytest.raises(RuleValidationError, match="missing column"):
        load_csv_table(write(tmp_path, "options,0.1\n", header="segment,value\n"))


def test_yaml_table(tmp_path: Path) -> None:
    p = tmp_path / "margin_rules.yaml"
    p.write_text(
        "rules:\n"
        "  - name: elm\n"
        "    params: {additional_elm_pct: 2.0}\n"
        "    effective_from: 2024-11-20\n"
        "    effective_to:\n"
        "    source_url: ''\n"
        "    verified_on:\n"
        "    status: UNVERIFIED\n"
        "    notes: x\n",
        encoding="utf-8",
    )
    t = load_yaml_table(p)
    row = t.lookup(date(2025, 1, 1), name="elm", allow_unverified=True)
    assert row.params["additional_elm_pct"] == 2.0


# ---------- the real config/rules directory ----------


def test_repo_rulebook_loads_and_validates(repo_root: Path) -> None:
    book = RuleBook.load(repo_root / "config" / "rules")
    expected = {
        "lot_sizes",
        "stt",
        "exchange_charges",
        "stamp_duty",
        "sebi_fee",
        "gst",
        "brokerage",
        "freeze_qty",
        "expiry_calendar",
        "holidays",
        "circuit_breakers",
        "margin_rules",
    }
    assert expected <= set(book.names)


def test_repo_known_values(repo_root: Path) -> None:
    """Values recorded in M0 (CLAUDE.md 'known as of 9 Nov 2026'); re-verify before trading."""
    book = RuleBook.load(repo_root / "config" / "rules")
    d = date(2026, 11, 9)
    assert book.lookup("stt", d, segment="options", leg="sell_premium").as_float() == 0.15
    assert book.lookup("stt", d, segment="options", leg="exercise_intrinsic").as_float() == 0.15
    assert book.lookup("stt", d, segment="futures", leg="sell").as_float() == 0.05
    assert book.lookup("stt", date(2026, 3, 31), segment="futures", leg="sell").as_float() == 0.02
    # Lot sizes are looked up by contract expiry
    assert (
        book.lookup("lot_sizes", date(2026, 1, 6), exchange="NSE", underlying="NIFTY").as_int()
        == 65
    )
    assert (
        book.lookup(
            "lot_sizes",
            date(2025, 12, 30),
            exchange="NSE",
            underlying="NIFTY",
            allow_unverified=True,
        ).as_int()
        == 75
    )
    assert book.lookup("freeze_qty", d, exchange="NSE", underlying="NIFTY").as_int() == 1800
    assert (
        book.lookup("expiry_calendar", d, exchange="NSE", underlying="NIFTY", kind="weekly").value
        == "TUE"
    )


def test_repo_freeze_limits_allow_at_least_one_lot(repo_root: Path) -> None:
    from engine.execution.slicer import max_order_qty

    book = RuleBook.load(repo_root / "config" / "rules")
    for row in book["freeze_qty"]:
        lot = book["lot_sizes"].lookup(row.effective_from, allow_unverified=True, **row.keys)
        q = max_order_qty(row.as_int(), lot.as_int())
        assert q % lot.as_int() == 0 and lot.as_int() <= q < row.as_int(), row


def test_csv_with_excel_bom(tmp_path: Path) -> None:
    p = tmp_path / "stt.csv"
    row = f"options,0.1,pct,2024-10-01,,{URL},2026-11-09,secondary,\n"
    p.write_bytes(("﻿" + HEADER + row).encode("utf-8"))  # what Excel writes
    t = load_csv_table(p)
    assert t.key_columns == ("segment",)
    assert t.lookup(date(2025, 1, 1), segment="options").value == "0.1"
