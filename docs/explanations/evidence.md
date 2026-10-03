# Evidence and promotion

At a few trades a day, one idea will look dominant for weeks before the difference means
anything. So the system is careful about what counts as evidence, and it decides the big step,
real money, from its own logs with thresholds the strategist can't change.

## What counts

| Source | Counts towards the go-live gate | Counts towards promotion | On the scoreboard |
|---|---|---|---|
| Paper trades by the strategist's classifiers | yes | yes, on the classifier's current spec | yes |
| `control_*` trades | no | no (it's the benchmark) | yes, as "control" |
| Sim trades (`mode: sim`) | no | no | separate "sim" board |
| Probe decisions (`mode: probe`) | no | no | no (Probes page) |
| Replays (backtests) | no | no | per replay |
| Paper trades before `experiment.start_date` | no | only if on the current spec | hidden unless asked |
| Round trips logged `(price estimated)` | no | no | no |

All judgements are made after a slippage haircut of 0.05% a side (0.1% a round trip) on broker
fills, because paper fills are optimistic. Sim and replay fills build slippage in instead (see
[Sim accounts](#sim-accounts)), so no extra haircut is charged on them.

## The scoreboard

The dashboard's scoreboard (`src/trader/scoreboard.py`) shows, per classifier and pooled per
**family**, closed trades, win rate, the mean net return per trade with a 95% interval, total
dollars and promotion progress. Its race chart plots cumulative net P&L, and it compares novel
against conventional and novel against the control.

Each non-control classifier carries `family: novel` (the strategist's own hypothesis from what it
observed, not a textbook indicator strategy) or `family: conventional` (anything else). It's an
honest label, not behaviour: it doesn't change how the classifier trades or restart its record.
The scoreboard exists so the human can see whether the strategist's own ideas beat textbook ones
and the control.

Trades on the same day share that day's tape, so they aren't independent. The intervals are
clustered by trading day: evidence grows with the number of distinct days traded, not just the
trade count. The verdicts are deliberately cautious:

- *too few to judge* under 10 trades or 3 trading days;
- *ahead* or *losing* only when the 95% interval clears zero;
- otherwise *can't tell from luck yet*.

## The go-live gate

The runner evaluates the gate after every session from the paper book's own `trades.csv` and
`equity.csv` in the runtime directory (`src/trader/golive.py`). It passes when all of these hold:

- at least **10 trading days** since the experiment start (a day counts once it has an equity mark
  after its opening row);
- at least **20 closed paper trades** by the strategist's classifiers (not `control_*`);
- **positive expectancy** (mean return per trade) after slippage;
- **no day down 5%** or more;
- the paper book **not halted**;

and, at the switch itself, the live account funded with at least **$100**.

The gate is a safety floor, not a target. Twenty trades prove very little; the strategist's
charter forbids adding classifiers or loosening triggers to reach it sooner, and if no edge shows
up, the right outcome is that the gate never passes.

### Arming, the veto window and going live

```{mermaid}
stateDiagram-v2
    [*] --> pending
    pending --> armed: gate passes after a session
    armed --> armed: gate still passes,<br/>one veto session used
    armed --> pending: gate fails or logs unreadable<br/>(disarm)
    armed --> live: 3 sessions done, final check passes,<br/>live equity >= $100
    live --> demoted: live book halts
    pending --> vetoed: HOLD LIVE
    armed --> vetoed: HOLD LIVE
    live --> vetoed: HOLD LIVE
    vetoed --> pending: trader release-live
    demoted --> pending: trader release-live
```

The diagram shows the common paths only: `trader release-live` resets to pending from any state,
and HOLD LIVE works from any state too, including demoted and corrupt.

- When the gate passes, go-live is **armed** and the human gets an alert and **3 paper sessions
  to veto** it.
- The gate is re-checked after every veto-window session and again just before the switch. If it
  no longer passes (a losing session drags expectancy below zero, a −5% day, a paper halt) or the
  logs can't be read, it **disarms** back to pending with an alert. A later re-pass starts a fresh
  3-session window.
- If the live account holds less than $100 when the switch is due, the runner stays on paper and
  alerts until it's funded.
- The account's first 5 live sessions ever trade at **half size**. The count lives in the live
  book's `risk.json` and isn't reset by a demotion, so going live again later is full size.
- A HOLD always wins, even one pressed while the runner is re-checking the gate: every automatic
  write to `golive.json` is a compare-and-swap under a lock.
- An unreadable or invalid `golive.json` reads as a non-live `corrupt` state: paper, one alert,
  and no automatic transition over it until a human HOLDs or releases.
- `config/mode.yaml` set to `paper` or `live` overrides all of this.

`trader golive` prints the current state and the gate's numbers.

## Promotion to `mode: live`

Once the account is live, `shadow` classifiers still trade paper; only `mode: live` ones trade the
live account. A classifier may run live only with at least **20 closed paper trades and positive
expectancy after slippage, on its current spec**. The runner enforces this at each session start:
a `mode: live` classifier that doesn't qualify is run in shadow instead, with an urgent alert
when the account is live.

"Current spec" is a hash of everything in the spec except `mode`, `enabled` and `family`, plus,
if it uses any custom feature, a digest of all the custom feature code. Changing a threshold, a
criterion, a feature list or an execution field restarts the record from that day; switching
`mode` doesn't. The records live in `runtime/promotion.json`.

## Probes

A probe (`mode: probe`) asks its entry question at its cadence whenever its trigger holds, and
logs P(ENTER) with the price, without ever ordering. Because it never trades, it can watch many
symbols at a short cadence: thousands of scored predictions a week, against the handful of trades
a traded classifier makes.

`trader probe-report` joins those rows to later SIP minute bars and asks two questions per probe
and horizon (15, 30 and 60 minutes by default, cut at the 15:45 flatten):

1. **Does P(ENTER) rank later returns?** The information coefficient (IC) is the Spearman rank
   correlation between P(ENTER) and the forward return, computed within each day and averaged
   across days. The day is the unit of evidence, and no t-statistic is shown under 5 days.
2. **Does Jev add anything over its own inputs?** A ridge regression on everything Jev was shown
   (the features, minutes since open and the last ten 1-minute returns) is fitted walk-forward
   (on earlier days only, scored on the next), with and without P(ENTER) as an extra input. If
   adding Jev doesn't lift the out-of-sample IC, its inputs carry whatever signal there is, and a
   plain trigger would do. A lift is weaker evidence: beating a linear model is a low bar.

After each close the post-close wrapper writes the last 30 days' report to
`logs/probe_report.json`, and the dashboard's Probes page shows it. Probe results count towards
nothing; they tell the strategist which questions deserve a traded classifier.

## Sim accounts

All shadow classifiers share one paper account: one position per symbol, a limited amount of
settled cash, and the first classifier to trigger takes the slot. A `mode: sim` classifier
instead trades its own simulated account (`capital.sim_cash`, $250 as shipped), fed by the same
live bars, with the whole execution toolkit, the guardrails, the kill switch and the halt. Cash
carries over between days.

Fills are simulated (`src/trader/broker.py`):

- market entries, model exits, targets, scale-outs and time stops at the bar close, ± 0.05%,
  as on paper (a target is a market sell once the touch is seen, not a resting order);
- stops at their level (or the bar's open, if it gapped through), less 0.05%;
- limit entries only when a later bar trades strictly below the limit, at the limit (or the open,
  if lower), with no slippage.

That flatters a strategy a little, more so on thin names. Sim trades count towards nothing; to
promote an idea it moves to `mode: shadow`, and only its paper trades count from then on.

## Backtests

`trader replay` runs classifiers through the real engine on historical SIP bars with a simulated
broker. Every `live`, `shadow` and `sim` spec in a replay trades the same single simulated account;
probes stay probes, and only log their answers. Backtests never qualify
anything: only forward paper results count.

Replay fills follow the sim rules above with one difference. The engine decides on a completed
bar, so a market order (an entry, a model exit, a target or scale-out, a time stop, a flatten or a
kill) executes at the **next bar's open**, ± 0.05%, not at the close it has just seen: a gap after
the signal bar moves the price. Paper and sim accounts fill at the close instead, because the live
order goes out seconds after it. Stops and limit entries are resting orders, so they fill where a
bar traded through them, as in sim. No latency beyond that one bar is modelled. The strategist's charter adds research hygiene on
top: tune on older sessions and confirm on recent ones it didn't look at, log every variant it
replays, and treat anything under about 30 trades as a hypothesis, not evidence.
