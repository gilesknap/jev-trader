# Data repo template

What a new owner's private data repo starts with (#169). `deploy/setup/0-data.sh` builds it:

- `main/` → the data repo's `main` branch (human-owned deployment config). The deploy files rendered
  from `config.yaml` are not templated: `0-data.sh` renders them.
- `strategist/` → the data repo's orphan `strategist` branch (state, journal, logs, custom features,
  proposals). `strategist/CLAUDE.md.template` is installed there as `CLAUDE.md`. It is kept under
  another name here so Claude Code never loads it as a nested memory file in a checkout of this repo.

The tests run against these files when the code checkout has no `config.yaml` (see `tests/conftest.py`).
