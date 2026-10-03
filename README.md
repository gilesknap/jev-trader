# trader

An autonomous day-trading experiment. An Opus strategist (headless Claude Code on a Max subscription) designs
intraday classifiers. A runner daemon executes them on Alpaca, asking TypeSafe's **Jev** decision model for each
go/no-go, with hard guardrails enforced in code and by OS user separation.

- **DESIGN.md**: the agreed design and the reasons behind it.
- **CLAUDE.md**: the strategist's charter (the schema, rules and tools it works with).
- **SETUP.md**: setting up your own copy from scratch (VPS, Tailscale, Alpaca, OpenRouter, Claude, GitHub).
- This README: setup and operations.

## How it fits together

```
 trader (Claude, paper keys only)                runner (no sudo, live keys)
 ───────────────────────────────                 ───────────────────────────
 systemd timers → scripts/strategist.sh         systemd timer → trader run (one session/day)
   premarket / postclose / weekly                  reads  /srv/trading/strategist/state/classifiers.yaml
   edits state/, journal/, features/custom/        gates  features/custom/, validates the spec
   commits to branch `strategist`                  streams IEX bars → Engine → Jev → guardrails → Alpaca
   weekly PR strategist → main                     writes /srv/trading/runtime (status, logs, fills)
 /srv/trading/strategist  (git: strategist)      /srv/trading/main  (git: main, deployed code)
                                                 dashboard (FastAPI) ← tailscale serve (HTTPS, your login only)
```

There's one engine (`src/trader/engine.py`) with three modes:
- **replay:** historical SIP bars and a simulated broker.
- **paper:** Alpaca paper account.
- **live:** Alpaca live account. In live mode, `shadow` classifiers still trade the paper account.

## One-time setup

New to this? Start with **[SETUP.md](SETUP.md)**: it covers the accounts, the VPS and `config.yaml`, and walks through the table below in order.

Accounts:
- **`trader`**: Claude and the strategist, holding paper keys only.
- **`runner`**: the runner daemon and dashboard. It holds the live keys and has **no sudo**, because it executes strategist-authored feature code.
- **An admin account** (your own login) with sudo, which runs the setup scripts.

All scripts are idempotent, so re-running one is always safe. Run each as a single short command, not by pasting long command lists.

| # | As | Command | Does |
|---|---|---|---|
| 0 | trader | `git clone https://github.com/<owner>/<repo> /tmp/trading-setup` (the repo in `config.yaml`) | Gets the scripts before `/srv/trading` exists (a fresh install only) |
| 1 | admin | `sudo bash /tmp/trading-setup/deploy/setup/1-host.sh` | Creates the `trading` group, the `runner` user (no sudo, lingering) and `/srv/trading/{strategist,main,runtime}` with the right owners and modes |
| 2 | trader, **from a fresh login** | `bash /tmp/trading-setup/deploy/setup/2-strategist.sh` | Checks out the `strategist` branch, creates `.env` (paper keys only, generated ntfy topic), installs the strategist's systemd timers, links `~/trading` |
| 3a | admin | `sudo bash /srv/trading/strategist/deploy/setup/3-runner.sh key` | Creates `runner`'s deploy key and prints it. Add it on GitHub → Settings → Deploy keys, **read-only** |
| 3b | admin | `sudo bash /srv/trading/strategist/deploy/setup/3-runner.sh install` | Checks out `main` as runner, runs the tests, installs `trading-deploy`, the services and `tailscale serve` → dashboard socket |
| ✓ | admin | `sudo bash /srv/trading/main/deploy/setup/check.sh` | Verifies users, permissions, secrets placement, services, socket isolation and the strategist timers. Every line should read PASS |

After step 2, fill in `/srv/trading/strategist/.env` (`ALPACA_PAPER_KEY`, `ALPACA_PAPER_SECRET`, `OPENROUTER_API_KEY`) and subscribe to the printed `NTFY_TOPIC` in the ntfy app.

**Live keys go only in `runner`'s file**, `/home/runner/.config/trading/env`, as `ALPACA_LIVE_KEY` and `ALPACA_LIVE_SECRET`:
```bash
sudo -u runner -H nano /home/runner/.config/trading/env
```
Never put live keys in the strategist's `.env`. `check.sh` fails if they're there.

