# Setting up your own copy

This guide takes you from nothing to a runner trading on paper, on your own VPS, running the public code with your own private data repository. It covers the accounts you need and the order to do things in. The [explanations](../explanations.md) cover the architecture, and the [how-to guides](../how-to.md) cover day-to-day operations; this guide doesn't repeat them.

Plan on an evening for steps 1–7, then a few days of paper before the experiment starts (step 8).

What you'll end up with:
- **A VPS** that you reach by SSH and whose dashboard you open over your private Tailscale network. The VPS itself can't open connections to your other devices.
- **Three accounts on it:** your admin account (sudo), `trader` (Claude, the strategist, paper keys only) and `runner` (the trading daemon, live keys, no sudo).
- **Two repositories:** the public code, [gilesknap/jev-trader](https://github.com/gilesknap/jev-trader) (or your fork of it), and a **private data repository** of your own, holding your deployment config and the strategist's memory. [Architecture](../explanations/architecture.md#two-repositories) explains the split.
- **Five external services:** Alpaca (the broker), OpenRouter (the Jev decision model), Claude (the strategist), GitHub (the two repositories) and ntfy (alerts on your phone).

Items marked **TODO** weren't verified when this was written; check them as you go.

## 0. Your data repository and `config.yaml`

1. **Decide where your code comes from.**
   - **(A) Straight from `gilesknap/jev-trader`.** Nothing to do now: your runner will deploy the
     upstream maintainer's merged pull requests.
   - **(B) From your own fork of it.** Fork `gilesknap/jev-trader` on GitHub (a fork of a public
     repository is public; it holds only code). You review each upstream update in a pull request
     on your fork before you deploy it. This is the safer choice once real money is involved, and
     you need a fork anyway to contribute or to change the universe.

   [Take updates](../how-to/take-updates.md) compares the two. You can switch later.
2. **Create an empty private repository** on GitHub for your data: no README, licence or
   `.gitignore`. Keep it private: the strategist's journal and your trading results end up in it.
3. **Fill it with `0-data.sh`**, on your laptop or the VPS, from a clone of the code (yours, as
   your normal GitHub user, not the strategist's). It needs `git`, `uv` and the GitHub CLI `gh`,
   logged in with access to the new repository (`gh auth login`, then `gh auth setup-git` so `git
   push` uses it):
   ```bash
   git clone https://github.com/gilesknap/jev-trader.git && cd jev-trader   # or your fork
   bash deploy/setup/0-data.sh <you>/<your-data-repo>
   ```
   It asks for your name, the experiment start date and the Tailscale logins allowed into the
   dashboard (or take them as `--name`, `--start-date` and `--users`; `--help` lists them). Then
   it builds both branches from the code's `templates/data/` and pushes them together:
   - `main`: `config.yaml` with your values, `config/mode.yaml` (`paper`), and the timers and
     environment file rendered from them;
   - `strategist`, an orphan branch: a fresh `state/` (holding the pre-launch pack as
     `state/classifiers.yaml`, see step 8), empty `journal/` and `logs/`, `features/custom/`,
     `proposals/`, and a stub `CLAUDE.md`.

   It also makes `main` the default branch and creates the `needs-human` and `weekly` labels, which
   the strategist uses. It refuses a repository that isn't empty, and re-running it on one it set
   up only re-checks the default branch and the labels.
4. **Review `config.yaml`** on your data repository's `main`. It holds everything specific to one
   deployment; the comments in the file itself are the reference for each key. Check:
   - `owner.github_repo` is your **data** repository: the setup scripts clone it.
   - `dashboard.users`: your Tailscale login(s). **Empty means nobody can open the dashboard.**
   - `experiment.start_date`: for now, any date comfortably after your install (a few weeks out).
     You'll set the real one in step 8, when you run the pre-launch pack. The go-live gate and the
     scoreboard ignore everything before it.
   - `schedule.*` if you aren't in the UK. `local_tz` is your clock, and `runner_start` and the
     `strategist` timer specs (systemd `OnCalendar` syntax) are in it. Keep `runner_start` safely
     before 09:30 New York in every daylight-saving week, and the post-close timer after 16:00 New
     York.

   To change it, edit it in a checkout of your data repository, re-render the files that can't
   read YAML, and commit them together:
   ```bash
   git clone https://github.com/<you>/<your-data-repo>.git ~/my-data     # main
   # edit ~/my-data/config.yaml, then, from the code checkout:
   TRADER_DATA_ROOT=~/my-data uv run trader config render-deploy --data-root ~/my-data
   git -C ~/my-data commit -am "Configure for <you>" && git -C ~/my-data push
   ```
   A malformed `config.yaml` (an unknown key, a bad date, an unquoted time) stops every `trader`
   command with a message naming the file, so a typo can't slip through quietly. Before the
   install, pushing straight to `main` is fine; once the system runs, change it through pull
   requests (see "Changing `config.yaml` later").

Your data repository never holds code, and the code repository never holds your data. Leave
`config/mode.yaml` at `paper` until you mean to change it. The universe isn't in your data
repository: it's `config/universe.yaml` in the code, because it must match the allocator's
buckets.

## The admin account and Claude Code

You'll do setup and maintenance from a personal admin account with sudo (step 1 creates it). Running Claude Code in that account is a good way to do it: it can read this guide, run the scripts and fix what goes wrong. Two things to be clear about first.

**Use auto mode, not "skip permissions".** Start it with `claude --permission-mode auto` (or press Shift+Tab to cycle to it, or put `{"permissions": {"defaultMode": "auto"}}` in `~/.claude/settings.json`). In auto mode a classifier reviews each action before it runs and blocks the dangerous kinds: sending data off the box, `curl … | bash`, force-pushes and `git reset --hard`, mass deletion, granting permissions, production deploys (so expect it to stop at `trading-deploy`: run that yourself). It's the default mode in recent Claude Code (2.1.283 and later). It needs a recent model (Opus or Sonnet 4.6 or later, or Fable). See [permission modes](https://code.claude.com/docs/en/permission-modes.md#eliminate-prompts-with-auto-mode). Don't use `--dangerously-skip-permissions` here.

**Be honest about what that account can do.** With sudo it can read the live Alpaca keys and place orders directly, outside the runner and its guardrails. What bounds the damage:
- Alpaca API keys can trade but can't withdraw money.
- `trading-deploy` deploys GitHub-signed code merges without a diff review; anything else, and every config change, makes you read the diff.
- The one-way tailnet (step 2): the VPS can't reach your other devices.

So:
- Run Claude in the admin account only in sessions you're watching, never unattended.
- Add the live keys last, after setup works on paper.
- Keep the live balance small.
- Turn on 2FA for Alpaca, GitHub and OpenRouter, and give the OpenRouter key a spend limit.
- While live keys are on the box, be careful what untrusted content you have it read (web pages, issues, files from elsewhere): that's how instructions get smuggled in.

**The strategist is different on purpose.** `trader` runs Claude headless in auto mode (step 5), with nobody there to answer a prompt, so the account itself is the fence: no sudo, no live keys, no write access to the deployed code, the deployed config or the runtime, no way to reach the dashboard, and a GitHub token that reaches only your data repository. One gap: that token can push to the data repository's `main` or merge a pull request there through the API, and those merges are GitHub-signed too. That's why `trading-deploy` always shows you the full diff of the config and needs a typed `yes` for it, signed or not: read it.

## 1. The VPS

**Size.** The original runs on a Hostinger KVM plan with 2 vCPUs, 8 GB RAM and a 100 GB disk, on Ubuntu 26.04 LTS. That's comfortable: the runner is light, and the heaviest moments are the strategist's backtests and the test suite. 4 GB RAM would probably do; don't go below 2 vCPUs. Pick a data centre near you; latency to Alpaca doesn't matter at one-minute bars. Any Ubuntu 24.04+ host works the same way.

**First login.** Hostinger's panel (hPanel → VPS) shows the root password and IP. It also has a **browser terminal**, which works even when SSH doesn't: it's your way back in if you lock yourself out below.

**Your admin account and SSH keys.** On your laptop, if you don't have a key yet:
```bash
ssh-keygen -t ed25519 -C "laptop"
```
On the VPS, as root:
```bash
adduser alice                      # your admin name
usermod -aG sudo alice
adduser trader                     # the strategist's account (1-host.sh expects it to exist)
```
From the laptop, copy your public key to the admin account (or paste it into hPanel → VPS → SSH keys, which adds it for root, then copy root's `~/.ssh/authorized_keys` into `/home/alice/.ssh/`, owned by alice, mode 600):
```bash
ssh-copy-id alice@<vps-ip>
```
Add an alias on the laptop, in `~/.ssh/config`:
```
Host trading
    HostName <vps-ip>            # later: the VPS's Tailscale name, see step 2
    User alice
    IdentityFile ~/.ssh/id_ed25519
```
Now `ssh trading` works. Check that it logs you in **without a password** before the next step.

**Lock SSH down.** In `/etc/ssh/sshd_config.d/10-hardening.conf`:
```
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
```
then `sudo sshd -t && sudo systemctl reload ssh`. Keep your current session open and test a new `ssh trading` in another terminal before closing it.

**Firewall.** SSH on port 22 is open to the whole internet until you restrict it, so keys only (above) matters. Allow SSH, and nothing else from the internet:
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable
```
Once Tailscale works (step 2), you can close public SSH too, if you don't need the SSH-tunnel dashboard (step 2b) from a machine without Tailscale: `sudo ufw allow in on tailscale0` then `sudo ufw delete allow OpenSSH`. After that you reach the box only over Tailscale, or the hPanel browser terminal. Hostinger also has a panel firewall; either is fine, but don't run two you forget about.

**Packages.**
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git gh gpg bubblewrap curl openssl util-linux acl
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh   # the units expect /usr/local/bin/uv
```
- `bubblewrap` sandboxes the strategist's feature code. On Ubuntu 24.04+ it needs AppArmor's `bwrap-userns-restrict` profile, which the `apparmor` package ships; `check.sh` tests that the sandbox works.
- `uv` installs the right Python for the project by itself.
- `gpg` lets `trading-deploy` check that merge commits were signed by GitHub.
- `acl` provides `setfacl`, which `2-strategist.sh` uses to let `runner` see the strategist's run lock, so a deploy can refuse while a run is live.

## 2. Tailscale, one way

The dashboard (with its STOP button) is served only on your tailnet, and the VPS should be able to answer your devices but not reach them. A trading box that runs AI-written code shouldn't be a way into your laptop.

1. Make a Tailscale account (free) and install Tailscale on your laptop and phone.
2. In the admin console → Access controls, replace the default allow-all policy with something like this (swap in your login):
   ```javascript
   {
     "tagOwners": {
       "tag:vps": ["autogroup:admin"]
     },
     "grants": [
       // Your devices may reach the VPS: SSH and the dashboard.
       { "src": ["you@example.com"], "dst": ["tag:vps"], "ip": ["tcp:22", "tcp:8444"] },
       // Your own devices may reach each other as usual.
       { "src": ["you@example.com"], "dst": ["you@example.com"], "ip": ["*"] }
     ]
     // No grant has tag:vps as its source, so the VPS can't open connections to anything.
     // Replies to connections you open are still allowed.
   }
   ```
   If you share the tailnet with others, keep `src` to your own login.
3. In the admin console → DNS, enable **MagicDNS** and **HTTPS Certificates**. `3-runner.sh` serves the dashboard with `tailscale serve --https`, which fails without them.
4. On the VPS:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up --advertise-tags=tag:vps
   ```
   Open the printed link and approve it. Tagging the machine also means its key doesn't expire with your login.
5. Point the `HostName` in your `~/.ssh/config` alias at the VPS's Tailscale name (`tailscale status` shows it), and close public SSH as described in step 1 if you like.

**How the dashboard is protected.** `3-runner.sh` runs `tailscale serve` to proxy `https://<vps>:8444` to a Unix socket only `runner` can reach. Tailscale adds a `Tailscale-User-Login` header naming who is connecting, and the dashboard lets in only the logins listed in `config.yaml` (the dashboard users). A request without that header is refused. The one exception is `TRADER_DASHBOARD_ALLOW_LOCAL=1`, which lets header-less requests in. It exists for local development only (see [Your first replay](first-replay.md)). **Never set it on the Tailscale-served dashboard**, not even "temporarily" in a systemd drop-in. `tailscale serve` sends no login header for requests from tagged devices, and the VPS itself is tagged, so any process on the VPS (the strategist included) could reach the dashboard and press STOP. To use the dashboard from a machine without Tailscale, use the SSH tunnel (step 2b), the only unit where that setting belongs.

## 2b. Dashboard without Tailscale (SSH tunnel)

For a machine that can't run Tailscale (a locked-down work laptop, say), there's a second dashboard reachable only through SSH. `1-host.sh` creates a `dashview` group, adds the admin who ran it (never `trader` or `runner`), and makes `/srv/trading-dashview`, which only that group can enter. `trader-dashboard-ssh.service` serves the dashboard on a socket in that directory. From the other machine:
```bash
ssh -N -L 18444:/srv/trading-dashview/dashboard.sock <admin>@<vps>
```
then open http://localhost:18444. (Any free local port works; 18444 just avoids the usual clashes with 8080-style dev servers.) Forwarding to a Unix socket needs OpenSSH 6.7 or newer on the client, which any current Linux, macOS or Windows has. Phone SSH apps generally forward only to a host and port, not a socket, so use Tailscale on phones.

**Easier: a host entry in `~/.ssh/config`** on that machine:
```
Host trading-dash
    HostName <vps address>
    User <admin>
    LocalForward 18444 /srv/trading-dashview/dashboard.sock
    ExitOnForwardFailure yes
    ServerAliveInterval 30
    ControlMaster auto
    ControlPath ~/.ssh/cm-%C
    ControlPersist yes
```
Then `ssh -fN trading-dash` starts the tunnel in the background, `ssh -O check trading-dash` says whether it's up, and `ssh -O exit trading-dash` stops it. `ExitOnForwardFailure` makes it fail loudly if the local port is taken instead of running with no tunnel, and `ServerAliveInterval` drops a dead connection rather than leaving it hanging. Plain `ssh trading-dash` still gives you a shell, sharing the same connection; `ControlPersist` keeps the tunnel up whichever of the two you start first. If `-O check` says it's up but the page doesn't load, the tunnel is fine and the dashboard end isn't: check `trader-dashboard-ssh.service` and that you're in `dashview`.

This dashboard trusts anyone who reaches its socket, so it's the one place `TRADER_DASHBOARD_ALLOW_LOCAL=1` belongs: on `trader-dashboard-ssh.service` only. Access control is the directory's group, plus SSH. `check.sh` fails if the Tailscale-served dashboard has it set.

## 3. Alpaca

1. Sign up at alpaca.markets. You get a **paper** account at once. The **live** account needs identity checks, which can take a few days, so start early. **TODO:** check Alpaca accepts residents of your country for live trading.
2. Make the live account a **cash** account, not margin. The runner never shorts or borrows, and a cash account makes sure it can't. Settled-cash rules (T+1) apply; the runner sizes from settled cash.
3. Market data: the free plan is fine. The runner streams **IEX** bars live (the free feed, up to 30 symbols), and backtests read historical SIP bars, which the free plan allows for data older than 15 minutes.
4. Generate API keys, separately for paper (in the paper dashboard) and live (in the live dashboard). Each gives a key id and a secret.
5. Reset the paper account's balance to about what you'll fund live with (the original uses $250), so paper sizing matches reality. After any later paper reset, run `trader rebase-paper` ([Use the controls](../how-to/controls.md)).
6. Fund the live account with at least the go-live minimum ($100 in `src/trader/golive.py`) before the gate is likely to pass. Until it's funded, the runner stays on paper and alerts you. Deposits and withdrawals are yours alone; the code never moves money.

**Where the keys go:**
- Paper keys → the strategist's `/srv/trading/strategist/.env` (created in step 6), as `ALPACA_PAPER_KEY` and `ALPACA_PAPER_SECRET`.
- Live keys → **only** runner's `/home/runner/.config/trading/env`, as `ALPACA_LIVE_KEY` and `ALPACA_LIVE_SECRET` (runner's file starts as a copy of the strategist's, so it has the paper keys too). `check.sh` fails if the strategist's `.env` has any `ALPACA_LIVE_` line, or if trader can read runner's env file.

## 4. OpenRouter (the Jev decision model)

Every entry and exit question goes to **Jev** (`typesafe/jev-*`, pinned in `config.yaml`) through OpenRouter's Decisions API. A call costs about $0.00002; a busy paper day with the baseline probe is a few cents.

1. Sign up at openrouter.ai and buy some credit ($5–10 lasts months). Consider auto top-up.
2. Create an API key. Give it a **spend limit** (say $10), so a runaway loop can't drain the account.
3. Check the key can reach Jev: the runner's first session will show `decision_errors` on the dashboard if it can't. **TODO:** confirm whether the Decisions API (`/api/alpha/decisions`) or the Jev model needs to be enabled on your OpenRouter account.
4. Put the key in both env files as `OPENROUTER_API_KEY` (the strategist's `.env` and runner's `env`).

The daily housekeeping run alerts you when credit is under $3 or will run out within 3 weeks, when the key's spend limit is nearly used, and when the key is about to expire.

## 5. Claude, GitHub and ntfy (as `trader`)

The strategist is headless Claude Code, run as `trader` by systemd user timers (installed by `2-strategist.sh`).

**Claude.** It needs a Claude subscription with Claude Code (the original uses Max; Opus runs of up to 50 minutes a day add up). Log in as trader:
```bash
sudo -iu trader
curl -fsSL https://claude.ai/install.sh | bash      # installs ~/.local/bin/claude
claude                                              # log in with your subscription, then /exit
```
The wrapper unsets `ANTHROPIC_API_KEY`, so runs can only use the subscription. Headless runs can't answer permission prompts, so put `trader` in auto mode, in `~trader/.claude/settings.json`:
```json
{ "permissions": { "defaultMode": "auto" } }
```
That's what the original runs headless, and the deny rules below were tested in it. Don't use `bypassPermissions`. Auto mode's classifier is a second line, not the fence: the strategist runs as its own account with paper keys only, so what it can reach is limited by the OS, not by prompts. `2-strategist.sh` (step 6d) later adds `permissions.deny` rules to the same file, for the routes to GitHub's public content, and leaves everything else in it alone. Don't make `~trader/.claude/CLAUDE.md` import the charter: the wrapper passes it to every run, and an import would load it twice (`2-strategist.sh` and `check.sh` both flag one). If the login expires, runs fail and you get an alert; log in again the same way. **TODO:** how long a subscription login lasts before it needs renewing.

**GitHub (trader).** Create a **fine-grained** personal access token for **your data repository only** ("Only select repositories"), with *Contents*, *Pull requests* and *Issues* set to read and write, and an expiry you'll notice (housekeeping warns 3 weeks ahead). Never give it access to the code repository or your fork: the strategist mustn't be able to write anything public. Don't log `trader` in with your own GitHub account (OAuth) either: that would carry all your access, and `check.sh` fails unless the token is a fine-grained one. Then as trader:
```bash
gh auth login          # GitHub.com → HTTPS → paste the token
gh auth setup-git      # so git push uses it
git config --global user.name "strategist" && git config --global user.email "<your noreply address>"
```
The token lets the strategist push its branch and open issues (its `needs-human` requests, code proposals and the weekly report). It could technically push to the data repository's `main` too (branch protection isn't available on private repositories on the free plan), which is why nothing on `main` runs until **you** deploy it, after reading its diff ([Deploy a change](../how-to/deploy.md)).

**GitHub (runner).** `runner` gets a **read-only deploy key** on your data repository in step 6. It only ever pulls the data repository's `main`, and the public code over HTTPS without credentials.

**Merging.** Merge PRs in GitHub's web UI with **"Create a merge commit"**, on your fork and on your data repository. Those merge commits are signed by GitHub, and `trading-deploy` checks that signature (against `deploy/github-web-flow.gpg`): signed code merges deploy after a PR list and the tests, and anything else makes you read the diff and type `yes`. Config changes always show their diff.

**ntfy.** Install the ntfy app on your phone. Step 6 generates a random topic name (`NTFY_TOPIC` in the strategist's `.env`); subscribe to exactly that name. Alerts go to the public ntfy.sh server, so the random name is what keeps them private: don't share it.

## 6. Install

As your admin account on the VPS, get the code and a checkout of your data repository's `main` (the scripts read `config.yaml` from it until the runner has its own copy):
```bash
git clone https://github.com/gilesknap/jev-trader.git ~/jev-trader      # or your fork
git clone https://github.com/<you>/<your-data-repo>.git ~/my-data       # main; needs read access, e.g. gh auth login
```
Then follow the table. All the scripts are idempotent, so re-running one is always safe. Run each as a single short command, not by pasting long command lists.

| # | As | Command | Does |
|---|---|---|---|
| 6a | admin | `sudo bash ~/jev-trader/deploy/setup/1-host.sh` | Creates the `trading` group, the `runner` user (no sudo, lingering) and `/srv/trading/{strategist,main,config,runtime}` with the right owners and modes |
| 6b | admin | `sudo TRADER_DATA_ROOT=$HOME/my-data bash ~/jev-trader/deploy/setup/3-runner.sh key` | Creates `runner`'s deploy key and prints it. Add it on your **data** repository → Settings → Deploy keys, **read-only** |
| 6c | admin | `sudo TRADER_DATA_ROOT=$HOME/my-data bash ~/jev-trader/deploy/setup/3-runner.sh install --split` | As `runner`: clones the public code over HTTPS to `/srv/trading/main` and your data repository's `main` (with the deploy key) to `/srv/trading/config`, runs the tests against that config, installs `trading-deploy`, the services and timers, and `tailscale serve` → dashboard socket. For option (B), add `--code-repo <you>/jev-trader` to clone your fork instead |
| 6d | trader, **from a fresh login** | `bash /srv/trading/main/deploy/setup/2-strategist.sh` | Clones your data repository's `strategist` branch to `/srv/trading/strategist`, creates `.env` (paper keys only, generated ntfy topic), builds `trader`'s virtual environment of the deployed code and installs the `trader`, `trader-python` and `trader-test` commands, adds Claude Code deny rules for GitHub's public content, installs the strategist's unit (from the code) and timers (from your config), and links `~/trading` |
| 6e | admin | `sudo bash /srv/trading/main/deploy/setup/3-runner.sh install` | Copies the strategist's `.env` to `runner`'s env file (6c couldn't: it didn't exist yet) and re-checks the rest |
| ✓ | admin | `sudo bash /srv/trading/main/deploy/setup/check.sh` | Verifies users, permissions, secrets placement, services, socket isolation, the strategist's timers and commands, and the split layout. Every line should read PASS |

Notes:
- 6b and 6c need `TRADER_DATA_ROOT` pointing at your data checkout: without it, `3-runner.sh key` stops, because the code carries no `config.yaml` of its own. 6c needs it only because `/srv/trading/config` doesn't exist yet; from then on the scripts read `config.yaml` there and find the split layout by themselves. 6c also warns that the strategist's `.env` doesn't exist yet: expected, 6e deals with it.
- 6d needs `trader`'s GitHub token (step 5) to clone your private data repository.
- Before 6d, make `trader`'s login umask `022` (as trader: `grep -qx 'umask 022' ~/.profile || echo 'umask 022' >> ~/.profile`, and the same in `~/.bash_profile` if it exists, then log in again), so the checkout `2-strategist.sh` clones isn't writable by the `trading` group, which `runner` is in. If it already is, `sudo chmod -R g-w /srv/trading/strategist` fixes it.
- After 6d, `2-strategist.sh` suggests running `check.sh` next: do 6e first, which copies the strategist's `.env` for `runner`, then check.
- Between 6d and 6e: fill in `/srv/trading/strategist/.env` (paper keys and the OpenRouter key) and subscribe to the printed ntfy topic.

After 6e, as admin:
```bash
sudo -u runner -H nano /home/runner/.config/trading/env        # add ALPACA_LIVE_KEY / ALPACA_LIVE_SECRET
sudo bash /srv/trading/main/deploy/setup/check.sh               # every line should read PASS
```
**Live keys go only in `runner`'s file.** Never put them in the strategist's `.env`: `check.sh` fails if they're there.

`3-runner.sh install` prints the dashboard's address. Open it from your laptop; you should see "The runner isn't trading right now".

You can delete `~/my-data` now if you like: from here on, `/srv/trading/config` is the copy that counts, and it changes only when you deploy.

## 7. First day

- The pre-launch pack (step 8) is already `state/classifiers.yaml` on a new `strategist` branch, so expect its `test_*` rules to trade on paper from the first session; they stop on the start date.
- **Before the session:** `trader session` (as trader) shows today's session times. `trader validate` should say `classifiers OK`.
- **At the runner's start** (the timer, shortly before the US open): an ntfy "runner started session …". The dashboard's banner switches to "The runner is trading now".
- **During the session:** watch `decision_errors` and `probe_errors` in the dashboard's At a glance panel. A few Jev timeouts a day pause decisions for 5 minutes each and are harmless; a steady stream means a key or credit problem.
- **After the close:** a Daily P&L ntfy, then the strategist's post-close run commits a journal to your data repository's `strategist` branch (check the branch on GitHub).
- **Saturday:** the weekly run opens a weekly issue (label `weekly`) in your data repository, its body the weekly journal. Reading it is your weekly job ([Daily operations](../how-to/daily-operations.md)).

If something doesn't happen, the logs are in `/srv/trading/runtime/alerts.log` (runner), `/srv/trading/strategist/strategist-alerts.log` (the strategist wrapper's and housekeeping's alerts), `journalctl --user` as runner, and `~trader/.local/state/trader/` for the strategist's runs (one log per run).

## 8. Pre-launch (1–2 trading days)

Before the experiment starts, push real orders through every execution path on paper, so a bug shows up while nothing counts.

1. Set `experiment.start_date` in `config.yaml` to **2–3 trading days after the first pre-launch session** (a Monday is tidiest), then render, merge and deploy (see "Changing `config.yaml` later"). This is the one real start date: the pack runs on the days before it.
2. As trader, on the `strategist` branch, copy the pack into place, set its `date:` to the next session, validate and push (on a new `strategist` branch the pack is already `state/classifiers.yaml`, so the `cp` changes nothing). Do it between strategist runs:
   ```bash
   sudo -iu trader
   cd /srv/trading/strategist && git pull
   cp /srv/trading/main/templates/data/strategist/state/classifiers.yaml state/classifiers.yaml
   sed -i 's/^date: .*/date: YYYY-MM-DD/' state/classifiers.yaml   # the next session's date
   trader validate                                                 # must end "classifiers OK"
   git commit -am "Pre-launch plumbing pack" && git push
   ```
   The pack is `control_orb`, five `test_*` rules (each deliberately enters on its own symbol, AAPL, MSFT, XLF, XLI or XLV, to hit one path) and the universe baseline probe, which never orders.
3. Let it run for a session or two, then check each path in `/srv/trading/runtime/books/paper/trades.csv` (the `reason` column), or the dashboard's trade list and Rules panel:

   | Path | Proof |
   |---|---|
   | Jev entry, market order | `buy` rows with reason `ENTER` (AAPL, MSFT, XLV) |
   | Jev-driven exit | MSFT `sell` with reason `classifier EXIT` (a time stop or flatten doesn't prove it) |
   | Time stop, re-arm | AAPL `sell` with reason `time stop`, then a second `buy` |
   | Target | AAPL `sell` with reason `target` (its target is only 0.1%) |
   | Limit fill | XLF `buy` at a price just under the price before it |
   | Scale-out | XLF `sell_part` row with reason `scale out` |
   | Breakeven or trailing stop | XLF `sell` with reason `server stop` (Alpaca's stop fired) or `stop (raised)` (the engine's, above the initial stop), at about the entry price (breakeven) or just under its high (trail) |
   | Limit expiry | no XLI trade row; the XLI line in Rules shows the note `limit expired unfilled` and goes back to `armed` |
   | End-of-day flatten | XLV `sell` with reason `eod flatten` |

   Some paths depend on the market: on a quiet day AAPL may never reach its target. Give it another day before suspecting a bug, and raise a `needs-human` issue (or look at `alerts.log`) if something that should have happened didn't.
4. Before the start date, put `state/classifiers.yaml` back to `control_orb` plus the probe (the observe phase runs nothing else).

**Why the tests must stop at launch.** Every shadow rule shares one paper account, and the go-live gate judges that account's days: a plumbing rule's trades and drawdowns would crowd out and distort the real experiment. So from the start date the runner drops every `test_*` rule by itself with an urgent alert, and `trader validate` refuses them. Their earlier trades never count: the gate, promotion and the live scoreboard start at the start date (the scoreboard can show them with its pre-start toggle).

**Any day, for free:** `trader replay --decider stub --days 2` (as trader) runs the classifiers through the real engine on historical bars with a stub decision model: no orders, no Jev cost. It's the quick plumbing check after any change, but it simulates fills, so only paper proves the broker side.

## Changing `config.yaml` later

Edit it on a branch of your data repository, re-render from a code checkout (`TRADER_DATA_ROOT=<data checkout> uv run trader config render-deploy --data-root <data checkout>`), open a pull request on the data repository, merge it with a merge commit, and deploy ([Deploy a change](../how-to/deploy.md)). The deploy shows you the whole config diff and refuses if the rendered files don't match. Then:
- **A schedule change** also needs the strategist's timers reinstalled, as trader: `bash /srv/trading/main/deploy/setup/2-strategist.sh` (the deploy reminds you; `check.sh` fails until you do). The runner timer is reinstalled by the deploy.
- **A dashboard users change** also needs `TRADER_DASHBOARD_USERS` in `/home/runner/.config/trading/services.env` updated to match, then a dashboard restart. That file is installed once and is never overwritten, and its value wins over `config.yaml`.
- **A start date change** takes effect at the runner's next session start.

[Configuration](../reference/configuration.md#when-a-change-takes-effect) lists every key.

## Taking updates

New code reaches your runner only when you deploy it. How you take upstream changes depends on the choice you made in step 0: see [Take updates](../how-to/take-updates.md), which also covers the rare change that needs an edit in your data repository.

## Starting over later

To restart the experiment (say after changing the design): pick a new start date in `config.yaml` and deploy it, reset the `strategist` branch's `state/` and `journal/` to the template (`templates/data/strategist/` in the code: copy its `state/` over yours and empty `journal/` and `logs/`, keeping the `.gitkeep` files), reset the paper balance and run `trader rebase-paper` (as runner, see [Use the controls](../how-to/controls.md)). The runtime's history (`/srv/trading/runtime`) isn't backed up anywhere; copy it off first if you want to keep it. Everything the strategist has written is in your data repository's history.

## The docs

These docs are published from the public code repository. Your data repository has none of its own, and a fork builds them like any checkout (see [Build the docs](../how-to/build-docs.md)):

```bash
uv run --group docs sphinx-build -W --keep-going docs build/html
```
