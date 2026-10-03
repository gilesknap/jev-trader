# Your first replay

This tutorial runs the trading engine on your own Linux machine, on real recent market data,
without any accounts or keys and without placing an order. You'll replay the shipped control
classifier through the same engine the runner uses, look at its trades, and watch a session on
the dashboard. It takes about ten minutes.

It uses two stand-ins, so it costs nothing:

- **yfinance** for the last few days of 1-minute bars, instead of Alpaca's historical data;
- **the stub decider** instead of Jev: it answers ENTER whenever the classifier's trigger holds,
  and EXIT once a position is down more than 0.3%, otherwise HOLD.

So the results say nothing about any strategy. The point is to see the machinery work.

## Before you start

You need git and [uv](https://docs.astral.sh/uv/getting-started/installation/). uv installs the
right Python by itself.

## 1. Get the code

```bash
git clone https://github.com/gilesknap/jev-trader.git
cd jev-trader
uv sync --extra dev
```

The `dev` extra adds pytest and yfinance.

## 2. Make a data directory

The code repository holds no one's settings or strategy: those live in each owner's private data
repository. For this tutorial, a scratch directory made from the code's template will do. Copy
both halves of the template into it, and point `trader` at it:

```bash
mkdir ../my-data
cp -r templates/data/main/. templates/data/strategist/. ../my-data/
export TRADER_DATA_ROOT="$(realpath ../my-data)"
```

`templates/data/main/` is what a data repository's `main` branch starts with (`config.yaml` and
`config/mode.yaml`); `templates/data/strategist/` is what its `strategist` branch starts with
(`state/`, `journal/`, `logs/`, `features/custom/`). In a real install they are two checkouts; here
one directory serves as both, because the strategist root defaults to the data root. Keep the
`export` in this terminal (and repeat it in any other you use below): without it, every `trader`
command stops, saying it can't find `config.yaml`.

## 3. Look at the control classifier

Open `../my-data/state/classifiers.yaml`. It's the pre-launch pack a new install starts with: some
`test_*` plumbing rules, a probe, and `control_orb`, the permanent benchmark, an opening-range
breakout on SPY and QQQ. Find `control_orb` and read it top to bottom:

- `window` and `cadence_min`: it looks between 09:45 and 15:00 US Eastern, at most every 2 minutes
  per symbol.
- `trigger`: it only asks the model when the price is above both the first 15 minutes' high and
  VWAP. This is plain arithmetic on features; no model call.
- `entry` and `exit`: the questions, with one description per possible answer.
- `size_fraction`, `stop_pct`, `target_pct`: 20% of equity per position, a 0.4% stop and a 0.8%
  target.

## 4. Validate it

```bash
uv run trader validate --source yfinance
```

This checks any custom features (there are none yet) and validates the file. It ends with
`classifiers OK: [...]`, listing every classifier in the file. This is the check the runner repeats
at every session start.

## 5. Replay a few days

```bash
uv run trader replay --decider stub --source yfinance --days 3 --only control_orb --name first
```

The engine replays each session minute by minute: for each minute it enforces stops and targets,
runs the risk checks, asks the (stub) decider when the trigger holds, and sends orders to a
simulated broker with 0.05% slippage a side. A market order fills at the next minute's open, after
the bar the decision saw. It prints a summary per day (equity, the day's P&L,
trades, NAV) and the run directory, `runtime/replay/first/` in the code checkout.

Look inside it:

```bash
cat runtime/replay/first/sim/trades.csv
```

Each `buy` row has the reason `ENTER`; each `sell` row says what closed the position (`stop`,
`target`, `classifier EXIT`, `eod flatten`) and carries the round trip's P&L. Nothing is held
overnight: anything still open 15 minutes before the close is sold.

`runtime/replay/first/decisions/` has one line per question asked, with the probabilities the
decider returned and the feature values it was shown.

## 6. Watch it on the dashboard

Start the dashboard locally. `TRADER_DASHBOARD_ALLOW_LOCAL=1` lets requests in without a Tailscale
identity, so use it only on your own machine:

```bash
TRADER_DASHBOARD_ALLOW_LOCAL=1 uv run trader dashboard
```

Open <http://127.0.0.1:8321> and pick the `first` replay as the source. You'll see the equity
curve against buy-and-hold SPY, the trades, the scoreboard and the classifier's rules in plain
words.

To watch a session unfold, run a replay with a pace (seconds per simulated minute) in another
terminal, and pick it on the dashboard while it runs:

```bash
uv run trader replay --decider stub --source yfinance --days 1 --only control_orb --pace 0.2 --name watch
```

## 7. Change something

Edit `control_orb` in `../my-data/state/classifiers.yaml`: say, raise `target_pct` to `1.2`. Validate it, and
replay the same days under a new name:

```bash
uv run trader validate --source yfinance
uv run trader replay --decider stub --source yfinance --days 3 --only control_orb --name first-wider
```

Compare the two runs on the dashboard. With the stub decider and a few days of data, any
difference is noise; with Jev and more days it's still mostly noise until there are dozens of
trades. Keeping that in mind is most of what [Evidence and promotion](../explanations/evidence.md)
is about. Put the control back as it was when you're done: it's the benchmark, and only bug fixes
change it.

## Where next

- [How a decision is made](../explanations/decisions.md) explains what the engine did in each
  minute.
- [Write and test a classifier](../how-to/write-a-classifier.md) is the same loop with Jev and
  Alpaca's data.
- [Install your own copy](installation.md) sets up the full system on a server.
