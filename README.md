# Retail Quant Engine

Personal Indian F&O research, paper-trading and (gated) live-trading engine. Personal use only.
Design: [`docs/SPEC.md`](docs/SPEC.md). Standing rules: [`CLAUDE.md`](CLAUDE.md).
Build log and open questions: [`docs/DECISIONS.md`](docs/DECISIONS.md).

**New here? Start with the user guide: [`docs/guide/`](docs/guide/README.md)** — setup, the daily routine, every Quant Cockpit element, every signal and strategy, paper trading, and how to test a new strategy.

## Setup (Windows / macOS / Linux)

```bash
pip install --user uv          # once
uv sync --group dev            # add --system-certs if your network intercepts TLS
cp .env.example .env           # fill in keys; .env is gitignored
```

## Commands

`make <target>` where `make` exists, otherwise `uv run python tasks.py <target> [KEY=VALUE]`:

| Target | What | Status |
|---|---|---|
| `test` | pytest | M0 |
| `lint` | ruff + mypy (strict on `engine/`) | M0 |
| `rules` | validate `config/rules/*` and list UNVERIFIED rows | M0 |
| `record`, `qc DATE=` | recorder, data-quality report | M2 |
| `mine-log FILE=`, `self-review` | trade-log mining, journal review | M1 |
| `paper STRAT=` | paper trading | M5 |
| `backtest STRAT= FROM= TO=`, `report` | backtests and reports | M6 |
| `event-arb [DATE=] [SEND=1] [FETCH=0]` | event-arbitrage scan → `reports/event_arb/`, Telegram alerts | M13-EA (built) |

Operational checklists: [`docs/RUNBOOK.md`](docs/RUNBOOK.md). System review and open proposals: [`docs/REVIEW_2026-11-09.md`](docs/REVIEW_2026-11-09.md).

## Layout (built so far)

```
config/settings.yaml, modes/{free,paid}.yaml   global + mode overlays (MODE env var, default free)
config/rules/*.csv, margin_rules.yaml          effective-dated regulatory/fee/contract tables
engine/core/{config,rules,env,logging_setup}.py
ops/logging.yaml                               logging with secret redaction
docs/seed/parse_report_seed.py                 original trade-log parser (ported in M1)
data/                                          gitignored (raw, parquet, duckdb, private)
```

See `docs/DATA_DICTIONARY.md` for the rule-table format.
