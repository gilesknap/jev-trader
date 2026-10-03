# Related work: LLM-driven strategy discovery

A short survey (September 2026) of who else lets a model pick trading strategies and test them quickly, and what this project can learn from them. It is based on web search and paper summaries, not a full read of every paper.

## Three camps

**1. A human sets the strategy; the model makes each call.**
- [beebots](https://beebots.tech/) ([code](https://github.com/imikerussell/beebots)): three "bees" trade crypto perpetuals on OKX. Like this project, every decision comes from Jev, and a plain-code risk layer sits underneath.
  - A human writes a one-sentence trading intent, and OpenAI turns it into rules within one of three fixed styles (Breakout, Trend, Momentum). The rules "can't invent moves the style doesn't have."
  - Nothing sits above that layer to propose, test or retire strategies. The leaderboard shows % gain or loss.
- [Alpha Arena (Nof1)](https://nof1.ai/): frontier models got $10k each to trade from a shared prompt: crypto perpetuals on Hyperliquid in Season 1 (18 Oct–3 Nov 2025), then US equities in Season 1.5 (ended 3 Dec 2025). It's a benchmark of models, not a research loop, and a two-week ranking is mostly luck.

**2. A model searches for alpha offline (academic).**
- Examples: [AlphaAgent](https://arxiv.org/html/2502.16789v2), [QuantaAlpha](https://arxiv.org/pdf/2602.07085), [FaVOR](https://arxiv.org/pdf/2608.30192), [AgonAlpha](https://arxiv.org/pdf/2608.11250).
- The closest in spirit is [AQuA](https://arxiv.org/html/2608.12841). It proposes hypotheses with a stated mechanism and falsification criteria written before testing, keeps a persistent belief memory, and evaluates against a sealed held-out test set.
- All of these are backtest-only. AQuA notes that its test isolation rests on "operator discipline rather than a hard technical barrier."

**3. The sceptics.**
- [*What survives honest evaluation?*](https://arxiv.org/html/2608.27734): GPT-4.1 and Claude Sonnet 5 searched for strategies using a look-ahead-proof toolset, with every evaluation recorded. Results were then discounted by the true trial count.
- **Of 100 proposed strategies (99 valid), none survived.** Their summary: "autonomy inflates the trial count."
- The [agentic trading survey](https://arxiv.org/html/2605.19337v1) adds that most papers under-report how much searching they did, their costs and their leakage controls.
- Caveat: the sceptics' paper used daily and multi-asset data, not intraday. The lesson carries over; the numbers may not.

## Where this project sits

What's unusual here:
- **Forward testing:** the model invents its own hypotheses, and they are forward-tested in shadow on the live market. Only forward results count toward promotion and the go-live gate. Papers stop at backtests, and live arenas skip the research step.
- **Novel vs conventional:** the `family: novel` scoreboard asks directly whether the agent's own ideas beat textbook ones and the `control_orb` baseline.
- **Testing the decision model itself:** probes, and the "does Jev beat a linear model on its own inputs?" check, test Jev rather than a strategy.

The weakness the sceptics name also applies here: the count of variants tried is self-reported in the journal's replay log. [#112](https://github.com/gilesknap/trading/issues/112) proposes a runner-kept, append-only trial ledger to make that count structural.
