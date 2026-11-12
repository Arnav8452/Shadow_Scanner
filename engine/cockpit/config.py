"""Cockpit settings (``config/settings.yaml`` -> ``cockpit``)."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.cockpit.condor import CondorConfig
from engine.cockpit.setups import SetupConfig
from engine.cockpit.strategies import DebitConfig, GammaConfig
from engine.core.config import Settings


@dataclass(frozen=True)
class BotPaperConfig:
    enabled: bool = True
    min_score: float = 80.0  # Track B takes A+ signals only
    option_stop_pct: float = 25.0  # HYPOTHESIS: paper stop on premium, needed to size risk


@dataclass(frozen=True)
class DirectionalConfig:
    """SPEC 5.2 directional options, with order flow as the entry confirmation (HYPOTHESIS)."""

    enabled: bool = True
    min_score: float = 80.0
    first_entry: str = "09:30"
    last_entry: str = "14:00"
    max_entries_per_day: int = 2  # one idea, plus one same-direction re-entry after a stop
    stop_pct: float = 10.0
    target_pct: float = 30.0
    trail: tuple[tuple[float, float], ...] = ((15.0, 7.0), (25.0, 12.0))
    underlyings: tuple[str, ...] = ("NIFTY", "SENSEX")


@dataclass(frozen=True)
class CockpitConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    gateway_port: int = 8765
    engine_port: int = 8766
    replay_port: int = 8767
    publish_interval_ms: int = 200
    analytics_interval_s: float = 1.0
    chain_interval_s: float = 60.0
    events_kept: int = 200
    bot: BotPaperConfig = BotPaperConfig()
    directional: DirectionalConfig = DirectionalConfig()
    setups: SetupConfig = SetupConfig()  # noqa: RUF009 (frozen, immutable default)
    condor: CondorConfig = CondorConfig()  # noqa: RUF009 (frozen, immutable default)
    debit: DebitConfig = DebitConfig()  # noqa: RUF009 (frozen, immutable default)
    gamma: GammaConfig = GammaConfig()  # noqa: RUF009 (frozen, immutable default)
    user_pins_max: int = 3
    # execution.disconnect_kill_after_s (APPROVED): no fresh quote for this long in market hours
    # freezes new paper entries and pauses exits (never flatten on stale prices).
    feed_stale_s: float = 10.0
    manual_default_stop_pct: float = 25.0
    key_file: Path = Path("data/state/cockpit.key")
    journal_dir: Path = Path("data/state")
    replay_dir: Path = Path("data/replay")
    events_file: Path = Path("config/events/macro_calendar.csv")
    block_entries_in_event_window: bool = True


def load_cockpit_config(settings: Settings, root: Path) -> CockpitConfig:
    raw: Any = settings.raw.get("cockpit")
    if not isinstance(raw, Mapping):
        return CockpitConfig(
            key_file=root / "data" / "state" / "cockpit.key",
            journal_dir=root / "data" / "state",
            replay_dir=root / "data" / "replay",
            events_file=root / "config" / "events" / "macro_calendar.csv",
        )
    bot_raw: Any = raw.get("bot", {}) or {}
    dir_raw: Any = raw.get("directional", {}) or {}
    directional = DirectionalConfig(
        enabled=bool(dir_raw.get("enabled", True)),
        min_score=float(dir_raw.get("min_score", 80.0)),
        first_entry=str(dir_raw.get("first_entry", "09:30")),
        last_entry=str(dir_raw.get("last_entry", "14:00")),
        max_entries_per_day=int(dir_raw.get("max_entries_per_day", 2)),
        stop_pct=float(dir_raw.get("stop_pct", 10.0)),
        target_pct=float(dir_raw.get("target_pct", 30.0)),
        trail=tuple((float(a), float(b)) for a, b in dir_raw.get("trail", ((15, 7), (25, 12)))),
        underlyings=tuple(str(u) for u in dir_raw.get("underlyings", ("NIFTY", "SENSEX"))),
    )
    if not 1.0 <= directional.stop_pct <= 90.0:
        raise ValueError("cockpit.directional.stop_pct must be between 1 and 90")
    st_raw: Any = raw.get("setups", {}) or {}
    setups = SetupConfig(
        confirm_move_pct=float(st_raw.get("confirm_move_pct", 0.05)),
        hold_s=float(st_raw.get("hold_s", 60)),
        window_min=float(st_raw.get("window_min", 10)),
        fail_move_pct=float(st_raw.get("fail_move_pct", 0.08)),
        valid_min=float(st_raw.get("valid_min", 60)),
    )
    c_raw: Any = raw.get("condor", {}) or {}
    condor = CondorConfig(
        enabled=bool(c_raw.get("enabled", True)),
        underlying=str(c_raw.get("underlying", "NIFTY")),
        first_entry=str(c_raw.get("first_entry", "10:00")),
        last_entry=str(c_raw.get("last_entry", "14:30")),
        min_days_to_expiry=int(c_raw.get("min_days_to_expiry", 2)),
        short_delta=float(c_raw.get("short_delta", 0.16)),
        wing_widths=tuple(float(w) for w in c_raw.get("wing_widths", (200, 150, 100))),
        min_credit_pct_of_width=float(c_raw.get("min_credit_pct_of_width", 10)),
        min_iv_percentile=float(c_raw.get("min_iv_percentile", 50)),
        veto_lookback_min=float(c_raw.get("veto_lookback_min", 30)),
        one_sided_on_trend=bool(c_raw.get("one_sided_on_trend", True)),
        take_profit_pct=float(c_raw.get("take_profit_pct", 50)),
        stop_mult=float(c_raw.get("stop_mult", 2.0)),
        exit_on_short_touch=bool(c_raw.get("exit_on_short_touch", True)),
        defend_within_pct=float(c_raw.get("defend_within_pct", 0.5)),
        exit_time=str(c_raw.get("exit_time", "15:00")),
        max_per_day=int(c_raw.get("max_per_day", 1)),
        hold_block_categories=tuple(
            str(x)
            for x in c_raw.get(
                "hold_block_categories", ("RBI_MPC", "UNION_BUDGET", "ELECTION_EXIT_POLL")
            )
        ),
        hold_earnings_weight_pct=float(c_raw.get("hold_earnings_weight_pct", 10)),
    )
    if not 0.05 <= condor.short_delta <= 0.40:
        raise ValueError("cockpit.condor.short_delta must be between 0.05 and 0.40")
    d_raw: Any = raw.get("debit", {}) or {}
    debit = DebitConfig(
        enabled=bool(d_raw.get("enabled", True)),
        underlyings=tuple(str(u) for u in d_raw.get("underlyings", ("NIFTY", "SENSEX"))),
        min_score=float(d_raw.get("min_score", 80)),
        first_entry=str(d_raw.get("first_entry", "09:30")),
        last_entry=str(d_raw.get("last_entry", "14:00")),
        width_steps=int(d_raw.get("width_steps", 2)),
        target_pct_of_max=float(d_raw.get("target_pct_of_max", 60)),
        stop_pct_of_debit=float(d_raw.get("stop_pct_of_debit", 50)),
        square_off=str(d_raw.get("square_off", "15:20")),
        max_entries_per_day=int(d_raw.get("max_entries_per_day", 2)),
    )
    g_raw: Any = raw.get("gamma", {}) or {}
    gamma = GammaConfig(
        enabled=bool(g_raw.get("enabled", True)),
        underlyings=tuple(str(u) for u in g_raw.get("underlyings", ("NIFTY", "SENSEX"))),
        morning_end=str(g_raw.get("morning_end", "13:30")),
        first_entry=str(g_raw.get("first_entry", "13:45")),
        last_entry=str(g_raw.get("last_entry", "15:15")),
        compression_pct=float(g_raw.get("compression_pct", 0.45)),
        vol_mult=float(g_raw.get("vol_mult", 1.8)),
        stop_pct=float(g_raw.get("stop_pct", 30)),
    )
    if condor.stop_mult <= 1.0:
        raise ValueError("cockpit.condor.stop_mult must be above 1 (x credit)")
    host = str(raw.get("host", "127.0.0.1"))
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("cockpit.host must be a loopback address (local use only)")
    stop = float(bot_raw.get("option_stop_pct", 25.0))
    manual_stop = float(raw.get("manual_default_stop_pct", 25.0))
    for name, v in (("bot.option_stop_pct", stop), ("manual_default_stop_pct", manual_stop)):
        if not 1.0 <= v <= 90.0:
            raise ValueError(f"cockpit.{name} must be between 1 and 90 (% of premium)")
    return CockpitConfig(
        enabled=bool(raw.get("enabled", True)),
        host=host,
        gateway_port=int(raw.get("gateway_port", 8765)),
        engine_port=int(raw.get("engine_port", 8766)),
        replay_port=int(raw.get("replay_port", 8767)),
        publish_interval_ms=int(raw.get("publish_interval_ms", 200)),
        analytics_interval_s=float(raw.get("analytics_interval_s", 1.0)),
        chain_interval_s=float(raw.get("chain_interval_s", 60.0)),
        events_kept=int(raw.get("events_kept", 200)),
        bot=BotPaperConfig(
            enabled=bool(bot_raw.get("enabled", True)),
            min_score=float(bot_raw.get("min_score", 80.0)),
            option_stop_pct=stop,
        ),
        directional=directional,
        setups=setups,
        condor=condor,
        debit=debit,
        gamma=gamma,
        user_pins_max=int(raw.get("user_pins_max", 3)),
        feed_stale_s=float(
            (settings.raw.get("execution") or {}).get("disconnect_kill_after_s", 10)
        ),
        manual_default_stop_pct=manual_stop,
        key_file=root / str(raw.get("key_file", "data/state/cockpit.key")),
        journal_dir=root / str(raw.get("journal_dir", "data/state")),
        replay_dir=root / str(raw.get("replay_dir", "data/replay")),
        events_file=root / str(raw.get("events_file", "config/events/macro_calendar.csv")),
        block_entries_in_event_window=bool(raw.get("block_entries_in_event_window", True)),
    )


def page_token(path: Path) -> str:
    """The page's access token, kept across gateway restarts (gitignored, local only)."""
    if path.exists():
        tok = path.read_text(encoding="utf-8").strip()
        if len(tok) >= 24:
            return tok
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(tok, encoding="utf-8")
    tmp.replace(path)
    return tok


def auth_key(path: Path) -> bytes:
    """Shared secret for the engine <-> gateway socket; created on first use (gitignored)."""
    if path.exists():
        key = path.read_text(encoding="utf-8").strip()
        if len(key) >= 32:
            return key.encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(key, encoding="utf-8")
    tmp.replace(path)
    return key.encode("ascii")
