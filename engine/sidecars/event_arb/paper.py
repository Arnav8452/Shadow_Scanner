"""Paper account for small-shareholder tender buybacks. PAPER ONLY: nothing here places orders.

The rules are the research trade test ``T_bb1``. It passed its pre-registered gate once tax was
set to the owner's 0% (private research log, 18 Nov 2026). They are restated here, and no
research code is imported:

- **entry:** the close of the last session strictly before the record date (T+1 settlement),
  plus a half-spread (5 bp if that day's turnover is at least ₹50 crore, else 15 bp). Only when
  that close is below the buyback price;
- **sizing:** two ₹50,000 slots, ``qty = floor(ticket / entry price)``. A new event is skipped
  when both slots are held. The whole holding is tendered;
- **exit:** on the first session on or after ``unaccepted_return_date``, once the post-buyback
  small-shareholder acceptance (``actual_small_acceptance_pct``) is entered in the events file:
  - accepted shares, ``round(qty × acceptance)``, are paid at the buyback price;
  - the returned rest is sold at that day's close less the half-spread;
- **costs:** three orders from ``config/rules`` via the injected charge function: the delivery
  buy; the tender, a corporate-action order (the broker's flat fee per order, charged even
  when nothing is accepted, plus STT and DP on the accepted shares); the market sale of the
  returned shares with DP. No tax (owner's basic exemption);
- **kill rules:** new entries pause after one event loses more than ``kill_event_loss_inr``,
  or after the cumulative net falls to ``-kill_cum_loss_inr``. A review line appears at
  ``review_after_events`` closed events.

Fills are EOD-estimated from the cash bhavcopy. The record-date holding value is checked
against the ₹2 lakh small-shareholder limit and flagged, never auto-fixed.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from engine.sidecars.event_arb.models import BuybackTerms, Event, EventType

ChargeFn = Callable[[str, float, bool], float]  # (buy|sell|tender, value ₹, DP?) -> ₹


class Calendar(Protocol):
    def previous_trading_day(self, d: date) -> date: ...


@dataclass(frozen=True)
class BuybackPaperConfig:
    enabled: bool = True
    start_date: date = date(2026, 11, 18)  # events whose entry day is earlier are ignored
    capital_inr: float = 100_000.0
    slots: int = 2
    ticket_inr: float = 50_000.0
    small_holder_cap_inr: float = 200_000.0
    liquid_turnover_inr: float = 500_000_000.0
    half_spread_liquid_bps: float = 5.0
    half_spread_illiquid_bps: float = 15.0
    broker: str = "zerodha"
    kill_event_loss_inr: float = 7_500.0
    kill_cum_loss_inr: float = 5_000.0
    review_after_events: int = 20


def buyback_paper_config(raw: Mapping[str, Any] | None) -> BuybackPaperConfig:
    raw = dict(raw or {})
    if "start_date" in raw and not isinstance(raw["start_date"], date):
        raw["start_date"] = date.fromisoformat(str(raw["start_date"]))
    known = BuybackPaperConfig.__dataclass_fields__
    return BuybackPaperConfig(**{k: v for k, v in raw.items() if k in known})


@dataclass(frozen=True)
class Quote:
    close: float
    turnover_inr: float


@dataclass
class Position:
    event_id: str
    symbol: str
    qty: int
    entry_day: str
    entry_px: float
    buy_charges: float
    record_date: str
    buyback_price: float
    half_spread_bps: float
    mark: float = 0.0
    record_value: float | None = None


@dataclass
class PaperState:
    positions: list[Position] = field(default_factory=list)
    done: list[str] = field(default_factory=list)  # event ids entered, skipped or closed
    closed_events: int = 0
    cum_net: float = 0.0
    paused: str = ""

    @classmethod
    def load(cls, path: Path) -> PaperState:
        if not path.is_file():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            positions=[Position(**p) for p in raw.get("positions", [])],
            done=list(raw.get("done", [])),
            closed_events=int(raw.get("closed_events", 0)),
            cum_net=float(raw.get("cum_net", 0.0)),
            paused=str(raw.get("paused", "")),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")
        tmp.replace(path)


def cum_date(record_date: date, cal: Calendar) -> date:
    """The last session strictly before the record date: the latest buy that settles in time."""
    return cal.previous_trading_day(record_date)


def _ts(day: date) -> str:
    return f"{day.isoformat()}T15:30:00+05:30"


def _hs(bps: float) -> float:
    return bps / 10_000.0


def step(
    day: date,
    events: Sequence[Event],
    quotes: Mapping[str, Quote],
    state: PaperState,
    cfg: BuybackPaperConfig,
    cal: Calendar,
    charges: ChargeFn,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Apply one session's closes: settle, check record-date value, mark, then enter."""
    lines: list[str] = []
    journal: list[dict[str, Any]] = []
    by_id = {e.id: e for e in events}

    # 1. settle positions whose return date has come and whose acceptance is published
    for p in list(state.positions):
        ev = by_id.get(p.event_id)
        terms = ev.terms if ev is not None else None
        if not isinstance(terms, BuybackTerms) or terms.unaccepted_return_date is None:
            continue
        if day < terms.unaccepted_return_date:
            continue
        acc = terms.actual_small_acceptance_pct
        if acc is None:
            lines.append(
                f"buyback {p.symbol}: return date {terms.unaccepted_return_date} passed; enter "
                f"actual_small_acceptance_pct in the events file to settle the paper trade"
            )
            continue
        q = quotes.get(p.symbol)
        if q is None or q.close <= 0:
            lines.append(f"buyback {p.symbol}: no close on {day}; settlement waits a session")
            continue
        q_acc = min(p.qty, max(0, round(p.qty * min(100.0, max(0.0, acc)) / 100.0)))
        q_ret = p.qty - q_acc
        ret_px = q.close * (1 - _hs(p.half_spread_bps))
        tender_val = q_acc * p.buyback_price
        ret_val = q_ret * ret_px
        # The tender is a corporate-action order: the broker's flat fee is charged per order,
        # accepted or not (non-refundable); STT and the DP debit apply to the accepted shares.
        ch = charges("tender", tender_val, q_acc > 0) + (
            charges("sell", ret_val, True) if q_ret else 0.0
        )
        net = tender_val + ret_val - p.qty * p.entry_px - p.buy_charges - ch
        state.positions.remove(p)
        state.closed_events += 1
        state.cum_net += net
        rec = {
            "type": "close", "t": _ts(day), "id": p.event_id, "strategy": "buyback",
            "symbol": p.symbol, "qty": p.qty, "entry_day": p.entry_day,
            "entry_price": round(p.entry_px, 4), "exit_day": day.isoformat(),
            "buyback_price": p.buyback_price, "acceptance_pct": acc, "q_acc": q_acc,
            "q_ret": q_ret, "return_price": round(ret_px, 4),
            "charges": round(p.buy_charges + ch, 2), "net": round(net, 2), "est": "EOD-estimated",
        }  # fmt: skip
        journal.append(rec)
        lines.append(
            f"buyback {p.symbol}: settled {q_acc} accepted at ₹{p.buyback_price:,.2f} and "
            f"{q_ret} sold at ₹{ret_px:,.2f}; net ₹{net:,.0f}"
        )
        reason = ""
        if -net > cfg.kill_event_loss_inr:
            reason = (
                f"event {p.event_id} lost ₹{-net:,.0f} > ₹{cfg.kill_event_loss_inr:,.0f}: "
                "owner review before new entries"
            )
        elif state.cum_net <= -cfg.kill_cum_loss_inr:
            reason = (
                f"cumulative net ₹{state.cum_net:,.0f} at or below "
                f"-₹{cfg.kill_cum_loss_inr:,.0f}: owner review before new entries"
            )
        if reason and not state.paused:
            state.paused = reason
            journal.append({"type": "kill", "t": _ts(day), "reason": reason})
            lines.append(f"buyback paper PAUSED: {reason}")
        if state.closed_events == cfg.review_after_events:
            lines.append(
                f"buyback paper: {cfg.review_after_events} events closed; promotion review due"
            )

    # 2. record-date value against the small-shareholder limit, then 3. marks
    for p in state.positions:
        q = quotes.get(p.symbol)
        if q is None or q.close <= 0:
            continue
        p.mark = round(q.close * (1 - _hs(p.half_spread_bps)), 4)
        if p.record_value is None and day >= date.fromisoformat(p.record_date):
            p.record_value = round(p.qty * q.close, 2)
            if p.record_value > cfg.small_holder_cap_inr:
                lines.append(
                    f"buyback {p.symbol}: record-date value ₹{p.record_value:,.0f} is over the "
                    "₹2 lakh small-shareholder limit; the reserved-category acceptance may not "
                    "apply"
                )

    # 4. entries at today's close
    for ev in events:
        terms = ev.terms
        if ev.type is not EventType.TENDER_BUYBACK or not isinstance(terms, BuybackTerms):
            continue
        if ev.id in state.done or terms.record_date is None:
            continue
        entry = cum_date(terms.record_date, cal)
        if entry < cfg.start_date or entry > day:
            continue

        def skip(reason: str, ev: Event = ev) -> None:
            state.done.append(ev.id)
            journal.append({"type": "skip", "t": _ts(day), "id": ev.id, "symbol": ev.symbol,
                            "reason": reason})  # fmt: skip
            lines.append(f"buyback {ev.symbol}: skipped ({reason})")

        if entry < day:
            skip(f"missed entry day {entry}")
            continue
        if state.paused:
            skip(f"paused: {state.paused}")
            continue
        q = quotes.get(ev.symbol)
        if q is None or q.close <= 0:
            skip("no close on the entry day")
            continue
        if q.close >= terms.price:
            skip(f"no discount: close ₹{q.close:,.2f} >= buyback ₹{terms.price:,.2f}")
            continue
        if len(state.positions) >= cfg.slots:
            skip(f"both {cfg.slots} slots held")
            continue
        bps = (
            cfg.half_spread_liquid_bps
            if q.turnover_inr >= cfg.liquid_turnover_inr
            else cfg.half_spread_illiquid_bps
        )
        px = q.close * (1 + _hs(bps))
        qty = math.floor(cfg.ticket_inr / px)
        if qty <= 0:
            skip(f"price ₹{px:,.2f} above the ₹{cfg.ticket_inr:,.0f} ticket")
            continue
        ch = charges("buy", qty * px, False)
        state.positions.append(
            Position(
                ev.id,
                ev.symbol,
                qty,
                day.isoformat(),
                px,
                ch,
                terms.record_date.isoformat(),
                terms.price,
                bps,
            )
        )
        state.done.append(ev.id)
        journal.append({
            "type": "fill", "t": _ts(day), "id": ev.id, "strategy": "buyback", "side": "buy",
            "symbol": ev.symbol, "qty": qty, "price": round(px, 4), "charges": round(ch, 2),
            "buyback_price": terms.price, "record_date": terms.record_date.isoformat(),
            "est": "EOD-estimated",
        })  # fmt: skip
        lines.append(
            f"buyback {ev.symbol}: bought {qty} at ₹{px:,.2f} (buyback ₹{terms.price:,.2f}, "
            f"record date {terms.record_date}); tender the whole holding"
        )
    return lines, journal


