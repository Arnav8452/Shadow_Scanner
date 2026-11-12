"""XSS-safe HTML Dashboard & JSONL Ledger writer for Version 1 (Shadow Order Book Scanner).

Security guarantees (per ``mandatory-secure-web-skills``):
- Every dynamic value rendered into HTML is escaped via :func:`html.escape` with ``quote=True``.
- No inline JavaScript ``innerHTML`` or ``document.write`` is used.
- Includes a strict Content-Security-Policy meta tag.
"""

from __future__ import annotations

import html
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from engine.core.config import EngineVersion
from engine.shadow.models import ShadowLedgerOutcome
from engine.shadow.scanner import ShadowOrderBookScanner


def _esc(val: Any) -> str:
    return html.escape(str(val), quote=True)


def render_shadow_dashboard_html(
    scanner: ShadowOrderBookScanner,
    *,
    session_date: date,
    engine_version: EngineVersion = EngineVersion.V1_SHADOW,
    data_feed: str = "unspecified",
) -> str:
    """Render self-contained HTML dashboard for the Shadow Order Book Scanner."""
    signals = scanner.ranked_signals()
    watchlist = scanner.ranked_watchlist()
    summary = scanner.ledger_summary()
    ver_badge = (
        "VERSION 1: SHADOW ORDER BOOK SCANNER (HUMAN-IN-THE-LOOP — ZERO AUTO ORDERS)"
        if engine_version is EngineVersion.V1_SHADOW
        else "VERSION 2: AUTOMATED BOT ENGINE (CONFLUENCE + EXECUTION)"
    )

    sig_rows: list[str] = []
    for s in signals:
        bd = s.breakdown
        sig_rows.append(
            "<tr>"
            f"<td><strong>{_esc(s.symbol)}</strong><br><small>{_esc(s.signal_id)}</small></td>"
            f"<td><span class='badge {_esc(s.direction.lower())}'>{_esc(s.direction)}</span><br>"
            f"<small>{_esc(s.wave.value)}</small></td>"
            f"<td><strong>{_esc(f'{s.confidence_score:.1f}')} / 100</strong> "
            f"({_esc(s.grade.value)})<br>"
            f"<small>Loc:{_esc(bd.structural_location_pts)} | "
            f"Flow:{_esc(bd.order_flow_anti_spoof_pts)} | "
            f"OBI:{_esc(bd.obi_air_pocket_pts)} | "
            f"Deriv:{_esc(bd.cross_asset_derivative_pts)}</small></td>"
            f"<td><strong>{_esc(f'{s.entry_price:.2f}')}</strong><br>"
            f"<small>LTP: {_esc(f'{s.ltp_at_signal:.2f}')}</small></td>"
            f"<td class='sl'><strong>{_esc(f'{s.stop_loss:.2f}')}</strong><br>"
            f"<small>{_esc(s.sl_wall_info)}</small></td>"
            f"<td class='tp'><strong>T1: {_esc(f'{s.target_1:.2f}')} "
            f"({_esc(f'{s.rr_t1:.1f}')}R)</strong><br>"
            f"T2: {_esc(f'{s.target_2:.2f}')} ({_esc(f'{s.rr_t2:.1f}')}R)<br>"
            f"<small>{_esc(s.target_wall_info)}</small></td>"
            f"<td>RER: {_esc(f'{s.real_execution_ratio:.2f}')}<br>"
            f"OBI_L5: {_esc(f'{s.obi_l5:+.2f}')}<br>"
            f"AirPocket: {_esc(f'{s.air_pocket_ratio:.2f}')}<br>"
            f"CVD_Z: {_esc(f'{s.cvd_z_score:+.2f}')}</td>"
            f"<td>{_esc(s.recommended_qty)} qty<br>"
            f"<small>({_esc(f'{s.recommended_risk_pct:.2f}')}% Quarter-Kelly)</small></td>"
            f"<td>{_esc(s.rationale)}</td>"
            "</tr>"
        )

    watch_rows: list[str] = []
    for w in watchlist:
        watch_rows.append(
            "<tr>"
            f"<td><strong>{_esc(w.symbol)}</strong></td>"
            f"<td>{_esc(w.direction)} ({_esc(w.wave.value)})</td>"
            f"<td>{_esc(f'{w.confidence_score:.1f}')} / 100</td>"
            f"<td>{_esc(f'{w.entry_price:.2f}')}</td>"
            f"<td>{_esc(f'{w.stop_loss:.2f}')}</td>"
            f"<td>{_esc(f'{w.target_1:.2f}')} ({_esc(f'{w.rr_t1:.1f}')}R)</td>"
            f"<td>{_esc(w.rationale)}</td>"
            "</tr>"
        )

    sig_table_body = (
        "\n".join(sig_rows)
        if sig_rows
        else "<tr><td colspan='9'>No signals ≥ 75/100 currently active.</td></tr>"
    )
    watch_table_body = (
        "\n".join(watch_rows)
        if watch_rows
        else "<tr><td colspan='7'>No pre-alert watchlist setups (65–74.9) active.</td></tr>"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';">
<title>Shadow Order Book Scanner — {_esc(session_date.isoformat())}</title>
<style>
  body {{
    font-family: system-ui, -apple-system, sans-serif;
    margin: 24px; background: #0f172a; color: #e2e8f0;
  }}
  h1, h2 {{ color: #f8fafc; }}
  .header-bar {{
    display: flex; justify-content: space-between; align-items: center;
    background: #1e293b; padding: 16px 20px; border-radius: 8px;
    border-left: 5px solid #38bdf8; margin-bottom: 20px;
  }}
  .metrics {{
    display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 24px;
  }}
  .card {{ background: #1e293b; padding: 14px; border-radius: 8px; border: 1px solid #334155; }}
  .card .label {{ font-size: 12px; color: #94a3b8; text-transform: uppercase; }}
  .card .value {{ font-size: 20px; font-weight: 700; margin-top: 4px; color: #f8fafc; }}
  table {{
    width: 100%; border-collapse: collapse; background: #1e293b;
    border-radius: 8px; overflow: hidden; margin-bottom: 28px;
  }}
  th, td {{
    padding: 10px 12px; border-bottom: 1px solid #334155;
    text-align: left; font-size: 13px; vertical-align: top;
  }}
  th {{ background: #0f172a; color: #94a3b8; text-transform: uppercase; font-size: 11px; }}
  .badge {{ padding: 3px 8px; border-radius: 4px; font-weight: 700; font-size: 11px; }}
  .badge.long {{ background: #065f46; color: #6ee7b7; }}
  .badge.short {{ background: #7f1d1d; color: #fca5a5; }}
  .sl {{ color: #fca5a5; }}
  .tp {{ color: #6ee7b7; }}
</style>
</head>
<body>
  <div class="header-bar">
    <div>
      <h1 style="margin:0;font-size:20px;">Retail Quant Engine — Shadow Order Book Radar</h1>
      <div style="color:#94a3b8;font-size:13px;margin-top:4px;">
        Session: {_esc(session_date.isoformat())} | Feed: {_esc(data_feed)} (L1–L30 Microstructure)
      </div>
      <div style="color:#fbbf24;font-size:12px;margin-top:4px;">
        HYPOTHESIS: scores and thresholds are unvalidated priors — signals are unproven until
        out-of-sample tests on recorded sessions reach t ≈ 3. Not trade recommendations.
      </div>
    </div>
    <div style="background:#0284c7;color:#fff;padding:8px 14px;border-radius:6px;font-weight:700;">
      {_esc(ver_badge)}
    </div>
  </div>

  <div class="metrics">
    <div class="card">
      <div class="label">Active Actionable Signals</div>
      <div class="value">{_esc(len(signals))}</div>
    </div>
    <div class="card">
      <div class="label">Shadow Ledger Trades</div>
      <div class="value">{_esc(summary["total_trades"])}</div>
    </div>
    <div class="card">
      <div class="label">Shadow Win Rate</div>
      <div class="value">{_esc(summary["win_rate_pct"])}%</div>
    </div>
    <div class="card">
      <div class="label">Net Ledger P&amp;L (all charges, {_esc(summary["cost_broker"])})</div>
      <div class="value">₹{_esc(summary["net_pnl_inr"])}</div>
    </div>
    <div class="card">
      <div class="label">Net at 2&times; slippage</div>
      <div class="value">₹{_esc(summary["net_pnl_2x_slippage_inr"])}</div>
    </div>
    <div class="card">
      <div class="label">Scored, not paper-traded</div>
      <div class="value">{_esc(sum(summary["not_paper_traded"].values()))}</div>
    </div>
    <div class="card">
      <div class="label">Quarter-Kelly Risk Cap</div>
      <div class="value">{_esc(summary["quarter_kelly_risk_pct"])}%</div>
    </div>
  </div>

  <h2>Actionable Shadow Order Book (Score ≥ 75 / 100 — Anti-Spoofing Verified)</h2>
  <table>
    <thead>
      <tr>
        <th>Symbol / ID</th>
        <th>Side / Wave</th>
        <th>100-Pt Confidence</th>
        <th>Entry Price</th>
        <th>Microstructure SL</th>
        <th>Targets (T1 / T2)</th>
        <th>Order-Flow &amp; Depth</th>
        <th>Kelly Sizing</th>
        <th>Confluence Rationale</th>
      </tr>
    </thead>
    <tbody>
      {sig_table_body}
    </tbody>
  </table>

  <h2>Pre-Alert Watchlist Radar (Score 65.0 – 74.9 / 100)</h2>
  <table>
    <thead>
      <tr>
        <th>Symbol</th>
        <th>Side / Wave</th>
        <th>Score</th>
        <th>Candidate Entry</th>
        <th>Candidate SL</th>
        <th>Candidate T1</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>
      {watch_table_body}
    </tbody>
  </table>
</body>
</html>
"""


def write_shadow_dashboard_and_ledger(
    scanner: ShadowOrderBookScanner,
    *,
    session_date: date,
    report_dir: Path,
    ledger_file: Path,
    engine_version: EngineVersion = EngineVersion.V1_SHADOW,
    data_feed: str = "unspecified",
) -> Path:
    """Write the HTML dashboard and append closed outcomes to the JSONL Shadow Ledger."""
    report_dir.mkdir(parents=True, exist_ok=True)
    html_path = report_dir / f"{session_date.isoformat()}.html"
    html_text = render_shadow_dashboard_html(
        scanner,
        session_date=session_date,
        engine_version=engine_version,
        data_feed=data_feed,
    )
    html_path.write_text(html_text, encoding="utf-8")

    if scanner.closed_outcomes:
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        with ledger_file.open("a", encoding="utf-8") as fh:
            for outcome in scanner.closed_outcomes:
                fh.write(_serialize_outcome(outcome) + "\n")
    return html_path


def _serialize_outcome(outcome: ShadowLedgerOutcome) -> str:
    d = asdict(outcome)
    d["wave"] = outcome.wave.value
    d["status"] = outcome.status.value
    d["opened_at"] = outcome.opened_at.isoformat()
    d["closed_at"] = outcome.closed_at.isoformat()
    return json.dumps(d, sort_keys=True)
