# Understand the alerts

Alerts go to your phone through [ntfy](https://ntfy.sh) (`alerts.ntfy_server` in `config.yaml`,
with the secret topic `NTFY_TOPIC` from the secrets file), as `urgent` (high priority) or `info`.
Every alert is also logged:

- the runner's, the watchdog's and the dashboard's to `/srv/trading/runtime/alerts.log`;
- the strategist's (the wrapper's failed runs, pushes and merges, and housekeeping's) to
  `/srv/trading/strategist/strategist-alerts.log` (0640, git-ignored), because `trader` can't
  write the runtime directory by design.

If `config.yaml` can't be read or is invalid, alerts aren't pushed anywhere, since the file that
names your server is the thing that's broken. They are still written to the logs above, marked
`(not pushed: config.yaml unreadable)`, and the watchdog logs hourly that the file is unreadable,
so check the logs if your phone goes quiet.

## Use your own ntfy server

To push to a self-hosted ntfy server instead of ntfy.sh:

1. Set `alerts.ntfy_server` in `config.yaml` to its `https://` URL and deploy the change.
2. Set `NTFY_TOPIC` in both secrets files (the strategist's `.env` and runner's `env`) to the
   topic you subscribe to on that server.
3. If the server refuses anonymous publishing, create an access token there that may write to
   that topic (`ntfy token add`, plus `ntfy access` for its user), and add it to both secrets
   files as `NTFY_TOKEN`. Alerts then carry `Authorization: Bearer <token>`.
4. Subscribe to the topic on that server in the ntfy app, with a login that may read it if the
   server refuses anonymous reading too.

Without `NTFY_TOKEN` alerts are published anonymously, which is what ntfy.sh's public topics
need. The token is never written to a log, and it is sent only to the server `config.yaml`
names.

## What pages you

The runner alerts when something needs a human or changes the money picture, for example:

- the kill switch, a halt, STOP;
- a failing decision model (and its recovery), a stale market-data feed;
- an order with no clear answer, a refused entry, a position closed outside the engine, a position
  still held after a flatten, an untracked position sold;
- an invalid classifier file or a rejected custom feature (nothing, or less, trades that day);
- go-live: armed, each veto session, disarmed, going live, demoted;
- the session start and the **Daily P&L** summary after the close.

Recurring conditions are throttled, so a persistent failure doesn't flood your phone. Alerts about
sim accounts are info-level.

## The watchdog

`trader watchdog` runs every 10 minutes on weekdays. It alerts if:

- the runner's heartbeat is more than 5 minutes old during market hours (at most hourly);
- no post-close strategist run has succeeded for the latest session by its deadline (once per
  session);
- Alpaca's calendar can't be read, so those checks were skipped;
- `config.yaml` can't be read or is invalid, so the post-close check was skipped and the runner
  won't start a new session until it's fixed (at most hourly).

## Housekeeping

A daily `trader housekeeping` run (through `scripts/strategist.sh housekeeping`) alerts when:

- **OpenRouter credit** is under $3, or will run out within 21 days at the current burn rate;
- the OpenRouter key's spend limit is nearly used, or the key expires soon;
- `trader`'s **GitHub token** expires within 21 days, or stops working;
- `trader`'s **Claude login** expires within 21 days, or can't be found (renew it with
  `claude auth login` as trader);
- **merged changes haven't been deployed** for 48 hours: on the code repository's `main` (read
  anonymously, over HTTPS), or on your data repository's `main` (the deployment config), each
  alerted separately;
- **disk** has less than 3 GB free.

Each repeats weekly, then daily once it's within 7 days.
