# Architectural decision records

Short records of the decisions that shaped the system: the context, what was decided and what it
costs. Each is dated to when it was made; numbers are in the order they were recorded. The system was built in a private repository before the
code moved here (see [0014](decisions/0014-public-code-private-data.md)), so the issue and pull-request
numbers in **Origin** refer to that repository, and aren't links.

| # | Date | Decision |
|---|---|---|
| [0001](decisions/0001-separate-runner-and-strategist-accounts.md) | 2026-09-27 | Separate accounts for the runner and the strategist |
| [0002](decisions/0002-dashboard-on-a-private-socket.md) | 2026-09-27 | The dashboard listens on a private Unix socket behind Tailscale |
| [0003](decisions/0003-guardrails-in-code.md) | 2026-09-27 | Money rules are enforced by the runner, not the charter |
| [0004](decisions/0004-sandbox-strategist-features.md) | 2026-09-27 | Strategist-authored features run in a sandbox |
| [0005](decisions/0005-automatic-go-live-with-veto.md) | 2026-09-27 | Go-live is automatic after a veto window; the gate is a floor |
| [0006](decisions/0006-no-overnight-holds.md) | 2026-09-27 | Flat every night: no overnight holds |
| [0007](decisions/0007-deploy-from-signed-merges.md) | 2026-09-27 | Deploys: tested candidates, signed merges, never mid-session |
| [0008](decisions/0008-scoreboard-families-and-control.md) | 2026-09-27 | A scoreboard of novel against conventional ideas and a fixed control |
| [0009](decisions/0009-probe-mode.md) | 2026-09-28 | Probe mode: measure the model's judgement without trading |
| [0010](decisions/0010-sim-accounts.md) | 2026-09-28 | Each experiment can trade its own simulated account |
| [0011](decisions/0011-root-config-yaml.md) | 2026-09-29 | Personal and deployment settings live in one config.yaml |
| [0012](decisions/0012-systemd-timers-for-the-strategist.md) | 2026-09-29 | The strategist's schedule uses systemd user timers, not cron |
| [0013](decisions/0013-wrapper-publishes-the-strategist-branch.md) | 2026-09-30 | The wrapper, not the model, publishes the strategist's branch |
| [0014](decisions/0014-public-code-private-data.md) | 2026-10-03 | Public code repository, private data repository per owner |
| [0015](decisions/0015-human-steering.md) | 2026-10-03 | The human steers the strategist through state/steering.md |
| [0016](decisions/0016-execution-toolkit-in-the-spec.md) | 2026-09-27 | Strategies are declarative specs with an execution toolkit |
| [0017](decisions/0017-strategist-checkout-is-untrusted-input.md) | 2026-09-27 | The strategist's checkout is untrusted input |
| [0018](decisions/0018-adopt-python-copier-template.md) | 2026-10-03 | Adopt DiamondLightSource/python-copier-template |

```{toctree}
:hidden:
:glob:

decisions/*
```
