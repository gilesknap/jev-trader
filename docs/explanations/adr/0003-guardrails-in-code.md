# 0003. Money rules are enforced by the runner, not the charter

- **Date:** 2026-09-27
- **Status:** accepted
- **Origin:** #34 in the private development repository

## Context

A pre-go-live review found rules that existed only as instructions to the strategist (shadow-to-live
promotion), and money handling that could break the rules it implied: same-day sale proceeds
recycled into new buys (a good-faith violation risk in a cash account), and risk state
forgotten on a restart. An agent can misread or ignore a charter; it can't ignore code it can't
edit.

## Decision

- Guardrails, the kill switch, the halt and the settled-cash ledger live in code on the deployed
  checkout, which the strategist can't write. Their state is persisted and survives restarts.
- New entries may use at most the cash settled at the open, less today's buys.
- Promotion to `mode: live` is enforced at every session start: a classifier without at least 20
  closed paper trades with positive expectancy after slippage, **on its current spec**, is
  downgraded to shadow. Any spec change other than `mode`, `enabled` (and later `family`)
  restarts the record.
- The charter describes these rules but never claims a check that the code doesn't make.

## Consequences

The strategist designs inside limits it can't move. Thresholds are deliberately not
configurable in `config.yaml` (see [0011](0011-root-config-yaml.md)).
