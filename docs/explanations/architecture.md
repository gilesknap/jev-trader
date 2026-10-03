# Architecture

The system is two programs that never share a process: a **strategist** that writes trading
rules, and a **runner** that executes them. They run as different Unix users, from different git
checkouts, and talk only through files. That separation is the main safety property: the part
that is creative and AI-driven can't touch real money or the code that guards it.

## The pieces

```{mermaid}
flowchart LR
    subgraph trader["user: trader (paper keys only)"]
        T[systemd timers] --> W[scripts/strategist.sh]
        W --> C[headless Claude Code]
        C -->|edits| S[(/srv/trading/strategist<br/>branch strategist)]
    end
    subgraph runner["user: runner (live keys, no sudo)"]
        RT[systemd timer] --> R[trader run]
        R -->|writes| RD[(/srv/trading/runtime)]
        D[dashboard] -->|reads| RD
        M[(/srv/trading/main<br/>branch main)] -->|code| R
    end
    S -->|state/classifiers.yaml<br/>features/custom/| R
    R <-->|orders, bars| A[Alpaca]
    R <-->|decisions| J[Jev via OpenRouter]
    C -->|push, PRs| G[GitHub]
    G -->|human merge + trading-deploy| M
    TS[tailscale serve] --> D
```

- **The strategist** is headless Claude Code, started by `trader`'s systemd timers through
  `scripts/strategist.sh` (pre-market, post-close, a Saturday weekly run and a daily housekeeping
  check). It reads its charter (`CLAUDE.md`) and its own notes, researches, and writes the next
  session's rules to `state/classifiers.yaml` on the `strategist` branch.
- **The runner** is `trader run`, started once per weekday by `runner`'s systemd timer. One
  invocation runs one session: it waits for the open, streams minute bars, ticks the engine
  every minute, flattens before the close, writes the day's summary and exits.
- **The engine** (`src/trader/engine.py`) is the one code path for replay, paper and live: the
  runner drives it with live bars, `trader replay` with historical ones.
- **Jev** is a typed-decision model on OpenRouter. It never sees dates or absolute prices: the
  engine sends it dimensionless features and the classifier's question, and gets back
  probabilities over the classifier's criteria (see [How a decision is made](decisions.md)).
- **The dashboard** is a FastAPI app that reads the runtime files and the strategist's notes. Its
  only controls are STOP and HOLD LIVE.

## Three accounts

| Account | Holds | Can | Can't |
|---|---|---|---|
| your admin account | sudo | run the setup scripts, deploy, read everything | (it can do anything: use it only in sessions you watch) |
| `trader` | the strategist checkout, Alpaca **paper** keys, a GitHub token, the Claude login | edit `state/`, `journal/`, `features/custom/`, `logs/`; push the `strategist` branch; open PRs and issues | read the live keys, write the deployed code or the runtime directory, reach the dashboard |
| `runner` | the deployed checkout of `main`, the runtime directory, Alpaca **live** keys | trade, write runtime state, serve the dashboard | sudo (it executes strategist-written feature code, so it must not be able to escalate) |

The setup scripts (`deploy/setup/1-host.sh` and friends) create the `trading` group and the
directories with these owners. `deploy/setup/check.sh` verifies the result, including that
`trader` can't write the deployed code or the runtime, that the live keys are only in
`runner`'s file, and that the dashboard socket is unreachable from other users.

## Three roots

`src/trader/config.py` names three directories. In development they are all the same checkout;
in production they are separate:

| Root | Environment variable | Production path | Owner |
|---|---|---|---|
| Code | `TRADER_CODE_ROOT` | `/srv/trading/main` (branch `main`) | runner |
| Strategist | `TRADER_STRATEGIST_ROOT` | `/srv/trading/strategist` (branch `strategist`) | trader |
| Runtime | `TRADER_RUNTIME` | `/srv/trading/runtime` | runner (read-only to trader) |

The runner reads its code from the code root but its *rules* from the strategist root:
`state/classifiers.yaml` and `features/custom/`. So strategy changes need no deploy; they take
effect at the next session start. Code changes do need one, and only a human can run it (see
[Deploy a change](../how-to/deploy.md)).

The runner never follows symlinks in the strategist's `state/` or `features/custom/`, and never
imports strategist-written code: custom features run in a bubblewrap sandbox with no network, no
secrets and no access to the runtime directory.

## How code changes flow

```{mermaid}
flowchart LR
    S[strategist run] -->|state/, journal/,<br/>features/custom/, logs/| B[branch strategist]
    S -->|code proposals| P[branch proposal/*]
    B -->|weekly PR| Main[main]
    P -->|PR| Main
    H[human] -->|reviews, merges| Main
    Main -->|"sudo -u runner trading-deploy"| Run[/srv/trading/main/]
```

- The strategist's branch may change only `state/`, `journal/`, `features/custom/` and `logs/`.
  The wrapper reverts anything else after each run and alerts.
- Anything else (code, the universe, the prompts, the charter) goes through a `proposal/<topic>`
  branch and a pull request that a human reviews.
- Merged code reaches the runner only when the human runs `trading-deploy`. A token that can push
  to `main` still can't make anything run.

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
