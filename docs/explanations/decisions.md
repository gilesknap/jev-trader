# How a decision is made

A **classifier** is one entry in `state/classifiers.yaml`: a set of symbols, a time window, a
question to ask the decision model, and the rules for sizing and exiting a position. The engine
turns that spec into a small state machine per symbol and asks the model only when the spec says
so. This page follows a classifier from "armed" to "flat again". The fields are listed in the
[classifier schema](../reference/classifier-schema.md).

## The per-symbol state machine

```{mermaid}
stateDiagram-v2
    [*] --> armed
    armed --> holding: P(ENTER) >= threshold,<br/>market order fills
    armed --> pending: P(ENTER) >= threshold,<br/>limit order rests
    pending --> holding: limit fills
    pending --> armed: limit expires unfilled
    armed --> retired: P(STAND_DOWN) >= threshold
    holding --> armed: exit, after_exit rearm<br/>and trades < max_trades
    holding --> retired: exit, otherwise
    retired --> [*]
```

Every classifier and symbol pair starts the day **armed**. The flatten 15 minutes before the
close retires everything for the rest of the day.

## Asking to enter

Each minute, the engine **considers** an armed symbol when the time is inside the classifier's
`window` (US Eastern), at least `cadence_min` minutes have passed since it last considered this
symbol, the feed isn't stale, and decisions aren't paused. Considering it restarts the cadence
clock, whether or not a question follows. It then asks the entry question only if:

1. The book isn't blocked (kill switch, halt or STOP) and doesn't already hold or have an order
   resting for this symbol.
2. Every `trigger` condition holds. Triggers are plain comparisons on feature values
   (`{feature: vwap_dist_pct, op: ">", value: 0.1}`); they are evaluated locally and cost nothing,
   so they're the cheap filter that keeps the model's attention on moments that matter.

Then the engine computes the classifier's `features` and sends Jev a state like this:

```json
{
  "symbol": "QQQ",
  "minutes_since_open": 47,
  "features": {"vwap_dist_pct": 0.1834, "rel_volume_15m": 1.42},
  "recent_1m_returns_bps": [1.2, -0.4, 3.1, 0.0, 2.2, 1.8, -0.9, 0.5, 1.1, 2.7],
  "position": null,
  "strategy_note": "<the classifier's context>"
}
```

- Feature values are rounded to 4 decimal places, and a feature with no value yet is `null`.
- The returns are the last ten 1-minute close-to-close returns, in basis points.
- There are no dates, times of day or prices: everything is dimensionless, so the model can't
  key on a particular day or price level.

With it go the classifier's `entry.instructions` and `entry.criteria`: a short description per
answer (`ENTER`, plus `WAIT` and/or `STAND_DOWN`). Jev returns a probability for each key.

- **P(ENTER) ≥ `entry.threshold`:** the engine tries to enter (below).
- **P(STAND_DOWN) ≥ threshold:** the symbol retires for the day ("stood down").
- **Anything else:** wait, and ask again after `cadence_min`.

Because the model returns probabilities rather than a free-text verdict, the threshold is the
classifier's own dial for how sure it must be. Criteria work best as crisp, mutually exclusive
descriptions of observable conditions.

## Entering

An ENTER goes through several gates, each of which can only shrink or skip it:

1. **Size.** `size_fraction` of current equity, or, with `risk_pct`, the size at which a
   stop-out loses about `risk_pct`% of equity, whichever is smaller. The account's first five live
   sessions ever run at half size. The result is capped by today's buying power: cash settled at the open, less
   today's buys (sale proceeds settle T+1 in a cash account, and re-using them can cause
   good-faith violations).
