# Known limitations

The edge cases, and what the system does about each. Most come from three facts: a small account
trades fractional shares, the free market-data feed is IEX only, and the runner can crash or lose
an answer from the broker at any moment.

## No bracket orders

Alpaca rejects bracket orders for fractional quantities, and at this account size almost every
position is fractional. Instead:

- Entries are notional market orders (or limit orders, if the classifier asks).
- A server-side stop is placed where Alpaca accepts one.
- The engine enforces the stop and target on every 1-minute bar regardless.
- If the runner dies with a position open and no server-side stop, the watchdog alerts within
  about 10 minutes. The restarted runner recovers the position from `entries.json`, and STOP
  flattens directly through Alpaca.

## Positions the engine didn't open are sold

Every minute from the first tick after the open, anything held that the engine isn't tracking
(say, a hand-bought share) is market-sold, with no trade recorded. Don't trade by hand on these
accounts.

- Every entry order is saved, with its cash reserved, before it is sent. If the answer is lost (a
  timeout, a crash), the order is found by its client order id and its fill becomes a tracked,
  protected position; the symbol and cash stay reserved until then. Only if Alpaca has no such
  order a minute later (or refused it outright, a 403) is the reservation released.
- The trade-off: while that lookup keeps failing (an Alpaca outage), shares the order did buy sit
  in the account with **no server-side stop and no engine stop**, and they aren't sold as an
  orphan either. They're adopted and protected as soon as the lookup succeeds; the end-of-day
  flatten and STOP still close them. The urgent "got no clear answer" and "check failed" alerts
  are the cue to look at Alpaca.
- A fill's price is never the reference price. A market fill whose average price Alpaca hasn't
  reported (after a brief re-poll once the order is final; for part of an order not yet final,
  after 3 minutes) is priced at the position's average cost. If that is missing too, it's
  protected at the last price and logged `ENTER (price estimated)`, and its round trip isn't
  evidence. So is a limit fill with no reported price, protected at its limit.
- One order is at most one round trip: shares an entry order fills after its position has closed
  are sold as untracked, never a second trade.

## Exits whose real price is unknown

An exit whose price can't be found (the position vanished with no fill found, or a close-all sold
it) is logged with `(price estimated)` in the reason and no `pnl_pct`, so the go-live gate,
promotion and the scoreboard leave it out.

That includes a position that closed while the runner was down with no fill found: it's recorded
at its stop, so its dollar P&L still counts against today's loss budget, but it's
`(price estimated)` too. A lookup that *fails* (an Alpaca error) doesn't count as "no fill found":
the position stays tracked, is never sold, and its fill is looked up again each minute, for up to
10 minutes or until the end-of-day flatten, before the estimated row is written.

## Exits that fill in pieces

A close cancelled part-filled, or a server stop that partly fired, is booked leg by leg:

- Each order's fill is a `sell_part` row at its real price, booked once by its order id, so a
  retry or a restart never counts it twice.
- The shares left stay tracked, and the exit is retried. Their server stop is re-placed for just
  their quantity, unless the old stop's cancel isn't final: then the old one stays, so two are
  never alive. An exit that fails with nothing sold re-places the server stop the same way (except
  in the flatten, whose close-all follows at once), so the shares aren't left without one.
- A close refused because open sell orders hold the shares (a server stop Alpaca took, whose
  answer was lost) cancels the symbol's open sells, never a buy, books anything they sold by order
  id, and is retried once in the same call. If that fails too, the shares get a server stop again
  while the exit retries.
- A close booked before it was final is re-read by its order id at the next exit attempt, so
  anything it sold later is booked at its real price. A crash between a sell and its booking loses
  that leg (Alpaca's `close_position` takes no client id to find it by), and the round trip ends
  `(price estimated)`.
- The final `sell` row carries the whole round trip's P&L: one trade. A leg whose price Alpaca
  never reported makes the round trip `(price estimated)`, and so does a final fill that accounts
  for fewer shares than were held.

## Aggregate limits are estimates, not loss bounds

The book-level limits (see [Money safety](money-safety.md#book-level-limits)) plan with each
position's stop. Gaps, stale quotes and failed exits can lose more than a stop plans for, and the
buckets are overlap proxies, not correlations. A restart re-reads today's realised losses from
`trades.csv`; if it can't, it alerts and counts today's equity change instead, which includes
unrealised loss.

The paper book is shared by every shadow classifier, so a shadow classifier may get fewer entries
on paper than it would in its own sim account. Compare the `allocation` rows in the decisions log
before reading that as a weaker signal.

## Market data

- **The live stream takes at most 30 symbols** (the free IEX limit). Symbols held from before
  startup are streamed only while there's room after the classifiers' symbols and SPY; anything
  left out is alerted. Non-equity holdings (crypto, say) get no data but are still sold.
- **A stale stream** (no SPY bar for over 3 minutes) blocks new entries. While it lasts, the paper
  and live books' held symbols get their IEX bars over REST once a minute, so their engine stops
  keep working; the stream's bars win any minute both have. A failed REST poll alerts at most
  every 30 minutes, and the positions rely on their server-side stops and the kill switch.
- **IEX live data and SIP backtest data differ.** IEX is a few percent of consolidated volume, so
  volume-based features differ in level between live and replay; ratios within a session travel
  better. Paper fills are optimistic too, which is why every judgement applies a slippage haircut.

## Custom features

Strategist-written feature code never runs inside the runner. A bubblewrap worker runs it with no
network, no secrets, no runtime access and memory limits, from a runner-owned copy of the files.
If the worker fails or hangs, custom features return NaN and dependent entries are blocked. The
runner never follows symlinks in the strategist's `state/` or `features/custom/`. The static check
in `src/trader/features/harness.py` is only a first filter.
