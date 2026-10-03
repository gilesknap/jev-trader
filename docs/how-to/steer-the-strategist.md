# Steer the strategist

The strategist runs headless, but you can still argue with it. Open an ordinary interactive
Claude Code session, have it read the strategist's state and logs, talk the strategy through, and
end with a pull request into the `strategist` branch of your data repository. You get the full
Claude Code session (subagents, other models, backtests in scratch space), and the strategist
picks up the result at its next run with no deploy.

## How the strategist hears you

Decisions go in `state/steering.md` on the `strategist` branch. The file is yours: the strategist
reads it every run and never edits it. Each entry has an id, a date, a status, the decision and
the reasoning:

```markdown
## S1 — 2026-10-03 — No trading in the first 5 minutes (active)

**Decision.** Don't let any classifier enter before 09:35 ET. ...

**Why.** ...
```

An `active` entry is binding until you retire it (change its status to `retired`, and say why).
The strategist applies a new entry to the rest of `state/` in the first run that sees it, and
acknowledges it by id in that run's journal. If it disagrees, it says so in the journal, or in a
`needs-human` issue if it matters, and keeps following the entry meanwhile.

Write the reasoning, not just the rule. The strategist reads it, and a rule without a reason tends
to come back in another form a few weeks later.

Comments on the weekly issue don't reach the strategist: it doesn't read them. Steer through this
file.

## Run a steering session

1. Start Claude Code anywhere you can read `/srv/trading/strategist`, and ask it to read the
   charter (`/srv/trading/main/CLAUDE.md`), `state/`, the recent journals and
   `logs/probe_report.json`. The dashboard shows the same results.
2. Discuss. Ask why a classifier exists, what the evidence for a belief is, what a replay of your
   own idea shows.
3. Have it write the outcome into `state/steering.md` in a clone of the `strategist` branch
   (`/srv/trading/strategist` belongs to `trader`; don't edit that checkout), and open a pull
   request into `strategist`.
4. Merge it at a safe time (below), with **Create a merge commit**.

By default a steering pull request changes **only** `steering.md`, and the strategist applies the
decision itself. That keeps it the single author of its strategy, so its journal explains every
change, and it avoids conflicts with its own edits. Edit `classifiers.yaml` or other `state/`
files directly only when it can't wait (see [In an emergency](#in-an-emergency)). If you do, run
`trader validate` on the result, and remember that changing any classifier spec field other than
`mode`, `enabled` or `family` restarts that classifier's record (see the
[classifier schema](../reference/classifier-schema.md)).

## When to merge

The wrapper pulls `origin/strategist` at the start of every strategist run, so a merge reaches the
strategist at its next run. The runner reads `state/` only at each session start, so a merge never
changes the current day's trading by itself.

- **Never merge while a strategist run is active.** If your merge touches lines the run is
  editing, the wrapper can't rebase the run's work onto it, and that run isn't published (it
  alerts). Each run holds the strategist lock for its whole length, so check that it's free:

  ```bash
  sudo -u trader flock -n ~trader/.local/state/trader/strategist.lock true \
      && echo "no strategist run" || echo "a strategist run is active: wait"
  ```

- **Best: from the close until the post-close run** (the run starts at 21:30 UK as
  shipped). The post-close
  run is the long one, so the strategist has a full run to absorb the decision and draft the next
  day around it. After the post-close run also works, but then only the short pre-market run
  reacts before the next open.

## In an emergency

If a classifier is doing something plainly wrong during the session, don't wait for the
strategist:

1. In a steering pull request, set that classifier's `enabled: false` (or `mode: shadow` if it's
   live), and add a steering entry saying why. Merge it (still not during a strategist run).
2. Bring the merge into the host's checkout. Only strategist runs pull it otherwise, and the
   runner reads the checkout, not GitHub. Holding the lock means this can't collide with a run:

   ```bash
   sudo -u trader flock -n ~trader/.local/state/trader/strategist.lock \
       git -C /srv/trading/strategist pull -q --ff-only origin strategist \
       && echo pulled || echo "NOT pulled: a run or deploy holds the lock, or the pull failed"
   ```

   If the pull refuses as not a fast-forward, the checkout holds a strategist run's unpublished
   commit (the wrapper will have alerted): resolve that first.

3. Restart the runner so it rereads `classifiers.yaml`. It resumes from its saved state
   (see [Daily operations](daily-operations.md#applying-a-classifier-change-mid-session)).

Use STOP from the dashboard (see [Use the controls](controls.md)) when you need everything flat
straight away.
