# Hypotheses and experiments

The strategist's charter asks for a written, falsifiable record for every idea it runs or rejects,
kept in `state/watchlist.md` on its branch. A record says, before the confirming data exists, what
would count as the idea being wrong. Without one, a run that has tried several prompts, features
and thresholds can keep whichever version happens to look good, and a small forward sample is
split across a stream of new ideas without any of them being tested.

This page explains the template and gives two worked examples. **Both examples are invented**, as
are their numbers. They show the shape of a record and aren't strategy advice: neither idea has
been tested by this project.

See also: compare [AQuA](related-work.md#three-camps), which also writes a mechanism and
falsification criteria before testing and keeps a persistent belief memory, but stops at
backtests.

## The template

```text
### H<n>: <name>. Status: exploring | confirming | retained | rejected (<date>)
- Observation: what you saw, where, on which sessions.
- Mechanism: why it should happen (who is forced to trade, what adjusts slowly).
- Prediction: the conditional behaviour, as "when A, B within T, more than when not A".
- Simpler explanation: the boring rival (market beta, time of day, volatility, noise) and the check that tells them apart.
- Disconfirmed if: the result that kills it, stated as a number.
- Baseline: what it must beat on the same days (control_orb, the unconditional move, the probe report's linear baseline).
- Costs: edge per trade needed after 0.1% round-trip slippage; fill assumptions (limit fills, thin names, IEX volume).
- Checkpoint: the date or closed-trade count at which you decide, and the decision rule.
- Rules: classifier ids, with the date each spec was frozen. Record: journal dates for its findings and replay log.
```

An idea has two stages:

- **Exploring.** Replays, probes and sim accounts, tuned freely, with every variant counted in the
  journal's replay log (the tools also count each replay in the
  [trial ledger](../reference/files.md#the-trial-ledger)). Nothing at this stage is evidence.
- **Confirming.** The spec is frozen, and the checkpoint and its decision rule are fixed before
  the first confirming session. Sessions the strategist has already looked at don't count. The
  spec is edited only for a bug or a steering entry, and any edit, even one of those, ends the
  attempt: it is recorded, and a new attempt starts with a new checkpoint. The runner's promotion
  record restarts on the same edits, and on any change to the custom feature code if the rule
  uses a custom feature (see [Evidence and promotion](evidence.md#promotion-to-mode-live)), so
  those files are left alone while such a rule is confirming.

At the checkpoint the decision rule is applied as written. A close result doesn't earn a later
checkpoint. The outcome is `rejected`, `retained` (it passed its last planned checkpoint), or
`confirming` again with the next checkpoint, if the rule planned one. Rejecting an idea after a cheap check, before it ever trades, is a result too. It
meets the weekly exploration requirement, so the strategist is never pushed to trade a weak idea
just to have something novel running.

Each non-control classifier belongs to one record. A YAML comment above it in
`state/classifiers.yaml` names the record. A comment is used rather than `context` because editing
`context` would change the classifier's identity and restart its promotion record. The `family`
label describes where an idea came from (`novel` or `conventional`), not how good it is.

## Example: taken to its checkpoint

```text
### H7: Midday range break. Status: confirming (checkpoint 1 passed 2026-11-30; checkpoint 2 at 50 trades)
- Observation: in 40 older SIP sessions, when QQQ's 11:30–13:00 range was under half its
  09:30–11:30 range, the first later break above the midday high often ran on (eyeballed, then
  counted: 23 of 40 sessions qualified).
- Mechanism: liquidity and participation return after lunch; orders resting at the edge of a
  quiet range fire together, and traders waiting for a direction join the first break.
- Prediction: after a qualifying quiet midday, a break above the midday high reaches +0.3% within
  45 minutes more often than an equal-sized break of the morning range does.
- Simpler explanation: an afternoon upward drift, or SPY beta. Check: compare with SPY's return
  over the same 45 minutes, and with the same rule fired at the same times on days whose midday
  wasn't quiet.
- Disconfirmed if: at the checkpoint, mean return per trade after slippage is ≤ 0, or no better
  than control_orb on the same days.
- Baseline: control_orb; the non-quiet-day version of the rule (replay); SPY over the same window.
- Costs: 0.1% round trip; target 0.6%, stop 0.3%, so the hit rate must exceed about 45%. Market
  entries in QQQ and IWM only (deep enough that the slippage assumption holds).
- Checkpoint: 25 closed paper trades or 2026-12-18, whichever comes first. Rule: if mean net > 0
  and above control_orb, keep confirming in shadow to a second checkpoint at 50 trades; else reject.
- Rules: midday_break (shadow), frozen 2026-11-02 (attempt 2). Record: journal 2026-10-27
  (exploration, 6 replay variants), 2026-11-02 (attempt 1 ended), 2026-11-30 (checkpoint 1).
```

Exploration produced six counted replay variants. The first attempt at confirmation ran for 7
trades; then the strategist added a trailing stop, which ended the attempt (recorded in the
journal), and attempt 2 started on fresh sessions. It reached 25 trades over 19 trading days,
before the checkpoint's date: mean +0.04% per trade after slippage, against −0.02% for control_orb on the same days.
The day-clustered interval spans zero. The decision rule said keep going, so the rule stayed in
shadow, still confirming, with its spec still frozen, towards the second checkpoint at 50 trades. The weekly journal says plainly that the edge isn't
distinguishable from luck yet.

## Example: rejected before trading

```text
### H9: Haven bid, equities lag. Status: rejected (2026-11-14), before trading
- Observation: on a few mornings when TLT and GLD both rose in the first hour, SPY was weak in the
  afternoon.
- Mechanism: a cross-asset move to safety shows up first in bonds and gold, and equity selling
  follows as slower money de-risks.
- Prediction: when TLT and GLD are both up over 09:30–10:30, SPY's 13:00–15:30 return is lower
  than on other days.
- Simpler explanation: SPY's own weak first hour (bonds and gold rise because equities fall) and
  ordinary afternoon continuation. Check: regress SPY's afternoon return on its first-hour return
  with and without the haven flag.
- Disconfirmed if: the flag's coefficient is indistinguishable from zero once SPY's first hour is
  in the regression (|t| < 2), or too few days qualify to reach a checkpoint within a quarter.
- Baseline: SPY's first-hour return alone.
- Costs: long only, so the tradeable form is long TLT or GLD in the afternoon, not short SPY. That
  needs its own edge after 0.1% round trip, a different prediction from the one observed.
- Checkpoint: one ad-hoc study on 60 older SIP sessions. Rule: probe it only if the flag survives.
- Rules: none. Record: journal 2026-11-14.
```

The study took one post-close run. 9 of the 60 sessions qualified. The raw afternoon difference
was −0.12%, but with SPY's first hour in the regression the flag's coefficient had t = 0.4: the
simpler explanation accounted for it. At about 3 qualifying days a month, a 20-trade checkpoint
would also have taken more than six months. The idea was rejected without a probe, and the
record satisfied that week's exploration requirement. Once it is a month old the strategist cuts
it to one line, so a later run doesn't try it again blindly:

```text
- H9 Haven bid, equities lag: rejected 2026-11-14. SPY's own first hour explains it (t 0.4, 60 sessions); also ~3 events/month.
```
