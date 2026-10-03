# Watch, veto or re-arm go-live

Go-live is automatic: when the runner's gate passes, it arms, gives you 3 paper sessions to veto,
then switches the account to live by itself. [Evidence and promotion](../explanations/evidence.md#the-go-live-gate)
explains the gate.

## See where it stands

```bash
trader golive
```

(as `trader`, or `uv run trader golive` as `runner` with the services environment loaded, see
[Use the controls](controls.md)). It prints the override from `config/mode.yaml`, the effective mode, the saved state and the gate
evaluated now (days, trades, expectancy after slippage, worst day, and what's blocking). The same
state is in `/srv/trading/runtime/golive.json`, and the Daily P&L alert names the blocking reasons
while the gate is pending. The weekly journal carries the strategist's go-live assessment when the
gate is close or has armed.

## What you'll be told

- **Gate passed:** an alert, then one after each paper session with the sessions left.
- **Disarmed:** the gate stopped passing (or the logs couldn't be read) during the window; it's
  back to pending, and a later pass starts a fresh window.
- **Going live:** at the session start after the window, after a final gate check.
- **Live account under $100:** the runner stays on paper and alerts until it's funded.

The first 5 live sessions trade at half size, and so do the first 5 after every return to live (a re-arm after a demotion or a HOLD LIVE): any paper session, or clearing a live halt, restarts that count.

## Veto

Press **HOLD LIVE** on the dashboard, or as `runner` (with the services environment loaded, see
[Use the controls](controls.md)):

```bash
uv run trader hold-live
```

A HOLD always wins, at any stage, even one pressed while the runner is re-checking the gate. It
stays until you release it.

The runner decides paper or live once, when it starts (`schedule.runner_start`, 12:50 UK as
shipped, well before the open); the switch to live happens then too. A HOLD pressed after that
day's runner has started, even before the open, applies only from the next session; to stop today's live
trading as well, press STOP.

## Re-arm

```bash
uv run trader release-live
```

This resets the state to pending; the gate is re-evaluated after each session from then on. Use it
after a veto, or after clearing a live halt (which demotes go-live to paper). It's refused, with
nothing changed, while the live book is still halted (or its `risk.json` can't be read): run
`trader clear-halt live` after the close first.

Every go-live alert (armed, countdown, disarmed, going live, demoted) is saved in `golive.json`
with the change it announces until it has been sent, so a runner crash in between can't lose it:
the next runner start sends it, marked "delayed by a runner restart".

## A corrupt `golive.json`

If the file can't be read or holds an invalid state, the runner stays on paper, sends one urgent
alert, and changes nothing by itself: it doesn't reset to pending, so you can look first. HOLD LIVE
or `trader release-live` replaces it with a clean vetoed or pending state and keeps the bad file as
`golive.json.corrupt-<time>` (the newest 5 are kept).
