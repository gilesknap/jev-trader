# Money safety

The strategist designs what to trade; it never decides how much can be lost. Every limit on this
page is code, in the runner's checkout of the code repository, which the strategist can't write
and its GitHub token can't reach. Changing one needs a pull request that a human reviews, merges
and deploys. Whatever `classifiers.yaml` says,
the runner enforces these. Compare [beebots](related-work.md#three-camps), which also puts a
plain-code risk layer under a model that makes each call.

The layers, from a single order up to the whole account:

```{mermaid}
flowchart TD
    E[ENTER answer] --> S[size: size_fraction / risk_pct,<br/>half size in the first 5 live sessions,<br/>capped by settled cash]
    S --> A[book-level limits: aggregate stop risk,<br/>equity exposure, overlapping buckets]
    A --> G[per-order guardrails]
    G --> O[order]
    O --> P[position: stop and target every bar,<br/>server-side stop where Alpaca allows]
    P --> K[book: kill switch at -5% on the day,<br/>halt at -30% from the NAV high or under $50]
    K --> F[flatten 15 min before the close]
```

## Per-order guardrails

`src/trader/guardrails.py` checks every entry just before it is sent. An entry is refused if:

- it isn't a buy (long only: no shorting, margin or options; the live account is a cash account);
- the symbol isn't in `config/universe.yaml`, or the book already holds it or has an order
  resting for it (one position per symbol);
- the book is blocked (kill switch, halt or STOP);
- it's inside the end-of-day flatten window;
- the price is under $5;
- its notional is over 25% of equity, or over the settled cash available;
- its stop isn't below the entry, or is more than 10% below it, or its target isn't above it.

The values are constants, listed in [Guardrails and limits](../reference/guardrails.md).

## Book-level limits

A position's stop is a plan, not a promise, and several positions can fall together. So before
the per-order checks, each book (paper, live and each sim account, separately) may shrink an
entry so that (`src/trader/allocator.py`):

- **Aggregate stop risk:** the planned stop losses of everything held or pending, plus today's
  realised loss, stay within 3% of the smaller of current and day-start equity. That's two points
  under the −5% kill switch, as a buffer for gaps and slippage. Realised gains never enlarge it.
- **Equity exposure:** stocks and equity ETFs total at most 75% of equity. TLT and GLD are outside
  this cap.
- **Overlapping buckets:** each bucket is at most 50% of equity. The buckets are growth (QQQ,
  XLK, SMH, XLY and their mega-caps), financials, health and energy; SPY, DIA and IWM count
  against every bucket, since they hold most of each.

These only ever shrink an entry. A shrunk entry is placed only if it keeps at least the larger of
$10 and a quarter of what was asked; otherwise it is skipped, so a dust trade never counts as
evidence. The binding limit is logged as an `allocation` row in the decisions log.

Two 25% positions with 10% stops already plan to lose 5%, so the 3% budget binds well before the
per-position cap does. The buckets are conservative overlap proxies, not correlation estimates,
and gaps, stale quotes and failed exits can lose more than any stop plans for.

## Cash settlement

A cash account settles sale proceeds T+1. Buying with unsettled cash and selling the same day is
a good-faith violation. So each book records the cash that was settled at the open (measured at
startup, before anything sells), and entries may use at most that, less today's buys. A sale
doesn't enlarge today's buying power.

## The kill switch and the halt

Every minute, each book's equity is checked (`risk_check`):

- **Kill switch:** equity at or below 95% of the day-start equity (−5% on the day) flattens the
  book and blocks new entries until the next session. Day-start equity is persisted, so a restart
  mid-session keeps the same baseline.
- **Halt:** NAV per unit 30% or more below its high-water mark, or equity under $50, flattens the
  book and blocks it until a human clears it with `trader clear-halt`. A halt never clears itself.
  Clearing it rebases the high-water mark to the current NAV, so it doesn't re-trigger at once.

The high-water mark is on unit NAV, not equity, so withdrawing profits doesn't look like a
drawdown. If the live book halts, go-live is demoted back to paper, and only the human can re-arm
it.

## Flat every night

From 15 minutes before the close, every tick flattens every book and retries until the bell.
Nothing is ever held overnight. Overnight holds were considered and set aside: the system's edge
and protection (minute bars, per-bar stops, the kill switch) don't work while the market is shut,
and starting each day flat keeps each day's P&L attributable to that day's decisions. See the
parked ideas in the [design](design.md).

## Human controls

- **STOP** (dashboard, or `trader stop`): flattens every book, sim accounts included, and blocks
  them for the rest of the trading day. If the runner looks dead (no heartbeat for 3 minutes),
  STOP flattens directly through Alpaca.
- **HOLD LIVE** (dashboard, or `trader hold-live`): vetoes going live, at any stage, until the
  human runs `trader release-live`. The runner reads it when it starts (`schedule.runner_start`, well
  before the open), so once that day's runner has started it applies from the next session; STOP is the immediate control.
- **`config/mode.yaml`** (in the data repository's `main`): forces `paper` or `live`, overriding
  the automatic go-live, after a deploy, which always shows the change.
- **Money movements** are the human's alone. No part of the system has transfer permissions:
  Alpaca API keys can trade but can't withdraw.

## Positions the engine doesn't know

- **Positions the engine didn't open are sold.** From the first tick after the open, anything held
  that the engine isn't tracking (a crash before an entry was saved, a hand-bought share) is
  market-sold, with an urgent alert and no trade recorded. Don't trade by hand on these accounts.
- **Lost answers are found again.** Every entry order is saved, with its cash reserved, before it
  is sent. If the answer is lost, the order is found by its client order id and its fill becomes
  a tracked, protected position. Only if Alpaca has no such order a minute later (or refused it
  outright) is the reservation released.
- **A position that vanished** (its server-side stop fired, or it was closed outside the engine)
  has its exit booked from the broker's fill. If no fill can be found, the exit is recorded at an
  estimated price with `(price estimated)` in the reason and no `pnl_pct`. Such a round trip
  counts towards nothing: not the go-live gate, not promotion, not the scoreboard.

## Strategist code never runs in the runner

The runner holds the live keys, but custom features are strategist-written Python. They run only
in a bubblewrap worker with no network, a cleared environment and only `/usr`, `/etc`, the code checkout and a
runner-owned copy of the feature files mounted, read-only. The runner never imports them, and
there is no in-process fallback: without bubblewrap, custom features are disabled. If the worker
fails or hangs, custom features return NaN for the rest of the session, so triggers that depend
on them fail and nothing new is entered, while stops keep working. The static check on the source
(an import allow-list and banned names) is only a first filter.

For the edge cases (fractional positions without server-side stops, partial exits, data outages)
see [Known limitations](limitations.md).
