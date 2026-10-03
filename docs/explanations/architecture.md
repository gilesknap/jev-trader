# Architecture

The system is two programs that never share a process: a **strategist** that writes trading
rules, and a **runner** that executes them. They run as different Unix users, from different git
checkouts, and talk only through files. That separation is the main safety property: the part
that is creative and AI-driven can't touch real money or the code that guards it.

## Two repositories

The code and each owner's data live in separate repositories:

- **The code** is the public repository
  [gilesknap/jev-trader](https://github.com/gilesknap/jev-trader): everything in `src/`, the
  tests, these docs, the strategist's charter (`CLAUDE.md`) and prompts, the setup scripts and
  systemd units, the default universe, and a template for a data repository (`templates/data/`).
  Nothing in it is specific to one owner.
- **The data** is a private repository per owner, made from that template by
  `deploy/setup/0-data.sh`. It has two branches that share no history and are never merged:
  - `main` is the human's **deployment config**: `config.yaml`, `config/mode.yaml`, and the
    timers and environment file rendered from them.
  - `strategist` is the **strategist's data**: `state/`, `journal/`, `logs/`, `features/custom/`
    and `proposals/`, plus a short `CLAUDE.md` that points at the charter.

Both the code and the deployment config reach the runner only through `trading-deploy`, which a
human runs. The strategist's branch needs no deploy: the runner reads it at each session start.

## The pieces

```{mermaid}
flowchart LR
    subgraph trader["user: trader (paper keys only)"]
        T[systemd timers] --> W[strategist.sh<br/>from /srv/trading/main]
        W --> C[headless Claude Code]
        C -->|edits| S[(/srv/trading/strategist<br/>data repo, branch strategist)]
    end
    subgraph runner["user: runner (live keys, no sudo)"]
        RT[systemd timer] --> R[trader run]
        R -->|writes| RD[(/srv/trading/runtime)]
        D[dashboard] -->|reads| RD
        M[(/srv/trading/main<br/>public code)] -->|code| R
        CF[(/srv/trading/config<br/>data repo, branch main)] -->|config.yaml, mode.yaml| R
    end
    S -->|state/classifiers.yaml<br/>features/custom/| R
    R <-->|orders, bars| A[Alpaca]
    R <-->|decisions| J[Jev via OpenRouter]
    C -->|push strategist,<br/>needs-human and weekly issues| G[(private data repo)]
    G -->|human merge to main<br/>+ trading-deploy| CF
    P[(public code repo)] -->|human merge<br/>+ trading-deploy| M
    TS[tailscale serve] --> D
```

- **The strategist** is headless Claude Code, started by `trader`'s systemd timers through
  `scripts/strategist.sh` in the deployed code checkout (pre-market, post-close, a Saturday weekly
  run and a daily housekeeping check). Its charter (`CLAUDE.md` in the code) is passed in its
  system prompt. It reads its own notes, researches, and writes the next session's rules to
  `state/classifiers.yaml` on the data repository's `strategist` branch.
- **The runner** is `trader run`, started once per weekday by `runner`'s systemd timer. One
  invocation runs one session: it waits for the open, streams minute bars, ticks the engine
  every minute, flattens before the close, writes the day's summary and exits.
- **The engine** (`src/trader/engine.py`) is the one code path for replay, paper and live: the
  runner drives it with live bars, `trader replay` with historical ones.
- **Jev** is a typed-decision model on OpenRouter. It never sees dates or absolute prices: the
  engine sends it dimensionless features and the classifier's question, and gets back
  probabilities over the classifier's criteria (see [How a decision is made](how-a-decision-is-made.md)).
- **The dashboard** is a FastAPI app that reads the runtime files and the strategist's notes. Its
  only controls are STOP and HOLD LIVE.

## Three accounts

| Account | Holds | Can | Can't |
|---|---|---|---|
| your admin account | sudo | run the setup scripts, deploy, read everything | (it can do anything: use it only in sessions you watch) |
| `trader` | the strategist checkout, its own virtual environment of the deployed code, Alpaca **paper** keys, a GitHub token for the **data** repository only, the Claude login | edit `state/`, `journal/`, `features/custom/`, `logs/`, `proposals/`; push the `strategist` branch; open issues in the data repository (and, technically, push or merge to its `main`: see [Deploy a change](../how-to/deploy.md)) | read the live keys, write the deployed code checkout, the deployed config checkout or the runtime directory, reach the dashboard, write to the public code repository |
| `runner` | the deployed code checkout, the deployed config checkout, the runtime directory, Alpaca **live** keys | trade, write runtime state, serve the dashboard | sudo (it executes strategist-written feature code, so it must not be able to escalate) |

The setup scripts (`deploy/setup/1-host.sh` and friends) create the `trading` group and the
directories with these owners. `deploy/setup/check.sh` verifies the result, including that
`trader` can't write the deployed code, the deployed config or the runtime, that the live keys
are only in `runner`'s file, that the dashboard socket is unreachable from other users, and that
`trader`'s GitHub token is a fine-grained personal access token.

## Four roots

`src/trader/config.py` names four directories. In development they can all be one directory; in
production they are separate:

| Root | Environment variable | Default | Production path | Owner |
|---|---|---|---|---|
| Code | `TRADER_CODE_ROOT` | the checkout containing `src/` | `/srv/trading/main` (public code, `main`) | runner |
| Data | `TRADER_DATA_ROOT` | the code root | `/srv/trading/config` (data repository, `main`) | runner (read-only to trader) |
| Strategist | `TRADER_STRATEGIST_ROOT` | the data root | `/srv/trading/strategist` (data repository, `strategist`) | trader |
| Runtime | `TRADER_RUNTIME` | `runtime/` in the code root | `/srv/trading/runtime` | runner (read-only to trader) |

- The **code root** holds the code and the universe (`config/universe.yaml`), which stays with
  the code because it must match the allocator's buckets.
- The **data root** holds `config.yaml`, `config/mode.yaml` and the files rendered from them.
  On the host it is a checkout only `runner` can write, so the strategist can't change the
  deployment settings or the paper/live override by editing files.
- The **strategist root** holds the rules the runner reads: `state/classifiers.yaml` and
  `features/custom/`. So strategy changes need no deploy; they take effect at the next session
  start.
- The **runtime** holds everything the runner writes (see [Files and logs](../reference/files.md)).

Code and config changes need a deploy, and only a human can run one (see
[Deploy a change](../how-to/deploy.md)).

The runner never follows symlinks in the strategist's `state/` or `features/custom/`, and never
imports strategist-written code: custom features run in a bubblewrap sandbox with no network, no
secrets and no access to the runtime directory.

## The strategist runs the deployed code

`trader` doesn't have its own copy of the code. A `trader` command on its `PATH`
(`~/.local/bin/trader`, installed from `scripts/trader-shim` by `2-strategist.sh`) runs the
deployed code in `/srv/trading/main` from `trader`'s own virtual environment
(`~/.local/share/trader/venv`), with the four roots fixed to the production paths. So the
strategist validates and replays against exactly the code the runner will run, and the wrapper
that path-checks each run is the deployed one, which the strategist can't change.
`trader-python` is the same environment's Python, for ad-hoc research.

## How changes flow

```{mermaid}
flowchart LR
    S[strategist run] -->|state/, journal/,<br/>features/custom/, logs/| B[data repo: strategist]
    S -->|"proposals/topic/*.patch<br/>+ needs-human issue"| B
    B -->|runner reads at session start| Run[runner]
    H[human] -->|"applies a patch on a fork,<br/>opens a public PR"| Code[jev-trader main]
    H -->|config PR, merge| Cfg[data repo: main]
    Code -->|"sudo -u runner trading-deploy"| Run
    Cfg -->|"sudo -u runner trading-deploy"| Run
```

- The strategist's branch may change only `state/`, `journal/`, `features/custom/`, `logs/` and
  `proposals/`. The wrapper reverts anything else after each run and alerts.
- A code change the strategist wants (code, the universe, the prompts, the charter) becomes a
  patch series and a rationale under `proposals/<topic>/`, and a `needs-human` issue in the data
  repository. Its token can't reach the public code repository. The human reviews the patch,
  applies it on a branch of their fork, and opens the public pull request with their own
  description (see [Review the strategist's proposals](../how-to/proposals.md)).
- Changes to the deployment config are pull requests on the data repository's `main`.
- Merged changes reach the runner only when the human runs `trading-deploy`, which deploys both
  repositories together. A token that can push to the data repository's `main` still can't make
  anything run, and the deploy always shows the full config diff.

## The dashboard and its protection

The dashboard listens only on a Unix socket in `runner`'s private runtime directory.
`tailscale serve` proxies `https://<host>:<tailscale_port>` to that socket and adds a
`Tailscale-User-Login` header naming who is connecting; the dashboard lets in only the logins in
`config.yaml` (`dashboard.users`). A localhost TCP port would let any local user, including
`trader`, forge that header, which is why there is none.

Tailscale adds the header to every request from your browser, including one that another website
triggers. So STOP and HOLD LIVE also refuse anything that isn't a same-origin
`Content-Type: application/json` POST: a cross-site form can't send that type, and the CORS
preflight a cross-site fetch needs is never approved.

An optional second dashboard (`trader-dashboard-ssh.service`) serves the same app on a socket in
`/srv/trading-dashview`, a directory only the `dashview` group can enter, for reaching it through
an SSH tunnel from a machine without Tailscale. It is the only place
`TRADER_DASHBOARD_ALLOW_LOCAL=1` belongs: with that set, requests without the identity header are
let in.

## Where the money is

There are up to three kinds of **book**, each with its own risk state, NAV and logs under
`runtime/books/`:

- **paper**: the Alpaca paper account. Every `shadow` classifier trades here, and so does
  everything while the system hasn't gone live.
- **live**: the Alpaca live account, used only once go-live has happened. In live mode, `shadow`
  classifiers still trade paper.
- **`sim:<id>`**: one simulated account per `mode: sim` classifier, fed by the same live bars.

Each book is accounted in unit NAV, fund-style (`src/trader/nav.py`): NAV per unit starts at 1.0,
and a deposit or withdrawal issues or redeems units at the current NAV, so it doesn't read as a
gain or a loss. For the live book, the runner reads deposits and withdrawals from Alpaca's account
activities at each session start. The paper book has no transfers; after resetting the paper
account's balance, `trader rebase-paper` restarts its NAV instead.
