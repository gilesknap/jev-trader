# Features

A feature is a pure function of today's 1-minute bars for one symbol (up to the current minute)
and a small context: the previous session's bars, today's SPY bars and the session clock. Every
value is dimensionless (percentages, ratios, z-scores), so the decision model never sees absolute
prices or dates. A feature returns NaN when there isn't enough data yet; a trigger on a NaN
feature never holds, and the model sees it as `null`.

`trader features` (as `trader`; `uv run trader features` in a development checkout) lists the library features and any custom features that passed the gate
(it reads recent Alpaca bars for the gate, so it needs Alpaca keys).

## Library features

Generated from `src/trader/features/lib.py`:

```{include} ../_generated/features.md
```

Raw volume differs in level between live IEX bars and the SIP bars replays use, so volume
features should be ratios. The prior session's bars (`ctx.prev_day`) carry SIP's prices (the
official close) and the volume of the same feed as today's bars, so `rel_volume_15m` compares
like with like both live and in replay.

## Custom features

The strategist can add its own in `features/custom/*.py`:

```python
import numpy as np
from trader.features import feature

@feature("my_feature_name", source="custom")
def my_feature(bars, ctx):
    """One-line description (shown in `trader features`)."""
    # bars: today's 1-min regular-session bars up to now (open, high, low, close, volume)
    # ctx.prev_day, ctx.spy, ctx.minutes_since_open, ctx.minutes_to_close
    return float(...)   # dimensionless; NaN if not enough data
```

Live IEX bars are sparse (a minute without an IEX trade has no bar), so select windows by
timestamp rather than counting rows: the library's minute-named features (`ret_*m_pct`, `or15_*`,
`or30_*`, `rel_volume_15m`) use elapsed exchange time, and its bar-named indicators (`rsi_14`,
`atr_14_pct`, ...) count bars. `ctx.minutes_since_open` is 1 for the 09:30 bar, live and in the gate.

Rules, enforced by the gate (`src/trader/features/harness.py`) and the sandbox:

- They run only in a bubblewrap sandbox: no network, a cleared environment, a private `/tmp`,
  and only `/usr`, `/etc`, the code checkout and a runner-owned copy of the feature files mounted,
  all read-only (no home directories, runtime directory or secrets).
- Allowed imports: `import math`, `statistics`, `numpy` or `pandas`; `from <one of those> import
  <name>` (not `*`, and not a name starting with `_` or `read_`, or a banned attribute below);
  `from __future__ import annotations`; and `from trader.features import feature`.
- Also rejected: names such as `open`, `print`, `eval`, `exec`, `type`, `getattr`; attributes
  that reach I/O or module internals (`.io`, `.os`, `.lib`, `.load`, `.save` and others);
  attributes starting with `_`, `read_` or `to_` (except `to_numpy`, `to_list` and `tolist`);
  `global` and `nonlocal`; and path or URL string literals.
- A custom feature may not reuse a library feature's name.
- Over recent SPY and QQQ sessions, each must not raise, must return a finite value on at least
  80% of bars after a 30-minute warm-up, and must average under 5 ms per call.
- Files are real files: the runner doesn't follow symlinks, and rejects a symlinked feature file.
  Files starting with `_` are ignored.

The runner re-checks every custom feature at each session start. A rejected feature makes every
classifier that uses it invalid, and an invalid classifier file means nothing trades that day, so
always run `trader validate` after an edit.
