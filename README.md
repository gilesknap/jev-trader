# jev-trader

An autonomous day-trading experiment. A strategist (headless Claude Code) designs intraday
classifiers in plain YAML. A runner daemon executes them on an Alpaca account, asking the
**Jev** decision model (via OpenRouter) for each go/no-go, with hard guardrails enforced in code
and by OS user separation. A permanent control strategy and a scoreboard measure whether any of
it beats doing nothing.

> **Warning: this is an experiment, not a product, and it can lose real money.** It trades a
> real brokerage account once its go-live gate passes. Nothing here is financial advice, and the
> design assumes a small balance you can afford to lose. Run it on paper first, read how the
> [money safety](https://gilesknap.github.io/jev-trader/explanations/money-safety.html) works,
> and keep the live account small.

The code runs on one Linux host with three accounts: an admin (you), `trader` (the strategist,
paper keys only) and `runner` (the trading daemon, live keys, no sudo). The strategist's memory is
this git repository; its changes to code reach the runner only through pull requests that a human
merges and deploys.

<!-- README only content. Anything below this line won't be included in index.md -->

**Documentation:** <https://gilesknap.github.io/jev-trader/>, covering
[installation](docs/tutorials/installation.md) (setting up your own copy from scratch), [operations](docs/how-to/daily-operations.md),
[how it works](docs/explanations/architecture.md) and [reference](docs/reference/cli.md).

- [DESIGN.md](DESIGN.md): the agreed design and the reasons behind it.
- [CLAUDE.md](CLAUDE.md): the strategist's charter (the schema, rules and tools it works with).
- [CONTRIBUTING.md](CONTRIBUTING.md): issues and pull requests.
