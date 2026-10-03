# Configuration

Configuration is split by who may change it and how often:

| File | Holds | Changed by | Takes effect |
|---|---|---|---|
| `config.yaml` | deployment settings: owner, dashboard users, start date, schedule, models | the human, through a PR | mostly after a deploy; some keys need more (see [below](#when-a-change-takes-effect)) |
| `config/mode.yaml` | the account-mode override | the human, through a PR | after a deploy |
| `config/universe.yaml` | the tradeable symbols | the human, through a PR | after a deploy |
| `state/classifiers.yaml` | the classifiers | the strategist | the next session start |
| secrets files | API keys | the human, on the host | the next run of each process |

Safety rules (the guardrails and the go-live gate's thresholds) are deliberately not
configuration: they are code. See [Guardrails and limits](guardrails.md).

## `config.yaml`

At the repository root, loaded and validated strictly by `src/trader/config.py`. An unknown key, a
bad date or an unquoted time stops every `trader` command with a message naming the file, so a
typo can't slip through quietly. The comments in the file itself describe each key too.

| Key | Type | Meaning |
|---|---|---|
| `owner.name` | string | The human operator's name. Validated, but nothing in the code reads it yet |
| `owner.github_repo` | `owner/repo` | Your repository on GitHub. The setup scripts clone it, and housekeeping names it in the token-expiry alert. (The dashboard's links take the repository from the deployed checkout's git remote instead) |
| `dashboard.users` | list | Tailscale logins allowed into the dashboard. Empty means nobody. `TRADER_DASHBOARD_USERS` overrides it |
| `dashboard.tailscale_port` | int | The port `tailscale serve --https` exposes the dashboard on |
| `experiment.start_date` | date | Observe-phase day 1. The go-live gate and the scoreboard count from here, and `test_*` classifiers stop running from this date |
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
| `capital.replay_cash` | number | The default pretend account for a replay run from code (USD). `trader replay` passes its own `--cash` (default 250) |

The `OnCalendar` specs carry no time zone: `schedule.local_tz` is appended to each.

**Generated files.** Files that can't read YAML are generated from `deploy/templates/` and
`config.yaml`, and committed:

- `deploy/systemd/trader-runner.timer`
- `deploy/systemd/trader.env` (installed as `runner`'s `~/.config/trading/services.env`)
- `deploy/systemd-trader/trader-strategist-<kind>.timer`, one per strategist run

After editing `config.yaml`, run `uv run trader config render-deploy` and commit the result. A test
fails (and so does every deploy, which runs the tests first) if they disagree with `config.yaml`.
`uv run trader config get <dotted.key>` prints one setting.

### When a change takes effect

The runner reads `config.yaml` from its own checkout, so most keys take effect after a deploy (see
[Deploy a change](../how-to/deploy.md)), at the runner's next session start; the strategist reads
its checkout's copy once it has merged `main`. A deploy copies only the runner's systemd units and
timers, though, so some keys need a step of their own:

| Keys | After the deploy, also |
|---|---|
| `schedule.runner_start`, `schedule.local_tz` (runner timer) | nothing: the deploy installs the re-rendered runner timer |
| `schedule.strategist.*`, `schedule.local_tz` (strategist timers) | re-run `bash /srv/trading/strategist/deploy/setup/2-strategist.sh` as `trader`, once the strategist checkout has merged `main`. Only that script installs the strategist's timers; `check.sh` fails until you do |
| `dashboard.users` | update `TRADER_DASHBOARD_USERS` in `runner`'s `~/.config/trading/services.env` to match, then restart the dashboard. That file is written only at install, when absent, and never by a deploy, and its value wins over `config.yaml` |
| `dashboard.tailscale_port` | re-run `sudo bash /srv/trading/main/deploy/setup/3-runner.sh install`, which points `tailscale serve` at the new port |
| `experiment.start_date`, `models.*`, `alerts.ntfy_server`, `capital.*`, `schedule.postclose_cutoff` | nothing |

A change to `deploy/templates/trader.env` itself reaches an installed `services.env` only by hand,
for the same reason.

## `config/mode.yaml`

```yaml
mode: auto
```

- `auto` (the default): go live automatically when the gate passes, after the veto window.
- `paper`: force paper trading.
- `live`: force live trading.

An unrecognised value reads as `paper`. Changes take effect only after a deploy.

## `config/universe.yaml`

```yaml
tickers:
  - SPY
  - QQQ
  # ...
```

The only symbols any classifier may trade. Keep it to at most 30, the free IEX stream's limit: it's a rule, not enforced in code. The cap the runner applies covers only the extra symbols it streams for positions already held; classifiers whose symbols together (plus SPY) exceed 30 would ask for a subscription the feed refuses whole, leaving the stream with no symbols at all. The book-level
buckets in `src/trader/allocator.py` are written for this universe, and a test requires every
universe symbol to be classified exactly once there (a bucket, broad, non-equity or unbucketed),
so a change to the universe needs a matching change to the allocator.

## Secrets

Secrets are `KEY=VALUE` lines in the first file that exists of: `$TRADER_SECRETS`, `.env` in the
code root (development and the strategist's checkout), `~/.config/trading/env`. Environment
variables override the file.

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
| `TRADER_STRATEGIST_ROOT` | The strategist's checkout (`state/`, `features/custom/`). Default: the code root |
| `TRADER_RUNTIME` | The runner's runtime directory. Default: `runtime/` in the code root |
| `TRADER_REPLAY_DIR` | Where replays are written. Default: `replay/` in the runtime directory |
| `TRADER_SECRETS` | The secrets file to read first |
| `TRADER_CONFIG` | An alternative `config.yaml` |
| `TRADER_STRATEGIST_STAMP` | Where the watchdog and the dashboard look for the strategist's last-run stamp (and, beside it, `.last_postclose`). The wrapper always writes `.last_run` in its own checkout, so this only moves where they look; it must point there |
| `TRADER_STRATEGIST_ALERTS` | Where alerts go from a process that can't write the runtime directory. Default: `strategist-alerts.log` in the strategist checkout |
| `TRADER_DASHBOARD_USERS` | Comma-separated Tailscale logins, overriding `dashboard.users` |
| `TRADER_DASHBOARD_ALLOW_LOCAL` | `1` lets requests without a Tailscale identity in. Only for local development and the SSH-tunnel dashboard; never on the Tailscale-served one |

In production, `runner`'s services read these from `~/.config/trading/services.env`, and the
strategist's unit sets its own. In development all three roots are the one checkout.
