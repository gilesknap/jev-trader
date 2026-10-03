# Decision records

Short records of the decisions that shaped the system: the context, what was decided and what it
costs. Each is dated to when it was made. The system was built in a private repository before the
code moved here (see [0014](adr/0014-public-code-private-data.md)), so the issue and pull-request
numbers in **Origin** refer to that repository, and aren't links.

| # | Date | Decision |
|---|---|---|
| [0001](adr/0001-separate-runner-and-strategist-accounts.md) | 2026-09-27 | Separate accounts for the runner and the strategist |
| [0002](adr/0002-dashboard-on-a-private-socket.md) | 2026-09-27 | The dashboard listens on a private Unix socket behind Tailscale |
| [0003](adr/0003-guardrails-in-code.md) | 2026-09-27 | Money rules are enforced by the runner, not the charter |
| [0004](adr/0004-sandbox-strategist-features.md) | 2026-09-27 | Strategist-authored features run in a sandbox |
| [0005](adr/0005-automatic-go-live-with-veto.md) | 2026-09-27 | Go-live is automatic after a veto window; the gate is a floor |
| [0006](adr/0006-no-overnight-holds.md) | 2026-09-27 | Flat every night: no overnight holds |
| [0007](adr/0007-deploy-from-signed-merges.md) | 2026-09-27 | Deploys: tested candidates, signed merges, never mid-session |
| [0008](adr/0008-scoreboard-families-and-control.md) | 2026-09-27 | A scoreboard of novel against conventional ideas and a fixed control |
| [0009](adr/0009-probe-mode.md) | 2026-09-28 | Probe mode: measure the model's judgement without trading |
| [0010](adr/0010-sim-accounts.md) | 2026-09-28 | Each experiment can trade its own simulated account |
| [0011](adr/0011-root-config-yaml.md) | 2026-09-29 | Personal and deployment settings live in one config.yaml |
| [0012](adr/0012-systemd-timers-for-the-strategist.md) | 2026-09-29 | The strategist's schedule uses systemd user timers, not cron |
| [0013](adr/0013-wrapper-publishes-the-strategist-branch.md) | 2026-09-30 | The wrapper, not the model, publishes the strategist's branch |
| [0014](adr/0014-public-code-private-data.md) | 2026-10-03 | Public code repository, private data repository per owner |
| [0015](adr/0015-human-steering.md) | 2026-10-03 | The human steers the strategist through state/steering.md |

```{toctree}
:hidden:
:glob:

adr/*
```
