# Command line

Everything is one command, `trader`, run through uv from a checkout: `uv run trader <command>`.
On the production host, run the commands that change runtime state (`run`, `stop`,
`clear-halt`, `watchdog`, `hold-live`, `release-live`, `rebase-paper`, and
`compact --scope runtime`) as `runner` in `/srv/trading/main`, with the services environment
loaded:

```bash
set -a; . ~/.config/trading/services.env; set +a
```

Without it, those commands refuse and change nothing, rather than writing to a stray `runtime/`
directory in the checkout. (In an emergency, the dashboard's STOP button works without this.)

Defaults shown as `/srv/trading/strategist/...` are the standard install layout; in a development
checkout they are paths in the checkout itself.

Exit codes worth knowing: `session` exits 2 when the market is closed today; `validate` exits 1
if any custom feature or classifier is rejected; `deploy-plan` exits 3 when something needs a
diff review and 2 on error; `config render-deploy --check` exits 1 when the generated files are
out of date.

```{eval-rst}
.. argparse::
   :module: trader.cli
   :func: build_parser
   :prog: trader
```
