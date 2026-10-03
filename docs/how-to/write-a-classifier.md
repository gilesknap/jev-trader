# Write and test a classifier

In normal running the strategist writes the classifiers. This is the same loop by hand, which is
useful for trying the system out, for checking a change, or for understanding what the strategist
does. The fields are in the [classifier schema](../reference/classifier-schema.md).

## 1. Write the spec

Add an entry to `state/classifiers.yaml`. Start from `control_orb`, the shipped control, and
change the parts that express your idea:

- `symbols`, `window` and `cadence_min`: where and how often to look.
- `trigger`: cheap, deterministic conditions that must hold before the model is asked. A tight
  trigger saves calls and cuts noise.
- `features`: what the model is shown. `uv run trader features` lists them.
- `context`, `entry` and `exit`: the question, and one crisp, observable description per answer.
  The model never sees dates or prices.
- Sizing and exits: `size_fraction`, `stop_pct`, `target_pct`, and optionally the execution
  toolkit (`trail_pct`, `max_hold_min`, `scale_out`, limit entries, `risk_pct`, `stop_atr_mult`).

Pick a mode:

- `probe` to measure whether the question carries information, without trading;
- `sim` to trade it on its own simulated account, competing with nothing;
- `shadow` to trade it on the paper account, where its trades count towards promotion.

Give it an honest `family`: `novel` only for your own hypothesis, not a textbook indicator
strategy or a close variant of one.

## 2. Validate

```bash
uv run trader validate
```

It gates any custom features and validates the file; it must end `classifiers OK`. Run it after
every edit: an invalid file means nothing trades that day.

## 3. Replay

```bash
uv run trader replay --days 10 --only my_idea --name my-idea-v1
```

This runs the classifier through the real engine on historical SIP bars, asking Jev (about
$0.00002 a call). Add `--decider stub` for a free plumbing check. Results go to
`my-idea-v1/` in the replay directory (`replays/` in the strategist checkout, `runtime/replay/` in a
development checkout): `summary.json`, and `sim/trades.csv`. The weekly compaction deletes runs
older than 14 days unless their name starts with `keep-`. Use `--file` to replay an alternative spec file, and `--start` and
`--end` for fixed dates.

Compare with `control_orb` on the same days (`--only my_idea,control_orb`). Jev isn't perfectly
deterministic, so replay more than once before trusting a small difference.

Be honest with the result: tune on older sessions and confirm on recent ones you didn't look at;
count every variant you try; and treat anything under about 30 trades as a hypothesis, not
evidence. Backtests never qualify a classifier for anything.

## 4. Run it forward

Push the change to the `strategist` branch (or, in development, just leave it in place); the
runner picks it up at the next session start. Then watch:

- the dashboard's Rules page (each classifier's state and last answer) and Scoreboard;
- for a probe, `uv run trader probe-report` (or `logs/probe_report.json` after a post-close run),
  which scores P(ENTER) against later returns and against a linear baseline on the same inputs.

To promote an idea from sim to paper, change its `mode` to `shadow`: changing only `mode` doesn't
restart its record, but sim trades never count, so its paper record starts from its first paper
trade.
