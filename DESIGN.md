# Autonomous Day-Trading Experiment — Design

Agreed 2026-09-27. Observe phase day 1: **Monday 2026-10-05**.

## Goal
Opus 5.5 strategist gets ~£200 (held as USD) and autonomy to invent and run an intraday strategy.
- Primary: beat buy-and-hold SPY risk-adjusted, net of slippage (measured on unit NAV).
- Standing aside is a valid outcome; every trade needs a written reason.
- Exploration mandate: ≥1 genuinely novel hypothesis in shadow per week, failures recorded.
- Honest self-assessment in weekly PR ("what I believed that was wrong", paper-vs-live, luck vs edge).
- Quarterly reviews (first at 3 months, mid-point at 6 weeks); runs indefinitely if successful.

## Stack
- Broker: **Alpaca**, US stocks/ETFs, **cash account**, fractional shares. Funded once in USD; all accounting in USD. W-8BEN.
- Market data: **free IEX** websocket 1-min bars live (~30-symbol liquid universe); **historical SIP** (>15 min old) for research/backtests. Alpaca news API + web search for context.
- Classifier: **TypeSafe Jev via OpenRouter** (≈$0.042/M input tokens, output free) behind an adapter; local fallback NanoJev/Laya. Smoke-test before building on it.
- Strategist: headless `claude -p` from systemd user timers on the Max subscription (wrapper unsets `ANTHROPIC_API_KEY`).

## Schedule (UK time, DST-mismatch handled in code)
- ~13:45 pre-market check (short): news, confirm/tweak/stand down drafted classifiers.
- 14:30–21:00 runner daemon trades.
- ~21:30 post-close review (heavy): review decisions/fills/P&L, cashflow reconciliation, update strategy, draft tomorrow.
- Saturday: weekly retrospective + PR `strategist` → `main`; compaction.

## Hard guardrails (code on `main`, owned by runner user, not agent-editable)
- Long-only; no shorting, margin or options.
- Whitelisted order types; every entry is a bracket order with a stop.
- Max 25% of equity per position.
- Per book: planned stop losses (held + pending) plus today's realised loss ≤ 3% of equity; stocks and equity ETFs ≤ 75%; each overlapping ETF/constituent bucket ≤ 50% (`src/trader/allocator.py`, #74). Entries are shrunk or skipped, never enlarged; a shrunk entry under max($10, 25% of its request) is skipped, so dust never counts as a trade.
- Daily loss −5% → kill switch: flatten, no new trades until next strategist run.
- **Trailing −30% from unit-NAV high-water mark** → halt, revert to paper, alert.
- Absolute equity floor **$50** → halt.
- Flatten 15 min before close (no overnight holds; relaxing requires human approval).
- Universe: liquid US stocks/ETFs, min price/volume, no penny stocks.
- Only the human moves money; no transfer permissions anywhere in the system.

## Classifiers
- Features: deterministic Python. Seed library plus **strategist-authored custom features** in `features/custom/`: pure functions (bars → number, no network/fs), must pass test harness **and backtest gate** on re-fetched SIP data before use.
- Jev gets compact state snapshot + strategist question; returns `ENTER`/`HOLD`/`EXIT`/`STAND_DOWN` with probabilities.
- `classifiers.yaml` per classifier: tickers, question/criteria, thresholds, size, max trades, re-arm vs retire, time window, stop/target, cadence (every N min or on feature trigger), mode (shadow/live).
- Runner validates spec + runs harness itself; invalid → classifiers off + alert.

## Phases
0. Observe (~days 1–5): research, hypotheses, custom features, no classifiers.
1. Paper (~days 5–10+): classifiers on Alpaca paper; slippage haircut (~0.05%/side) in review.
- Go-live gate (all): ≥10 trading days; ≥20 paper trades, positive expectancy after slippage, no simulated −5% day; strategist go-live memo in weekly PR; **human merge flips `mode: live`**.
- First live week at half size. Shadow mode permanent: new/changed classifiers must prove themselves on paper; strategist promotes per-classifier with same criteria.
- Auto-demote to paper on −30% HWM halt or live badly diverging from paper.

