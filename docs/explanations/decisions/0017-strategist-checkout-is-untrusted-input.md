# 0017. The strategist's checkout is untrusted input

- **Date:** 2026-09-27
- **Status:** accepted
- **Origin:** #49 in the private development repository

## Context

The runner reads the strategist's files, and the strategist writes them as a less trusted
account. Git stores symlinks, so a symlinked `classifiers.yaml` could point at any file the runner
can read, and a parse error would quote part of it into an alert the strategist can read.

## Decision

Every file the runner and the dashboard read from the strategist's checkout goes through one
module, `trader.safeio`. Every path component below the checkout is opened with `O_NOFOLLOW`,
relative to its parent's descriptor, so a symlink swapped in after a check can't redirect the read.
Only regular files are read, with a size cap, so a FIFO or a huge file can't hang the runner.

## Consequences

A symlink in the strategist's data is refused: a symlinked `classifiers.yaml` means nothing
trades that day, and `trader validate` names it. Companion to the feature sandbox
([0004](0004-sandbox-strategist-features.md)): the strategist's code and its data are both treated
as hostile.
