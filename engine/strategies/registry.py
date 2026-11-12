"""Every strategy in SPEC §5 and Addendum A with its honest status (cockpit "Strategies" view).

Statuses:

* ``live``      — places real orders. None: ``execution.live_enabled`` is false (CLAUDE.md).
* ``paper``     — runs on real data every session/night and books paper trades with full costs.
* ``research``  — code exists (decision function, screen, study or live display) but it does not
  run as a paper strategy yet, or it only informs manual decisions.
* ``not built`` — no code yet, or deliberately out of scope.

Change a status only when the evidence changes (a paper account starts booking it, a backtest
lands, the owner approves live). ``evidence`` names the files that show it running; the cockpit
reports the newest one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["live", "paper", "research", "not built"]
STATUSES: tuple[Status, ...] = ("live", "paper", "research", "not built")


@dataclass(frozen=True)
class StrategyItem:
    ref: str  # SPEC / addendum section, e.g. "5.8" or "A.1"
    name: str
    status: Status
    where: str  # module(s) or command
    note: str
    evidence: str = ""  # glob under the repo root whose newest match shows it ran


REGISTRY: tuple[StrategyItem, ...] = (
    StrategyItem(
        "5.1", "Iron condor / credit spreads (Nifty)", "paper",
        "engine/cockpit/condor.py",
        "Iron condor / bull put / bear call chosen by the order-flow veto; hedge-first legs; "
        "held overnight; adjustments not built",
        "data/state/paper_condor.jsonl",
    ),
    StrategyItem(
        "5.2", "Directional intraday, Log-3 style (Nifty, Sensex ATM options)", "paper",
        "engine/cockpit/ (cockpit.directional on the Bot account)",
        "Order flow confirms the entry; -10% stop, +30% target, trail. HYPOTHESIS rules from "
        "the Log-3 study; paper at recorded bid/ask.",
        "data/state/paper_bot.jsonl",
    ),
    StrategyItem(
        "5.2", "Order-flow signal study (Track A)", "research",
        "engine/shadow/, engine/cockpit/study.py (tasks.py shadow-live)",
        "Does a confirmed reading predict the index? Scalping retired 12 Nov 2026 (owner).",
        "data/state/signal_study.jsonl",
    ),
    StrategyItem(
        "5.3", "Opening-range breakout, Log-2 style", "research",
        "engine/strategies/opening_breakout.py",
        "Needs >= 30 recorded sessions of 09:14-09:35 depth before any test.",
    ),
    StrategyItem(
        "5.4", "Calendar spread", "research",
        "engine/strategies/optional_sleeves.py (evaluate_calendar_spread)",
        "Decision function only.",
    ),
    StrategyItem(
        "5.5", "Pre-earnings straddle (heavyweights)", "not built",
        "-",
        "Stock F&O deferred at ₹2L capital (settings.yaml capital note).",
    ),
    StrategyItem(
        "5.6", "Trend sleeve: 12-month TSMOM via NIFTYBEES", "paper",
        "engine/strategies/trend_sleeve.py (evening step swing)",
        "Monthly, cash ETF, no futures; allocation and disaster stop are HYPOTHESIS.",
        "data/state/swing/*.json",
    ),
    StrategyItem(
        "5.7", "Volatility-managed sizing overlay", "not built",
        "-",
        "Realised-vol estimators exist (engine/analytics/realized_vol.py); no sizing overlay.",
    ),
    StrategyItem(
        "5.8", "Swing: cash stocks", "paper",
        "engine/strategies/swing_scanner.py, engine/execution/swing_paper.py",
        "Nightly breakout/support-bounce scan, EOD paper fills; rules HYPOTHESIS.",
        "data/state/swing/*.json",
    ),
    StrategyItem(
        "5.8", "Swing: stock ATM options", "paper",
        "engine/strategies/swing_scanner.py, engine/execution/swing_paper.py",
        "Paper only while stock F&O is deferred at ₹2L; low IV rank, no results inside 10 days.",
        "data/state/swing/*.json",
    ),
    StrategyItem(
        "5.10", "Expiry-afternoon gamma breakout", "paper",
        "engine/cockpit/strategies.py (GammaBreakoutStrategy, cockpit.gamma)",
        "Expiry days 13:45-15:15 after a quiet morning; 30% stop, stop to +40% at +80% (one lot "
        "cannot be halved). Firing rate still unmeasured.",
        "data/state/paper_gamma.jsonl",
    ),
    StrategyItem(
        "5.2", "Debit spreads on confirmed setups (naked vs spread A/B)", "paper",
        "engine/cockpit/strategies.py (DebitSpreadStrategy, cockpit.debit)",
        "Bull call / bear put on the same confirmed setups as 5.2; target 60% of max value, "
        "stop at half the debit, 15:20 square-off.",
        "data/state/paper_debit.jsonl",
    ),
    StrategyItem(
        "test", "Score-follow (original A+ order-flow bot)", "paper",
        "engine/cockpit/core.py (score account, cockpit.bot)",
        "Original rules kept to measure them: ATM option on an A+ signal, 25% stop, exit when "
        "the signal's episode closes (short holds).",
        "data/state/paper_score.jsonl",
    ),
    StrategyItem(
        "5.9", "VRP measurement (Nifty)", "research",
        "engine/analytics/vrp.py, tasks.py iv-history",
        "Own-chain IV vs realised vol; first experiment.",
        "data/parquet/eod/iv_history.parquet",
    ),
    StrategyItem(
        "5.9", "Delivery-spike event study", "research",
        "engine/research/delivery_event_study.py (tasks.py delivery-study)",
        "72 trials; no cell passed DSR >= 90% and |t| >= 3. Spikes pick what to watch only.",
        "reports/research/delivery_event_study*.md",
    ),
    StrategyItem(
        "5.9", "Dispersion (index vs constituent IV)", "research",
        "engine/cockpit/analytics_live.py",
        "Live display in the cockpit; no strategy.",
    ),
    StrategyItem("5.9", "12-month stock momentum (IIMA factors)", "not built", "-", ""),
    StrategyItem("5.9", "Pairs HDFCBANK vs ICICIBANK", "not built", "-", ""),
    StrategyItem(
        "5.9", "Index rebalance, IPO lock-ins, insider buys, PEAD", "not built", "-", "",
    ),
    StrategyItem(
        "A.1", "Tender-offer buyback", "research",
        "engine/sidecars/event_arb/ (tasks.py event-arb)",
        "Full model; daily screen and alerts, information only. You decide and trade by hand.",
        "reports/event_arb/*.html",
    ),
    StrategyItem(
        "A.1", "Open offer", "research", "engine/sidecars/event_arb/open_offer.py",
        "Full model; information only.", "reports/event_arb/*.html",
    ),
    StrategyItem(
        "A.1", "Rights issue / rights entitlements", "research",
        "engine/sidecars/event_arb/rights.py", "Screener and calculator.",
        "reports/event_arb/*.html",
    ),
    StrategyItem(
        "A.1", "Offer for sale (OFS)", "research", "engine/sidecars/event_arb/ofs.py",
        "Screener and calculator.", "reports/event_arb/*.html",
    ),
    StrategyItem(
        "A.1", "Demerger / spin-off", "research", "engine/sidecars/event_arb/",
        "Research note only (addendum).",
    ),
    StrategyItem(
        "A.1", "Open-market buyback", "not built", "-", "Flagged and ignored by design.",
    ),
    StrategyItem("A.1", "Pre-IPO / unlisted shares", "not built", "-", "Out of scope."),
)  # fmt: skip
