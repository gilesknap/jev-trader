# Take updates

Your runner runs the code in `/srv/trading/main`, and that changes only when you run
`trading-deploy` ([Deploy a change](deploy.md)). Where the deploy takes new code from is the
checkout's `origin`: either the public repository itself, or your fork of it. You chose one when
you installed (step 0 of [the installation tutorial](../tutorials/installation.md)); this page
compares them and shows how to take updates with each.

## The two options

| | (A) Straight from `gilesknap/jev-trader` | (B) From your fork |
|---|---|---|
| `/srv/trading/main`'s `origin` | `https://github.com/gilesknap/jev-trader.git` | `https://github.com/<you>/jev-trader.git` |
| Installed with | `3-runner.sh install --split` | `3-runner.sh install --code-repo <you>/jev-trader` |
| Who decides what you deploy | the upstream maintainer, by merging | you, by merging an update pull request on your fork |
| What `trading-deploy` shows you | the list of upstream pull requests and the test results; no diff, because they're GitHub-signed merges | your one update pull request; you read its diff on GitHub before merging it |
| Effort | none | one pull request per update |

With (A), your deploy trusts whoever can merge to the public repository's `main`: their merges
come through as GitHub-signed pull request merges, so the deploy lists them and runs the tests
against your config, but doesn't make you read the code. A direct push to upstream `main` (which
its admins can make) isn't a signed merge, so that does show you its diff.

With (B), nothing reaches your runner that you haven't merged yourself. **(B) is the recommended
choice once real money is riding on someone else's merges.** It's also what you need to
contribute, or to run code that differs from upstream (a different universe, say).

Either way your data repository is untouched by an update, unless the update needs a data change
(below).

The checkout reads the public code anonymously over HTTPS, and housekeeping's daily check that
your deployed code is current does the same. A fork of a public repository is public too, so this
works for (B) as well.

## (A) Take updates from upstream

Housekeeping alerts when the code repository's `main` has had changes merged for 48 hours that
you haven't deployed, and the dashboard's links include "Not yet deployed", a comparison of the
deployed commit with `main`. When you're ready, outside a trading session:

```bash
sudo -u runner trading-deploy --dry-run   # what would be deployed
sudo -u runner trading-deploy
```

Check the listed pull requests against upstream's history, and read their descriptions for any
"data repository changes" section (below) before you deploy.

## (B) Take updates through your fork

1. In a clone of your fork, bring upstream's `main` onto a branch of your fork:

   ```bash
   git remote add upstream https://github.com/gilesknap/jev-trader.git   # once
   git fetch upstream
   git push origin upstream/main:refs/heads/upstream-sync
   ```

   (GitHub's "Sync fork" button would update your fork's `main` directly, without the pull
   request below, so don't use it for the branch you deploy from.)
2. Open a pull request on **your fork** from `upstream-sync` to `main`. Read its diff: this is the
   review. Check for a needed data change (below).
3. Merge it with **Create a merge commit** (not squash or rebase).
4. Deploy: `sudo -u runner trading-deploy`. It walks your fork's `main` by first parents, so it
   lists your one update pull request, not every upstream commit behind it, and deploys it after
   the tests.

If you keep your own changes on your fork's `main`, step 1's pull request may conflict; resolve
it on the `upstream-sync` branch (merge your `main` into it) before merging.

## Switch between (A) and (B)

As `runner`, point the code checkout at the other repository, then deploy:

```bash
sudo -u runner git -C /srv/trading/main remote set-url origin https://github.com/<you>/jev-trader.git
sudo -u runner trading-deploy
```

If both are at the same commit, there's nothing to deploy. Then, as `trader`, re-run
`bash /srv/trading/main/deploy/setup/2-strategist.sh`: it names the code repository in the
strategist's Claude Code deny rules (see [The strategist](../explanations/strategist.md#why-it-runs-unattended)),
and takes that name from this checkout's `origin`.

## Keep your data repository current

Most updates need nothing from your data repository. A few do: a new required key in
`config.yaml`, a change to the templates the timers and `trader.env` are rendered from, or a new
file the strategist's branch should have. Contributors describe such changes in a **"Data
repository changes"** section of the pull request (see [Contributing](contribute.md)); those
sections are the release notes to read before you deploy.

To see for yourself what changed between the code you run and the code you're about to deploy,
in a clone of the code:

```bash
OLD=$(sudo -u runner git -C /srv/trading/main rev-parse HEAD)   # the deployed commit
git fetch origin
git diff "$OLD" origin/main -- templates/data deploy/templates src/trader/config.py
```

Then:

- **`deploy/templates/` changed:** re-render your deploy files with the **new** code (a checkout
  of the commit you'll deploy), pointing at a checkout of your data repository's `main`, and merge
  the result into it with a pull request:

  ```bash
  TRADER_DATA_ROOT=~/my-data uv run trader config render-deploy --data-root ~/my-data
  ```

  If you forget, the deploy refuses: it renders with the candidate code and names the files that
  don't match. A change to `trader.env` also needs the same line changed by hand in `runner`'s
  `~/.config/trading/services.env`, which no deploy touches. A change to the strategist's timers
  needs `2-strategist.sh` re-run as `trader` after the deploy (the deploy says so).
- **`templates/data/main/config.yaml` changed:** compare it with your `config.yaml` and add any
  new key. `config.yaml` is validated strictly, so new code that requires a key you don't have
  fails its tests against your config, and the deploy refuses before changing anything.
- **`templates/data/strategist/` changed:** make the same change on your `strategist` branch by
  hand, as `trader` in `/srv/trading/strategist`, between strategist runs (if a commit outside the strategy paths
  lands during a run, the wrapper alerts about it). Only files under
  `state/`, `journal/`, `features/custom/`, `logs/` and `proposals/` are ever committed by a run;
  your own commit can change anything, such as `.gitignore` or `CLAUDE.md`.

Deploy the code and the data change together: `trading-deploy` fetches both repositories, tests
the new code against the new config, and switches both at once.
