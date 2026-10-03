# Command line

Everything is one command, `trader`. How you run it depends on where you are:

- **As `trader` on the host** (the strategist, or you in its account): plain `trader <command>`.
  It's `~/.local/bin/trader`, which runs the deployed code from `trader`'s own virtual
  environment with the production paths set (see [Architecture](../explanations/architecture.md#the-strategist-runs-the-deployed-code)).
- **As `runner` on the host**: `uv run --no-dev trader <command>` in `/srv/trading/main`, with the
  services environment loaded. Run the commands that change runtime state (`run`, `stop`,
  `clear-halt`, `watchdog`, `hold-live`, `release-live`, `rebase-paper`, and
  `compact --scope runtime`) this way:

  ```bash
  sudo -iu runner
  cd /srv/trading/main
  set -a; . ~/.config/trading/services.env; set +a
  ```

  Without it, those commands refuse and change nothing, rather than writing to a stray
  `runtime/` directory in the checkout, and every command fails to find `config.yaml`, which
  isn't in the code checkout. (In an emergency, the dashboard's STOP button works without this.)
- **In a development checkout of the code**: `uv run trader <command>`, with `TRADER_DATA_ROOT`
  set to a data directory (see [Your first replay](../tutorials/first-replay.md)).

Defaults shown as `/srv/trading/strategist/...` are the standard install layout; in development
they are paths in your data directory.

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
