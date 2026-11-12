"""The evening job (after ~19:30 IST): EOD files -> delivery watchlist -> results dates -> filings.

Steps, each failing soft (a line in the report, never an exception for network trouble):

1. NSE end-of-day files for the trade date (F&O bhavcopy, delivery) plus any delivery files
   missing from the 20-session lookback, and the current-only ban list / bulk / block deals.
2. Delivery-spike watchlist for the next session (``engine.data.eod.delivery_watch``).
3. Board meetings for the next ``board_meeting_days`` -> ``CONSTITUENT_EARNINGS`` rows in the
   event calendar for index members (``engine.data.events.earnings``).
4. Quarterly results and shareholding (XBRL) for the watchlist stocks and the index heavyweights
   (``engine.data.fundamentals.store``).

Nothing here trades or touches the broker.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from engine.core.calendar import TradingCalendar
from engine.core.net import http_get_bytes
from engine.data.constituents.table import heavyweights, load_constituent_table, members_on
from engine.data.constituents.weights import WeightInput, derive_weights, write_derived_weights
from engine.data.eod.bhavcopy import DeliveryBias, parse_cash_bhavcopy_csv
from engine.data.eod.delivery_watch import (
    OptionLiquidity,
    SpikeThresholds,
    build_delivery_watchlist,
    missing_delivery_days,
)
from engine.data.eod.download import eod_path, fetch_eod, read_eod_text
from engine.data.eod.flows import CURRENT_EOD_URLS, fetch_current_eod
from engine.data.events.earnings import earnings_rows, merge_calendar
from engine.data.fundamentals.nse import board_meetings_url, fetch_list, parse_board_meetings
from engine.data.fundamentals.store import refresh_symbol, shareholding_as_of

log = logging.getLogger(__name__)
EOD_READY = time(19, 30)


@dataclass(frozen=True)
class EveningConfig:
    index: str = "NIFTY"
    constituents_file: Path = Path("config/reference/nifty50_constituents.csv")
    calendar_file: Path = Path("config/events/macro_calendar.csv")
    watchlist_dir: Path = Path("data/state/delivery_watchlist")
    watch_max_symbols: int = 5
    watch_biases: tuple[str, ...] = ("LONG_ACCUMULATION", "RETAIL_CHURN_FADE")
    heavyweights_top_n: int = 10
    periods: int = 5
    pace_s: float = 1.0
    board_meeting_days: int = 45
    lookback_sessions: int = 20
    spike: SpikeThresholds = field(default_factory=SpikeThresholds)
    option_liquidity: OptionLiquidity = field(default_factory=OptionLiquidity)
    history_sessions: int = 75  # delivery files kept complete for the swing scanner


def evening_config(raw: Mapping[str, Any], delivery: Any, root: Path) -> EveningConfig:
    f = raw.get("fundamentals") or {}
    return EveningConfig(
        index=str(f.get("index", "NIFTY")),
        constituents_file=root
        / str(f.get("constituents_file", "config/reference/nifty50_constituents.csv")),
        calendar_file=root / "config" / "events" / "macro_calendar.csv",
        watchlist_dir=Path(delivery.dir),
        watch_max_symbols=int(delivery.max_symbols),
        watch_biases=tuple(delivery.biases),
        heavyweights_top_n=int(f.get("heavyweights_top_n", 10)),
        periods=int(f.get("periods", 5)),
        pace_s=float(f.get("pace_s", 1.0)),
        board_meeting_days=int(f.get("board_meeting_days", 45)),
        spike=delivery.spike,
        option_liquidity=delivery.option_liquidity,
    )


@dataclass
class EveningReport:
    trade_date: date
    lines: list[str] = field(default_factory=list)
    errors: int = 0
    watchlist: list[str] = field(default_factory=list)
    earnings_rows: int = 0
    fundamentals: list[str] = field(default_factory=list)

    def add(self, line: str, *, error: bool = False) -> None:
        self.lines.append(line)
        self.errors += error
        (log.warning if error else log.info)("evening: %s", line)


def evening_trade_date(now: datetime, cal: TradingCalendar) -> date:
    """Today when it is a trading day and NSE's files are due (19:30 IST), else the last one."""
    today = now.date()
    if cal.is_trading_day(today) and now.time() >= EOD_READY:
        return today
    return cal.previous_trading_day(today)


