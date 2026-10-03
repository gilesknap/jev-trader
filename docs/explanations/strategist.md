# The strategist

The strategist is headless Claude Code (the model is `models.strategist` in `config.yaml`), run
as the `trader` user. It starts every run with no memory: the `strategist` branch of your private
data repository *is* its memory. Its instructions are its charter, `CLAUDE.md` in the code, which
the wrapper passes in its system prompt, plus a short prompt per run kind in the code's
`prompts/`. Both come from the deployed checkout, so the charter is versioned with the code it
describes.

## The runs

| Run | When | Time limit | What it does |
|---|---|---|---|
| `premarket` | once, 30–75 min before the open | 25 min | news and the macro calendar; confirm, tweak or stand down today's classifiers; a few lines in today's journal |
| `postclose` | at least 20 min after the close | 50 min | review the day's trades, decisions and alerts; research; rewrite `state/strategy.md`; draft tomorrow's classifiers; the daily journal |
| `weekly` | Saturday | 50 min | the weekly retrospective; monthly and yearly compressions; the weekly issue |
| `housekeeping` | daily | (no Claude) | `trader housekeeping`: credit, token, deploy and disk checks |

The timers are systemd user timers (`deploy/systemd-trader/` in your data repository's `main`),
rendered from `config.yaml`'s `schedule.strategist`. They fire on a fixed local schedule; the wrapper decides from Alpaca's
calendar whether a run is due, so pre-market and post-close runs skip holidays and follow the
market through daylight-saving changes.

## Its memory

| Path | What |
|---|---|
| `state/strategy.md` | the living thesis: phase, beliefs, what's running and why (rewritten, not appended) |
| `state/classifiers.yaml` | the next session's classifiers |
| `state/watchlist.md` | its hypothesis records, active and rejected (see [Hypotheses and experiments](hypotheses.md)) |
| `state/steering.md` | the human's steering decisions (read every run, never edited by the strategist) |
| `features/custom/*.py` | its own feature functions |
| `journal/daily/`, `journal/weekly/`, `journal/monthly/`, `journal/yearly/` | its journal, compacted over time |
| `logs/trades.csv`, `logs/<book>_equity.csv`, `logs/<book>_cashflows.csv` | the runner's logs, archived (never edited) |
| `logs/decisions/*.jsonl.gz` | one line per classifier decision, kept 90 days on disk, not committed |
| `proposals/<topic>/` | its code proposals: a patch series and a rationale |

Each run reads a fixed budget (the charter, the strategy, the classifiers, the watchlist, the steering file, the last
5 dailies and 4 weeklies, and the logs it needs), so its context doesn't grow with the history.
`git log` on `state/strategy.md` shows how a belief evolved.

The human steers it through `state/steering.md`: decisions with their reasoning, made in an
interactive session and merged into `strategist` between runs. Active entries bind the
strategist; it applies each new one, acknowledges it in its journal, and argues back there
rather than editing the file (see [Steer the strategist](../how-to/steer-the-strategist.md)).

## What it may change

The `strategist` branch may change only `state/`, `journal/`, `features/custom/`, `logs/` and
`proposals/`, and not the human's `state/steering.md`. That's enforced by the wrapper, not by trust:

- Code, the universe, the prompts or the charter: a **proposal**. It clones the deployed code
  from `/srv/trading/main` into scratch space (a clone whose only origin is that local path),
  commits the change there and tests it with `trader-test` (the clone's code in `trader`'s
  environment; a change that needs new dependencies can't be tested this way). It writes the
  result as `proposals/<topic>/`: a `git format-patch` series against the commit it cloned, and a
  `README.md` with the problem, the evidence, what the tests show and the risk. Then it deletes
  the clone and opens a `needs-human` issue in the data repository pointing at the proposal. The human
  applies it on a fork and writes the public pull request (see
  [Review the strategist's proposals](../how-to/proposals.md)).
- Anything else it needs from the human (a new ticker, more data, an API): a GitHub issue in the
  data repository labelled `needs-human`.
- Each week: a weekly issue (label `weekly`, titled `Week <YYYY>-W<WW>: <summary>`) whose body is
  the weekly journal, ending with a link comparing the `strategist` branch with last week's, so
  the week's changes to `state/` are one click away. It closes last week's issue, and flags a
  go-live assessment in the title when there is one.
- Strategy changes need no deploy: the runner reads `state/` and `features/custom/` once per
  session, 2 minutes before the open; an edit made during a session applies only if the runner
  restarts. `trader validate` checks them first; a broken classifier file means nothing
  trades that day.

## The wrapper

`scripts/strategist.sh` is what the timers run, from the deployed code checkout
(`/srv/trading/main`). In order, it:

1. **Runs a private copy of itself**, so a deploy that replaces the script mid-run can't change
   what the run executes.
2. **Takes the run lock** (`~/.local/state/trader/strategist.lock`), so only one run happens at a
   time (a pre-market tick fires every 15 minutes, but a run may take 25). A run that can't get
   the lock is skipped. `trading-deploy` takes the same lock, so a deploy and a run never overlap.
3. **Syncs `trader`'s virtual environment** with the deployed `uv.lock` (the same `uv sync`
   command `2-strategist.sh` uses; a quick no-op when nothing changed). If that fails, it alerts
   and doesn't run, since the environment might not match the deployed code.
4. **Gates on the market clock** for pre-market and post-close (`trader session`): skips closed
   days, days it has already run, and times outside its window.
5. **Catches up with its branch:** pulls `origin/strategist`. There's no code to merge: it runs
   the deployed code through its `trader` command. Housekeeping skips the pull if the checkout is
   off `strategist` or has uncommitted changes.
6. **Checks its instructions:** without a charter or prompt in the deployed code, it alerts and
   doesn't run.
7. **Prepares inputs:** archives the runner's logs into `logs/` (post-close and weekly), writes
   `logs/probe_report.json` (post-close), applies the retention policy (weekly).
8. **Runs Claude** with the run's prompt and the charter appended to its system prompt, under
   `timeout` (25 or 50 minutes). `ANTHROPIC_API_KEY` is unset, so runs can only use the Claude
   subscription, never API billing.
9. **Path-checks the result:** undoes any commits the run made itself, reverts every change
   outside the allowed paths (with an alert), and commits what's left as one commit.
10. **Publishes** to `origin/strategist` as a fast-forward, never a force-push. If the run pushed
   commits of its own, the new commit builds on them and reverts anything disallowed they
   contained. It then checks that the branch differs from the run's start only under the allowed
   paths, and alerts if not.

A failed run or push sends an alert. The strategist's alerts go to `strategist-alerts.log` in its
own checkout, because `trader` can't write the runtime directory.

## Why it runs unattended

The strategist runs Claude headless in auto mode (`defaultMode: auto` in `trader`'s Claude Code
settings), without anyone there to answer a permission prompt. What bounds it is
the account, not the prompts: no sudo, no live keys, no write access to the deployed code, the
deployed config or the runtime, and no route to the dashboard.

Its GitHub token is a fine-grained one for the data repository only. It can push its branch and
open issues there, and could technically push to the data repository's `main` or merge a pull
request through the API. That's why nothing on `main` runs until the human deploys it, and why the
deploy always shows the full config diff (see [Deploy a change](../how-to/deploy.md)). It can't
write anything to the public code repository.

Reading public content can't be blocked by a token: the public repository's issues and pull
requests are open to anyone, including people who might write instructions aimed at it. So
`2-strategist.sh` adds Claude Code deny rules to `trader`'s settings for the obvious routes (web
fetches of GitHub, `curl` and `wget` against it, and `gh` naming the code repository), and the
charter tells it not to look. Both are pattern-based and porous (a Python one-liner can still read
a URL), and the settings file is `trader`'s own, so treat them as a reduction, not a guarantee.

## What the charter asks of it

The charter sets a mission (beat buy-and-hold SPY on a risk-adjusted basis, net of slippage;
staying out is a valid position), a weekly exploration mandate (at least one genuinely novel
hypothesis in shadow or sim, labelled `family: novel`, or one investigated and rejected before
trading, with failures recorded as findings), a falsifiable record for every hypothesis with a
pre-registered decision checkpoint (see [Hypotheses and experiments](hypotheses.md)), and an
honesty requirement (each week: what it believed that turned out wrong, how paper and live
diverged, and whether any edge is distinguishable from luck). It also sets research hygiene for
backtests. Read `CLAUDE.md` in the code for the full text: it is the strategist's operating manual, and the
[classifier schema](../reference/classifier-schema.md) here is checked against the same code.

See also: [Related work](related-work.md) compares this loop with other LLM-driven strategy
searches, including AQuA's hypothesis records and the sceptics' trial-count discounting.
