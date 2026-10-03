# Run the pre-launch dry run

Before the experiment starts, push real orders through every execution path on paper, so a bug
shows up while nothing counts. The pack is `templates/data/strategist/state/classifiers.yaml`:
`control_orb`, five `test_*` classifiers and a universe-wide baseline probe that never orders.

The `test_*` classifiers aren't strategies. Each deliberately enters on its own symbol (AAPL,
MSFT, XLF, XLI or XLV) so that every path runs: Jev entry and exit calls, market and limit
orders, the time stop, the target, scale-out, the trailing stop, limit expiry and the end-of-day
flatten. The go-live gate and the live scoreboard ignore trades before
`experiment.start_date`, so none of this counts towards anything.

## Steps

1. Set `experiment.start_date` in `config.yaml` to 2–3 trading days after the first pre-launch
   session, then render, merge and deploy it (see [Deploy a change](deploy.md)).
2. As `trader`, on the `strategist` branch, copy the pack into place, set its `date:` to the next
   session, validate and push:

   ```bash
   sudo -iu trader
   cd /srv/trading/strategist && git pull
   cp templates/data/strategist/state/classifiers.yaml state/classifiers.yaml
   sed -i 's/^date: .*/date: YYYY-MM-DD/' state/classifiers.yaml   # the next session's date
   uv run trader validate                                          # must end "classifiers OK"
   git commit -am "Pre-launch plumbing pack" && git push
   ```

3. Let it run for a session or two, then check each path in
   `/srv/trading/runtime/books/paper/trades.csv` (the `reason` column), or the dashboard's Trades
   and Rules pages. The comments in the pack say what each classifier should produce.
4. Before the start date, put `state/classifiers.yaml` back to `control_orb` plus the probe.

## Checking the exit path

Confirm that the Jev-exit test (`test_jev_exit`, on MSFT) actually records a `sell` with
`reason == "classifier EXIT"`. A `time stop`, stop, target or end-of-day flatten doesn't show that
the classifier-exit path ran. Its exit threshold of 0.5 makes Jev's typical scores for an
"always" criterion usable, but doesn't guarantee the path was exercised.

Some paths depend on the market: on a quiet day AAPL may never reach its target. Give it another
day before suspecting a bug.

## Why the tests must stop at launch

Every shadow classifier shares one paper account, and the go-live gate judges that account's days:
a plumbing rule's trades and drawdowns would crowd out and distort the real experiment. So from the
start date (by the exchange's session date) the runner drops every `test_*` classifier itself, with
an urgent alert, before promotion or engine creation, and `trader validate` rejects `test_` ids.
Remove them anyway.

## Any day, for free

```bash
uv run trader replay --decider stub --days 2
```

runs the classifiers through the real engine on historical bars with a stub decision model: no
orders, no Jev cost. It's the quick plumbing check after any change, but it simulates fills, so
only paper proves the broker side.
