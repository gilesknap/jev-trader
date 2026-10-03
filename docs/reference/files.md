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
| `model`, `code_sha`, `spec_hash` | Provenance, for splitting results into cohorts: the decision model, the code commit (12 hex) and the classifier's spec hash (as in `promotion.json`). A `sell` or `sell_part` row has the spec hash its position opened under. Blank on rows written before these columns existed, or when unknown |

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
| `mv`, `cv`, `h` | Provenance: the decision model, the code commit and the classifier's spec hash (as on trade rows) |

An `allocation` row records a book-level limit shrinking an entry: `constraint`, `requested`,
`allowed` and `floor`.

## The data repository

Each owner's private data repository has two branches, which share no history and are never
merged. `deploy/setup/0-data.sh` creates both from the code's `templates/data/`.

`main`, the human's deployment config, checked out by `runner` at `/srv/trading/config`:

| Path | What |
|---|---|
| `config.yaml` | Deployment settings (see [Configuration](configuration.md)) |
| `config/mode.yaml` | The paper/live override |
| `deploy/systemd/trader-runner.timer`, `deploy/systemd/trader.env` | Rendered from `config.yaml`; installed for `runner` |
| `deploy/systemd-trader/trader-strategist-<kind>.timer` | Rendered from `config.yaml`; installed for `trader` by `2-strategist.sh` |
| `README.md` | A note on what the repository is |

`strategist`, the strategist's data, checked out by `trader` at `/srv/trading/strategist`:

| Path | What |
|---|---|
| `state/` | `strategy.md`, `classifiers.yaml`, `watchlist.md`, `steering.md` (the human's) |
| `journal/` | `daily/`, `weekly/`, `monthly/`, `yearly/` |
| `logs/` | The runner's logs, archived (below) |
| `features/custom/` | The strategist's feature functions |
| `proposals/<topic>/` | Code proposals: a `git format-patch` series and a `README.md` rationale (see [Review the strategist's proposals](../how-to/proposals.md)) |
| `CLAUDE.md` | A stub for interactive sessions: the charter itself is `/srv/trading/main/CLAUDE.md` |
| `.gitignore` | Keeps `.env`, the wrapper's stamps, `strategist-alerts.log`, `replays/` and the decision logs out of git |

The wrapper commits only `state/`, `journal/`, `features/custom/`, `logs/` and `proposals/`.
Outside git, the checkout also holds the strategist's secrets (`.env`), `replays/` and
`strategist-alerts.log`.

## The strategist's logs

The post-close and weekly runs copy the runner's logs into the strategist checkout (`trader
archive`): decision logs to `logs/decisions/`, every book's trades (sim accounts included) merged
into `logs/trades.csv`, and the paper and live books' equity and cashflow files to
`logs/<book>_equity.csv` and `logs/<book>_cashflows.csv`. `logs/probe_report.json` is the latest
probe report.

Retention (`trader compact`, weekly): daily journals older than 28 days are deleted once the
week's weekly journal exists; monthlies older than 400 days once the yearly exists; archived
decision logs after 90 days; replay runs after 14 days, unless their name starts with `keep-`.

## The strategist's run files

Outside both repositories, in `trader`'s home:

| Path | What |
|---|---|
| `~/.local/state/trader/<time>-<kind>.log` | One log per strategist run, kept 14 days |
| `~/.local/state/trader/strategist.lock` | Held for each whole run. `trading-deploy` takes it too, and refuses while a run holds it |
| `~/.local/share/trader/venv` | `trader`'s virtual environment of the deployed code, built by `2-strategist.sh` and synced with the deployed `uv.lock` at the start of each run |
| `~/.local/bin/trader`, `trader-python`, `trader-test` | The commands that run it (from `scripts/` in the code) |
