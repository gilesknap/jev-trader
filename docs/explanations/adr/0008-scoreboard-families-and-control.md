# 0008. A scoreboard of novel against conventional ideas and a fixed control

- **Date:** 2026-09-27
- **Status:** accepted
- **Origin:** #51 in the private development repository

## Context

The interesting question is whether the strategist's **own** ideas beat textbook strategies and a
fixed benchmark, not just whether the account went up. A blended equity line can't answer it.

## Decision

- Every non-control classifier carries `family: novel | conventional`. `control_*` classifiers
  are the fixed benchmark and have no family. The label doesn't change behaviour, and relabelling
  doesn't restart a promotion record.
- A scoreboard reports per classifier and per family, after slippage: trades, win rate, mean net
  return with a confidence interval, and a cautious verdict ("can't tell from luck yet" until the
  interval clears zero). It adds Welch comparisons between families, and buy-and-hold SPY as a
  yardstick.
- The charter asks for honest labels and justification of each `novel` label, because the human
  may steer the project by this board.

## Consequences

The family label is a claim the human audits. The board deliberately withholds verdicts at small
samples.
