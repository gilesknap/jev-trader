# A day in the life

This page follows one trading day from the runner's start to the strategist's review. Local
times come from `config.yaml` (`schedule`); the values shipped are for a UK operator, and every
market-relative step reads Alpaca's calendar, so holidays, half-days and the weeks when the UK
and US clocks change on different dates are handled in code.

```{mermaid}
sequenceDiagram
    participant TT as trader timers
    participant S as strategist
    participant R as runner
    participant A as Alpaca
    participant J as Jev
    R->>A: calendar: is today a session?
    TT->>S: pre-market run (30-75 min before the open)
    S->>S: confirm, tweak or stand down today's classifiers
    R->>R: 2 min before the open: load and validate classifiers
    loop every minute until the close
        A-->>R: IEX minute bars
        R->>R: stops, targets, risk checks
        R->>J: entry / exit questions (when due)
        R->>A: orders (through the guardrails)
    end
    R->>A: 15 min before the close: flatten everything
    R->>R: end of day: equity, go-live gate, Daily P&L alert
    TT->>S: post-close run (after the close)
    S->>S: archive logs, review, research, draft tomorrow
```

## Before the open

**The runner starts** at `schedule.runner_start` on weekdays (`trader-runner.timer`). It asks
Alpaca's calendar whether today is a session. If not, it exits. If the calendar can't be read,
it retries briefly; a same-day restart may reuse the session times it saved earlier in
`runtime/session.json`, but it never guesses times or decides "no session" without the calendar.
Otherwise it alerts (at most hourly) and exits, and systemd's restart is the retry.

It then resolves today's account mode: `config/mode.yaml` in the deployed config (a human
override), else the go-live
state. If go-live is due today, this is where the final gate check and the switch happen (see
[Evidence and promotion](evidence.md)). In live mode it also records any new deposits or
withdrawals on the live account.

**The pre-market strategist run** fires every 15 minutes in its window, but the wrapper runs it
only once, 30 to 75 minutes before the actual open. It is short (25 minutes at most): news,
scheduled macro events, and a decision to confirm, tweak or stand down each classifier.

**Two minutes before the open** the runner loads the rules, so the pre-market run can still edit
them:

1. Custom features are gated in the sandbox on recent SPY and QQQ sessions. A rejected feature is
   alerted, and any classifier that uses it is invalid.
2. `state/classifiers.yaml` is validated. A classifier whose only fault is an unknown key is left
   out for the day with an urgent alert; any other problem rejects the whole file and nothing
   trades that day.
3. On and after the experiment start date, `test_*` plumbing classifiers are dropped.
4. Promotion is enforced: a `mode: live` classifier without a qualifying paper record runs in
   shadow instead.
5. Sim accounts are reconciled, and one book is opened per `mode: sim` classifier.
6. Each paper or live book's settled cash is read (today's buying limit), before anything sells,
   since today's sale proceeds won't settle until T+1.
7. Each paper or live book is reconciled with the broker: resting orders from earlier sessions are
   cancelled, and positions that closed while the runner was down get their exit recorded.
8. The engine starts the day: day-start equity (the kill switch's baseline) and an opening equity
   row.

## During the session

The runner subscribes to IEX minute bars for every classifier's symbols plus SPY, and for anything
already held while there's room under the free feed's 30-symbol limit (that cap applies only to
the extra held symbols; anything left out is alerted). Every minute, a few seconds after the bar
closes, it ticks the engine. One tick:

```{mermaid}
flowchart TD
    A[new minute] --> B[per book: poll resting orders]
    B --> C[enforce exits on every bar since the last check:<br/>stop, target, scale-out, trailing stop, time stop]
    C --> D[close positions the engine didn't open]
    D --> E[risk check: kill switch / halt]
    E --> F{STOP pressed?}
    F -->|yes| G[flatten the book, block for the day]
    F -->|no| H{15 min or less to the close?}
    G --> H
    H -->|yes| I[flatten everything]
    H -->|no| K[ask due classifiers their questions]
    I --> L[every 5 min: equity and SPY marks]
    K --> L
    L --> M[write status.json heartbeat]
```

- Exits are enforced on every bar since the last check, in order, so a slow tick can't jump past
  a stop.
- Classifiers are asked least-recently-asked first, trading classifiers before probes, within a
  per-tick time budget for decision calls (35 seconds; probes stop at 20), so a slow model can't
  starve exits and risk checks.
- A failed decision call pauses all decisions for 5 minutes: no new entries and no model-driven
  exits, while stops, targets and risk checks carry on.
- If SPY's last bar is more than 3 minutes old, the feed is treated as stale: no new entries.
  While it lasts, held paper and live positions get their bars over REST once a minute so their
  stops keep working.
- The watchdog (`trader watchdog`, every 10 minutes on weekdays) alerts if the runner's heartbeat
  is more than 5 minutes old during market hours.

## The close

From **15 minutes before the close**, every tick flattens every book: it cancels resting orders
and closes all positions, including ones the engine doesn't track, and retries each minute until
the close. A failing book or tick can't skip it: the runner falls back to closing everything at
the broker. Nothing is sent after the bell (a market order would sit queued for the next open);
anything still held at the close is alerted, to close by hand.

After the close, the runner:

- alerts if any book still holds a position;
- marks each book's equity and NAV, and appends to `equity.csv` (the engine's `end_day`, which
  also does the next two steps);
- records SPY's open and close in `benchmark.csv`, for the buy-and-hold comparison, and trims
  `spy_marks.csv` (SPY's price at each 5-minute mark, for the chart's intraday SPY line) to 10 days;
- compresses the day's decision log to `decisions/<date>.jsonl.gz`;
- runs the go-live state machine (arm, count down the veto window, disarm, or demote a halted
  live book);
- sends the **Daily P&L** alert, with the gate's blocking reasons while it's pending;
- prunes runtime decision logs older than 14 days.

## After the close

**The post-close strategist run** starts at least 20 minutes after the close, with one retry
scheduled later in the evening. Before Claude starts, the wrapper archives the runner's logs into
the strategist checkout's `logs/` and writes `logs/probe_report.json`. Then the strategist
reviews the day, researches (replays, custom features), rewrites `state/strategy.md`, drafts
tomorrow's `state/classifiers.yaml` and writes the daily journal, within 50 minutes. The wrapper
path-checks and pushes the result.

The watchdog also alerts once per session if no post-close run has succeeded by its deadline
(2.5 hours after the close, or `schedule.postclose_cutoff` local time, whichever is later).

## The week

- **Saturday:** the weekly retrospective. The wrapper archives logs and applies the retention
  policy (`trader compact`); the strategist writes the weekly journal and opens the weekly issue
  in the data repository, its body the journal. Reading it is the human's weekly job.
- **Daily housekeeping** (`trader housekeeping`) checks OpenRouter credit, token expiry,
  undeployed merges in either repository and disk space (see [Alerts](../how-to/alerts.md)).

## After a crash

The runner unit restarts on failure after 30 seconds and resumes from its saved state: open
positions from `entries.json`, resting and in-flight orders from `pending.json`, the day-start
equity, any kill or STOP already in force, and each classifier's trade count and retirements, so
`max_trades` and stand-downs still hold. Day-start equity is persisted, so a restart doesn't
reset the −5% baseline. A runner started after the close does nothing except check whether
anything is still held, and alert if so.
