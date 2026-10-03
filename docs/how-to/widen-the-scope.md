# Widen the trading scope

As shipped, the system trades **long only, intraday**, in a cash account, on about 30 liquid US
ETFs and mega-cap stocks, using Alpaca's free IEX market data. That scope is a choice, not a
limit of the design. This page lists the levers in your own fork, what each one involves, and
what it costs. Most of them are code changes: reviewed, merged and deployed like any other.

## Before you widen anything

Widen the scope when the evidence asks for it, not before. Every lever below adds a cost
(a data bill, more risk, or more code that touches money), and none of them creates an edge by
itself. The scoreboard and the probe report (see [Evidence and promotion](../explanations/evidence.md))
tell you whether the current scope has shown anything worth scaling. A wider universe also gives
the strategist more ways to fit noise.

## The universe

`config/universe.yaml` in the code repository lists the only symbols any classifier may trade.

- **Up to 30 symbols** is free: that's the free IEX stream's limit. Classifiers whose symbols
  together (plus SPY) exceed it ask for a subscription the feed refuses whole.
- **A change needs a matching allocator change.** The book-level buckets in
  `src/trader/allocator.py` classify every universe symbol, and a test checks it (see
  [Configuration](../reference/configuration.md)).
- **Smaller stocks are thin on IEX.** Mid-caps move more, but IEX carries a few percent of their
  volume, so minute bars go missing and features get noisy. They really want the paid feed below.

## Market data

The live feed is IEX: free, but only a few percent of consolidated volume. History for backtests
and probe scoring already comes from the full SIP feed, which is free when it's at least 15
minutes old.

- **Paid real-time SIP** (Alpaca's Algo Trader Plus, about $99 a month at the time of writing)
  gives the live runner the full consolidated tape, and lifts the 30-symbol stream limit.
- **What changes in code:** the runner asks for `feed="iex"` / `DataFeed.IEX` in
  `src/trader/runner.py` (the stream, the REST fallback, and the prior session's volume). Switch
  those, and then revisit [the IEX/SIP notes in Limitations](../explanations/limitations.md):
  live and backtest data would then come from the same feed.
- **Is it worth it?** On a small account, $99 a month is a large fraction of the capital. It pays
  only once there's an edge to scale.

## The guardrail limits

Position size, the minimum price, the stop distance, the daily kill switch and the halt are
constants in `src/trader/guardrails.py`, listed in [Guardrails and limits](../reference/guardrails.md).
The strategist can't change them; you can, in a pull request. Loosening one is a risk decision,
not a tuning knob: the go-live gate and the tests were written against these values.

## Beyond long-only intraday equities

These are larger changes. Each one touches the parts of the code that handle real money, so treat
them as projects, with their own design and review.

- **Overnight holds.** Parked on purpose: the [design](../explanations/design.md) lists why, under
  "Parked ideas", and the conditions for reopening it. The end-of-day flatten, the session model and the P&L attribution all assume flat
  nights.
- **Shorting and margin.** The engine, guardrails and order paths are long only, and the
  settled-cash ledger assumes a cash account. Shorting needs a margin account, borrow
  availability, short-side stops and different risk limits (a short's loss is unbounded).
- **Options and other derivatives.** These need new order types, pricing and Greeks, a different
  data feed, and position limits that understand leverage and expiry. The per-symbol state
  machine and the guardrails assume shares.
- **Crypto.** Alpaca trades it, but crypto runs around the clock, so the session model (start,
  flatten, close, day-start equity, the kill switch's day) needs rethinking. It also has its own
  data stream and fee schedule.

## What stays the same

Whatever you widen, keep the safety model: limits enforced in code the strategist can't edit,
experiments proven on paper (or in sim accounts) before they reach real money, and the control
strategy as the yardstick. See [Money safety](../explanations/money-safety.md).
