[![CI](https://github.com/gilesknap/jev-trader/actions/workflows/ci.yml/badge.svg)](https://github.com/gilesknap/jev-trader/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# jev-trader

An autonomous day-trading experiment. A strategist (headless Claude Code) designs intraday
classifiers in plain YAML. A runner daemon executes them on an Alpaca account, asking the
**Jev** decision model (via OpenRouter) for each go/no-go, with hard guardrails enforced in code
and by OS user separation. A permanent control strategy and a scoreboard measure whether any of
it beats doing nothing.

What            | Where
:---:           | :---:
Source          | <https://github.com/gilesknap/jev-trader>
Documentation   | <https://gilesknap.github.io/jev-trader>
Releases        | <https://github.com/gilesknap/jev-trader/releases>

> **Warning: this is an experiment, not a product, and it can lose real money.** It trades a
> real brokerage account by itself once its go-live gate passes and a
> three-session veto window ends. Nothing here is financial advice, and the
> design assumes a small balance you can afford to lose. Run it on paper first, read how the
> [money safety](https://gilesknap.github.io/jev-trader/explanations/money-safety.html) works,
> and keep the live account small.

## Why it's interesting

- **It invents its own strategies.** The strategist reads its results every evening, forms
  hypotheses, backtests them and writes the next day's classifiers. It labels each idea `novel`
  or `conventional`. A scoreboard ranks those families against a fixed control strategy, after
  slippage, and won't call anything an edge until the evidence clears luck. Buy-and-hold SPY
  runs alongside on the equity chart.
- **The safety lives in code, not in prompts.** Position limits, stops, the daily kill switch,
  settled-cash accounting and the go-live gate are enforced by a daemon the strategist can't
  edit. The strategist's own feature code runs in a sandbox.
- **It runs itself, and you can still steer it.** Pre-market, post-close and weekly runs happen
  on timers. You read a weekly retrospective, veto go-live if you disagree, and steer the
  strategist by talking it through in an ordinary Claude Code session that ends in a pull
  request.

## What it costs

Very little. The strategist runs on a **$20/month Claude subscription**. The thousands of Jev
calls a day cost **under $1 a week** through OpenRouter. **Alpaca charges no commission** on US
stocks and ETFs, and its free IEX feed supplies the market data. Add a small Linux VPS and
whatever you choose to trade with.

## What it trades

The current scope is deliberately narrow: **long only, intraday** (every position is flat
before the close), on about 30 liquid US ETFs and mega-cap stocks, in a cash account. No
shorting, no margin, no options or other derivatives, no crypto. That keeps the risk easy to
reason about and the results easy to attribute. None of it is fundamental to the design,
though: the universe is a config file, and the guardrails and broker adapter are ordinary code.
A fork can widen the scope as far as its owner is comfortable with: see
[widening the trading scope](https://gilesknap.github.io/jev-trader/how-to/widen-the-scope.html),
including where the next bills come from (paid real-time market data, for one).

## How it's laid out

The code runs on one Linux host with three accounts: an admin (you), `trader` (the strategist,
paper keys only) and `runner` (the trading daemon, live keys, no sudo). This public repository
holds only code. Each owner keeps their settings and the strategist's memory in a **private data
repository** of their own, made from the template in `templates/data/`. The strategist can write
only to that data repository: its code ideas reach this one as patches a human reviews and turns
into pull requests, and nothing runs until a human deploys it.

<!-- README only content. Anything below this line won't be included in index.md -->

**Documentation:** <https://gilesknap.github.io/jev-trader/>, covering
[installation](docs/tutorials/installation.md) (setting up your own copy from scratch), [operations](docs/how-to/daily-operations.md),
[how it works](docs/explanations/architecture.md) and [reference](docs/reference/cli.md).

- [DESIGN.md](DESIGN.md): the agreed design and the reasons behind it.
- [CLAUDE.md](CLAUDE.md): the strategist's charter (the schema, rules and tools it works with).
- [CONTRIBUTING.md](CONTRIBUTING.md): issues and pull requests.

Already running an install from a single private repository? See
[migrating to the split layout](docs/how-to/migrate-to-split.md).
