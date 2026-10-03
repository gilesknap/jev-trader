# Guardrails and limits

Every number here is a constant in code on `main`. The strategist can't change any of them;
changing one needs a reviewed, merged and deployed pull request. [Money safety](../explanations/money-safety.md)
explains how they fit together.

## Per order and per position (`src/trader/guardrails.py`)

| Constant | Value | Rule |
|---|---|---|
| `MAX_POSITION_FRACTION` | 0.25 | At most 25% of current equity per position |
| `MIN_PRICE` | 5.0 | No entries under $5 |
| `MAX_STOP_DISTANCE` | 0.10 | A stop at most 10% below the entry |
| `MIN_NOTIONAL` | 1.0 | No order under $1 (Alpaca's fractional minimum) |
| `FLATTEN_MINUTES_BEFORE_CLOSE` | 15 | No entries, and everything flattened, from 15 minutes before the close |
| `DAILY_LOSS_KILL` | 0.05 | Equity −5% from the day's start: flatten, no entries until the next session |
| `TRAILING_HALT` | 0.30 | NAV per unit −30% from its high-water mark: halt until a human clears it |
| `EQUITY_FLOOR` | 50.0 | Equity under $50: halt until a human clears it |

Also enforced on every entry: long only, a symbol from `config/universe.yaml`, one position per
symbol, within settled cash, a stop below the entry and a target above it.

## Per book (`src/trader/allocator.py`)

| Constant | Value | Rule |
|---|---|---|
| `OPEN_RISK_FRACTION` | 0.03 | Planned stop losses of everything held or pending, plus today's realised loss, within 3% of the smaller of current and day-start equity |
| `EQUITY_CAP` | 0.75 | Stocks and equity ETFs at most 75% of equity (TLT and GLD are outside) |
| `BUCKET_CAP` | 0.50 | Each overlapping bucket at most 50% of equity |
| `MIN_TRIM_USD`, `MIN_TRIM_FRACTION` | 10.0, 0.25 | A shrunk entry is skipped if it keeps less than max($10, 25% of the request) |

The buckets:

| Bucket | Symbols |
|---|---|
| growth | QQQ, XLK, SMH, XLY, AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, AMD, AVGO, NFLX, PLTR |
| financials | XLF, JPM, BAC |
| health | XLV, LLY, UNH |
| energy | XLE, XOM |
| every bucket | SPY, DIA, IWM |
| none (non-equity) | TLT, GLD |
| none | XLI, COST |

## Go-live and promotion (`src/trader/golive.py`)

| Constant | Value | Rule |
|---|---|---|
| `MIN_TRADING_DAYS` | 10 | Trading days since `experiment.start_date` before the gate can pass |
| `MIN_TRADES` | 20 | Closed paper trades by non-control classifiers (gate); closed paper trades on the current spec (promotion to `mode: live`) |
| `SLIPPAGE_PER_SIDE_PCT` | 0.05 | Haircut per side when judging expectancy |
| `VETO_SESSIONS` | 3 | Paper sessions between arming and going live |
| `MIN_LIVE_EQUITY` | 100.0 | Live equity needed at the switch |

The account's first 5 live sessions ever trade at half size (`src/trader/engine.py`); the count is in the live book's `risk.json` and isn't reset by a demotion.

## Runtime limits

| Limit | Value | Where |
|---|---|---|
| Symbols on the live stream | 30 | `STREAM_SYMBOL_LIMIT`, `src/trader/runner.py` |
| Stale feed | no SPY bar for over 3 minutes | `stale_feed`, `src/trader/engine.py` |
| Decision-call budget per tick | 35 s (probes: 20 s) | `src/trader/engine.py` |
| Pause after a failed decision call | 5 minutes | `DECISION_COOLDOWN`, `src/trader/engine.py` |
| Jev request timeout | 5 s per stage (connect, write, read), one retry | `src/trader/jev.py` |
| Custom-feature gate | finite on ≥80% of bars after 30 min; under 5 ms per call | `src/trader/features/harness.py` |
| Strategist run time | 25 min pre-market, 50 min otherwise | `scripts/strategist.sh` |