2. **Allocation.** The book-level limits (aggregate planned stop loss, equity exposure and
   overlapping buckets) may shrink it; a shrunk entry below max($10, 25% of the request) is
   skipped. See [Money safety](money-safety.md#book-level-limits).
3. **Guardrails.** The hard per-order checks: long only, in the universe, one position per
   symbol, not inside the flatten window, price at least $5, at most 25% of equity, within settled
   cash, a stop below the entry and no more than 10% away, a target above it.

The order is written to `pending.json`, with its cash reserved, *before* it is sent, so an order
whose answer is lost (a timeout, a crash) can still be found by its client order id.

- **Market entries** (the default) are notional market orders.
- **Limit entries** (`entry_order: {type: limit, offset_pct, expire_min}`) rest at
  `offset_pct`% below the last close, rounded down to whole cents. One that doesn't fill within
  `expire_min` minutes is cancelled; a non-fill isn't a trade, and the symbol re-arms. A partial
  fill cancels the rest, and the filled part is the position.

When the entry fills, the position's **stop** is `stop_pct`% below the entry (or, with
`stop_atr_mult`, that multiple of `atr_14_pct`, capped at `stop_pct`), and its **target** is
`target_pct`% above. A server-side stop order is placed at Alpaca where it accepts one (it often
won't for fractional quantities); the engine enforces the stop every bar regardless.

## Holding

While holding, the engine checks every bar since its last check, in order. (The bar a limit entry
filled in is checked only against the stop.) Per bar:

1. **Stop:** the bar's low at or below the stop exits. It's tested first, because the order of
   prices inside a bar is unknown, so the engine assumes the worst.
2. **Target:** the bar's high at or above the target exits. No profit order rests at the broker,
   so this is a market sell once the touch has been seen, at the latest price, not a fill at the
   target: a high that reverses within the minute is not a profit the engine could have taken.
3. **Scale-out** (`scale_out: {at_pct, fraction, stop_to_breakeven}`): the first time the high
   reaches `at_pct`% above the entry, `fraction` of the position is sold; with
   `stop_to_breakeven`, the stop rises to the entry price.
4. **Trailing stop** (`trail_pct`): the stop ratchets up to `trail_pct`% below the highest high
   since the entry, using this bar's high. It only ever rises, and never moves the stop that the
   same bar's low was tested against.

After the bars, a **time stop** (`max_hold_min`) exits once the position is that many minutes
old. Each position keeps the rules it was opened with, even if the spec is edited mid-session.

At each cadence, inside the window, the engine also asks the **exit question** (`exit.instructions` with exactly
`HOLD` and `EXIT` criteria). The state now includes the position:

```json
"position": {
  "unrealised_pct": 0.214,
  "minutes_held": 12,
  "stop_dist_pct": -0.386,
  "target_dist_pct": 0.586
}
```

plus `scaled_out` when the classifier has a scale-out. P(EXIT) ≥ `exit.threshold` sells.

## After the exit

The exit is recorded in the book's `trades.csv`: a `sell` row with the whole round trip's P&L
(any scale-out or piecemeal fill is a `sell_part` row, so one round trip is always one trade).
The `reason` column says what closed it: `stop`, `stop (raised)`, `target`, `time stop`,
`classifier EXIT`, `eod flatten`, `server stop` and so on.

Then `after_exit` decides: `rearm` arms the symbol again while it has made fewer than
`max_trades` trades today; `retire` (the default) ends its day.

## Probes and sims

Two modes change what happens after the model answers:

- **`mode: probe`** asks the entry question whenever its trigger holds and logs P(ENTER) with the
  price, and never orders or stands down. It measures whether the model's judgement carries
  information, cheaply, over many symbols.
- **`mode: sim`** runs the whole cycle above on the classifier's own simulated account, so
  experiments don't compete with each other for the paper account's symbols and cash.

Both are covered in [Evidence and promotion](evidence.md).

## When the model is unavailable

Jev calls have a 5-second timeout for each stage (connecting, sending, waiting for the answer) and are retried once (except for client errors other than rate
limits, which wouldn't improve on a retry). A failed
call pauses every trading decision for 5 minutes, with one urgent alert: no new entries and no
model-driven exits, while stops, targets, the time stop, the flatten and the risk checks carry on.
Probe failures pause only the probes. The runner fails closed: it never enters without an answer.
