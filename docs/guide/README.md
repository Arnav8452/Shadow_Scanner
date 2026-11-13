# Retail Quant Engine — User Guide

This guide is for someone opening the project for the first time. It explains what the system is, how to set it up, how to run it each day, what every part of the Quant Cockpit page means, how each signal and strategy works, and how to test a new strategy honestly.

It is personal software for one owner's own trading in Indian index options and futures (NSE Nifty, BSE Sensex), sized for ₹1–2 lakh. It never places a live order: every trade is a paper trade. It does not offer signals to anyone else, and it must not be changed to do so (see [CLAUDE.md](../../CLAUDE.md)).

## Read in this order

| # | Document | Read it when |
|---|---|---|
| 1 | [What the system is](01-what-it-is.md) | First. The parts, how data flows, and what is on, off or not built. |
| 2 | [Setup](02-setup.md) | Installing on a new PC, broker keys, first test runs, scheduled tasks. |
| 3 | [Daily routine](03-daily-routine.md) | Every trading day: morning checks, market hours, evening jobs, outages. |
| 4 | [The Quant Cockpit, element by element](04-cockpit.md) | Learning the page: every panel, column, tab and button. |
| 5 | [Signals: how each reading is made](05-signals.md) | Understanding the score, setups, spoofing, zones, OI reads and the charges gates. |
| 6 | [Strategies](06-strategies.md) | What each of the paper strategies does, when it trades and how it exits. |
| 7 | [Paper trading and reading results](07-paper-trading.md) | Taking manual paper trades, the risk gate, journals, replays, the Research tab. |
| 8 | [Building and testing a new strategy](08-new-strategies.md) | Adding your own idea and taking it from code to a verdict. |
| 9 | [Data, research and the nightly jobs](09-data-and-research.md) | Where data lives, quality checks, end-of-day files, screener, swing, event arbitrage. |
| 10 | [Risk rules, configuration and regulation](10-risk-and-config.md) | The hard rules, the live-trading lock, `settings.yaml`, `config/rules/`. |
| 11 | [Troubleshooting and FAQ](11-troubleshooting.md) | Something looks wrong. |
| 12 | [Glossary](12-glossary.md) | A term you don't know (OBI, RER, CVD, GEX, IVP, PCR, breakeven…). |

## The three things to remember

1. **Every threshold is a hypothesis.** Scores, setups and strategy rules are starting guesses, labelled `HYPOTHESIS` on the page and in `config/settings.yaml`. Nothing here is proven profitable. A rule earns trust only after 20–30 recorded sessions, with full costs, at 2× slippage, with a t-statistic near 3 and a deflated Sharpe ratio that survives the number of variants tried.
2. **Paper first, always.** Live order placement is locked behind five conditions (paid data mode, a config switch, a whitelisted static IP, a fresh daily 2FA session and the owner's confirmation in the session). None of them is met. See [10-risk-and-config.md](10-risk-and-config.md#102-the-live-trading-lock).
3. **The recorder is the foundation.** Option bid/ask history for Indian markets cannot be bought cheaply later. Every day the recorder misses is lost for good, and most strategies need its one-minute option chain to work at all.

## Other documents in `docs/`

- [SPEC.md](../SPEC.md): the original design and milestone plan (§13).
- [DECISIONS.md](../DECISIONS.md): the build log, newest first, with every owner decision and open question.
- [RUNBOOK.md](../RUNBOOK.md): short operational checklists (this guide expands on them).
- [DATA_DICTIONARY.md](../DATA_DICTIONARY.md): the rule-table format and data fields.
- [INTERCONNECTED_SYSTEM.md](../INTERCONNECTED_SYSTEM.md), [ADDENDUM_EVENT_ARB.md](../ADDENDUM_EVENT_ARB.md): background on the confluence engine and the event-arbitrage sleeve.
- [REVIEW_2026-11-09.md](../REVIEW_2026-11-09.md), [REVIEW_2026-11-10.md](../REVIEW_2026-11-10.md): system reviews.
- `new/` (not committed): the owner's strategy notes, which the playbook strategies were built from.

This guide describes the system as of 12 Nov 2026. When the code and this guide disagree, the code wins; please fix the guide.
