# 0009. Probe mode: measure the model's judgement without trading

- **Date:** 2026-09-28
- **Status:** accepted
- **Origin:** #56 in the private development repository

## Context

Traded classifiers ask the model only when a trigger holds, and stop once they reach their trade
limit. That gives about two trades a day to judge the model by, far too few to tell whether its
judgement is worth anything. Model calls are cheap; trades cost slippage and settled cash.

## Decision

- `mode: probe` asks a classifier's entry question whenever its trigger holds, logs P(ENTER)
  with the price, and never orders.
- A nightly probe report joins the answers to forward returns. It reports the information
  coefficient across days, quintile returns, each feature's IC, and the key test: a walk-forward
  linear model with and without P(ENTER). If the model doesn't lift out-of-sample IC,
  deterministic rules would do the same job.
- Probes run after trading classifiers each tick, in a bounded time budget, and their failures
  pause only the probes.

## Consequences

The experiment can learn about the model at hundreds of observations a day instead of a
handful.
