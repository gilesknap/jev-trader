# Contributing

Contributions are welcome as issues and pull requests on
[gilesknap/jev-trader](https://github.com/gilesknap/jev-trader).

Before you open a pull request:

- Run the tests: `uv run --extra dev pytest -q`.
- If you changed the docs, build them with warnings as errors:
  `uv run --group docs sphinx-build -W --keep-going docs build/html`.
- Keep changes to the guardrails (`src/trader/guardrails.py`, `src/trader/allocator.py`) and the
  go-live gate (`src/trader/golive.py`) small and well argued: they are the money-safety floor.
- Plain text only. The deploy tool refuses binary files and invisible or bidirectional Unicode
  characters, so use Mermaid or text SVG for diagrams, never images.
