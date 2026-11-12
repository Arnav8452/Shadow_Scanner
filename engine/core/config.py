"""Settings loader: ``config/settings.yaml`` deep-merged with ``config/modes/<MODE>.yaml``.

Hard trading rules from CLAUDE.md that can be checked from config alone are enforced here
(OPS cap < 10, no plain MARKET orders, Mode A never live). Whether a *live* order may be sent
at all is decided by :func:`live_gate`, which needs runtime facts too.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from engine.core.env import load_dotenv
from engine.core.rules import IST, RuleBook

REPO_ROOT = Path(__file__).resolve().parents[2]

# Project policy (CLAUDE.md rule 2), not a regulatory constant.
ALLOWED_ORDER_TYPES = frozenset({"LIMIT", "MARKET_PROTECTED"})
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def sebi_ops_threshold(root: Path, on: date) -> int:
    """SEBI/exchange retail-algo orders-per-second threshold, from config/rules (not code)."""
    book = RuleBook.load(root / "config" / "rules")
    # A stricter-than-needed cap is always safe, so an UNVERIFIED threshold may be used to
    # *reject* configs; it is never used to permit anything above the configured cap.
    return book.lookup(
        "sebi_algo_rules", on, allow_unverified=True, rule="retail_algo_ops_threshold"
    ).as_int()


class ConfigError(ValueError):
    pass


class Mode(StrEnum):
    FREE = "free"
    PAID = "paid"


class EngineVersion(StrEnum):
    V1_SHADOW = "v1_shadow"
    V2_BOT = "v2_bot"


@dataclass(frozen=True)
class ShadowScannerConfig:
    max_watchlist_symbols: int = 50
    depth_levels: int = 30
    min_confidence_score: float = 75.0
    a_plus_confidence_score: float = 80.0
    # HYPOTHESIS: a tradeable grade must hold (same direction) this long before it counts.
    signal_confirm_s: float = 5.0
    watchlist_score: float = 65.0
    rer_confirm_min: float = 0.75
    rer_spoof_max: float = 0.20
    obi_confirm_min: float = 0.50
    air_pocket_ratio_max: float = 0.55
    cvd_z_confirm_min: float = 2.0
    order_persistence_ms: int = 500
    stop_pool_band_min_pct: float = 0.15
    stop_pool_band_max_pct: float = 0.40
    sl_tick_buffer: int = 2
    min_rr_t1: float = 1.5
    min_rr_t2: float = 2.5
    kelly_fraction: float = 0.25
    kelly_max_risk_pct: float = 1.5
    kelly_rolling_window: int = 100
    # Eligibility (HYPOTHESIS, owner review 12 Nov 2026): options that cannot carry a reading.
    # Dead expiry-day contracts at 0.05 x 0.10 scored 61-65 on margin-cleanup flow.
    option_min_premium: float = 5.0  # mid below this: not scored
    option_max_spread_pct: float = 10.0  # (ask - bid) / mid above this: not scored
    option_min_ask: float = 0.25  # ask at or below this (tick floor): not scored
    # Location gate: without a structural level the other pillars alone can not make a grade.
    min_location_pts: float = 5.0
    location_gate_cap: float = 60.0
    # Book shape: when the touch (micro-price lean) and the top-5 imbalance disagree, a passive
    # wall below/above the touch is doing the work; scale the book pillar by this.
    book_disagreement_factor: float = 0.5
    # Paper ledger (forward test). Times are IST; brokerage plan rows live in brokerage.csv.
    paper_no_new_entries_after: time = time(15, 0)
    paper_square_off_at: time = time(15, 20)
    cost_broker: str = "upstox_plus"
    report_dir: Path = Path("reports/shadow_scanner")
    ledger_file: Path = Path("data/state/shadow_ledger.jsonl")


@dataclass(frozen=True)
class RiskLimits:
    capital_inr: float
    per_trade_max_loss_pct: Mapping[str, float]
    daily_loss_limit_pct: float
    weekly_loss_limit_pct: float
    max_open_risk_pct: float
    max_lots_per_strategy: int
    max_trades_per_day: int
    cooloff_after_loss_min: int
    block_first_minutes: int
    directional_no_entry_after: str
    stock_fno_expiry_exit_days_before: int
    # Approved by the owner on 11 Nov 2026 (defaults match; config is the source of truth).
    margin_headroom_max_used_pct: float = 50.0
    loss_streak_size_cut_after: int = 3
    loss_streak_halt_after: int = 4

    def inr(self, pct: float) -> float:
        return self.capital_inr * pct / 100.0


@dataclass(frozen=True)
class ExecutionConfig:
    live_enabled: bool
    broker: str
    allowed_order_types: frozenset[str]
    max_orders_per_second: int
    reconcile_interval_s: int
    disconnect_kill_after_s: int
    order_timeout_s: int
    static_ip: str


@dataclass(frozen=True)
class Settings:
    mode: Mode
    timezone: str
    risk: RiskLimits
    execution: ExecutionConfig
    data_primary: str
    data_fallbacks: tuple[str, ...]
    paths: Mapping[str, Path]
    logging_config: Path
    raw: Mapping[str, Any]
    engine_version: EngineVersion = EngineVersion.V1_SHADOW
    shadow_scanner: ShadowScannerConfig = ShadowScannerConfig()


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return doc


def _req(section: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in section or section[key] is None:
        raise ConfigError(f"missing required setting {where}.{key}")
    return section[key]


def _positive(value: Any, name: str) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if f <= 0:
        raise ConfigError(f"{name} must be > 0, got {value!r}")
    return f


def _non_negative_int(value: Any, name: str) -> int:
    try:
        i = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if i < 0:
        raise ConfigError(f"{name} must be >= 0, got {value!r}")
    return i


def _parse_engine_version(val: Any) -> EngineVersion:
    s = str(val or "v1_shadow").strip().lower()
    aliases = {
        "v1": EngineVersion.V1_SHADOW,
        "v1_shadow": EngineVersion.V1_SHADOW,
        "shadow": EngineVersion.V1_SHADOW,
        "v2": EngineVersion.V2_BOT,
        "v2_bot": EngineVersion.V2_BOT,
        "v2_auto": EngineVersion.V2_BOT,
        "auto_bot": EngineVersion.V2_BOT,
        "bot": EngineVersion.V2_BOT,
    }
    if s not in aliases:
        raise ConfigError(f"engine_version must be one of {sorted(aliases)}, got {val!r}")
    return aliases[s]


def _hhmm_time(value: Any, name: str) -> time:
    text = str(value)
    if not _HHMM.match(text):
        raise ConfigError(f"{name} must be HH:MM (24h)")
    hh, mm = text.split(":")
    return time(int(hh), int(mm))


def _build_shadow_scanner(raw: Mapping[str, Any], root: Path) -> ShadowScannerConfig:
    sc = raw.get("shadow_scanner")
    if not isinstance(sc, Mapping):
        return ShadowScannerConfig(
            report_dir=root / "reports" / "shadow_scanner",
            ledger_file=root / "data" / "state" / "shadow_ledger.jsonl",
        )
    return ShadowScannerConfig(
        max_watchlist_symbols=int(
            _positive(sc.get("max_watchlist_symbols", 50), "shadow_scanner.max_watchlist_symbols")
        ),
        depth_levels=int(_positive(sc.get("depth_levels", 30), "shadow_scanner.depth_levels")),
        min_confidence_score=_positive(
            sc.get("min_confidence_score", 75.0), "shadow_scanner.min_confidence_score"
        ),
        a_plus_confidence_score=_positive(
            sc.get("a_plus_confidence_score", 80.0), "shadow_scanner.a_plus_confidence_score"
        ),
        signal_confirm_s=float(sc.get("signal_confirm_s", 5.0)),
        watchlist_score=_positive(
            sc.get("watchlist_score", 65.0), "shadow_scanner.watchlist_score"
        ),
        rer_confirm_min=_positive(
            sc.get("rer_confirm_min", 0.75), "shadow_scanner.rer_confirm_min"
        ),
        rer_spoof_max=_positive(sc.get("rer_spoof_max", 0.20), "shadow_scanner.rer_spoof_max"),
        obi_confirm_min=_positive(
            sc.get("obi_confirm_min", 0.50), "shadow_scanner.obi_confirm_min"
        ),
        air_pocket_ratio_max=_positive(
            sc.get("air_pocket_ratio_max", 0.55), "shadow_scanner.air_pocket_ratio_max"
        ),
        cvd_z_confirm_min=_positive(
            sc.get("cvd_z_confirm_min", 2.0), "shadow_scanner.cvd_z_confirm_min"
        ),
        order_persistence_ms=_non_negative_int(
            sc.get("order_persistence_ms", 500), "shadow_scanner.order_persistence_ms"
        ),
        stop_pool_band_min_pct=_positive(
            sc.get("stop_pool_band_min_pct", 0.15), "shadow_scanner.stop_pool_band_min_pct"
        ),
        stop_pool_band_max_pct=_positive(
            sc.get("stop_pool_band_max_pct", 0.40), "shadow_scanner.stop_pool_band_max_pct"
        ),
        sl_tick_buffer=_non_negative_int(
            sc.get("sl_tick_buffer", 2), "shadow_scanner.sl_tick_buffer"
        ),
        min_rr_t1=_positive(sc.get("min_rr_t1", 1.5), "shadow_scanner.min_rr_t1"),
        min_rr_t2=_positive(sc.get("min_rr_t2", 2.5), "shadow_scanner.min_rr_t2"),
        kelly_fraction=_positive(sc.get("kelly_fraction", 0.25), "shadow_scanner.kelly_fraction"),
        kelly_max_risk_pct=_positive(
            sc.get("kelly_max_risk_pct", 1.5), "shadow_scanner.kelly_max_risk_pct"
        ),
        kelly_rolling_window=int(
            _positive(sc.get("kelly_rolling_window", 100), "shadow_scanner.kelly_rolling_window")
        ),
        option_min_premium=float(sc.get("option_min_premium", 5.0)),
        option_max_spread_pct=float(sc.get("option_max_spread_pct", 10.0)),
        option_min_ask=float(sc.get("option_min_ask", 0.25)),
        min_location_pts=float(sc.get("min_location_pts", 5.0)),
        location_gate_cap=float(sc.get("location_gate_cap", 60.0)),
        book_disagreement_factor=float(sc.get("book_disagreement_factor", 0.5)),
        paper_no_new_entries_after=_hhmm_time(
            sc.get("paper_no_new_entries_after", "15:00"),
            "shadow_scanner.paper_no_new_entries_after",
        ),
        paper_square_off_at=_hhmm_time(
            sc.get("paper_square_off_at", "15:20"), "shadow_scanner.paper_square_off_at"
        ),
        cost_broker=str(sc.get("cost_broker", "upstox_plus")),
        report_dir=root / str(sc.get("report_dir", "reports/shadow_scanner")),
        ledger_file=root / str(sc.get("ledger_file", "data/state/shadow_ledger.jsonl")),
    )


def _build(
    raw: Mapping[str, Any], root: Path, today: date, *, version_override: str | None = None
) -> Settings:
    try:
        mode = Mode(str(_req(raw, "mode", "<root>")))
    except ValueError as exc:
        raise ConfigError(f"mode must be one of {[m.value for m in Mode]}") from exc
    engine_ver = _parse_engine_version(version_override or raw.get("engine_version", "v1_shadow"))
    shadow_cfg = _build_shadow_scanner(raw, root)

    r = _req(raw, "risk", "<root>")
    per_trade = {
        k: _positive(v, f"risk.per_trade_max_loss_pct.{k}")
        for k, v in dict(_req(r, "per_trade_max_loss_pct", "risk")).items()
    }
    risk = RiskLimits(
        capital_inr=_positive(
            _req(_req(raw, "capital", "<root>"), "inr", "capital"), "capital.inr"
        ),
        per_trade_max_loss_pct=per_trade,
        daily_loss_limit_pct=_positive(
            _req(r, "daily_loss_limit_pct", "risk"), "daily_loss_limit_pct"
        ),
        weekly_loss_limit_pct=_positive(
            _req(r, "weekly_loss_limit_pct", "risk"), "weekly_loss_limit_pct"
        ),
        max_open_risk_pct=_positive(_req(r, "max_open_risk_pct", "risk"), "max_open_risk_pct"),
        max_lots_per_strategy=int(_positive(_req(r, "max_lots_per_strategy", "risk"), "max_lots")),
        max_trades_per_day=int(
            _positive(_req(r, "max_trades_per_day", "risk"), "max_trades_per_day")
        ),
        cooloff_after_loss_min=_non_negative_int(
            _req(r, "cooloff_after_loss_min", "risk"), "cooloff_after_loss_min"
        ),
        block_first_minutes=_non_negative_int(
            _req(r, "block_first_minutes", "risk"), "block_first_minutes"
        ),
        directional_no_entry_after=str(_req(r, "directional_no_entry_after", "risk")),
        stock_fno_expiry_exit_days_before=_non_negative_int(
            _req(r, "stock_fno_expiry_exit_days_before", "risk"),
            "stock_fno_expiry_exit_days_before",
        ),
        margin_headroom_max_used_pct=_positive(
            r.get("margin_headroom_max_used_pct", 50.0), "margin_headroom_max_used_pct"
        ),
        loss_streak_size_cut_after=int(
            _positive(r.get("loss_streak_size_cut_after", 3), "loss_streak_size_cut_after")
        ),
        loss_streak_halt_after=int(
            _positive(r.get("loss_streak_halt_after", 4), "loss_streak_halt_after")
        ),
    )
    if risk.loss_streak_size_cut_after >= risk.loss_streak_halt_after:
        raise ConfigError("loss_streak_size_cut_after must be < loss_streak_halt_after")
    if risk.margin_headroom_max_used_pct > 100:
        raise ConfigError("margin_headroom_max_used_pct must be <= 100")
    if not _HHMM.match(risk.directional_no_entry_after):
        raise ConfigError("risk.directional_no_entry_after must be HH:MM (24h)")
    if risk.daily_loss_limit_pct > risk.weekly_loss_limit_pct:
        raise ConfigError("daily_loss_limit_pct cannot exceed weekly_loss_limit_pct")

    e = _req(raw, "execution", "<root>")
    order_types = frozenset(str(t).upper() for t in _req(e, "allowed_order_types", "execution"))
    if "MARKET" in order_types:
        raise ConfigError("plain MARKET orders are forbidden; use LIMIT or MARKET_PROTECTED")
    if not order_types or not order_types <= ALLOWED_ORDER_TYPES:
        raise ConfigError(f"allowed_order_types must be a subset of {sorted(ALLOWED_ORDER_TYPES)}")
    ops = int(_positive(_req(e, "max_orders_per_second", "execution"), "max_orders_per_second"))
    threshold = sebi_ops_threshold(root, today)
    if ops >= threshold:
        raise ConfigError(f"max_orders_per_second must be < {threshold} (SEBI), got {ops}")
    reconcile = int(_req(e, "reconcile_interval_s", "execution"))
    if not 30 <= reconcile <= 60:
        raise ConfigError(f"reconcile_interval_s must be 30–60, got {reconcile}")
    live_enabled = _req(e, "live_enabled", "execution")
    if not isinstance(live_enabled, bool):
        raise ConfigError("execution.live_enabled must be true/false")
    if live_enabled and mode is not Mode.PAID:
        raise ConfigError("execution.live_enabled requires MODE=paid")
    execution = ExecutionConfig(
        live_enabled=live_enabled,
        broker=str(_req(e, "broker", "execution")),
        allowed_order_types=order_types,
        max_orders_per_second=ops,
        reconcile_interval_s=reconcile,
        disconnect_kill_after_s=int(
            _positive(_req(e, "disconnect_kill_after_s", "execution"), "disconnect_kill_after_s")
        ),
        order_timeout_s=int(_positive(_req(e, "order_timeout_s", "execution"), "order_timeout_s")),
        static_ip=str(e.get("static_ip") or ""),
    )

    d = _req(raw, "data", "<root>")
    paths = {k: (root / str(v)) for k, v in dict(_req(raw, "paths", "<root>")).items()}
    log_cfg = root / str(_req(_req(raw, "logging", "<root>"), "config", "logging"))

    return Settings(
        mode=mode,
        timezone=str(_req(raw, "timezone", "<root>")),
        risk=risk,
        execution=execution,
        data_primary=str(_req(d, "primary", "data")),
        data_fallbacks=tuple(str(x) for x in d.get("fallbacks") or ()),
        paths=paths,
        logging_config=log_cfg,
        raw=raw,
        engine_version=engine_ver,
        shadow_scanner=shadow_cfg,
    )


def load_settings(
    mode: str | None = None,
    *,
    version: str | None = None,
    root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    today: date | None = None,
) -> Settings:
    """Load settings. Mode precedence: argument > ``MODE`` env var / ``.env`` > ``free``."""
    root = root or REPO_ROOT
    today = today or datetime.now(IST).date()
    if environ is None:
        load_dotenv(root / ".env")
        environ = os.environ
    chosen = (mode or environ.get("MODE") or Mode.FREE.value).strip().lower()
    if chosen not in {m.value for m in Mode}:
        raise ConfigError(f"MODE must be one of {[m.value for m in Mode]}, got {chosen!r}")
    chosen_ver = version or environ.get("ENGINE_VERSION")
    base = _read_yaml(root / "config" / "settings.yaml")
    overlay = _read_yaml(root / "config" / "modes" / f"{chosen}.yaml")
    merged = deep_merge(base, overlay)
    if merged.get("mode") != chosen:
        raise ConfigError(f"modes/{chosen}.yaml declares mode={merged.get('mode')!r}")
    return _build(merged, root, today, version_override=chosen_ver)


@dataclass(frozen=True)
class LiveGate:
    allowed: bool
    reasons: tuple[str, ...]  # why live is blocked (empty when allowed)


def live_gate(
    settings: Settings,
    *,
    static_ip_whitelisted: bool,
    session_2fa_on: date | None,
    today: date,
    owner_confirmed: bool,
) -> LiveGate:
    """All of CLAUDE.md's live conditions must hold; otherwise every order goes to PaperBroker."""
    reasons: list[str] = []
    if settings.mode is not Mode.PAID:
        reasons.append("MODE is not paid")
    if not settings.execution.live_enabled:
        reasons.append("execution.live_enabled is false")
    if not settings.execution.static_ip or not static_ip_whitelisted:
        reasons.append("static IP not configured/whitelisted")
    if session_2fa_on != today:
        reasons.append("no fresh 2FA session for today")
    if not owner_confirmed:
        reasons.append("owner has not confirmed live trading in this session")
    return LiveGate(allowed=not reasons, reasons=tuple(reasons))
