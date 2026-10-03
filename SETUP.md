# Setting up your own copy

This guide takes you from nothing to a runner trading on paper, on your own VPS, from your own private copy of this repo. It covers the accounts you need and the order to do things in. [README.md](README.md) covers the architecture and day-to-day operations; this guide doesn't repeat them.

Plan on an evening for steps 1–7, then a few days of paper before the experiment starts (step 8).

What you'll end up with:
- **A VPS** that you reach by SSH and whose dashboard you open over your private Tailscale network. The VPS itself can't open connections to your other devices.
- **Three accounts on it:** your admin account (sudo), `trader` (Claude, the strategist, paper keys only) and `runner` (the trading daemon, live keys, no sudo).
- **Five external services:** Alpaca (the broker), OpenRouter (the Jev decision model), Claude (the strategist), GitHub (the repo and the strategist's memory) and ntfy (alerts on your phone).

Items marked **TODO** weren't verified when this was written; check them as you go.

## 0. Your copy of the repo and `config.yaml`

1. Create a **private** repository on GitHub and push the snapshot you were given into it (`main` branch). Keep it private: the strategist's journal and your trading results end up in it.
2. Edit `config.yaml` at the repo root. It holds everything specific to one deployment. Read the comments in the file itself for what each key does: that file, not this guide, is the reference. The keys you must change:
   - `owner.name` and `owner.github_repo` (`you/your-repo`): the setup scripts clone from it.
   - `dashboard.users`: your Tailscale login(s). **Empty means nobody can open the dashboard.**
   - `experiment.start_date`: see point 3.
   - `schedule.*` if you aren't in the UK. `local_tz` is your clock, and `runner_start` and the `strategist` timer specs (systemd `OnCalendar` syntax) are in it. Keep `runner_start` safely before 09:30 New York in every daylight-saving week, and the post-close timer after 16:00 New York.

   Then regenerate the files that can't read YAML (the runner timer, the strategist timers and `services.env`), and commit everything to `main`:
   ```bash
   uv sync --extra dev                    # on your laptop, in a checkout of your repo
   uv run trader config render-deploy     # rewrites deploy/systemd/trader-runner.timer, deploy/systemd/trader.env, deploy/systemd-trader/*.timer
   uv run --extra dev pytest -q           # a test fails if they don't match config.yaml
   git commit -am "Configure for <you>" && git push
   ```
   A malformed `config.yaml` (an unknown key, a bad date, an unquoted time) stops every `trader` command with a message naming the file, so a typo can't slip through quietly.
3. Start the experiment fresh:
   - For now, set `experiment.start_date` to any date comfortably after your install (a few weeks out). You'll set the real one in step 8, when you install the pre-launch pack. The go-live gate and the scoreboard ignore everything before it.
   - Empty `journal/` and `logs/` (keep the `.gitkeep` files): `logs/` holds the previous owner's `trades.csv`, equity and cashflow files, `probe_report.json` and decision logs, which would otherwise be read as your history. Then reset `state/`: `strategy.md` to a short "Phase: pre-launch, observe starts <date>" note, `watchlist.md` to empty, and `classifiers.yaml` to `control_orb` only (copy it from `deploy/prelaunch/classifiers.yaml` and delete the other rules, or use the whole pack for step 8).
   - Leave `config/universe.yaml` and `config/mode.yaml` (`paper`) as they are unless you mean to change them.
4. Create the `strategist` branch from `main` and push it. The strategist commits only there.
5. Create a label called `needs-human` (Issues → Labels). The strategist uses it when it needs something from you.

## The admin account and Claude Code

You'll do setup and maintenance from a personal admin account with sudo (step 1 creates it). Running Claude Code in that account is a good way to do it: it can read this guide, run the scripts and fix what goes wrong. Two things to be clear about first.

**Use auto mode, not "skip permissions".** Start it with `claude --permission-mode auto` (or press Shift+Tab to cycle to it, or put `{"permissions": {"defaultMode": "auto"}}` in `~/.claude/settings.json`). In auto mode a classifier reviews each action before it runs and blocks the dangerous kinds: sending data off the box, `curl … | bash`, force-pushes and `git reset --hard`, mass deletion, granting permissions, production deploys (so expect it to stop at `trading-deploy`: run that yourself). It's the default mode in recent Claude Code (2.1.283 and later). It needs a recent model (Opus or Sonnet 4.6 or later, or Fable). See [permission modes](https://code.claude.com/docs/en/permission-modes.md#eliminate-prompts-with-auto-mode). Don't use `--dangerously-skip-permissions` here.

**Be honest about what that account can do.** With sudo it can read the live Alpaca keys and place orders directly, outside the runner and its guardrails. What bounds the damage:
- Alpaca API keys can trade but can't withdraw money.
- `trading-deploy` deploys GitHub-signed merges without a diff review; anything else makes you read the diff.
- The one-way tailnet (step 2): the VPS can't reach your other devices.

So:
- Run Claude in the admin account only in sessions you're watching, never unattended.
- Add the live keys last, after setup works on paper.
- Keep the live balance small.
- Turn on 2FA for Alpaca, GitHub and OpenRouter, and give the OpenRouter key a spend limit.
- While live keys are on the box, be careful what untrusted content you have it read (web pages, issues, files from elsewhere): that's how instructions get smuggled in.

**The strategist is different on purpose.** `trader` runs Claude headless with `bypassPermissions` (step 5), because the account itself is the fence: no sudo, no live keys, no write access to the deployed code or the runtime, and no way to reach the dashboard. One gap: its GitHub token can merge PRs through the API, and those merges are GitHub-signed too. So every time you deploy, check that the PR list `sudo -u runner trading-deploy --dry-run` prints holds only PRs you reviewed.

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
sudo apt install -y git gh gpg bubblewrap curl openssl util-linux
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh   # the units expect /usr/local/bin/uv
```
- `bubblewrap` sandboxes the strategist's feature code. On Ubuntu 24.04+ it needs AppArmor's `bwrap-userns-restrict` profile, which the `apparmor` package ships; `check.sh` tests that the sandbox works.
- `uv` installs the right Python for the project by itself.
- `gpg` lets `trading-deploy` check that merge commits were signed by GitHub.

## 2. Tailscale, one way

The dashboard (with its STOP button) is served only on your tailnet, and the VPS should be able to answer your devices but not reach them. A trading box that runs AI-written code shouldn't be a way into your laptop.

1. Make a Tailscale account (free) and install Tailscale on your laptop and phone.
2. In the admin console → Access controls, replace the default allow-all policy with something like this (swap in your login):
   ```jsonc
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

**How the dashboard is protected.** `3-runner.sh` runs `tailscale serve` to proxy `https://<vps>:8444` to a Unix socket only `runner` can reach. Tailscale adds a `Tailscale-User-Login` header naming who is connecting, and the dashboard lets in only the logins listed in `config.yaml` (the dashboard users). A request without that header is refused. The one exception is `TRADER_DASHBOARD_ALLOW_LOCAL=1`, which lets header-less requests in. It exists for local development only (see README). **Never set it on the Tailscale-served dashboard**, not even "temporarily" in a systemd drop-in. `tailscale serve` sends no login header for requests from tagged devices, and the VPS itself is tagged, so any process on the VPS (the strategist included) could reach the dashboard and press STOP. To use the dashboard from a machine without Tailscale, use the SSH tunnel (step 2b), the only unit where that setting belongs.

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
5. Reset the paper account's balance to about what you'll fund live with (the original uses $250), so paper sizing matches reality. After any later paper reset, run `trader rebase-paper` (README → Controls).
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
The wrapper unsets `ANTHROPIC_API_KEY`, so runs can only use the subscription. Headless runs can't answer permission prompts, so `~trader/.claude/settings.json` needs:
```json
{ "permissions": { "defaultMode": "bypassPermissions" }, "skipDangerousModePermissionPrompt": true }
```
That's why the strategist runs as its own account with paper keys only: what it can reach is limited by the OS, not by prompts. If the login expires, runs fail and you get an alert; log in again the same way. **TODO:** how long a subscription login lasts before it needs renewing.

**GitHub (trader).** Create a fine-grained personal access token for **your repo only**, with *Contents*, *Pull requests* and *Issues* set to read and write, and an expiry you'll notice (housekeeping warns 3 weeks ahead). Then as trader:
```bash
gh auth login          # GitHub.com → HTTPS → paste the token
gh auth setup-git      # so git push uses it
git config --global user.name "strategist" && git config --global user.email "<your noreply address>"
```
The token lets the strategist push its branch and open PRs and issues. It could technically push to `main` too (branch protection isn't available on private repos on the free plan), which is why nothing runs until **you** deploy (README → Deploying).

**GitHub (runner).** `runner` gets a **read-only deploy key** in step 6. It only ever pulls `main`.

**Merging.** Merge PRs in GitHub's web UI with **"Create a merge commit"**. Those merge commits are signed by GitHub, and `trading-deploy` checks that signature (against `deploy/github-web-flow.gpg`): signed merges deploy after a PR list and the tests, and anything else makes you read the diff and type `yes`.

**ntfy.** Install the ntfy app on your phone. Step 6 generates a random topic name (`NTFY_TOPIC` in the strategist's `.env`); subscribe to exactly that name. Alerts go to the public ntfy.sh server, so the random name is what keeps them private: don't share it.

## 6. Install

Follow the table in [README.md → One-time setup](README.md#one-time-setup): step 0 (clone as trader), 1 (`1-host.sh`), 2 (`2-strategist.sh`, from a fresh trader login), 3a (`3-runner.sh key`, then add the printed key under your repo → Settings → Deploy keys, **read-only**), 3b (`3-runner.sh install`). Between 2 and 3b:

- Fill in `/srv/trading/strategist/.env`: paper keys and the OpenRouter key.
- Subscribe to the printed ntfy topic.

After 3b, as admin:
```bash
sudo -u runner -H nano /home/runner/.config/trading/env        # add ALPACA_LIVE_KEY / ALPACA_LIVE_SECRET
sudo bash /srv/trading/main/deploy/setup/check.sh               # every line should read PASS
```
The setup scripts read the repo and the Tailscale port from `config.yaml` (through `deploy/setup/cfg.sh`), so they clone your repo, not the original.

`3-runner.sh install` prints the dashboard's address. Open it from your laptop; you should see "The runner isn't trading right now".

## 7. First day

- **Before the session:** `uv run trader session` (as trader, in `/srv/trading/strategist`) shows today's session times. `uv run trader validate` should say `classifiers OK`.
- **At the runner's start** (the timer, shortly before the US open): an ntfy "runner started session …". The dashboard's banner switches to "The runner is trading now".
- **During the session:** watch `decision_errors` and `probe_errors` in the dashboard's At a glance panel. A few Jev timeouts a day pause decisions for 5 minutes each and are harmless; a steady stream means a key or credit problem.
- **After the close:** a Daily P&L ntfy, then the strategist's post-close run commits a journal to the `strategist` branch (check the branch on GitHub).
- **Saturday:** the weekly run opens a PR from `strategist` to `main`. Reviewing and merging it is your weekly job (README → Daily operations).

If something doesn't happen, the logs are in `/srv/trading/runtime/alerts.log` (runner), `/srv/trading/strategist/strategist-alerts.log` (the strategist wrapper's and housekeeping's alerts), `journalctl --user` as runner, and `~trader/.local/state/trader/` for the strategist's runs (one log per run).

## 8. Pre-launch (1–2 trading days)

Before the experiment starts, push real orders through every execution path on paper, so a bug shows up while nothing counts.

1. Set `experiment.start_date` in `config.yaml` to **2–3 trading days after the first pre-launch session** (a Monday is tidiest), then render, merge and deploy (see "Changing `config.yaml` later"). This is the one real start date: the pack runs on the days before it.
2. As trader, on the `strategist` branch, copy the pack into place, set its `date:` to the next session, validate and push:
   ```bash
   sudo -iu trader
   cd /srv/trading/strategist && git pull
   cp deploy/prelaunch/classifiers.yaml state/classifiers.yaml
   sed -i 's/^date: .*/date: YYYY-MM-DD/' state/classifiers.yaml   # the next session's date
   uv run trader validate                                          # must end "classifiers OK"
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

**Any day, for free:** `uv run trader replay --decider stub --days 2` runs the classifiers through the real engine on historical bars with a stub decision model: no orders, no Jev cost. It's the quick plumbing check after any change, but it simulates fills, so only paper proves the broker side.

## Changing `config.yaml` later

Edit it, run `uv run trader config render-deploy`, commit through a PR, merge and deploy (README → Deploying). Then:
- **A schedule change** also needs the strategist's timers reinstalled, as trader: `bash /srv/trading/strategist/deploy/setup/2-strategist.sh` (after the strategist checkout has merged `main`; `check.sh` fails until you do). The runner timer is reinstalled by the deploy.
- **A dashboard users change** also needs `TRADER_DASHBOARD_USERS` in `/home/runner/.config/trading/services.env` updated to match, then a dashboard restart. That file is installed once and is never overwritten, and its value wins over `config.yaml`.
- **A start date change** takes effect at the runner's next session start.

## Starting over later

To restart the experiment (say after changing the design): pick a new start date in `config.yaml`, reset `state/` and `journal/` as in step 0, reset the paper balance and run `trader rebase-paper`, and deploy. The runtime's history (`/srv/trading/runtime`) isn't backed up anywhere; copy it off first if you want to keep it. Everything the strategist has written is on GitHub.
