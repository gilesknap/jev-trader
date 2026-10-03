# 0006. Flat every night: no overnight holds

- **Date:** 2026-09-27
- **Status:** accepted
- **Origin:** #45 in the private development repository

## Context

Holding overnight was considered as a source of return.

## Decision

Parked. Every position is flattened before the close. The system is built around watching an
open market (minute-bar features, per-bar stops, the kill switch). Overnight risk is gap risk that
neither stops nor supervision can handle. Flat day-starts keep each day's P&L attributable, and
the overnight premium is a broad market effect, not a pattern a classifier could detect.
`DESIGN.md` lists the conditions for reopening it.

## Consequences

Strategies are strictly intraday. The end-of-day flatten is safety-critical, and it was later
made independent of the engine's tick so that it survives a failing tick.
