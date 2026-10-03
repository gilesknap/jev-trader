# 0012. The strategist's schedule uses systemd user timers, not cron

- **Date:** 2026-09-29
- **Status:** accepted
- **Origin:** #153 in the private development repository

## Context

Ubuntu's cron ignores `CRON_TZ`, so the strategist ran on UTC rather than the configured local
time. The post-close retry ended up 15 minutes before the watchdog's cutoff.

## Decision

Four systemd user timers (pre-market, post-close, weekly, housekeeping) run one oneshot service
template. Their calendar specs come from `config.yaml` without a zone, and the configured zone is
appended, which systemd honours. The `trader` account lingers so its timers run without a login.

## Consequences

Schedules are validated (`systemd-analyze calendar` in the tests) and checked by `check.sh`.
Each run holds a lock for its whole length; that lock, not the unit state, is how to tell a run
is active.
