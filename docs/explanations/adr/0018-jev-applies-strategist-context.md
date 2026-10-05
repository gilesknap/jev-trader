# 0018. Jev applies the strategist's context in real time, and is judged against a twin without it

- **Date:** 2026-10-05
- **Status:** accepted
- **Origin:** a steering session on the first observe day; implemented by #73

## Context

The project's premise is that a cheap decision model (Jev) paired with a daily Opus strategist can
make intraday calls that a mechanical rule can't. Until now, Jev saw only the classifier's numeric
features, recent one-minute returns, the open position and a fixed `context` paragraph. A linear
model can use those inputs just as well, and early probe scoring agreed: adding Jev to a linear
baseline on the same features changed its predictive power by almost nothing. Used this way, Jev
does mechanical work, more expensively and less reliably than a rule.

An LLM's advantage is reading text and matching a situation to a description. Opus's advantage is
slow, careful reasoning, once a day. Neither was being used in the live decision.

## Decision

- **Split the work.** The strategist (Opus) does the thinking before the open and writes it down.
  Jev applies it, tick by tick, to what is happening now.
- **Jev gets optional context inputs**, switched on per classifier: recent headlines for the symbol
  (cut off at the decision time, so replays never see the future), a shared daily note, a
  per-classifier playbook, and on exit asks a thesis recorded with the position at entry. The note,
  the playbook and the thesis text don't change a classifier's identity, so writing a new note each
  day doesn't restart its record. A missing input is sent empty and never pauses decisions.
- **A Jev-judged idea is judged against its twin.** The same trigger and exits without Jev's
  judgement, or without the new inputs, run alongside it on the same days, and the verdict is the
  difference after costs. If Jev doesn't beat its twin, that is the finding.
  The twin is a control, not a mechanical equivalent: it keeps the rule's skeleton and replaces
  only the judgement with something trivial (always, never, random at the same rate, or no context
  inputs), so even a rule with no mechanical equivalent has one. Where an obvious cheap proxy for the
  judgement exists, it runs as a second baseline.
- Mechanical rules stay first-class: they are the baselines, and the strategist may find edges with
  no Jev call at all.

## Consequences

The experiment can now answer its own question: does an LLM's judgement add value here, and where?
The daily note and the playbook exist only going forward, so ideas that use them can't be
backtested; that matches the existing rule that only forward results count. More input text makes
each Jev call cost more, so the inputs are opt-in. How the strategist chooses and steers these ideas
is a matter for the private steering file, not this record.
