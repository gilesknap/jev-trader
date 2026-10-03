# Migrate an existing install to the split layout

Older installs ran from one private repository holding both the code and the data, with the
strategist's branch carrying its own copy of the code and merging `main` into itself before each
run. This page moves such an install to the current layout: the code from the public
`gilesknap/jev-trader` (or your fork of it), and your existing private repository kept as your
**data repository**, so its history, issues and pull requests stay where they are.
[Architecture](../explanations/architecture.md#two-repositories) describes where you'll end up.

It's a weekend job with a planned rollback. Read it all before you start.

## When

After the Saturday weekly strategist run has published (check, as `trader`,
`journalctl --user -u 'trader-strategist@weekly'`), and before the runner's next weekday start.
That leaves about 48 hours, in daylight, to verify or roll back. The daily housekeeping run may
fire meanwhile; with the strategist's timers disabled (step C1), it won't.

## Before the day

- **P1. Run a version with split support.** Your deployed code must be recent enough to have
  `deploy/setup/0-data.sh` and the two-repo `trading-deploy` (it says "Two-repo layout" in its
  header), and the installed copy must be that version:

  ```bash
  grep -q 'Two-repo layout' /usr/local/bin/trading-deploy && echo ok
  sudo bash /srv/trading/main/deploy/setup/check.sh      # all PASS, in the old layout
  ```

  If not, take updates into your repository and deploy as you always have, then reinstall
  `trading-deploy` (`sudo install -m 755 /srv/trading/main/deploy/trading-deploy /usr/local/bin/`).
  Install the `acl` package too (`sudo apt install acl`): `2-strategist.sh` needs `setfacl`.
- **P2. Rehearse** on a scratch host or a spare pair of users if you can: a fresh install from
  [the installation tutorial](../tutorials/installation.md) exercises every new script.
- **P3. Decide where your code comes from**, (A) upstream or (B) your fork (see
  [Take updates](take-updates.md)). For (B), fork `gilesknap/jev-trader` now.
- **P4. Give `trader` a token for the data repository only, now.** It must be a fine-grained
  personal access token for your private repository **only** (Contents, Pull requests and Issues:
  read and write), never one that reaches the public code or your fork. This is a hard
  prerequisite: a token made for the old layout may well reach other repositories you own. Since
  the data repository *is* your existing repository, the new token works in the old layout too, so
  install it as `trader` now (`gh auth login`, `gh auth setup-git`), revoke the old one, and check
  it:

  ```bash
  sudo -iu trader
  cd "$(mktemp -d)" && git init -q && git commit -q --allow-empty -m probe
  git push --dry-run https://github.com/gilesknap/jev-trader.git HEAD:refs/heads/probe        # must FAIL (403 / permission denied)
  git push --dry-run https://github.com/<you>/<your-repo>.git HEAD:refs/heads/probe          # must succeed
  gh auth token | grep -q '^github_pat_' && echo "fine-grained token"
  ```

  A dry-run push asks GitHub for push access without sending anything, so neither command writes.
  For option (B), the push to your fork must fail too. (Don't rely on
  `gh api repos/... --jq .permissions.push`: it reports your account's role on the repository,
  not what the token was granted.)
- **P5. Tag the archive points** on your private repository, so the old layout is one name away:

  ```bash
  git tag pre-split-main origin/main
  git tag pre-split-strategist origin/strategist
  git push origin pre-split-main pre-split-strategist
  ```

## The cutover

Run everything as your admin account unless a step says otherwise. Each step can be re-run.
Below, `<you>/<your-repo>` is your existing private repository (now your data repository), and
`<code-url>` is `https://github.com/gilesknap/jev-trader.git`, or your fork's URL for (B).

The code you deploy in C5 must include the **cutover change**: the charter and prompts for the
split, and the strategist unit and `trader.env` template that name `/srv/trading/config`. Check
it in the clone of the code you'll render with in C2, at the commit C5 will deploy:

```bash
grep -q TRADER_DATA_ROOT deploy/templates/trader.env && echo "has the cutover change"
```

Render in C2 with that same commit. (For the upstream maintainer: merge and publish that change
only after C1 has stopped the timers, and before the C2 render.)

**C1. Stop everything.** Confirm the runner isn't running, disable the strategist's timers (so
a reboot can't bring them back; `2-strategist.sh` re-enables them in C6), and confirm no
strategist run is live. The run lock is the authoritative check: a run holds it for its whole
length, git steps included:

```bash
sudo -u runner XDG_RUNTIME_DIR=/run/user/$(id -u runner) systemctl --user status trader-runner
sudo -u trader XDG_RUNTIME_DIR=/run/user/$(id -u trader) systemctl --user disable --now \
    trader-strategist-{premarket,postclose,weekly,housekeeping}.timer
sudo -u trader flock -n ~trader/.local/state/trader/strategist.lock true && echo "no strategist run"
```

**C2. Turn `main` into data `main`.** On a branch of your private repository, delete everything
except these, and open a pull request:

- `config.yaml` and `config/mode.yaml` (not `config/universe.yaml`: the universe stays in the code);
- the rendered files: `deploy/systemd/trader-runner.timer`, `deploy/systemd/trader.env` and
  `deploy/systemd-trader/trader-strategist-*.timer`;
- a `README.md` saying what the repository now is (`templates/data/main/README.md` in the code
  is a starting point).

Re-render the files with the new code, so that `trader.env` gains
`TRADER_DATA_ROOT=/srv/trading/config` (from a clone of the code, with your branch checked out at
`~/my-data`):

```bash
TRADER_DATA_ROOT=~/my-data uv run trader config render-deploy --data-root ~/my-data
```

Merge the pull request with **Create a merge commit**.

**C3. Turn `strategist` into data `strategist`.** In a checkout of the `strategist` branch, make
one commit yourself (not through the wrapper; its timers are stopped) that deletes everything
except `state/`, `journal/`, `logs/` and `features/custom/`, and adds three files from the code's
`templates/data/strategist/`: `CLAUDE.md.template` as `CLAUDE.md`, `.gitignore`, and
`proposals/.gitkeep`. Push it. The data history stays intact, so `git log -p state/strategy.md`
still works.

**C4. Clone data `main` for the runner.** The deploy key you already have is on this repository,
so it covers it. (If you ever need a new one, run `3-runner.sh key` with `TRADER_DATA_ROOT`
pointing at a checkout of your data repository's `main`, as in the
[installation tutorial](../tutorials/installation.md); without it the script reads the code's
placeholder config and prints the wrong repository.) The directory may not exist on an older host:

```bash
sudo install -d -o runner -g trading -m 2750 /srv/trading/config
sudo -u runner -H bash -c 'umask 027 && git clone -q -b main git@github-trading:<you>/<your-repo> /srv/trading/config'
sudo -u runner chmod -R g-w,o-rwx /srv/trading/config
```

**C5. Point the code checkout at the public code and deploy.** As `runner`, record the commit
you're leaving (you need it to roll back), and switch the remote:

```bash
sudo -u runner git -C /srv/trading/main rev-parse HEAD      # write this down: OLD_MAIN
sudo -u runner git -C /srv/trading/main remote set-url origin <code-url>
```

Add `TRADER_DATA_ROOT=/srv/trading/config` to `/home/runner/.config/trading/services.env` (no
deploy ever writes that file):

```bash
sudo -u runner -H nano /home/runner/.config/trading/services.env
```

With `/srv/trading/config` now a checkout, `trading-deploy` runs in two-repository mode, and first
checks the strategist's run lock. `runner` can't reach it until `2-strategist.sh` grants access in
C6, so the deploy would refuse. Grant the same access by hand now, exactly as `2-strategist.sh`
does (close the directories that traverse would otherwise expose first):

```bash
sudo chmod o-rwx ~trader/.claude
sudo test -e ~trader/trading.pre-srv && sudo chmod o-rwx ~trader/trading.pre-srv
sudo -u trader mkdir -p ~trader/.local/state/trader
sudo -u trader touch ~trader/.local/state/trader/strategist.lock
for d in ~trader ~trader/.local ~trader/.local/state ~trader/.local/state/trader; do sudo setfacl -m u:runner:x "$d"; done
sudo setfacl -d -m u::rwx,g::rx,o::rx,u:runner:r ~trader/.local/state/trader
sudo setfacl -n -m u:runner:r ~trader/.local/state/trader/strategist.lock
```

(`~trader/trading.pre-srv` is where an older `2-strategist.sh` moved a previous `~/trading`.)
Then a dry run, and the deploy:

```bash
sudo -u runner trading-deploy --dry-run
sudo -u runner trading-deploy
```

The public code shares no history with your old repository, so the deploy shows the whole tree
difference for review: that's expected, and it shows exactly how the public code differs from what
you ran. Read it, type `yes`, and the tests run against `/srv/trading/config`. If it ends with a
note to re-run `2-strategist.sh`, that's C6, next.

**C6. Move the strategist onto the deployed code.** As `trader`:

```bash
sudo -iu trader
cd /srv/trading/strategist && git pull          # now data only
rm -rf .venv .pytest_cache runtime src tests __pycache__ build docs/_generated
bash /srv/trading/main/deploy/setup/2-strategist.sh
```

Then, as admin, take group write off the strategist's checkout, and make `trader`'s login umask
`022`. Older installs cloned it under `trader`'s default umask (`002`), which leaves files writable
by the `trading` group, and `runner` is in that group. Git doesn't need group write:

```bash
sudo chmod -R g-w /srv/trading/strategist
sudo -iu trader bash -c "grep -qx 'umask 022' ~/.profile || echo 'umask 022' >> ~/.profile"
```

If `~trader/.bash_profile` (or `~/.bash_login`) exists, bash reads it instead of `~/.profile` at
login: put the line there too.

Remove leftovers by name, as above. **Never** use `git clean -fdx` or `-X` here: they would
delete `.env`, `replays/` and the archived decision logs, which are git-ignored on purpose.
`2-strategist.sh` builds `trader`'s virtual environment of the deployed code, installs the
`trader` commands, adds the Claude Code deny rules, reinstalls the strategist's unit (now run
from `/srv/trading/main`) and its timers (from `/srv/trading/config`), and re-enables the timers.
It warns if `~trader/.claude/CLAUDE.md` imports a charter: remove that import, because the
charter now arrives in the strategist's system prompt.

**C7. Tokens.** Confirm `trader` has the data-only token from P4 (the check below repeats it). If
you skipped P4, stop here and do it now: the old token may reach the public repository.

**C8. Verify.**

```bash
sudo bash /srv/trading/main/deploy/setup/check.sh           # every line PASS, split checks included
sudo -iu trader
trader validate                                             # ends "classifiers OK"
trader golive                                               # reads the runtime
trader replay --days 1 --decider stub --name cutover-smoke
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user start trader-strategist@housekeeping   # commits nothing, alerts nothing
```

Then repeat P4's token check as `trader`: the dry-run push to `gilesknap/jev-trader` must fail and
the one to your data repository must succeed. Open the dashboard: its Deployed card shows the code
and config commits, and its links point at the public code and your data repository.

**Leftovers in the public repository.** For now `gilesknap/jev-trader` still carries a
placeholder `config.yaml`, `config/mode.yaml`, `state/` and rendered deploy files, left over from
before the split; a follow-up removes them. They aren't part of the code's layout and nothing on
your host reads them (the runner and the strategist read `/srv/trading/config` and
`/srv/trading/strategist`), but don't be surprised to see them in the C5 review.

**C9. The first weekday.** Watch the pre-market run's log (`~trader/.local/state/trader/`) and the
runner's start.

## Rolling back

Any time before the runner's next start. Each step undoes one above. **Keep this order:** the code
you return to understands `TRADER_DATA_ROOT` too, so every `trader` command (the strategist's and
`runner`'s) fails with a settings error if it still names a config checkout that has gone, and
the old `2-strategist.sh` needs the strategist's code back before it can run.

1. **Disable** the strategist's timers (`systemctl --user disable --now`, as in C1).
2. **Remove `TRADER_DATA_ROOT`** from `/home/runner/.config/trading/services.env`, before
   anything moves the config checkout.
3. **Remove the strategist's split commands**, as `trader`:
   `rm -f ~/.local/bin/trader ~/.local/bin/trader-python ~/.local/bin/trader-test`. The old
   layout runs `uv run trader`, and these name `/srv/trading/config`.
4. **Take the split layout away.** The scripts and `trading-deploy` choose it when
   `/srv/trading/config` is a checkout. Move the checkout aside, and put back the empty directory
   `1-host.sh` creates (an empty one means the single-repository layout):

   ```bash
   sudo mv /srv/trading/config /srv/trading/config.split-aside
   sudo install -d -o runner -g trading -m 2750 /srv/trading/config
   ```

5. **Data `main` and `strategist`:** revert, don't reset, so anything the strategist wrote since
   the cutover is kept. Revert the C3 commit on `strategist` and push it. On `main`, in this
   order, and **before anything restarts the strategist's timers** (step 7 does):
   - revert the C2 merge with a pull request (`git revert -m 1 <merge>`), and merge it;
   - if your private repository's `main` also took in the split's code change before the
     cutover (the charter and prompts for the split layout, and the strategist unit and
     `trader.env` that go with them), revert that merge the same way, and merge it.

   The old wrapper merges `origin/main` into `strategist` at the start of its first run, so
   anything of the split left on `main` would reach the strategist then, charter included.
6. **The code checkout**, as `runner`: set `origin` back to your private repository and return to
   `OLD_MAIN`:

   ```bash
   sudo -iu runner
   cd /srv/trading/main
   git remote set-url origin git@github-trading:<you>/<your-repo>
   git reset --hard <OLD_MAIN> && git clean -fd && chmod -R g-w,o-rwx /srv/trading/main
   uv sync --frozen --extra dev
   cp deploy/systemd/*.service deploy/systemd/*.timer ~/.config/systemd/user/
   XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user daemon-reload
   XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart trader-dashboard.service
   ```

   Or, preferably, run only the `git remote set-url` line above, then, once step 5 is merged,
   `sudo -u runner trading-deploy`: it shows a full review back to the old tree and does the reset,
   clean, permissions, sync, units and dashboard restart itself. `trading-deploy` never changes
   `origin`, so the `set-url` line is essential: if the deploy says "nothing to deploy", `origin`
   still points at the public code, and the runner would stay on it, reading the placeholder
   `config.yaml` and `config/mode.yaml` (`auto`) that repository still carries. By hand, use
   `git clean -fd`, never `-x`.
7. **The strategist**, as `trader`: pull the reverted branch (step 5), which brings its code back,
   then reinstall the old unit and timers from it:

   ```bash
   sudo -iu trader
   cd /srv/trading/strategist && git pull && uv sync --extra dev
   bash /srv/trading/strategist/deploy/setup/2-strategist.sh
   ```

   If `uv sync` says there's no `pyproject.toml`, the checkout is still data-only: the revert of
   C3 hasn't been pushed or pulled yet.
8. **Check:** `sudo bash /srv/trading/main/deploy/setup/check.sh`, every line PASS.

### What a rollback leaves behind

These live outside git, so the steps above don't undo them. None of them changes what the old
layout does, except the first:

| Left behind | What to do |
|---|---|
| `TRADER_DATA_ROOT` in `/home/runner/.config/trading/services.env` | **Remove it** (step 2, before step 4): the code you return to honours it |
| `/srv/trading/config.split-aside`, and an empty `/srv/trading/config` | Nothing reads either; keep the aside copy until you try again, and leave the empty directory (`1-host.sh` creates it on every host) |
| `~trader/.local/share/trader/venv` | Unused by the old layout; keep it for the next attempt, or delete it to save disk |
| `~trader/.local/bin/trader`, `trader-python`, `trader-test` | Remove them (step 3): the old layout runs `uv run trader`, and a leftover `trader` would fail or run the split layout's paths |
| The deny rules in `~trader/.claude/settings.json` | Harmless; keep them, or delete the `permissions.deny` entries `2-strategist.sh` added |
| `runner`'s traverse and read ACLs on `~trader`, `~trader/.local`, `~trader/.local/state`, `~trader/.local/state/trader` and the lock | The old layout never grants them. Remove them: `sudo setfacl -x u:runner` on each of the four directories and the lock file, and `sudo setfacl -k ~trader/.local/state/trader` for its default ACL |
| The `weekly` label and any weekly issues in your data repository | Harmless; close any open weekly issue. The old layout reports in a weekly pull request again |