Also:
- **Reset the Alpaca paper account balance to about what you'll fund live with** (the original uses $250), so paper sizing matches reality.
- The dashboard users in `config.yaml` must include your Tailscale login.

**How the dashboard is protected:** it listens only on a Unix socket in `runner`'s private runtime directory, and `tailscale serve` proxies `https://<host>:8444` to that socket. A localhost TCP port would let any local user (including `trader`) forge the `Tailscale-User-Login` header. Tailscale adds that header to every request from your browser, including one another website triggers, so STOP and HOLD LIVE also refuse anything that isn't a same-origin `Content-Type: application/json` POST (a cross-site form can't send that type, and the CORS preflight a cross-site fetch needs is never approved). To call them by hand, send that content type.

**Pre-launch dry run.** Before observe day 1 (the experiment start date in `config.yaml`), `state/classifiers.yaml` on the `strategist` branch can hold the `test_*` classifiers from `deploy/prelaunch/classifiers.yaml` alongside `control_orb` (see [SETUP.md → Pre-launch](SETUP.md#8-pre-launch-12-trading-days)). They aren't strategies. They deliberately enter on AAPL, MSFT, XLF, XLI and XLV so that every execution path runs on paper before the experiment starts: Jev entry and exit calls, market and limit orders, the time stop, scale-out, the trailing stop and the end-of-day flatten. The go-live gate and the live scoreboard ignore trades before the start date, so they don't count towards anything. **Remove every `test_*` classifier before the start date.** The runner also disables this reserved prefix on and after the start date (using the exchange session date) with an urgent alert, before promotion or engine creation, and from that date `trader validate` rejects `test_` ids, so a real idea can't silently take the prefix. These plumbing rules therefore cannot continue generating experiment trades if cleanup is missed. Confirm the Jev-exit test actually records `reason == "classifier EXIT"` in `trades.csv`; a `time stop`, stop, target or EOD flatten does not establish that the classifier-exit path ran. The 0.5 exit threshold makes its observed 45–57% scores usable, but does not guarantee that path was exercised. Classifier changes take effect at the next session start. To apply one mid-session, restart the runner (it resumes from its saved state): `sudo -u runner XDG_RUNTIME_DIR=/run/user/$(id -u runner) systemctl --user restart trader-runner`.

**How the strategist's runs are billed:** they use your Max subscription. The wrapper unsets `ANTHROPIC_API_KEY`, so they can never switch to API billing. If the Claude login expires, runs fail and you get an ntfy alert.

## Daily operations

| When (UK) | What |
|---|---|
| 12:50 Mon–Fri | Runner starts, checks Alpaca's calendar, waits for the open |
| 30–75 min before open | Strategist pre-market run (short) |
| open–close | Runner trades. The watchdog checks its heartbeat every 10 minutes. Any position the engine didn't open (left from a crash, or **bought by hand**) is sold within about a minute of the first tick after the open, with an urgent alert |
| close − 15 min | Everything is flattened, retried every minute until the close. A failing book or tick can't skip it: the runner falls back to closing everything at the broker. Nothing is sent after the bell (it would queue for the next open); anything still held is alerted, to close by hand in Alpaca |
| close | Runner writes the day summary and sends the **Daily P&L** ntfy |
| 21:30 | Strategist post-close review: archive logs, research, rewrite strategy, draft tomorrow |
| Sat 10:00 | Weekly retrospective, compaction, PR `strategist` → `main` |

**Your weekly job:** review and merge the weekly PR, and handle any `needs-human` issues and `proposal/*` PRs.

### Deploying (human only)
Strategy changes (`state/`, `features/custom/`) need **no deploy**: the runner reads them from the strategist checkout at each session start.

Code, universe and mode changes on `main` reach the runner only when **you** run:
```bash
sudo -u runner trading-deploy
```
Review happens in the pull request. When every commit since the last deploy is a PR merge made by GitHub (a GitHub-signed merge commit), it just lists those PRs, runs the tests on the new commit, and deploys. Anything else needs its diff reviewed and a typed `yes`: a direct push, a squash or rebase merge, or rewritten history. `trading-deploy --dry-run` shows what would happen. The hidden-character and binary-file refusals always apply.

**Deploy outside the session.** The runner starts at 12:50 London on weekdays, waits for the open and exits after the US close (normally about 21:00 London, 20:00 in the two weeks a year when only one country has changed its clocks; check with `sudo -u runner XDG_RUNTIME_DIR=/run/user/$(id -u runner) systemctl --user status trader-runner`). While it runs it holds `/srv/trading/runtime/session.lock`, and a deploy never replaces its code:
- **During a session**, `trading-deploy` stops straight away with `REFUSING: a trading session is running (trader-runner is active); nothing was changed.` Deploy after the runner has exited, or before 12:50 on the next weekday.
- **Just before 12:50**, the review and the tests can overrun the runner's start. The session then wins: the deploy refuses at the switch with the same message plus `the tests passed, but deploy <sha> again after the session`. Nothing is changed and the runner trades on the code it started with.
- The lock is taken only for the switch itself (checkout, `uv sync`, unit files), never during the review, the prompt or the tests. If the timer fires during a switch, the runner waits for it (a few seconds, at most 10 minutes) and starts on the new code. If a switch hangs (a stuck `uv sync`), the runner retries every ~10 minutes until you stop the deploy.
- A runner that crashed doesn't leave a stale lock: the kernel releases it with the process. In the 30 s before systemd restarts a crashed runner, the deploy still refuses. A runner that has given up (`failed`) doesn't block a deploy.
- The deploy restarts the dashboard after the switch, so never during trading: at most while a runner that just started waits for the open. The 10-minute watchdog doesn't take the lock: it's a short one-off run, and at worst one run fails and the next one works.
- **STOP**, `clear-halt` and the other CLI commands also run from `/srv/trading/main`, and a session can't be deployed over, so they always see the code the runner is running.

A deploy updates only the runner's checkout. The strategist's checkout (`/srv/trading/strategist`, branch `strategist`) merges `origin/main` (and pushes the merge) once a strategist run has passed its session gating, and at the daily housekeeping run unless the checkout is off `strategist` or has uncommitted changes. It then re-executes the merged `scripts/strategist.sh`, after checking that it parses. If anything in `deploy/systemd-trader/` changed, reinstall the strategist timers by re-running `bash deploy/setup/2-strategist.sh` as `trader` (`check.sh` fails until you do).

This is the real gate. On the free plan the strategist's token *can* push to `main`, but nothing it pushes runs until you've deployed it. A direct push shows up as a diff to review.
- **Check the listed PRs are ones you merged.** The token could also open and merge a PR itself. That's a genuine GitHub merge, so only the list shows it.
- Each line shows the merge commit. `gh pr view N --json mergeCommit -q .mergeCommit.oid` confirms it belongs to PR N; the number and title alone come from the commit message.
- If a merge you didn't expect is listed, press Ctrl-C before the tests finish.

### Controls
- Run these as runner in `/srv/trading/main` with the services environment loaded: `set -a; . ~/.config/trading/services.env; set +a`. Without it, the commands that change state (`stop`, `rebase-paper`, `clear-halt`, `hold-live`, `release-live`) refuse and change nothing, rather than writing to a stray `runtime/` in the checkout (#108).
- **STOP**, from the dashboard or `uv run trader stop` as runner: flattens everything and halts for the rest of the day. If the runner is dead, it flattens directly through Alpaca.
- **After resetting the Alpaca paper account:** `uv run trader rebase-paper` as runner, so the reset isn't read as a drawdown.
- **Clear a halt:** `uv run trader clear-halt live|paper|sim/<id>` as runner. A halt (−30% from high-water mark or the $50 floor) never clears itself. Clearing it also rebases the high-water mark to the current NAV, so it doesn't immediately re-trigger.
- **Go live: automatic.**
  - The runner checks the gate after every session: at least 10 trading days, at least 20 strategist trades with positive expectancy after slippage, no −5% day, and live equity of at least $100 (thresholds unchanged). A halted paper book also blocks it.
  - When it passes, you get an ntfy and **3 paper sessions to veto**. Then it trades live at half size for the first 5 sessions.
  - Arming can be reversed. The gate is re-checked after each veto session and again just before the switch; if it fails (a losing session, a −5% day, a paper halt) it **disarms** back to pending with an ntfy, and a later re-pass starts a fresh 3-session window. Unreadable or invalid paper logs fail closed the same way, with one alert (not one per session) until they read cleanly.
  - **Veto:** HOLD LIVE on the dashboard, or `uv run trader hold-live` as runner. A HOLD always wins, even one pressed while the runner is re-checking the gate. **Re-arm:** `uv run trader release-live`. **Status:** `uv run trader golive`.
  - **Corrupt `runtime/golive.json`:** the runner stays on paper, sends one urgent ntfy and changes nothing by itself (it doesn't reset to pending: look first). HOLD LIVE or `trader release-live` replaces it with a clean vetoed or pending state and keeps the bad file as `golive.json.corrupt-<time>` (the newest 5 are kept).
  - **Force either way:** set `config/mode.yaml` to `paper` or `live`, then deploy.
- **Withdraw profits:** use Alpaca's dashboard. The runner picks the transfer up at the next session start and redeems NAV units, so it doesn't count as a loss.

### Unattended-operation alerts
Every alert is also logged: the runner's (and watchdog's, dashboard's) to `/srv/trading/runtime/alerts.log`. The strategist runs as `trader`, which can't write the runtime dir by design, so its alerts (the wrapper's failed runs, pushes and merges, and housekeeping's) go to `/srv/trading/strategist/strategist-alerts.log` (0640, gitignored) instead, and to the journal (`journalctl --user -u 'trader-strategist@*'` as trader).

A daily 09:00 `trader housekeeping` run (via `scripts/strategist.sh housekeeping`) sends an ntfy when:
- **OpenRouter credit** is under $3, or will run out within 21 days at the current burn rate.
- The key's spend limit is nearly used, or the key expires soon.
- The **GitHub token** expires within 21 days, or stops working.
- **Merged changes on `main` haven't been deployed** for 48 hours.
- **Disk** has less than 3 GB free.

Each alert repeats weekly, then daily once it's within 7 days.

### Useful commands
```bash
uv run trader validate                          # gate custom features + validate classifiers.yaml
uv run trader replay --days 5                   # backtest current classifiers with Jev (≈$0.004/week of data)
uv run trader replay --decider stub --source yfinance --pace 0.2   # free demo for the dashboard
uv run trader features                          # list features
uv run trader session                           # today's session times (exit 2 = closed)
uv run pytest                                   # test suite (guardrails, NAV, engine, specs)
```

## Development
In development all three roots are this checkout. Runtime output goes to `./runtime/` and replays to `./runtime/replay/`.

Run the dashboard locally with:
```bash
TRADER_DASHBOARD_ALLOW_LOCAL=1 uv run trader dashboard      # dev only: TCP on 127.0.0.1:8321
```

## Known limitations
- **No bracket orders.** Alpaca rejects them for fractional quantities, and at this account size almost every position is fractional. Instead:
  - Entries are notional market orders.
  - A server-side stop is placed where Alpaca accepts one.
  - The engine enforces stop and target on every 1-minute bar regardless.
  - If the runner dies with a position open and no server-side stop, the watchdog alerts within about 10 minutes. The restarted runner recovers the position from `entries.json`, and STOP flattens directly.
- **Positions the engine didn't open are sold.** Every minute from the first tick after the open, anything held that the engine isn't tracking (say, a hand-bought share) is market-sold, with no trade recorded. Don't trade by hand on these accounts.
  - Every entry order is saved, with its cash reserved, before it is sent (#60). If the answer is lost (a timeout, a crash), the order is found by its client order id and its fill becomes a tracked, protected position; the symbol and cash stay reserved until then. Only if Alpaca has no such order a minute later (or refused it outright, a 403) is the reservation released.
  - Trade-off: while that lookup keeps failing (an Alpaca outage), shares the order did buy sit in the account with **no server-side stop and no engine stop**, and they aren't sold as an orphan either (before #60 they were sold the next minute). They're adopted and protected as soon as the lookup succeeds; the EOD flatten and STOP still close them. The urgent "got no clear answer" and "check failed" alerts are the cue to look at Alpaca.
  - A fill's price is never the reference price. A market fill whose average price Alpaca hasn't reported (after a brief re-poll, once the order is final, whether its answer came at once or was found later, #130; part of an order not yet final, after 3 minutes) is priced at the position's average cost. If that is missing too, it's protected at the last price and logged `ENTER (price estimated)`, and its round trip isn't evidence. So is a limit fill with no reported price, protected at its limit.
  - One order is at most one round trip: shares an entry order fills after its position has closed are sold as untracked, never a second trade.
- **An exit whose real price is unknown** (the position vanished with no fill found, or a close-all sold it) is logged with `(price estimated)` in the reason and no `pnl_pct`, so the go-live gate, promotion and the scoreboard leave it out. That includes a position that closed while the runner was down with no fill found: it is recorded at its stop, so its dollar P&L still counts against today's loss budget, but it's `(price estimated)` too. A lookup that *fails* (an Alpaca error) doesn't count as "no fill found": the position stays tracked, is never sold, and its fill is looked up again each minute, for up to 10 minutes or until the EOD flatten, before the estimated row is written.
- **An exit that fills in pieces** (a close cancelled part-filled, a server stop that partly fired) is booked leg by leg (#61):
  - Each order's fill is a `sell_part` row at its real price, booked once by its order id, so a retry or a restart never counts it twice.
  - The shares left stay tracked, and the exit is retried. Their server stop is re-placed for just their qty, unless the old stop's cancel isn't final: then the old one stays, so two are never alive. An exit that fails with nothing sold re-places the server stop the same way (except in the flatten, whose close-all follows at once), so the shares aren't left without one while it retries.
  - A close refused because open sell orders hold the shares (a server stop Alpaca took, but whose answer was lost, #127) cancels the symbol's open sells, never a buy, books anything they sold by order id, and is retried once in the same call. If that fails too, the shares get a server stop again while the exit retries.
  - A close booked before it was final (its cancel still settling) is re-read by its order id at the next exit attempt, so anything it sold later is booked at its real price. A crash between a sell and its booking loses that leg (Alpaca's `close_position` takes no client id to find it by), and the round trip ends `(price estimated)`.
  - The final `sell` row carries the whole round trip's P&L: one trade. A leg whose price Alpaca never reported makes the round trip `(price estimated)`, and so does a final fill that accounts for fewer shares than were held.
- **Aggregate limits are estimates, not loss bounds** (#74, constants in `src/trader/allocator.py`). Before the per-position guardrails, each book (paper, live, each sim account) shrinks or skips an entry so that:
  - the planned stop losses of everything held or pending, plus today's realised loss, stay within 3% of the smaller of current and day-start equity (a buffer under the −5% kill switch; realised gains don't enlarge it; a restart re-reads today's losses from `trades.csv`, or, if it can't, alerts and counts today's equity change instead, which includes unrealised loss);
  - stocks and equity ETFs total at most 75% of equity (TLT and GLD are outside it);
  - each bucket of overlapping symbols (growth, financials, health, energy) is at most 50%, with SPY, DIA and IWM counting against every bucket.

  A shrunk entry is placed only if it keeps at least max($10, 25% of what was asked); otherwise it is skipped (#118), so a dust trade never counts towards the go-live gate or promotion. Entries no limit shrinks are unaffected. The binding limit is logged as a `q: allocation` row in the decisions log, with that `floor`. Gaps, stale quotes and failed exits can lose more than a stop plans for, and the buckets are overlap proxies, not correlations. The paper book is shared by all shadow classifiers, so a shadow classifier may get fewer entries on paper than it would in its own sim account; compare the allocation rows before reading that as a weaker signal.
- **The live stream takes at most 30 symbols** (the free IEX limit). Symbols held from before startup are streamed only while there's room after the classifiers' symbols and SPY; anything left out is alerted. Non-equity holdings (e.g. crypto) get no data but are still sold.
- **A stale stream** (no SPY bar for over 3 minutes) blocks new entries. While it lasts, the paper/live books' held symbols get their IEX bars over REST once a minute, so their engine stops keep working; the stream's bars win any minute both have. A failed REST poll alerts at most every 30 minutes, and the positions rely on their server-side stops and the kill switch (#50).
- **IEX live and SIP backtest data differ**, volume especially.
- **Custom features run in a sandbox.** Strategist-written feature code never runs inside the runner (which holds the live keys). A bubblewrap worker runs it with no network, no secrets, no runtime access and memory limits, from a runner-owned copy of the files; if the worker fails or hangs, custom features return NaN and dependent entries are blocked. The runner never follows symlinks in the strategist's `state/` or `features/custom/` (#49). The static check in `features/harness.py` is only a first filter.
