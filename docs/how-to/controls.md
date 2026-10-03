# Use the controls

Run these as `runner` in `/srv/trading/main`, with the services environment loaded:

```bash
sudo -iu runner
cd /srv/trading/main
set -a; . ~/.config/trading/services.env; set +a
```

Without it, the commands that change state (`stop`, `rebase-paper`, `clear-halt`, `hold-live`,
`release-live`) refuse and change nothing, rather than writing to a stray `runtime/` directory in
the checkout. (The services environment also names the config checkout, `TRADER_DATA_ROOT`;
without it no command finds `config.yaml`.)

## STOP

From the dashboard, or:

```bash
uv run trader stop
```

STOP flattens every book (paper, live and every sim account) and blocks new entries for the rest
of the trading day. If the runner looks dead (no heartbeat for 3 minutes), it also flattens the
paper and live accounts directly through Alpaca. A STOP expires at the next trading day.

## Clear a halt

A halt (NAV 30% below its high-water mark, or equity under $50) never clears itself:

```bash
uv run trader clear-halt live     # or: paper, sim/<classifier id>
```

Clearing it also rebases the high-water mark to the current NAV, so it doesn't re-trigger at once.
It's refused while the runner is running (the runner holds NAV in memory and would overwrite the
change): run it after the close. If the live book halted, go-live was demoted to paper; re-arm it
with `trader release-live` once you've cleared the halt. Clearing a live halt also restarts the
half-size week: the next 5 live sessions trade at half size.

## After resetting the Alpaca paper account

```bash
uv run trader rebase-paper
```

This restarts the paper book's NAV at 1.0 from its next mark and clears any halt, so the reset
isn't read as a drawdown. Run it outside the session. Reset the paper balance to about what you
fund live with, so paper sizing matches reality.

## Force paper or live

Set `config/mode.yaml` on your data repository's `main` to `paper` or `live` (or back to
`auto`), through a pull request, merge it, and deploy. The deploy shows the change in full and
needs your `yes`. The override wins over the automatic go-live.

## Withdraw profits

Use Alpaca's dashboard. While the account is live, the runner picks the transfer up from Alpaca's
account activities at the next session start and redeems NAV units, so it doesn't count as a loss.
Only you move money; the code never does.
