# Deploy a change

Strategy changes (`state/`, `features/custom/`) need **no deploy**: the runner reads them from the
strategist checkout at each session start.

Code, universe, mode and `config.yaml` changes on `main` reach the runner only when you run, from
your admin account:

```bash
sudo -u runner trading-deploy
```

`sudo -u runner trading-deploy --dry-run` shows what would happen without changing anything.

## What it checks

Review happens in the pull request, so the deploy's job is to confirm that what it's deploying is
what was reviewed:

- When every commit since the last deploy is a pull-request merge made by GitHub (a two-parent
  merge commit, signed with GitHub's web-flow key), it lists those pull requests, runs the tests on
  the new commit, and deploys.
- Anything else needs its diff reviewed and a typed `yes`: a direct push, a squash or rebase
  merge, or rewritten history.
- It always refuses a diff containing terminal control characters, invisible or bidirectional
  Unicode characters, or binary files, since those can hide code from a reviewer.
- It always runs the test suite on the candidate before switching to it.

The signature check runs from the currently deployed code and its pinned copy of GitHub's key
(`deploy/github-web-flow.gpg`), never from the commit being deployed.

**Check the listed pull requests are ones you merged.** The strategist's token could open and
merge a pull request itself; that's a genuine GitHub merge, so only the list shows it. Each line
shows the merge commit, and `gh pr view N --json mergeCommit -q .mergeCommit.oid` confirms it
belongs to pull request N (the number and title alone come from the commit message). If a merge
you didn't expect is listed, press Ctrl-C before the tests finish.

## Deploy outside the session

The runner starts at `schedule.runner_start` on weekdays, waits for the open and exits after the
US close. While it runs it holds `/srv/trading/runtime/session.lock`, and a deploy never replaces
its code:

- **During a session**, `trading-deploy` stops straight away with
  `REFUSING: a trading session is running (trader-runner is active); nothing was changed.`
  Deploy after the runner has exited, or before the next weekday's start.
- **Just before the runner starts**, the review and the tests can overrun the start. The session
  then wins: the deploy refuses at the switch, says the tests passed and to deploy that commit
  again after the session. Nothing is changed, and the runner trades on the code it started with.
- The lock is taken only for the switch itself (checkout, `uv sync`, unit files), never during the
  review or the tests. If the runner's timer fires during a switch, the runner waits for it (at
  most 10 minutes) and starts on the new code.
- A crashed runner doesn't leave a stale lock: the kernel releases it with the process. In the 30
  seconds before systemd restarts a crashed runner, the deploy still refuses. A runner that has
  given up (`failed`) doesn't block a deploy.
- The deploy restarts the dashboard after the switch.

Check whether the runner is up with:

```bash
sudo -u runner XDG_RUNTIME_DIR=/run/user/$(id -u runner) systemctl --user status trader-runner
```

## The strategist's checkout

A deploy updates only the runner's checkout. The strategist's checkout (branch `strategist`)
merges `origin/main` and pushes the merge at the start of each strategist run that passes its
gating, and at the daily housekeeping run unless the checkout is off `strategist` or has
uncommitted changes. If anything in `deploy/systemd-trader/` changed (a schedule change, say),
reinstall the strategist's timers as `trader`:

```bash
bash /srv/trading/strategist/deploy/setup/2-strategist.sh
```

`deploy/setup/check.sh` fails until you do.
