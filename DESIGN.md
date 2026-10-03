# Autonomous Day-Trading Experiment — Design

Agreed 2026-09-27; observe phase day 1 is `experiment.start_date` in `config.yaml`.

This page is the **current contract**: every statement above [History](#history) is in effect
now. Decisions that were later replaced are kept, dated, in that last section, so nothing here
needs reading against an amendment. Each rule says who enforces it: **code** (on `main`, owned by
the `runner` user, not editable by the strategist), the **charter** (`CLAUDE.md`, instructions the
strategist follows) or the **human**. Where a safeguard exists only as an instruction, it says so;
see [Not enforced in code](#not-enforced-in-code).

Numbers in parentheses, such as (#39), refer to issues and pull requests in the project's original
private repository, which holds the review record up to the repository split (2026-10). They are
not links to this repository's tracker.

## Goal
Opus 5.5 strategist gets ~£200 (held as USD) and autonomy to invent and run an intraday strategy.
- Primary: beat buy-and-hold SPY risk-adjusted, net of slippage (measured on unit NAV).
- Standing aside is a valid outcome; every classifier needs a written reason.
- Exploration mandate: ≥1 genuinely novel hypothesis per week, in shadow or sim, or investigated and rejected before trading; each with a falsifiable record and a pre-registered decision checkpoint; failures recorded (charter; `docs/explanations/hypotheses.md`).
- Honest self-assessment in the weekly report ("what I believed that was wrong", paper-vs-live, luck vs edge), published as a weekly issue in the data repo.
- Quarterly reviews (first at 3 months, mid-point at 6 weeks); runs indefinitely if successful.

## Stack
- Broker: **Alpaca**, US stocks/ETFs, **cash account**, fractional shares. Funded once in USD; all accounting in USD. W-8BEN.
- Market data: **free IEX** websocket 1-min bars live (at most 30 symbols); **historical SIP** (>15 min old) for research and backtests. Alpaca news API + web search for context.
- Decision model: **TypeSafe Jev via OpenRouter** (`models.jev`, `typesafe/jev-1.13` as shipped) through its Decisions API, behind an adapter (a local model can be swapped in by implementing its `decide`). About $0.00002 per call.
- Strategist: headless `claude -p` from systemd user timers on the Max subscription (the wrapper unsets `ANTHROPIC_API_KEY`).

## Schedule
Times are US Eastern; the host's timers fire on the operator's clock (`schedule` in `config.yaml`), and everything that matters gates on Alpaca's calendar, which handles holidays, half-days and UK/US daylight-saving mismatches.
- **Runner:** starts on weekdays at `schedule.runner_start` (12:50 UK as shipped), loads the classifiers 2 minutes before the open, trades 09:30 until the close, and exits after it.
- **Pre-market strategist run:** once, 30–75 minutes before the open (25-minute limit): news, confirm, tweak or stand down the drafted classifiers.
- **Post-close run:** at least 20 minutes after the close (50-minute limit): review decisions, fills and P&L, research, rewrite the strategy, draft tomorrow.
- **Weekly run:** Saturday: retrospective, weekly issue, compaction.
- **Housekeeping:** daily, no Claude: credit, token, deploy and disk checks.
- **Watchdog:** every 10 minutes on weekdays: runner heartbeat in market hours, post-close run staleness.

## Hard guardrails (code)
- Long-only; no shorting, margin or options.
- Symbols only from `config/universe.yaml` (a curated liquid list in the code repo); price at least $5.
- At most 25% of equity per position, one position per symbol, within settled cash.
- Every entry has a stop below it (at most 10% away) and a target above it, enforced by the engine (see [Orders and exits](#orders-and-exits)).
- Per book (paper, live and each sim account): planned stop losses (held + pending) plus today's realised loss ≤ 3% of equity; stocks and equity ETFs ≤ 75%; each overlapping ETF/constituent bucket ≤ 50% (`src/trader/allocator.py`, #74). Entries are shrunk or skipped, never enlarged; a shrunk entry under max($10, 25% of its request) is skipped, so dust never counts as a trade.
- Daily loss −5% from the day-start equity → kill switch: flatten that book, no new entries until the next session.
- **−30% from the unit-NAV high-water mark**, or equity under **$50** → halt: flatten, block until a human clears it. A live-book halt also demotes go-live to paper.
- Flatten from 15 minutes before the close; nothing is held overnight (relaxing it needs human approval).
- Only the human moves money; no transfer permissions anywhere in the system.

The constants are listed in `docs/reference/guardrails.md`.

## Orders and exits
Alpaca rejects bracket orders for fractional quantities, and at this size almost every position is fractional, so there are no brackets. The order types the code sends (`src/trader/broker.py`) are:
- **Entries:** a notional market buy (the default), or, with `entry_order: {type: limit}`, a limit buy resting below the last price that is cancelled if unfilled after `expire_min`. Both are day orders with idempotent client order ids.
- **Server-side stop:** a stop sell for the position's quantity, placed where Alpaca accepts one (often not for fractional quantities) and replaced when the engine raises the stop.
- **Exits:** a market sell for the quantity held, or Alpaca's close-position.

The engine enforces each position's stop, target, scale-out, trail and time stop on every 1-minute bar, whether or not a server-side stop exists. If the whole IEX stream goes stale (judged on SPY, never on a quiet single symbol) while paper or live holds positions, the runner polls the held symbols' IEX bars over REST once a minute (6 s timeout) and merges them under the stream's, so those exits keep running on real prices; entries stay blocked, and a failed poll alerts and leaves the server stop and kill switch (#50). STOP flattens directly through Alpaca if the runner is dead.

## Classifiers
- **One engine, three modes** (replay / paper / live). Replay drives the backtest gate, strategist research and dashboard demos through the same code as live.
- Features: deterministic Python. A seed library plus **strategist-authored custom features** in `features/custom/`: pure functions (bars → number) that run only in a bubblewrap sandbox, never in the runner. At each session start the runner gates them on recent SIP sessions of SPY and QQQ (finite on ≥80% of bars after 30 minutes, under 5 ms per call). A classifier that uses a rejected feature makes the whole file invalid, so nothing trades that day.
- Jev gets a compact, dimensionless state (features, minutes since open, the last ten 1-minute returns, the position if held, the classifier's `context`; never dates or absolute prices) plus the classifier's question, and returns probabilities over its criteria: `ENTER` with `WAIT` and/or `STAND_DOWN` for entries, `HOLD`/`EXIT` for exits.
- `state/classifiers.yaml`, per classifier: symbols, question and criteria, thresholds, size, max trades, re-arm vs retire, time window, stop/target, cadence and trigger, the optional execution toolkit, `family`, and `mode`:
  - `shadow` (default): trades the shared paper account;
  - `live`: trades the live account once the account is live and the classifier is eligible; otherwise it runs as shadow;
  - `sim`: trades its own simulated account (`capital.sim_cash`) on the live bars;
  - `probe`: asks the entry question and logs the answer; never orders.
- The schema is strict (`src/trader/classifier.py`). At session start, a classifier whose only fault is an unknown key is left out for the day with an urgent alert and the rest trade (#150); any other problem rejects the whole file, and nothing trades that day.
- **`control_orb`:** a permanent shadow control classifier (opening-range breakout, SPY/QQQ). It's a benchmark alongside SPY and is labelled as not a hint. The `control_` prefix is reserved for controls, which the gate excludes; `test_` is reserved for pre-launch plumbing, which the runner drops from `experiment.start_date`.

## Timing, reloads and restarts
**Windows and always-on exits.** A classifier's `window` (US Eastern, default 09:45–15:30) bounds only its model questions: entries and the model's exit question are asked only inside it, at most every `cadence_min`. Everything protective runs all session, every minute, inside or outside any window: stops, targets, scale-outs, trails and time stops on every bar; the kill switch, halt and STOP checks; and, from 15 minutes before the close, the flatten. A position still open when its window ends is held until one of those closes it, at the latest by the flatten. A stale feed or a failed model call blocks new entries (a failed call also pauses model exits for 5 minutes); the mechanical exits carry on.

**When a change takes effect.**

| Change | Read | Takes effect |
|---|---|---|
| `state/classifiers.yaml`, `features/custom/` | once per runner start: 2 minutes before the open, or at once if the runner starts later | the next session; an edit made during a session is picked up only if the runner restarts |
| Paper or live (`golive.json`, `config/mode.yaml`) | once, when the runner process starts (`schedule.runner_start`, 12:50 UK as shipped, well before the open; or at once on a restart) | that session. The final gate check, funding check and switch to live all happen then, so a HOLD LIVE pressed after that start, even before the open, applies only from the next session (STOP is the immediate control) |
| Promotion eligibility (`mode: live`) | at each runner start, after the classifiers load | that session |
| Code, `config.yaml`, `config/mode.yaml`, the universe | only after a human deploy, which never happens mid-session | the runner's next start |

Each open position keeps the rules it was opened with (`entries.json`), whatever the file says later. The pre-market run must finish before the load (its 25-minute limit inside the 30–75-minute window ensures that), and the post-close run edits the next session's file.

**Restarts.** systemd restarts a failed runner after 30 seconds, and the runner re-reads the classifier file, the go-live state and the promotion records as at any start. Everything the session needs survives in the runtime directory:
- **Positions and orders:** open positions in `entries.json`, with the rules each was opened with; every entry order in `pending.json` before it is sent, so an order whose answer was lost is found by its client order id and adopted (#60); exit legs already booked, per order id, so a retried or restarted exit never books one twice (#61). Positions that closed while the runner was down are booked from the broker's fill, or at the stop as `(price estimated)` if none is found.
- **Classifier state:** today's per-symbol trade counts and stand-downs (`classifier_state.json`), so `max_trades` and retirements still hold.
- **Risk state:** the day-start equity, settled cash at the open, today's buys and any kill switch or STOP (`risk.json`), so a restart doesn't reset the −5% baseline or lift a block; a halt is sticky.
- **Bars:** a stream that subscribes after the open (a restart, or a slow start) catches up on today's bars over REST at its first tick, after subscribing, so no minute falls between the two (not inside the flatten window).
- **Unknown positions:** anything held that the engine isn't tracking is sold by the first tick after the open (retried each minute), not at start-up, which is usually before the open, when a market sell can't fill.
- **Streaming:** everything held at start-up stays subscribed, even if its classifier is gone, so its exits work on real prices, as long as the 30-symbol limit leaves room (the classifiers' symbols come first; anything left out is alerted).
- **After the close:** a runner that starts after the close does nothing but alert if positions are still held.

**Day-start equity.** If equity can't be read at the start (retried briefly, then alerted), the session still runs, with exits, stops and the flatten, but makes no new entries: the baseline stands in as the last NAV mark (never marked into NAV). When equity reads again, it becomes the baseline only if nothing was held, so it can't have moved since the open, and entries resume; otherwise, or with no stand-in at all (the first session), the read may only raise the baseline and there are no new entries that day (#119). Each day's equity log opens with a row at the day-start equity (once a day, so a restart adds none), so the gate's worst day counts from the open; a stand-in never goes in the log (#50).

## Rule lifecycle
A rule (classifier) moves through these states. The first four are per classifier; the go-live states belong to the whole account, and a classifier reaches the live account only when both allow it.

| State | Who or what moves it here | Evidence required | Persisted in | Takes effect | Enforced by |
|---|---|---|---|---|---|
| **observe** (phase) | the calendar: from `experiment.start_date`, roughly days 1–5 | none | the phase line in `state/strategy.md` | at once | charter (the runner trades whatever the file holds; only `control_orb` should be there) |
| **probe** | strategist sets `mode: probe` | none | `state/classifiers.yaml`; answers in the decisions log | next runner start | code; counts towards nothing |
| **sim** | strategist sets `mode: sim` | none | `state/classifiers.yaml`; its account in `runtime/books/sim/<id>/` | next runner start | code; counts towards nothing |
| **shadow** | strategist sets `mode: shadow` (the default); also where the runner puts an ineligible `mode: live` | none to enter; its paper trades build its record | `state/classifiers.yaml`; the record (spec hash, start date, family) in `runtime/promotion.json` | next runner start | code |
| **eligible** (computed, not a mode) | the runner, at each start | ≥20 closed paper trades since its record started (and not before `experiment.start_date`), mean return > 0 after 0.05%/side; any spec change except `mode`, `enabled`, `family` restarts the record, and so does any edit to `features/custom/` if the spec uses a custom feature; sim, probe, replay and `(price estimated)` trades don't count | recomputed each start from the paper `trades.csv` and `promotion.json` | that session | code (`golive.enforce_promotion`) |
| **live** (rule) | strategist sets `mode: live` | eligible, and the account is live | `state/classifiers.yaml` | next runner start | code. While the account is paper, `mode: live` trades paper like shadow. Once it trades live its paper record stops growing, and it stays eligible until its spec changes |
| **demoted** (rule) | strategist sets `mode: shadow` when live underperforms its paper record; the runner runs it as shadow at any start where it isn't eligible (alerting when the account is live) | strategist's judgement | `state/classifiers.yaml` (the runner's downgrade isn't written back; it's re-decided each start) | next runner start | charter for underperformance; code for ineligibility |
| account **pending** | the start; a disarm; `trader release-live` | the gate, after each session: ≥10 trading days, ≥20 closed non-control paper trades, positive expectancy after slippage, no day ≤ −5%, paper book not halted | `runtime/golive.json` | — | code (`src/trader/golive.py`) |
| account **armed** | the runner, after a session in which the gate passes | the gate, re-checked after each of 3 paper sessions and again at the switch; a failure disarms to pending | `runtime/golive.json` | counts down per session | code |
| account **live** | the runner, at the first runner start (12:50 UK as shipped) after the 3 sessions | final gate check, and live equity ≥ $100 at that start (otherwise it stays armed and alerts each start) | `runtime/golive.json`; live session count in the live book's `risk.json` | that session; the first 5 live sessions after every return to live at half size | code |
| account **vetoed** | the human: HOLD LIVE (dashboard) or `trader hold-live`, from any state | none | `runtime/golive.json` | next runner start | code; only `trader release-live` leaves it |
| account **demoted** | the runner, at the end of a session in which the live book halted | the halt | `runtime/golive.json`; `halted` in the live book's `risk.json` | the live book is already flat and blocked; paper from the next start | code; the human clears the halt and releases |

`config/mode.yaml` (`paper` or `live`, deployed by the human) overrides the account states entirely while set. An unreadable `golive.json` reads as `corrupt`: paper, one alert, no automatic transition until the human holds or releases (#115).

## Go-live
- The user wants the system to run unattended, so go-live is automatic, with a veto window (`src/trader/golive.py`). The runner evaluates the gate after each session from its own paper logs, which the strategist can't write, with thresholds in code: at least 10 trading days since `experiment.start_date`, at least 20 non-control closed trades, positive expectancy after 0.05%/side slippage, no day ≤ −5%, and the paper book not halted.
- Pass → **armed**, with an alert each session → after 3 more paper sessions, **live**. The live account's funding (at least $100) is checked only then, at the start of the session that would go live; it isn't part of arming.
- (#63) Arming is reversible: the gate is re-checked after each armed session and again just before activation. A failure, or logs that can't be read or hold invalid values, disarms back to pending (fail closed, one alert per episode); a re-pass starts a fresh full veto window. Every automatic write to `golive.json` is a compare-and-swap under a lock, so a HOLD pressed meanwhile always wins.
- Veto: HOLD LIVE (dashboard, identity-gated) or `trader hold-live`. Re-arm: `trader release-live` (human).
- If the live book halts, it's demoted to paper, and re-arming needs the human.
- `config/mode.yaml` is a human override: `auto` (default) | `paper` | `live`, effective after a human deploy.

## Phases
0. **Observe** (~days 1–5): research, hypotheses, custom features; only `control_orb` runs (charter).
1. **Paper** (~day 5 onward): the strategist's classifiers on the Alpaca paper account; judged after a 0.05%/side slippage haircut.
2. **Live**, once go-live completes. Shadow stays permanent: new or changed classifiers prove themselves on paper, and the strategist promotes them one at a time with `mode: live` once each is eligible (see [Rule lifecycle](#rule-lifecycle)).

## Scoreboard and classifier families
- The human wants to see whether the strategist's own ideas beat conventional ones and the control, and may steer the project by it.
- Each non-control classifier carries `family: novel | conventional`, set honestly by the strategist. `trader validate` requires it, but the runner never rejects the file over it: a bad label shows as "unlabelled" with an info alert, because stopping all trading over a label would be absurd. It's a label, not behaviour, so it's left out of the spec identity and changing it doesn't restart the promotion record. The runner remembers each id's family in `promotion.json`, so classifiers removed from the file stay on the board (marked "removed").
- The dashboard scoreboard shows, per classifier and pooled per family, closed trades, win rate, average net return per trade after 0.05%/side slippage with a 95% t-interval clustered by trading day (same-day trades share the tape), total $ and promotion progress (paper book only). The live board ignores trades before the experiment start, like the gate; replays charge no extra slippage since sim fills include it. It also shows a race chart of cumulative net P&L and novel-vs-conventional and novel-vs-control Welch comparisons.
- Verdicts are deliberately cautious: nothing is judged under 10 trades or 3 trading days, and otherwise "can't tell from luck yet" until the interval clears zero.
- The engine records SPY's open and close each session in `benchmark.csv`, and its price at each 5-minute equity mark in `spy_marks.csv`, so the Performance chart draws buy-and-hold SPY next to NAV, moving intraday on the 1D view.

## Accounting
- Unit NAV (fund-style); deposits/withdrawals auto-detected from Alpaca account activities into the live book's `cashflows.csv`. Sizing is % of current equity.

## Security split
- **Two repositories** (since 2026-10-03):
  - **Public code**, `gilesknap/jev-trader`: all code, tests, docs, the charter (`CLAUDE.md`) and prompts, the systemd units, setup scripts, `trading-deploy`, the default universe, and a data-repo template (`templates/data/`). Nothing owner-specific.
  - **A private data repo per owner**, two unrelated branches, never merged: `main` is human-owned deployment config (`config.yaml`, `config/mode.yaml`, the rendered timers and `trader.env`); `strategist` is the strategist's data (`state/`, `journal/`, `logs/`, `features/custom/`, `proposals/`, a stub `CLAUDE.md`).
- **Three checkouts on the host:** `/srv/trading/main` (public code, runner-owned, deployed), `/srv/trading/config` (data `main`, runner-owned, deployed, read-only to trader) and `/srv/trading/strategist` (data `strategist`, trader-owned). The runner never reads config from a directory trader can write. Code paths: `TRADER_CODE_ROOT`, `TRADER_DATA_ROOT` (config), `TRADER_STRATEGIST_ROOT`, `TRADER_RUNTIME` (`/srv/trading/runtime`).
- `trader` (Claude): the strategist checkout, **paper keys only**, a fine-grained GitHub PAT for the **data repo only** (Contents, PRs, Issues rw), so it can't write anything public. It runs the **deployed** code: its own venv (`~/.local/share/trader/venv`, built from `/srv/trading/main`) through a `trader` shim, and the wrapper itself is run from the deployed checkout, so the strategist can't change the script that path-checks it. The charter is passed with `--append-system-prompt-file` from the deployed checkout. Claude Code deny rules close the obvious routes to GitHub's public content (porous; the charter carries the rule too).
- `runner` (dedicated, **no sudo**): `/srv/trading/main`, `/srv/trading/config`, `/srv/trading/runtime` (logs/fills/heartbeats, trader read-only), **live keys** in `/home/runner/.config/trading/` 600, systemd user services with `UMask=0027`. It executes strategist feature code (in a sandbox), so it must not be able to escalate (the human's admin account has sudo, so it isn't used).
- **The `strategist` branch may only change `state/`, `journal/`, `features/custom/`, `logs/` and `proposals/`.** The wrapper reverts anything else, including commits made or pushed during the run, and publishes as a fast-forward of `origin/strategist`.
- **Code proposals stay private:** the strategist writes a `git format-patch` series plus rationale under `proposals/<topic>/` on its branch and opens a `needs-human` issue in the data repo. The human applies it on their fork and writes the public PR text.
- **Deploys are human-only:** `sudo -u runner trading-deploy`, covering both repos together.
  - Code is reviewed in its PR. If every code commit since the last deploy is a GitHub-signed PR merge commit, the deploy lists the PRs and proceeds. The deployed code checks this against a pinned key (#39). Anything else shows its diff and requires `yes`. The public repo's `main` has a ruleset requiring PRs.
  - Data `main` is a free private repo without branch protection, and the strategist's token can push to it or merge its own PRs there. So the deploy **always** shows the full data diff and needs `yes`, signed or not.
  - The candidate code is tested against the candidate config, the rendered files must match `config.yaml` under the new code, and the strategist's live specs are validated (a warning only).
  - Never mid-session: the runner unit holds a shared `flock` on `runtime/session.lock` for the whole session. The deploy refuses while that lock is held or systemd shows the runner up; it doesn't wait. It holds the lock exclusively only around the switch (checkouts, `uv sync`, unit files), and a runner starting at 12:50 waits that out.
  - Never mid-run: the strategist holds its run lock for each whole run; the deploy refuses while it's held and holds it across the switch.
- Owners deploy code straight from upstream (trusting its merges) or from their own fork, taking upstream by a merge PR they review (recommended with real money).

## Repo layout
Public code repo:
```
CLAUDE.md                 charter, guardrail summary, file map, run procedures
prompts/                  per-run prompts (premarket, postclose, weekly)
src/trader/               runner, engine, guardrails, allocator, features library, dashboard
config/universe.yaml      the universe (must match the allocator's buckets)
deploy/                   trading-deploy, systemd units, templates, setup scripts
scripts/                  strategist.sh (the wrapper), the trader shim and helpers
templates/data/           what a new owner's data repo starts with
tests/ docs/
```
Private data repo, branch `main` (deployed to `/srv/trading/config`):
```
config.yaml               owner, dashboard users, start date, schedule, models
config/mode.yaml          auto | paper | live (human override)
deploy/systemd*/          timers and trader.env rendered from config.yaml
```
Private data repo, branch `strategist` (`/srv/trading/strategist`):
```
CLAUDE.md                 stub pointing at the charter in the code
features/custom/          strategist-authored features
state/strategy.md         living thesis, rewritten, ~2–3k words cap
state/classifiers.yaml    active specs
state/watchlist.md        hypothesis records, active and rejected
state/steering.md         the human's steering decisions (strategist reads, never edits)
journal/daily|weekly|monthly|yearly/
logs/trades.csv           permanent (tax record; rows with book sim:<id> are simulated, not real trades)
logs/<book>_equity.csv, logs/<book>_cashflows.csv   permanent
logs/decisions/*.jsonl.gz one line per classifier call (on disk, not committed)
proposals/<topic>/        code proposals: patch series + rationale
replays/                  replay output (git-ignored)
```
- **Replays.** `trader replay` writes to `TRADER_REPLAY_DIR`. Its default, `replay/` inside the runtime directory, suits a development checkout where one user owns everything. On the host the runtime directory belongs to `runner` and is read-only to the strategist, so the deployed environments (the `trader` shim, `trader-python`, the strategist's unit and `services.env`) set it to `/srv/trading/strategist/replays`.
- Never store market data; re-fetch. Git history is the strategy history.
- Retention: dailies deleted after 4 weeks (once weekly exists); decision logs after 3 months; monthlies → yearly; replays after 14 days unless named `keep-*`.
- Strategist reads fixed-size context: CLAUDE.md, strategy.md, classifiers.yaml, watchlist.md, steering.md, last 5 dailies, last 4 weeklies, the logs it needs.
- Target < 100 MB/year.

## Alerts & dashboard
- ntfy.sh (secret topic): kill switch, halts, daemon crash, failed strategist run/auth, order rejections, **daily P&L one-liner**.
- GitHub issues in the data repo labelled `needs-human`: proposals and strategist requests; and a weekly issue (label `weekly`) carrying any go-live assessment.
- Watchdog timer every 10 min: daemon heartbeat in market hours + post-close run staleness.
- Dashboard: FastAPI + single page, `tailscale serve` HTTPS; viewing and controls restricted to the dashboard users in `config.yaml` via Tailscale identity header. Pages: Today (accounts, equity with buy-and-hold SPY, health, positions, live classifier states), Scoreboard (per classifier and per family), Rules (each classifier's settings in plain words), Trades, Probes, Strategist's notes (strategy/journal), System and links. Controls: STOP (flatten, cancel, halt for the day; confirm) and HOLD LIVE.

## Defaults
- Jev unavailable → fail closed: no new entries and no model exits for 5 minutes after a failed call; stops, targets and the flatten carry on; alert.
- Broker is source of truth; runner reconciles positions/orders on start; idempotent `client_order_id`s.
- Market holidays/half-days from Alpaca calendar API; strategist runs skip non-trading days. The runner saves each day's open and close in `runtime/session.json`; if the calendar can't be read on a same-day restart (retried briefly), it reuses them, with an alert. Otherwise it never guesses times or decides "no session" without the calendar: it alerts (hourly at most) and exits, and systemd's restart is the retry (#125).

## Not enforced in code
These are charter instructions or human practice, not code, and nothing stops a run that ignores them; the weekly review and the human's reading of it are the check.
- The observe phase: the runner trades whatever `state/classifiers.yaml` holds from the first session.
- Demoting a live classifier that underperforms its paper record, or whose live results diverge from paper: the strategist's job. The code demotes the whole account only on a live-book halt, and its only per-classifier downgrade is for an ineligible paper record, which live results never change.
- The weekly novelty mandate, the honesty of the `family` label, research hygiene and not trading for volume to reach the gate.
- Keeping `config/universe.yaml` to at most 30 symbols (a test only requires every symbol to be bucketed).

Edge cases the code does handle, with their limits, are in `docs/explanations/limitations.md`.

## Human to-dos
- Run the setup commands as the admin account (see the installation tutorial in `docs/`).
- Alpaca account (KYC, USD funding, W-8BEN); paper keys early.
- A private data repo (from `templates/data/` with `deploy/setup/0-data.sh`) + a fine-grained PAT for it only; optionally a fork of the code.
- OpenRouter credit (few $); ntfy app.

## Parked ideas
Considered and deliberately set aside. Reopen only with the human, and say what has changed since.

- **Overnight holds** (parked 2026-09-27). The flatten-before-close guardrail stays.
  - This system is built to watch an open market: minute-bar features, Jev deciding from recent 1-minute returns, stops enforced every bar, and the kill switch and STOP. Overnight there's nothing to watch, just one close-to-open jump driven by news, earnings and foreign markets, so none of that edge or protection carries over.
  - Stops don't protect against gaps, nothing supervises the book outside the session, and fractional positions often lack server-side stops.
  - Starting each day flat keeps P&L attributable to that day's decisions, which the weekly luck-vs-edge assessment relies on.
  - The documented overnight return premium is real, but it's a broad market effect (closer to "hold the index overnight") rather than a pattern to detect intraday, so it wouldn't test the strategist's skill.
  - If ever reopened: ETFs only (no earnings gaps), a reduced size cap, a separate overnight-drawdown limit, a news/earnings calendar check, shadow first, and a human-reviewed guardrail change on `main`.

## History
Superseded decisions, newest first. None of these is in effect; the sections above say what replaced them. The reasoning behind the decisions that stand is in the decision records (`docs/explanations/adr/`).

- **2026-10-03, repository split.** One private repository held code and data together. The strategist's branch could also change code through `proposal/*` pull requests, and the weekly retrospective was a pull request `strategist` → `main`. Since the split, code is public, data is private, code proposals are patch series under `proposals/` with a `needs-human` issue, and the weekly retrospective is an issue.
- **2026-09-29, strict classifier keys** (#150). An invalid classifier file used to switch every classifier off. Now a classifier whose only fault is an unknown key is dropped alone for the day; other problems still reject the whole file.
- **2026-09-28, more modes.** `mode` was `shadow | live`; `sim` (own simulated account) and `probe` (ask and log only) were added.
- **2026-09-27, automatic go-live.** The original gate needed a go-live memo in the weekly pull request, and a **human merge flipped `mode: live`**. Replaced the same day by the automatic gate with a 3-session veto window (see [Go-live](#go-live)). The original gate also listed live funding as a condition; it is checked only at the switch.
- **2026-09-27, no bracket orders.** The original design whitelisted order types and made every entry a bracket order with a stop. Alpaca rejects brackets for fractional quantities, so entries became notional market (or limit) orders with a server-side stop where accepted and engine-enforced exits (see [Orders and exits](#orders-and-exits)).
- **2026-09-27, original details since corrected.**
  - "Auto-demote to paper on live badly diverging from paper" was never built: the account is demoted only on a live-book halt, and per-classifier demotion is the strategist's job (see [Not enforced in code](#not-enforced-in-code)).
  - "First live week at half size" is implemented as the first 5 live sessions after every return to live: any paper session, or clearing a live halt, restarts the count.
  - The kill switch blocked entries "until the next strategist run"; it lasts until the next session.
  - Custom features were to pass a "backtest gate"; the gate checks they are finite and fast on recent sessions, not whether they are profitable.
  - The universe was to be filtered by minimum price and volume; it is a fixed list in `config/universe.yaml`, with a $5 price floor in code.
