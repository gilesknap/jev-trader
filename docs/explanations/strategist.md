# The strategist

The strategist is headless Claude Code (the model is `models.strategist` in `config.yaml`), run
as the `trader` user. It starts every run with no memory: the repository *is* its memory. Its
instructions are its charter, `CLAUDE.md` at the repository root, plus a short prompt per run kind
in `prompts/`.

## The runs

| Run | When | Time limit | What it does |
|---|---|---|---|
| `premarket` | once, 30–75 min before the open | 25 min | news and the macro calendar; confirm, tweak or stand down today's classifiers; a few lines in today's journal |
| `postclose` | at least 20 min after the close | 50 min | review the day's trades, decisions and alerts; research; rewrite `state/strategy.md`; draft tomorrow's classifiers; the daily journal |
| `weekly` | Saturday | 50 min | the weekly retrospective; monthly and yearly compressions; the weekly pull request |
| `housekeeping` | daily | (no Claude) | `trader housekeeping`: credit, token, deploy and disk checks |

The timers are systemd user timers (`deploy/systemd-trader/`), generated from `config.yaml`'s
`schedule.strategist`. They fire on a fixed local schedule; the wrapper decides from Alpaca's
calendar whether a run is due, so pre-market and post-close runs skip holidays and follow the
market through daylight-saving changes.

## Its memory

| Path | What |
|---|---|
| `state/strategy.md` | the living thesis: phase, beliefs, what's running and why (rewritten, not appended) |
| `state/classifiers.yaml` | the next session's classifiers |
| `state/watchlist.md` | hypotheses not yet traded |
| `features/custom/*.py` | its own feature functions |
| `journal/daily/`, `journal/weekly/`, `journal/monthly/`, `journal/yearly/` | its journal, compacted over time |
| `logs/trades.csv`, `logs/<book>_equity.csv`, `logs/<book>_cashflows.csv` | the runner's logs, archived (never edited) |
| `logs/decisions/*.jsonl.gz` | one line per classifier decision, kept 90 days on disk, not committed |

Each run reads a fixed budget (the charter, the strategy, the classifiers, the watchlist, the last
5 dailies and 4 weeklies, and the logs it needs), so its context doesn't grow with the history.
`git log` on `state/strategy.md` shows how a belief evolved.

## What it may change

The `strategist` branch may change only `state/`, `journal/`, `features/custom/` and `logs/`.
Everything else is enforced by the wrapper, not by trust:

- Code, the universe, the prompts or the charter: a `proposal/<topic>` branch and a pull request,
  which the human reviews.
- Anything else it needs from the human (a new ticker, more data, an API): a GitHub issue labelled
  `needs-human`.
- Strategy changes need no deploy: the runner reads `state/` and `features/custom/` at each
  session start. `trader validate` checks them first; a broken classifier file means nothing
  trades that day.

## The wrapper

`scripts/strategist.sh` is what the timers run. In order, it:

1. **Takes a lock**, so only one run happens at a time (a pre-market tick fires every 15 minutes,
   but a run may take 25). A run that can't get the lock is skipped.
2. **Gates on the market clock** for pre-market and post-close (`trader session`): skips closed
   days, days it has already run, and times outside its window.
3. **Catches up with `main`:** pulls `origin/strategist`, merges `origin/main` and pushes the
   merge, then re-executes the merged copy of itself (after checking that it parses), so
   everything below uses the new code. Housekeeping skips this if the checkout is off
   `strategist` or has uncommitted changes.
4. **Prepares inputs:** archives the runner's logs into `logs/` (post-close and weekly), writes
   `logs/probe_report.json` (post-close), applies the retention policy (weekly).
5. **Runs Claude** with the run's prompt, under `timeout` (25 or 50 minutes).
   `ANTHROPIC_API_KEY` is unset, so runs can only use the Claude subscription, never API billing.
6. **Path-checks the result:** undoes any commits the run made itself, reverts every change
   outside the allowed paths (with an alert), and commits what's left as one commit.
7. **Publishes** to `origin/strategist` as a fast-forward, never a force-push. If the run pushed
   commits of its own, the new commit builds on them and reverts anything disallowed they
   contained. It then checks that the branch differs from the run's start only under the allowed
   paths, and alerts if not.

A failed run, push or merge sends an alert. The strategist's alerts go to
`strategist-alerts.log` in its own checkout, because `trader` can't write the runtime directory.

## Why it runs unattended

The strategist runs Claude with permission prompts bypassed, because nobody is there to answer
them. What bounds it is the account, not the prompts: no sudo, no live keys, no write access to
the deployed code or the runtime, and no route to the dashboard. Its GitHub token can push and
open pull requests, and could technically push to `main` or merge a pull request through the
API; that's why nothing on `main` runs until the human deploys it, and why the deploy lists every
merged pull request for the human to check (see [Deploy a change](../how-to/deploy.md)).

## What the charter asks of it

The charter sets a mission (beat buy-and-hold SPY on a risk-adjusted basis, net of slippage;
staying out is a valid position), a weekly exploration mandate (at least one genuinely novel
hypothesis in shadow or sim, labelled `family: novel`, with failures recorded as findings), and an
honesty requirement (each week: what it believed that turned out wrong, how paper and live
diverged, and whether any edge is distinguishable from luck). It also sets research hygiene for
backtests. Read `CLAUDE.md` for the full text: it is the strategist's operating manual, and the
[classifier schema](../reference/classifier-schema.md) here is checked against the same code.
