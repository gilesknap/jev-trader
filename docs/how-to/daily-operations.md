# Daily operations

What happens on a normal day, and what's yours to do. Local times are from `config.yaml`
(`schedule`); the shipped values are for a UK operator, shown here.

| When (UK, as shipped) | What |
|---|---|
| 09:00 daily | Housekeeping checks: OpenRouter credit, token expiry, undeployed merges in either repository, disk (see [Alerts](alerts.md)) |
| 12:50 Mon–Fri | The runner starts, checks Alpaca's calendar and waits for the open |
| 30–75 min before the open | The strategist's pre-market run (short) |
| open–close | The runner trades. The watchdog checks its heartbeat every 10 minutes. Any position the engine didn't open (left from a crash, or **bought by hand**) is sold within about a minute of the first tick after the open, with an urgent alert |
| close − 15 min | Everything is flattened, retried every minute until the close. Nothing is sent after the bell; anything still held is alerted, to close by hand in Alpaca |
| close | The runner writes the day summary and sends the **Daily P&L** alert |
| 21:30 (retry 22:30) | The strategist's post-close review: archive logs, research, rewrite the strategy, draft tomorrow |
| Sat 10:00 | The weekly retrospective, compaction, and the weekly issue in your data repository |

## Your weekly job

- Read the weekly issue in your data repository (label `weekly`, titled `Week <YYYY>-W<WW>: ...`;
  the strategist closes the previous week's). Its body is the weekly journal, including any
  go-live assessment (flagged in the title), and ends with a link to the week's diff of the
  `strategist` branch, if you want to see exactly what changed in `state/`. Nothing needs
  merging: the runner already reads the strategist's branch. The strategist doesn't read comments
  on the issue: to change its course, [steer it](steer-the-strategist.md).
- Handle `needs-human` issues in your data repository: the strategist's requests, and its code
  proposals (see [Review the strategist's proposals](proposals.md)).
- Deploy merged changes outside market hours (see [Deploy a change](deploy.md)), and take code
  updates when you choose to (see [Take updates](take-updates.md)).

The dashboard's links include all of these. Merge pull requests in GitHub's web UI with **Create
a merge commit**: GitHub signs those, and the deploy uses the signature to skip the diff review for
code. (Config changes always show their diff.)

## Where to look when something's wrong

- The dashboard: its banner says whether the runner is trading, and the At a glance panel shows
  `decision_errors` and `probe_errors`. A few Jev timeouts a day are harmless (each pauses decisions
  for 5 minutes); a steady stream means a key or credit problem.
- `/srv/trading/runtime/alerts.log`: every alert from the runner, the watchdog and the dashboard.
- `/srv/trading/strategist/strategist-alerts.log`: the strategist wrapper's and housekeeping's
  alerts.
- `journalctl --user` as `runner` (the runner and dashboard services), and as `trader`
  (`journalctl --user -u 'trader-strategist@*'`).
- `~trader/.local/state/trader/`: one log per strategist run, kept 14 days.

## Applying a classifier change mid-session

Classifier changes take effect at the next session start. To apply one mid-session, restart the
runner; it resumes from its saved state:

```bash
sudo -u runner XDG_RUNTIME_DIR=/run/user/$(id -u runner) systemctl --user restart trader-runner
```
