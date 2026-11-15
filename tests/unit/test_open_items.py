"""Open-items checklist scanner."""

from __future__ import annotations

from pathlib import Path

from engine.core.open_items import collect, render, scan_decisions, scan_rules, scan_settings

DECISIONS = """# Decisions log

## Feature A (14 Nov 2026)

- **Built:** something.
- **Open:** sessions are counted as weekdays.
- **Open setups close at the bell.** Not an open item, just a phrase.

## Feature B (13 Nov 2026)

**Open questions for the owner**
1. Trend sleeve allocation (20%).
2. Should cash swings count against the margin cap? Today it limits
   the book to two names.

---

## Feature C (12 Nov 2026)

**Open:**
- The screener's volume ratio includes expiry days.
  - nested detail line
- The page's extra feed slots are not restored.

**Decision:** next paragraph ends the block.
- **Open (owner):** a per-index cap would change a risk limit.
"""


def test_scan_decisions_finds_every_open_form() -> None:
    items = scan_decisions(DECISIONS)
    texts = [x.text for x in items]
    assert texts == [
        "sessions are counted as weekdays.",
        "Trend sleeve allocation (20%).",
        "Should cash swings count against the margin cap? Today it limits the book to two names.",
        "The screener's volume ratio includes expiry days. nested detail line",
        "The page's extra feed slots are not restored.",
        "a per-index cap would change a risk limit.",
    ]
    owners = {x.text[:20]: x.owner for x in items}
    assert owners["sessions are counted"] == "claude"
    assert owners["Trend sleeve allocat"] == "owner"  # "for the owner" block
    assert owners["a per-index cap woul"] == "owner"  # (owner) tag
    assert items[0].source.endswith("Feature A (14 Nov 2026)")
    assert len({x.id for x in items}) == len(items)


def test_rules_settings_and_resolution(tmp_path: Path) -> None:
    rules = tmp_path / "config" / "rules"
    rules.mkdir(parents=True)
    (rules / "brokerage.csv").write_text(
        "broker,segment,component,value,unit,effective_from,effective_to,source_url,"
        "verified_on,status,notes\n"
        "fyers,options,flat_per_order,20,inr,2024-10-01,,,,UNVERIFIED,Verify on fyers.in\n"
        "upstox,options,flat_per_order,20,inr,2024-10-01,,https://x,2026-11-09,secondary,\n",
        encoding="utf-8",
    )
    (rules / "holidays.csv").write_text("a,status\n1,primary\n", encoding="utf-8")
    (tmp_path / "config" / "settings.yaml").write_text(
        "risk:\n  daily_loss_pct: 2   # PROPOSED\n  other: 1\n", encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "DECISIONS.md").write_text(DECISIONS, encoding="utf-8")

    r = scan_rules(rules)
    assert len(r) == 1 and r[0].owner == "owner" and "fyers" in r[0].text
    assert "Verify on fyers.in" in r[0].text and r[0].source == "config/rules/brokerage.csv:2"
    s = scan_settings(tmp_path / "config" / "settings.yaml")
    assert len(s) == 1 and s[0].owner == "owner" and "daily_loss_pct" in s[0].text

    items, resolved = collect(tmp_path)
    assert resolved == 0 and len(items) == 8
    done = items[0].id
    (tmp_path / "config" / "open_items_resolved.csv").write_text(
        f"id,resolved_on,how\n{done},2026-11-15,checked\n", encoding="utf-8"
    )
    items2, resolved2 = collect(tmp_path)
    assert resolved2 == 1 and len(items2) == 7 and done not in {x.id for x in items2}

    text = render(items2, resolved2, generated="15 Nov 2026 20:00")
    assert "## Needs the owner" in text and "## Needs Claude" in text and "1 resolved" in text
