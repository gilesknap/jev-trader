# 0010. Each experiment can trade its own simulated account

- **Date:** 2026-09-28
- **Status:** accepted
- **Origin:** #58 in the private development repository

## Context

All shadow classifiers shared one paper account, with one position per symbol and about five
entries a day from settled cash. Experiments crowded each other and the control, so each record
partly reflected scheduling luck, and adding a candidate could change another's evidence.

## Decision

- `mode: sim` gives a classifier its own simulated account, fed by the same live bars, with
  the same guardrails, kill switch, halt and cash ledger as paper. It persists across restarts.
- Sim trades count towards nothing: promotion and the go-live gate still use the paper book only.
- STOP covers sim accounts. A halted sim account is cleared per account.

## Consequences

Full strategies (entries, exits, the execution toolkit) can be evaluated in isolation, at the
cost of fills that are simulated rather than the broker's.
