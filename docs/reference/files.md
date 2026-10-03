# Files and logs

## The runtime directory

The runner writes everything it knows to `TRADER_RUNTIME` (`/srv/trading/runtime` in production;
read-only to the strategist). The dashboard reads it.

| Path | What |
|---|---|
| `status.json` | The runner's heartbeat and live state, rewritten every tick. The watchdog and STOP judge the runner alive by its age |
| `session.json` | Today's open and close, saved so a restart can survive a calendar outage |
| `session.lock` | Held (shared) by the runner for the whole session; `trading-deploy` refuses while it's held |
| `golive.json` | The go-live state: `pending`, `armed`, `live`, `vetoed` or `demoted` |
| `promotion.json` | Each classifier's spec hash, the date its record started, and its family label |
| `classifier_state.json` | Today's per-symbol trade counts and retirements, for a mid-session restart |
| `alerts.log` | Every alert the runner, watchdog and dashboard sent |
| `benchmark.csv` | SPY's open and close per session, for the buy-and-hold comparison |
| `decisions/<date>.jsonl[.gz]` | One line per decision (below). Kept 14 days here; the strategist archives them |
| `books/paper/`, `books/live/` | One directory per Alpaca book (below) |
| `books/sim/<id>/` | One per `mode: sim` classifier, with `sim_state.json` (its simulated cash and positions) |
| `sim_quarantine/` | Sim accounts whose files couldn't be read, moved aside |

Each book directory holds:

| File | What |
|---|---|
| `trades.csv` | Every fill (below) |
| `equity.csv` | Equity marks: an opening row at the day-start equity, then every 5 minutes and at the close |
| `nav.json` | Units, high-water mark and last equity, for unit-NAV accounting |
| `risk.json` | The day's baseline, settled cash at the open, today's buys, any kill or STOP in force, and a sticky `halted` flag |
| `entries.json` | Open positions, with the rules each was opened with |
| `pending.json` | Entry orders resting or in flight |
| `stop.json` | A STOP pressed today |
| `cashflows.csv` | Deposits and withdrawals seen (live book) |

## `trades.csv`

| Column | Meaning |
|---|---|
| `time` | Fill time, US Eastern |
| `book` | `paper`, `live`, `sim` (replays) or `sim:<id>` |
| `classifier` | The classifier id |
| `symbol` | The ticker |
| `side` | `buy`, `sell_part` (a scale-out or one leg of a piecemeal exit) or `sell` (the end of a round trip) |
| `qty`, `price`, `notional` | The fill |
| `reason` | Why: `ENTER`, `stop`, `stop (raised)`, `target`, `scale out`, `time stop`, `classifier EXIT`, `eod flatten`, `server stop`, `daily kill switch`, `HALT`, `manual STOP` and so on. `(price estimated)` marks a price the runner couldn't get |
| `pnl`, `pnl_pct` | On `sell` rows, the whole round trip's P&L, in dollars and as % of its cost. Empty `pnl_pct` means the price was estimated: such a trade counts towards nothing |

## The decisions log

One JSON object per line. Every answered question:

| Key | Meaning |
|---|---|
| `t` | Time, `HH:MM` US Eastern |
| `c`, `s` | Classifier id and symbol |
| `q` | `entry`, `exit`, `probe` or `allocation` |
| `p` | The model's probabilities per criteria key |
| `f` | The feature values shown |
| `pos` | The position state (exit questions) |
| `px`, `m`, `r` | Probes only: the price, minutes since open and recent returns, so `probe-report` can score them |

An `allocation` row records a book-level limit shrinking an entry: `constraint`, `requested`,
`allowed` and `floor`.

## The strategist's repository

The post-close and weekly runs copy the runner's logs into the strategist checkout (`trader
archive`): decision logs to `logs/decisions/`, every book's trades (sim accounts included) merged
into `logs/trades.csv`, and the paper and live books' equity and cashflow files to
`logs/<book>_equity.csv` and `logs/<book>_cashflows.csv`. `logs/probe_report.json` is the latest
probe report.

Retention (`trader compact`, weekly): daily journals older than 28 days are deleted once the
week's weekly journal exists; monthlies older than 400 days once the yearly exists; archived
decision logs after 90 days; replay runs after 14 days, unless their name starts with `keep-`.
