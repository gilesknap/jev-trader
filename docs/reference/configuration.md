# Configuration

Configuration is split by who may change it and how often:

| File | Where | Holds | Changed by | Takes effect |
|---|---|---|---|---|
| `config.yaml` | data repository, `main` | deployment settings: owner, dashboard users, start date, schedule, models | the human, through a PR on the data repository | mostly after a deploy; some keys need more (see [below](#when-a-change-takes-effect)) |
| `config/mode.yaml` | data repository, `main` | the account-mode override | the human, through a PR on the data repository | after a deploy |
| `config/universe.yaml` | code repository | the tradeable symbols | a PR on the code repository (with the matching allocator change) | after a deploy |
| `state/classifiers.yaml` | data repository, `strategist` | the classifiers | the strategist | the next session start |
| secrets files | on the host | API keys | the human, on the host | the next run of each process |

Safety rules (the guardrails and the go-live gate's thresholds) are deliberately not
configuration: they are code. See [Guardrails and limits](guardrails.md).

## `config.yaml`

At the root of the data repository's `main` branch (the **data root**, `TRADER_DATA_ROOT`;
`/srv/trading/config` on the host), loaded and validated strictly by `src/trader/config.py` when
a command first needs it: up front for every `trader` command except `stop`, `watchdog` and
`config render-deploy` (which validates its own data root's file just as strictly). An unknown
key, a bad date or an unquoted time stops those commands with a message naming the file, so a
typo can't slip through quietly: the runner never starts a session on it, and `trader validate`
fails. The last resorts don't depend on it: `trader stop` still flags and flattens, alerts still
push (to the default `https://ntfy.sh`; see [Understand the alerts](../how-to/alerts.md)), and
`trader watchdog` alerts hourly that the file is unreadable. A missing file names the data root
it looked in. The comments in the file itself describe each key too; the template is
`templates/data/main/config.yaml` in the code.

| Key | Type | Meaning |
|---|---|---|
| `owner.name` | string | The human operator's name. Validated, but nothing in the code reads it yet |
| `owner.github_repo` | `owner/repo` | Your private **data** repository on GitHub. The setup scripts clone it, housekeeping names it in the token-expiry alert, and the dashboard links to its issues, journal and strategy. (The dashboard's links to the code take that repository from the deployed code checkout's git remote) |
| `dashboard.users` | list | Tailscale logins allowed into the dashboard. Empty means nobody |
| `dashboard.tailscale_port` | int | The port `tailscale serve --https` exposes the dashboard on |
| `experiment.start_date` | date | Observe-phase day 1. The go-live gate, promotion and the scoreboard count from here, and `test_*` classifiers stop running from this date |
| `schedule.local_tz` | IANA zone | The operator's clock, used by the runner timer and the strategist timers |
| `schedule.runner_start` | `"HH:MM"` | When the runner starts on weekdays, local time. Keep it safely before 09:30 New York in every daylight-saving week |
| `schedule.postclose_cutoff` | `"HH:MM"` | Local time: the watchdog's latest deadline for the post-close run |
| `schedule.strategist.premarket` | systemd `OnCalendar` | When the pre-market timer fires (the run itself happens once, 30–75 min before the open) |
| `schedule.strategist.postclose` | systemd `OnCalendar` | When the post-close timer fires (after the close; a second time is a retry) |
| `schedule.strategist.weekly` | systemd `OnCalendar` | The weekly retrospective |
| `schedule.strategist.housekeeping` | systemd `OnCalendar` | The daily credit, token, deploy and disk checks |
| `models.jev` | string | The OpenRouter decision model, pinned. Bump it deliberately, after checking behaviour |
| `models.strategist` | string | The Claude Code model for the strategist runs |
| `alerts.ntfy_server` | `https://` URL | The ntfy push server. The topic is `NTFY_TOPIC` in the secrets file |
| `capital.sim_cash` | number | Each `mode: sim` classifier's simulated account starts with this (USD) |
| `capital.replay_cash` | number | The pretend account a replay starts with (USD), unless `trader replay --cash` gives another |

The `OnCalendar` specs carry no time zone: `schedule.local_tz` is appended to each.

**Generated files.** Files that can't read YAML are generated from the code's
`deploy/templates/` and `config.yaml`, and committed beside `config.yaml` on the data
repository's `main`:

- `deploy/systemd/trader-runner.timer`
- `deploy/systemd/trader.env` (installed as `runner`'s `~/.config/trading/services.env`)
- `deploy/systemd-trader/trader-strategist-<kind>.timer`, one per strategist run

After editing `config.yaml`, render them from a checkout of the code, pointing at your data
checkout, and commit the result to the data repository:

```bash
TRADER_DATA_ROOT=/path/to/data uv run trader config render-deploy --data-root /path/to/data
```

`trading-deploy` checks them with the code it is about to deploy
(`trader config render-deploy --check`) and refuses, naming the stale files, if they disagree with
`config.yaml`. So a change to a template in the code also needs a re-render in the data
repository (see [Take updates](../how-to/take-updates.md#keep-your-data-repository-current)).
`trader config get <dotted.key>` prints one setting.

### When a change takes effect

The runner and the strategist both read `config.yaml` from the deployed config checkout
(`/srv/trading/config`), so most keys take effect after a deploy (see
[Deploy a change](../how-to/deploy.md)), at the runner's next session start and the strategist's
next run. A deploy installs only the runner's systemd units and timers, though, so some keys need
a step of their own:

| Keys | After the deploy, also |
|---|---|
| `schedule.runner_start`, `schedule.local_tz` (runner timer) | nothing: the deploy installs the re-rendered runner timer |
| `schedule.strategist.*`, `schedule.local_tz` (strategist timers) | re-run `bash /srv/trading/main/deploy/setup/2-strategist.sh` as `trader`. Only that script installs the strategist's timers (from the deployed config checkout); the deploy prints a reminder, and `check.sh` fails until you do |
| `dashboard.users` | nothing: the deploy restarts the dashboard, which reads it at start. `services.env` must not set `TRADER_DASHBOARD_USERS` (an install from before it was dropped from the template may still have the line: delete it), or that wins |
| `dashboard.tailscale_port` | re-run `sudo bash /srv/trading/main/deploy/setup/3-runner.sh install`, which points `tailscale serve` at the new port |
| `experiment.start_date`, `models.*`, `alerts.ntfy_server`, `capital.*`, `schedule.postclose_cutoff` | nothing |

A change to `deploy/templates/trader.env` itself reaches an installed `services.env` only by hand:
setup writes that file only at install, when absent, and a deploy never does.

## `config/mode.yaml`

```yaml
mode: auto
```

- `auto` (the default): go live automatically when the gate passes, after the veto window.
- `paper`: force paper trading.
- `live`: force live trading.

An unrecognised value reads as `paper`. Changes take effect only after a deploy. When the data
root is a separate directory from the code (as on the host), the file must exist: a missing one
is an error, never a silent `auto`, and `trading-deploy` refuses data without it.

## `config/universe.yaml`

```yaml
tickers:
  - SPY
  - QQQ
  # ...
```

In the code repository, not the data repository: the only symbols any classifier may trade. Keep it to at most 30, the free IEX stream's limit: it's a rule, not enforced in code. The cap the runner applies covers only the extra symbols it streams for positions already held; classifiers whose symbols together (plus SPY) exceed 30 would ask for a subscription the feed refuses whole, leaving the stream with no symbols at all. The book-level
buckets in `src/trader/allocator.py` are written for this universe, and a test requires every
universe symbol to be classified exactly once there (a bucket, broad, non-equity or unbucketed),
so a change to the universe needs a matching change to the allocator. Owners can't override it
in their data repository; one who wants a different universe deploys from their own fork.

## Secrets

Secrets are `KEY=VALUE` lines in the first file that exists of: `$TRADER_SECRETS`, `.env` in the
data root (development), `~/.config/trading/env`. Environment variables override the file. On the
host both processes set `TRADER_SECRETS`: the strategist's `trader` command points it at
`/srv/trading/strategist/.env` (git-ignored), and `runner`'s services at
`/home/runner/.config/trading/env`.

| Key | Who has it | Used for |
|---|---|---|
| `ALPACA_PAPER_KEY`, `ALPACA_PAPER_SECRET` | strategist and runner | the paper account, market data, the calendar, research |
| `ALPACA_LIVE_KEY`, `ALPACA_LIVE_SECRET` | **runner only** | the live account. `check.sh` fails if the strategist's `.env` has any `ALPACA_LIVE_` line |
| `OPENROUTER_API_KEY` | strategist and runner | Jev decisions (replays and the runner) and the credit check |
| `NTFY_TOPIC` | strategist and runner | alerts. Anyone who knows the topic can read them, so keep it random and private |

## Environment variables

| Variable | Meaning |
|---|---|
| `TRADER_CODE_ROOT` | The checkout the code runs from. Default: the checkout containing `src/` |
| `TRADER_DATA_ROOT` | The data checkout holding `config.yaml`, `config/mode.yaml` and the rendered deploy files (the data repository's `main`). Default: the code root. A code checkout has no `config.yaml` of its own, so set this to run `trader` from one |
| `TRADER_STRATEGIST_ROOT` | The strategist's checkout (`state/`, `features/custom/`). Default: the data root |
| `TRADER_RUNTIME` | The runner's runtime directory. Default: `runtime/` in the code root |
| `TRADER_REPLAY_DIR` | Where replays are written. Default: `replay/` in the runtime directory, which suits a development checkout. On the host the runtime directory is read-only to the strategist, so its `trader` command, `trader-python`, its unit and `services.env` set `/srv/trading/strategist/replays` |
| `TRADER_SECRETS` | The secrets file to read first |
| `TRADER_CONFIG` | An alternative `config.yaml`. Default: `config.yaml` in the data root |
| `TRADER_STRATEGIST_STAMP` | Where the watchdog and the dashboard look for the strategist's last-run stamp (and, beside it, `.last_postclose`). The wrapper always writes `.last_run` in its own checkout, so this only moves where they look; it must point there |
| `TRADER_STRATEGIST_ALERTS` | Where alerts go from a process that can't write the runtime directory. Default: `strategist-alerts.log` in the strategist checkout |
| `TRADER_DASHBOARD_USERS` | Comma-separated Tailscale logins, overriding `dashboard.users`. For local development only: never set it in `services.env`, where it would silently outlive a `config.yaml` change |
| `TRADER_DASHBOARD_ALLOW_LOCAL` | `1` lets requests without a Tailscale identity in. Only for local development and the SSH-tunnel dashboard; never on the Tailscale-served one |

Give the `TRADER_*` directories as **absolute paths**. A relative `TRADER_DATA_ROOT` is resolved
against the working directory once, at start-up, but the others are used as given, so a relative
one would point somewhere else after any change of directory (the strategist's wrapper changes
into its checkout, for example).

In production, `runner`'s services read these from `~/.config/trading/services.env`; the
strategist's unit sets its own, and its `trader` command fixes the roots to the production paths.
In development, one data directory holding both `config.yaml` and `state/` can serve as the data
root and the strategist root (see [Your first replay](../tutorials/first-replay.md)).