## Accounting
- Unit NAV (fund-style); deposits/withdrawals auto-detected from Alpaca account activities into `logs/cashflows.csv`. Sizing is % of current equity.

## Security split
- `trader` (Claude): `/srv/trading/strategist` checkout (`strategist` branch), **paper keys only**, GitHub fine-grained PAT (Contents + PRs rw) in `~/.config/trading/` 600.
- `runner` (dedicated, **no sudo**): `/srv/trading/main` (deployed code), `/srv/trading/runtime` (logs/fills/heartbeats, trader read-only), **live keys** in `/home/runner/.config/trading/` 600, systemd user services with `UMask=0027`. It executes strategist feature code, so it must not be able to escalate (the human's admin account has sudo, so it isn't used).
- **Deploys are human-only:** `sudo -u runner trading-deploy`.
  - GitHub branch protection isn't enforced on free private repos, so the token *can* push to `main`, and this step is the real gate.
  - Code is reviewed in its PR. If every commit since the last deploy is a GitHub-signed PR merge commit, the deploy lists the PRs and proceeds. The deployed code checks this against a pinned key (#39).
  - Anything else shows its diff and requires `yes`.
  - The human checks that the listed PRs are ones they merged.
  - Never mid-session: the runner unit holds a shared `flock` on `runtime/session.lock` for the whole session. The deploy refuses while that lock is held or systemd shows the runner up; it doesn't wait. It holds the lock exclusively only around the switch (checkout, `uv sync`, unit files), and a runner starting at 12:50 waits that out.
- Code changes to `runner/`/`guardrails/` via `proposal/*` branches → human-merged PRs.

## Repo layout
```
CLAUDE.md                 charter, guardrail summary, file map, run procedures
guardrails/ runner/ dashboard/ features/lib/   (main only)
features/custom/          strategist-authored features
state/strategy.md         living thesis, rewritten, ~2–3k words cap
state/classifiers.yaml    active specs
state/watchlist.md        untraded hypotheses
journal/daily|weekly|monthly|yearly/
logs/trades.csv           permanent (tax record; rows with book sim:<id> are simulated, not real trades)
logs/cashflows.csv        permanent
logs/decisions/*.jsonl.gz one line per classifier call
```
- Never store market data; re-fetch. Git history is the strategy history.
- Retention: dailies deleted after 4 weeks (once weekly exists); decision logs after 3 months; monthlies → yearly.
- Strategist reads fixed-size context: CLAUDE.md, strategy.md, last 5 dailies, last 4 weeklies, day's logs.
- Target < 100 MB/year.

## Alerts & dashboard
- ntfy.sh (secret topic): kill switch, halts, daemon crash, failed strategist run/auth, order rejections, **daily P&L one-liner**.
- GitHub issues labelled `needs-human`: go-live memo, proposals, strategist requests.
- Watchdog timer every 10 min in market hours: daemon heartbeat + last-run staleness.
- Dashboard: FastAPI + single page, `tailscale serve` HTTPS; viewing and STOP restricted to the dashboard users in `config.yaml` via Tailscale identity header. Pages: Today (accounts, equity with buy-and-hold SPY, health, positions, live classifier states), Scoreboard (per classifier and per family), Rules (each classifier's settings in plain words), Trades, Strategist's notes (strategy/journal), System and links. Only control: STOP (flatten, cancel, halt; confirm).

## Defaults (decided without discussion)
- Jev unavailable → fail closed: no new entries, existing brackets protect positions, alert.
- Broker is source of truth; runner reconciles positions/orders on start; idempotent `client_order_id`s.
- Market holidays/half-days from Alpaca calendar API; strategist runs skip non-trading days. The runner saves each day's open and close in `runtime/session.json`; if the calendar can't be read on a same-day restart (retried briefly), it reuses them, with an alert. Otherwise it never guesses times or decides "no session" without the calendar: it alerts (hourly at most) and exits, and systemd's restart is the retry (#125).

## Human to-dos
- Run the setup commands as the admin account (see the installation tutorial in `docs/`).
- Alpaca account (KYC, USD funding, W-8BEN); paper keys early.
- GitHub repo + fine-grained PAT; protect `main`.
- OpenRouter credit (few $); ntfy app.

## Changes made during the build (2026-09-27)
- **No bracket orders.** Alpaca rejects them for fractional quantities, and at this size almost every position is fractional. Instead:
  - Entries are notional market orders.
  - A server-side stop is placed where accepted.
  - The engine enforces stop and target on every 1-minute bar.
  - If the whole IEX stream goes stale (judged on SPY, never on a quiet single symbol) while paper or live holds positions, the runner polls the held symbols' IEX bars over REST once a minute (6 s timeout) and merges them under the stream's, so stops, targets, trails and time stops keep running on real prices. Entries stay blocked; a failed poll alerts and leaves the server stop and kill switch (#50).
  - STOP flattens directly through Alpaca if the runner is dead.
- **One engine, three modes** (replay / paper / live). Replay drives the backtest gate, strategist research and dashboard demos through the same code as live.
- **`control_orb`:** a permanent shadow control classifier (opening-range breakout, SPY/QQQ). It's a benchmark alongside SPY and is labelled as not a hint.
- **Decision model:** Jev `typesafe/jev-1.13` via OpenRouter's Decisions API (`/api/alpha/decisions`).
  - Smoke test: 0.43 s and about $0.00002 per call.
  - A 5-day replay of the control cost $0.0036.
  - Jev sees only dimensionless features, never dates or absolute prices.
- **The `strategist` branch may only change `state/`, `journal/`, `features/custom/` and `logs/`.** The wrapper reverts anything else, including commits made or pushed during the run, and publishes as a fast-forward of `origin/strategist`. Code changes go via `proposal/*` PRs.
- **Timing:**
  - The runner starts at 12:50 UK and loads classifiers 2 minutes before the open, so the pre-market run can still edit them.
  - Pre-market is gated to 30–75 minutes before the open and post-close to after the close, both from Alpaca's calendar, which also handles UK/US DST offsets.
- **Day-start equity is persisted**, so a mid-session restart doesn't reset the −5% baseline. If equity can't be read at the start (retried briefly, then alerted), the session still runs, with exits, stops and the flatten, but makes no new entries: the baseline stands in as the last NAV mark (never marked into NAV). When equity reads again, it becomes the baseline only if nothing was held, so it can't have moved since the open, and entries resume; otherwise, or with no stand-in at all (the first session), the read may only raise the baseline and there are no new entries that day (#119). Each day's equity log opens with a row at the day-start equity (once a day, so a restart adds none), so the gate's worst day counts from the open, not from the first 5-minute mark. A stand-in never goes in the log: after an unreadable start it opens at the first real read if nothing was held, otherwise at the first 5-minute mark (#50). Open positions are persisted in `entries.json`, and every entry order in `pending.json` before it is sent, so an order whose answer was lost is found by its client order id and adopted (#60). Exit legs already booked are kept per order id in `entries.json`, so a retried or restarted exit never books one twice (#61). Unknown broker positions are closed by the first tick after the open (retried each minute), not at start-up, which is usually before the open, when a market sell can't fill. Everything held at start-up stays subscribed to market data, even if its classifier is gone, so its stops and exits work on real prices, as long as the stream's 30-symbol limit leaves room (the classifiers' symbols come first; anything left out is alerted).
- **Replays live in `replays/`** in the strategist checkout (the runner's `runtime/` is read-only to the strategist).

## Automatic go-live (2026-09-27, replaces "human merge flips mode: live")
- The user wants the system to run unattended. Go-live is now automatic, with a veto window. `src/trader/golive.py`:
  - The runner evaluates the gate after each session from its own paper logs (which the strategist can't write), with thresholds in code on `main`: at least 10 trading days since 2026-10-05, at least 20 non-control closed trades, positive expectancy after 0.05%/side slippage, no day ≤ −5%, and live equity of at least $100.
  - Pass → **armed**, with an ntfy each session → after 3 more paper sessions, **live** at half size for the first 5 sessions.
  - (#63) Arming is reversible: the gate (plus "paper book not halted") is re-checked after each armed session and again just before activation. A failure, or logs that can't be read or hold invalid values, disarms back to pending (fail closed, one alert per episode); a re-pass starts a fresh full veto window. Every automatic write to `golive.json` is a compare-and-swap under a lock, so a HOLD pressed meanwhile always wins.
  - Veto: HOLD LIVE (dashboard, identity-gated) or `trader hold-live`. Re-arm: `trader release-live` (human).
  - (#115) An unreadable or invalid `golive.json` reads as a non-live `corrupt` state: paper, one alert, and no automatic transition over it. Only a human HOLD or release replaces it, setting the bad file aside for diagnosis.
  - If the live book halts, it's demoted to paper, and re-arming needs the human.
- `config/mode.yaml` is a human override: `auto` (default) | `paper` | `live`, effective after a human deploy.
- The `control_` id prefix is reserved for control classifiers, which the gate excludes.

## Scoreboard and classifier families (2026-09-27)
- The human wants to see whether the strategist's own ideas beat conventional ones and the control, and may steer the project by it.
- Each non-control classifier carries `family: novel | conventional`, set honestly by the strategist. `trader validate` requires it, but the model never rejects the file over it: a bad label shows as "unlabelled" with an info alert, because stopping all trading over a label would be absurd. It's a label, not behaviour, so it's left out of the spec identity and changing it doesn't restart the promotion record. The runner remembers each id's family in `promotion.json`, so classifiers removed from the file stay on the board (marked "removed").
- The dashboard scoreboard shows, per classifier and pooled per family, closed trades, win rate, average net return per trade after 0.05%/side slippage with a 95% t-interval clustered by trading day (same-day trades share the tape), total $ and promotion progress (paper book only). The live board ignores trades before the experiment start, like the gate; replays charge no extra slippage since sim fills include it. It also shows a race chart of cumulative net P&L and novel-vs-conventional and novel-vs-control Welch comparisons.
- Verdicts are deliberately cautious: nothing is judged under 10 trades or 3 trading days, and otherwise "can't tell from luck yet" until the interval clears zero.
- The engine records SPY's open and close each session in `benchmark.csv`, and the Performance chart draws buy-and-hold SPY next to NAV.

## Parked ideas
Considered and deliberately set aside. Reopen only with the human, and say what has changed since.

- **Overnight holds** (parked 2026-09-27). The flatten-before-close guardrail stays.
  - This system is built to watch an open market: minute-bar features, Jev deciding from recent 1-minute returns, stops enforced every bar, and the kill switch and STOP. Overnight there's nothing to watch, just one close-to-open jump driven by news, earnings and foreign markets, so none of that edge or protection carries over.
  - Stops don't protect against gaps, nothing supervises the book outside the session, and fractional positions often lack server-side stops.
  - Starting each day flat keeps P&L attributable to that day's decisions, which the weekly luck-vs-edge assessment relies on.
  - The documented overnight return premium is real, but it's a broad market effect (closer to "hold the index overnight") rather than a pattern to detect intraday, so it wouldn't test the strategist's skill.
  - If ever reopened: ETFs only (no earnings gaps), a reduced size cap, a separate overnight-drawdown limit, a news/earnings calendar check, shadow first, and a human-reviewed guardrail change on `main`.
