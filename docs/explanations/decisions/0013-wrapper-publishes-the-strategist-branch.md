# 0013. The wrapper, not the model, publishes the strategist's branch

- **Date:** 2026-09-30
- **Status:** accepted
- **Origin:** #155, #192 in the private development repository

## Context

The strategist may change only its own data paths, and the wrapper reverts anything else after
each run. Once the model could push, it pushed its own commits, which reached GitHub before the
wrapper's path check, and broke the wrapper's own push.

## Decision

- The model is told not to commit or push. The wrapper path-checks the working tree, commits the
  checked tree on top of whatever the run already published, and pushes a plain fast-forward. It
  never force-pushes.
- Anything the run pushed outside the allowed paths is reverted in that commit, with an alert.
- If someone else pushed during the run, the wrapper rebases onto it, or aborts and alerts.
- The revert works path by path from NUL-separated, literal pathspecs, so one odd filename can't
  skip the rest (#192, 2026-10-03).

## Consequences

The allowed-paths rule is enforced, not trusted. Humans can push to the strategist branch
between runs (see [0015](0015-human-steering.md)); the wrapper pulls it before every run.