def quotes_from_cash_rows(rows: Sequence[Any], day: date) -> dict[str, Quote]:
    """``symbol -> Quote`` from the day's cash bhavcopy rows (EQ series preferred)."""
    pri = {"EQ": 3, "BE": 2, "SM": 1}
    best: dict[str, tuple[int, Quote]] = {}
    for r in rows:
        if r.trade_date != day or r.close <= 0:
            continue
        rank = pri.get(r.series, 0)
        if rank == 0:
            continue
        cur = best.get(r.symbol)
        if cur is None or rank > cur[0]:
            best[r.symbol] = (rank, Quote(float(r.close), float(r.turnover_inr)))
    return {k: v for k, (_, v) in best.items()}


def run_buyback_paper(
    *,
    day: date,
    raw_dir: Path,
    state_dir: Path,
    events_path: Path,
    book: Any,
    cal: Calendar,
    cfg: BuybackPaperConfig,
) -> list[str]:
    """The nightly paper step: reads the day's cash bhavcopy and the curated events file,
    appends to ``paper_buyback.jsonl`` and saves ``buyback_paper_state.json``."""
    if not cfg.enabled:
        return []
    from engine.backtest.costs import equity_delivery_charges
    from engine.data.eod.bhavcopy import parse_cash_bhavcopy_csv
    from engine.data.eod.download import eod_path, read_eod_text
    from engine.sidecars.event_arb.sources import load_events

    path = eod_path(raw_dir, "cm", day)
    if not path.exists():
        raise FileNotFoundError(f"cash bhavcopy for {day} not downloaded")
    quotes = quotes_from_cash_rows(parse_cash_bhavcopy_csv(read_eod_text(path)), day)
    events = load_events(events_path)

    def charges(side: str, value: float, dp: bool) -> float:
        return equity_delivery_charges(
            book, day, side="buy" if side == "buy" else "sell", value_inr=value,
            broker=cfg.broker, include_dp=dp, allow_unverified=True,
            corporate_action=side == "tender",
        ).total  # fmt: skip

    state_path = state_dir / "buyback_paper_state.json"
    state = PaperState.load(state_path)
    lines, journal = step(day, events, quotes, state, cfg, cal, charges)
    if journal:
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / "paper_buyback.jsonl").open("a", encoding="utf-8") as fh:
            for rec in journal:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    state.save(state_path)
    held = ", ".join(f"{p.symbol} x{p.qty}" for p in state.positions) or "none"
    lines.append(
        f"buyback paper: holding {held}; {state.closed_events} closed, net ₹{state.cum_net:,.0f}"
        + (f"; PAUSED ({state.paused})" if state.paused else "")
    )
    return lines


__all__ = [
    "BuybackPaperConfig",
    "ChargeFn",
    "PaperState",
    "Position",
    "Quote",
    "buyback_paper_config",
    "cum_date",
    "quotes_from_cash_rows",
    "run_buyback_paper",
    "step",
]
