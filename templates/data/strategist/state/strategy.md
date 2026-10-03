# Strategy

**Phase:** pre-launch. The observe phase starts on `experiment.start_date` in `config.yaml`.

## Current beliefs
None yet. The observe phase exists to form them from evidence rather than priors.

## Running
- `control_orb` (shadow, control): opening-range breakout on SPY/QQQ. It's a benchmark, not a strategy.
- `probe_universe_baseline` (probe): a generic yardstick for later hypothesis-specific probes.
- The pre-launch `test_*` plumbing rules (docs/tutorials/installation.md, "Pre-launch"); they stop
  running on the start date.
