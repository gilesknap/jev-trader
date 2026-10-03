# Contributing

Contributions are welcome as issues and pull requests on
[gilesknap/jev-trader](https://github.com/gilesknap/jev-trader). Fork it, make your change on a
branch of your fork, and open a pull request against `main`. The maintainer merges pull requests
with a merge commit.

## Keep your data out

This repository is public and holds code only. **Never include anything from a data repository**
(yours or anyone's): no `config.yaml` values, journal text, trades, results, logins or hostnames,
in the code, a commit message, an issue or a pull request description. A strategist proposal you
are turning into a pull request is no exception: describe the change in your own words.

Keep any data directory you use for development outside the code checkout (the first-replay
tutorial uses `../my-data`), and check your diff before you push.

## Before you open a pull request

- Run the tests: `env -u TRADER_DATA_ROOT uv run --extra dev pytest -q`. A checkout of the code
  has no `config.yaml` of its own (once a follow-up removes the placeholder left over from before
  the split), so the tests run against a temporary copy of `templates/data/` (see
  `tests/conftest.py`), unless `TRADER_DATA_ROOT` is already set, in which case they use that
  directory: hence the `env -u`, in case you exported it for development. To run them against your
  own data checkout on purpose, set `TRADER_TEST_DATA_ROOT` to it.
- If you changed the docs, build them with warnings as errors:
  `uv run --group docs sphinx-build -W --keep-going docs build/html`.
- Keep changes to the guardrails (`src/trader/guardrails.py`, `src/trader/allocator.py`) and the
  go-live gate (`src/trader/golive.py`) small and well argued: they are the money-safety floor.
- Plain text only. The deploy tool refuses binary files and invisible or bidirectional Unicode
  characters, so use Mermaid or text SVG for diagrams, never images.

## What CI runs

Every pull request runs two GitHub Actions workflows, with a read-only token and no secrets (no
test needs keys):

- **CI** (`.github/workflows/ci.yml`): `uv sync --frozen --extra dev`, then `pytest -q` against
  the data template, on Python 3.12. Tests that need `bwrap` or `systemd-analyze` skip where those
  are missing.
- **Docs** (`.github/workflows/docs.yml`): the Sphinx build with warnings as errors. Merges to
  `main` publish the result to GitHub Pages.

## Changes that need owners to change their data

Every owner deploys this code against their own private data repository, which a merge here
doesn't touch. If your change needs owners to do something there, such as add a new required
`config.yaml` key, re-render because `deploy/templates/` changed, edit `services.env` by hand, or
add a file to their `strategist` branch, say so in a **"Data repository changes"** section of
the pull request description, with the exact steps. Update `templates/data/` to match, so new
owners start right. Those sections are the release notes owners read before they deploy.
