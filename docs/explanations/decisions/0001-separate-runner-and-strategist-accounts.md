# 0001. Separate accounts for the runner and the strategist

- **Date:** 2026-09-27
- **Status:** accepted
- **Origin:** #2 in the private development repository

## Context

The runner holds the live brokerage keys and also executes feature code the strategist writes. The
human's admin account has sudo, so running services there would put the live keys one
step from anything that compromised the session. The first design also let the strategist's
account trigger deploys through a sudoers rule.

## Decision

- Services, secrets and the deployed checkout move to a dedicated `runner` account with no sudo.
- The headless strategist runs as `trader`, with paper keys only and no access to the runner's
  secrets or runtime directory.
- Deploys are human-only: `trading-deploy` is interactive and refuses non-interactive callers.
  The rule that let `trader` deploy is dropped. Branch protection can't be relied on (it isn't
  enforced on free private repositories), so the deploy is the real gate.

## Consequences

Each component can be reasoned about by what its account can't do. The cost is some ceremony:
operational commands need `sudo -u runner` with the services environment loaded. Later refined
by [0007](0007-deploy-from-signed-merges.md), which removed the forced code diff.
