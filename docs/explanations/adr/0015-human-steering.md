# 0015. The human steers the strategist through state/steering.md

- **Date:** 2026-10-03
- **Status:** accepted
- **Origin:** #201 in the private development repository; jev-trader #10

## Context

The docs told the human to comment on the weekly issue to give feedback, but nothing made the
strategist read those comments. The human wants to argue with the strategist's choices, using a
full interactive Claude Code session rather than a bespoke chat interface.

## Decision

- Steering happens in an ordinary interactive session that reads the strategist's state and
  logs, and ends in a pull request into the `strategist` branch.
- Decisions go in `state/steering.md`, a human-owned file of numbered entries with reasoning.
  Active entries bind the strategist. It never edits the file, applies each new entry, and
  acknowledges it in its journal. It argues back in the journal, not by editing.
- By default a steering PR changes only `steering.md`, and the strategist applies the decision
  itself. Direct state edits are kept for emergencies.
- Merge only when no strategist run holds its lock; ideally between the close and the post-close
  run.

## Consequences

The strategist stays the single author of its strategy, and its journal explains every change.
See [Steer the strategist](../../how-to/steer-the-strategist.md).