def run_evening(
    cfg: EveningConfig,
    *,
    raw_dir: Path,
    parquet_dir: Path,
    now: datetime,
    calendar: TradingCalendar,
    trade_date: date | None = None,
    fetcher: Callable[[str], bytes] = http_get_bytes,
    sleep: Callable[[float], None] | None = None,
    steps: Sequence[str] = ("eod", "watchlist", "weights", "calendar", "swing", "fundamentals"),
    swing: Callable[[date], list[str]] | None = None,
) -> EveningReport:
    day = trade_date or evening_trade_date(now, calendar)
    rep = EveningReport(day)
    pause = sleep if sleep is not None else _sleep

    if "eod" in steps:
        days = [day, *missing_delivery_days(
            raw_dir, day, weekdays_back=max(cfg.lookback_sessions + 1, cfg.history_sessions),
            is_trading_day=calendar.is_trading_day,
        )]  # fmt: skip
        for d in dict.fromkeys(days):
            for ds in ("fo", "delivery") if d == day else ("delivery",):
                res = fetch_eod(ds, d, raw_dir=raw_dir, now=now, fetcher=fetcher)
                if res.status == "ok":
                    pause(0.4)
                if res.status not in ("ok", "cached"):
                    rep.add(f"eod {ds} {d}: {res.status} {res.detail}", error=res.status == "error")
        for ds in CURRENT_EOD_URLS:  # NSE keeps only the latest copy; files by its own date
            cur = fetch_current_eod(ds, raw_dir=raw_dir, now=now, fetcher=fetcher)
            line = f"{ds}: {cur.status} {cur.day} {cur.detail}".rstrip()
            rep.add(line, error=cur.status == "error")

    if "watchlist" in steps:
        try:
            payload = build_delivery_watchlist(
                raw_dir,
                cfg.watchlist_dir,
                day,
                now=now,
                max_symbols=cfg.watch_max_symbols,
                biases=[DeliveryBias(b) for b in cfg.watch_biases],
                lookback_days=cfg.lookback_sessions,
                spike=cfg.spike,
                liquidity=cfg.option_liquidity,
            )
            rep.watchlist = [i["symbol"] for i in payload["items"]]
            rep.add(
                f"delivery watchlist {day}: {', '.join(rep.watchlist) or 'none'} "
                f"({payload['sessions_used']} sessions; ban list for "
                f"{payload['ban_trade_date'] or 'n/a'} excluded {payload['excluded_for_ban']}; "
                f"thin options excluded {payload['excluded_thin_options']})"
            )
        except (FileNotFoundError, ValueError) as exc:
            rep.add(f"delivery watchlist: {exc}", error=True)

    if "weights" in steps:
        try:
            n, note = update_index_weights(
                cfg, raw_dir=raw_dir, parquet_dir=parquet_dir, day=day, now=now,
                fetcher=fetcher, pause=pause,
            )  # fmt: skip
            rep.add(f"index weights: {n} {cfg.index} member(s) derived; {note}")
        except (FileNotFoundError, ValueError) as exc:
            rep.add(f"index weights: {exc}", error=True)

    constituents = load_constituent_table(cfg.constituents_file)
    if "calendar" in steps:
        start = now.date()
        url = board_meetings_url(start, start + timedelta(days=cfg.board_meeting_days))
        lst = fetch_list(
            url,
            raw_dir / "fundamentals" / "lists" / "board_meetings" / f"{start.isoformat()}.json",
            now=now,
            fetcher=fetcher,
        )
        pause(cfg.pace_s)
        if lst.status == "ok" and lst.path is not None:
            meetings = parse_board_meetings(lst.path.read_text("utf-8"))
            rows = earnings_rows(meetings, constituents, index=cfg.index, on=start)
            rep.earnings_rows = merge_calendar(cfg.calendar_file, rows, today=start)
            rep.add(
                f"results dates: {len(rows)} {cfg.index} member(s) "
                f"({', '.join(r['symbol'] + ' ' + r['date'] for r in rows) or 'none'}); "
                f"{rep.earnings_rows} written to {cfg.calendar_file.name}"
            )
        else:
            rep.add(f"board meetings: {lst.detail}", error=True)

    if "swing" in steps and swing is not None:  # after "calendar": it saves the results dates
        try:
            for line in swing(day):
                rep.add(line)
        except (FileNotFoundError, ValueError) as exc:
            rep.add(f"swing: {exc}", error=True)

    if "fundamentals" in steps:
        top_rows = heavyweights(constituents, cfg.index, now.date(), cfg.heavyweights_top_n)
        top = [r.symbol for r in top_rows]
        symbols = list(dict.fromkeys([*rep.watchlist, *top]))
        for sym in symbols:
            fr = refresh_symbol(
                sym,
                raw_dir=raw_dir,
                parquet_dir=parquet_dir,
                now=now,
                n_periods=cfg.periods,
                fetcher=fetcher,
                pace_s=cfg.pace_s,
                sleep=pause,
            )
            rep.fundamentals.append(sym)
            msg = f"filings {sym}: +{fr.results_new} results, +{fr.shareholding_new} holdings"
            rep.add(msg + (f"; {'; '.join(fr.errors)}" if fr.errors else ""), error=bool(fr.errors))
    return rep


