# 0016. Strategies are declarative specs with an execution toolkit

- **Date:** 2026-09-27
- **Status:** accepted
- **Origin:** #38, #41 in the private development repository

## Context

The strategist can change its own data but not code. If every new idea for entering or exiting
a position needed code, the strategist would be limited to the few behaviours the engine shipped
with, and every experiment would wait for a human-reviewed code change.

## Decision

A classifier is a YAML spec, and the engine provides a toolkit of optional execution fields that
it enforces every bar, inside the hard guardrails: trailing stops, time stops, limit entries,
scale-outs, ATR-based stops, re-arming after an exit. Unknown keys are rejected: a misspelt
field drops that one classifier for the day, with an alert, rather than being silently ignored
(2026-09-29). Unset fields are left out of the spec's identity, so adding a new toolkit field
doesn't restart existing promotion records.

## Consequences

Most strategy changes need no deploy: the runner reads the specs at each session start. New
toolkit features are code, and go through the proposal route
([0014](0014-public-code-private-data.md)).
