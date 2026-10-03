# Deploy a change

Strategy changes (`state/`, `features/custom/` on the strategist's branch) need **no deploy**: the
runner reads them from the strategist checkout at each session start.

Everything else reaches the runner only when you run, from your admin account:

```bash
sudo -u runner trading-deploy
```

That covers both repositories at once: the code (`/srv/trading/main`, from the public repository
or your fork) and your deployment config (`/srv/trading/config`, your data repository's `main`:
`config.yaml`, `config/mode.yaml` and the rendered files).

`sudo -u runner trading-deploy --dry-run` shows what would happen without changing anything.

## What it checks

Review happens in the pull request, so the deploy's job is to confirm that what it's deploying is
what was reviewed. It fetches both repositories, pins the commits it will deploy, and for each one
that moved:

- **Code:** when every commit since the last deploy is a pull-request merge made by GitHub (a
  two-parent merge commit, signed with GitHub's web-flow key), it lists those pull requests.
  Anything else needs its diff reviewed: a direct push, a squash or rebase merge, or rewritten
  history.
- **Config:** the full diff is **always** shown, and needs the typed `yes`, even when every commit
  is a signed pull-request merge. The strategist's token can push to, or merge pull requests on,
  your data repository; the diff is how you'd see it. That includes `config/mode.yaml`.
- It always refuses a diff containing terminal control characters, invisible or bidirectional
  Unicode characters, or binary files, since those can hide code from a reviewer.

The diffs that need review are shown together in one pager, each headed with its repository, and
one typed `yes` covers them. Then, before switching anything, it tests the new code against the
new config, in throwaway checkouts:

- the test suite, with `TRADER_DATA_ROOT` pointing at the candidate config and every other
  `TRADER_*` variable unset, so a shell that happens to carry the runner's environment can't point
  the tests at live data;
- the config must hold `config/mode.yaml`, and the rendered files (the timers, `trader.env`) must
  match `config.yaml` as the new code renders them (`trader config render-deploy --check`). If
  they don't, it refuses and names the files to re-render;
- the strategist's live `state/classifiers.yaml` and custom features are validated with the new
  code. A failure here is a **warning**, not a refusal: the strategist's branch isn't reviewed
  input, but if the new code rejects today's specs, nothing they cover trades at the next session
  until the strategist fixes them, and you should know before then. This step fetches recent
  Alpaca bars for the feature gate, so an Alpaca error (or no network) can make it print a
  traceback; that's still only a warning, and the deploy carries on.

The signature check runs from the currently deployed code and its pinned copy of GitHub's key
(`deploy/github-web-flow.gpg`), never from the commit being deployed.

**Check the listed pull requests are ones you expect.** Each line shows the merge commit, and
`gh pr view N --json mergeCommit -q .mergeCommit.oid` confirms it belongs to pull request N (the
number and title alone come from the commit message). With code taken straight from upstream,
those are the upstream maintainer's merges; from your fork, your own (see
[Take updates](take-updates.md)). If something you didn't expect is listed, press Ctrl-C before
the tests finish.

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
- The lock is taken only for the switch itself (both checkouts, `uv sync`, unit files), never
  during the review or the tests. If the runner's timer fires during a switch, the runner waits
  for it (at most 10 minutes) and starts on the new code.
- A crashed runner doesn't leave a stale lock: the kernel releases it with the process. In the 30
  seconds before systemd restarts a crashed runner, the deploy still refuses. A runner that has
  given up (`failed`) doesn't block a deploy.
- The deploy restarts the dashboard after the switch.

Check whether the runner is up with:

```bash
sudo -u runner XDG_RUNTIME_DIR=/run/user/$(id -u runner) systemctl --user status trader-runner
```

## Not during a strategist run

The strategist runs the deployed code, so a deploy mustn't swap it out from under a run. Each run
holds `~trader/.local/state/trader/strategist.lock`; the deploy checks it before the review and
again at the switch, and holds it through the switch:

- **A run is live:** `REFUSING: a strategist run is live ...`. Runs take up to about half an hour;
  deploy again after it.
- **The deploy can't tell** (`runner` can't reach or read the lock): it refuses rather than guess.
  `2-strategist.sh` grants `runner` the access it needs; re-run it as `trader`.
- A run that starts during the switch skips, as it does when another run holds the lock.

## What the switch installs

Both checkouts are reset to the deployed commits, their permissions closed to group and other
writes, and the code's virtual environment synced. Then, into `runner`'s systemd units: every
`.service` and `trader-watchdog.timer` from the code, and `trader-runner.timer` (rendered from your
schedule) from the config. `runner`'s `~/.config/trading/services.env` is never touched: change it
by hand when a release note says so.

It ends by printing the deployed code and config commits, and:

- if the strategist's unit (in the code) or timers (in the config) changed, or differ from the
  copies `trader` has installed, a reminder to reinstall them as `trader`:
  `bash /srv/trading/main/deploy/setup/2-strategist.sh`. `check.sh` fails until you do;
- if `trading-deploy` itself changed, a reminder to reinstall it:
  `sudo install -m 755 /srv/trading/main/deploy/trading-deploy /usr/local/bin/`.

## The strategist after a deploy

Nothing to do in most cases: the strategist's `trader` command runs the code in
`/srv/trading/main` and reads the config in `/srv/trading/config`, so its next run uses what you
just deployed. Its checkout holds only data and never merges anything from the code. Each run
starts by syncing `trader`'s virtual environment with the deployed `uv.lock`, so a dependency
change is picked up too. If that sync fails, the run alerts and stops; re-running
`bash /srv/trading/main/deploy/setup/2-strategist.sh` as `trader` rebuilds the environment by
hand.