def update_index_weights(
    cfg: EveningConfig,
    *,
    raw_dir: Path,
    parquet_dir: Path,
    day: date,
    now: datetime,
    fetcher: Callable[[str], bytes] = http_get_bytes,
    pause: Callable[[float], None] = lambda s: None,
    max_shp_age_days: int = 120,
) -> tuple[int, str]:
    """Derive all members' weights (``engine.data.constituents.weights``) from ``day``'s close
    and the latest shareholding pattern known ``now``; shareholding is fetched only when
    missing or older than ``max_shp_age_days`` (patterns are quarterly)."""
    p = eod_path(raw_dir, "delivery", day)
    if not p.exists():
        raise FileNotFoundError(f"delivery file for {day} not downloaded")
    closes = {r.symbol: r.close for r in parse_cash_bhavcopy_csv(read_eod_text(p))}
    members = members_on(load_constituent_table(cfg.constituents_file), cfg.index, day)
    inputs: dict[str, WeightInput] = {}
    missing: list[str] = []
    fetched = 0
    for m in members:
        shp = shareholding_as_of(parquet_dir, m.symbol, now)
        if not shp or (day - shp[0]["as_on"]).days > max_shp_age_days:
            refresh_symbol(
                m.symbol, raw_dir=raw_dir, parquet_dir=parquet_dir, now=now, n_periods=1,
                fetcher=fetcher, pace_s=cfg.pace_s, sleep=pause, kinds=("shareholding",),
            )  # fmt: skip
            fetched += 1
            shp = shareholding_as_of(parquet_dir, m.symbol, now)
        close = closes.get(m.symbol)
        if not shp or not close or not shp[0]["total_shares"]:
            missing.append(m.symbol)
            continue
        inputs[m.symbol] = WeightInput(
            close=close,
            shares=float(shp[0]["total_shares"]),
            promoter_pct=shp[0]["promoter_pct"],
            shp_as_on=shp[0]["as_on"],
        )
    if missing:  # a partial set would inflate everyone else's weight
        raise ValueError(f"weights not derived; no close or shareholding for {missing}")
    weights = derive_weights(inputs)
    n = write_derived_weights(cfg.constituents_file, weights, index=cfg.index, price_date=day)
    top = sorted(weights.values(), key=lambda w: -w.weight_pct)[:3]
    note = f"{fetched} pattern(s) fetched; top: " + ", ".join(
        f"{w.symbol} {w.weight_pct:.2f}%" for w in top
    )
    return n, note


def _sleep(seconds: float) -> None:
    import time as _t

    if seconds > 0:
        _t.sleep(seconds)
