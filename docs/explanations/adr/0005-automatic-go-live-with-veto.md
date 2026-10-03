# 0005. Go-live is automatic after a veto window; the gate is a floor

- **Date:** 2026-09-27
- **Status:** accepted
- **Origin:** #7, #33 in the private development repository

## Context

The first design flipped the account to live when a human merged a change. The aim is an
experiment that runs unattended, but switching to real money shouldn't be invisible.

## Decision

- The runner evaluates a go-live gate from its own paper logs, which the strategist can't write.
  The gate needs: a minimum number of trading days, at least 20 closed trades by non-control
  classifiers, positive expectancy after slippage, no paper day at or below −5%. A minimum live
  equity is checked separately, only at the switch.
- When it passes, go-live **arms**: the human is alerted every session and gets three paper
  sessions to veto it (HOLD LIVE on the dashboard, or `trader hold-live`). Then the account goes
  live by itself, at half size for its first five live sessions. A live halt demotes it to paper, and only the
  human can re-arm.
- `config/mode.yaml` can force `paper` or `live` instead of `auto`.
- The charter says the gate is a safety floor, not a target. The strategist must never add
  classifiers, loosen thresholds or raise cadence to reach it sooner; not passing is the right
  outcome when there's no edge.

## Consequences

The human's job shrinks to watching and vetoing. Later work re-checks the gate during the veto
window and just before switching, so a deteriorating record disarms it.
