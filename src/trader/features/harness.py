"""Gate for strategist-authored features in features/custom/.

The security boundary is the sandbox (trader.features.sandbox): custom code only ever runs
in a bubblewrap worker with no network, no secrets and no runtime access. The static check
below is defence in depth that rejects obviously unsuitable code early:
1. Static check: exact-module import whitelist; no file/network/process/introspection access.
2. Evaluation (inside the sandbox): run over every minute of the supplied sessions; must not
   raise, must return finite floats for >=80% of bars after a 30-minute warm-up, and must
   average <5 ms per call.
"""

from __future__ import annotations

import ast
import importlib.util
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from trader import features as F

ALLOWED_MODULES = {"math", "statistics", "numpy", "pandas"}
ALLOWED_FROM = {"math": None, "statistics": None, "numpy": None, "pandas": None,
                "__future__": {"annotations"}, "trader.features": {"feature"}}
BANNED_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "dir",
    "getattr", "setattr", "delattr", "input", "breakpoint", "exit", "quit", "help",
    "type", "object", "memoryview", "super", "classmethod", "staticmethod", "property", "print",
}
# Attribute names that reach I/O, processes or module internals from numpy/pandas objects.
BANNED_ATTRS = {"io", "os", "sys", "lib", "ctypes", "compat", "util", "testing", "api", "core",
                "plotting", "errors", "system", "popen", "subprocess", "load", "loads", "save",
                "savez", "savetxt", "loadtxt", "genfromtxt", "memmap", "fromfile", "tofile",
                "DataSource", "show_versions", "eval", "query"}
ALLOWED_TO = {"to_numpy", "to_list", "tolist"}


@dataclass
class HarnessReport:
    ok: bool
    features: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    docs: dict[str, str] = field(default_factory=dict)


def static_check(path: Path) -> list[str]:
    problems = []
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_MODULES:
                    problems.append(f"import {a.name} not allowed (line {line})")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level or mod not in ALLOWED_FROM:
                problems.append(f"from {mod or '.'} import not allowed (line {line})")
            else:
                allowed = ALLOWED_FROM[mod]
                for a in node.names:
                    if (allowed is not None and a.name not in allowed) or a.name == "*" or a.name in BANNED_ATTRS \
                            or a.name.startswith(("_", "read_")):
                        problems.append(f"from {mod} import {a.name} not allowed (line {line})")
        elif isinstance(node, ast.Name) and (node.id in BANNED_NAMES or node.id.startswith("__")):
            problems.append(f"use of {node.id} not allowed (line {line})")
        elif isinstance(node, ast.Attribute):
            a = node.attr
            if a.startswith("_") or a in BANNED_ATTRS or a.startswith("read_") \
                    or (a.startswith("to_") and a not in ALLOWED_TO):
                problems.append(f"attribute .{a} not allowed (line {line})")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append(f"global/nonlocal not allowed (line {line})")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(("/", "http:", "https:", "file:")):
            problems.append(f"path/URL string literal not allowed (line {line})")
    return problems


def load_custom_inprocess(directory: Path) -> tuple[list[str], dict[str, str]]:
    """Import every custom feature file that passes the static check.

    Only the sandboxed worker may call this: it executes strategist-authored code."""
    loaded, errors = [], {}
    if not directory.exists():
        return loaded, errors
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        problems = static_check(path)
        if problems:
            errors[path.name] = "; ".join(problems)
            continue
        before = set(F.REGISTRY)
        lib_names = {n for n, src in F.SOURCES.items() if src == "lib"}
        try:
            spec = importlib.util.spec_from_file_location(f"custom_features.{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            errors[path.name] = f"import failed: {e!r}"
            continue
        new = sorted(set(F.REGISTRY) - before)
        clobbered = sorted(n for n in lib_names if F.SOURCES.get(n) != "lib")
        if clobbered:  # reject the whole file; library features are computed outside the sandbox
            errors[path.name] = f"redefines library features {clobbered}"
            for n in new:
                F.REGISTRY.pop(n, None)
            continue
        for name in new:
            F.SOURCES[name] = path.name
            loaded.append(name)
    return loaded, errors


def _session_clock(bars: pd.DataFrame) -> tuple[list[float], float]:
    """Each bar's minutes_since_open (1 for the 09:30 bar) and the session's length in minutes,
    from the sample's own last bar (samples are cut at their close: 210 for a 13:00 close, 390
    for a full session). Worked out once per session, never per bar."""
    if bars.empty:
        return [], 390.0
    first = bars.index[0]
    open_ = first.replace(hour=9, minute=30, second=0, microsecond=0, nanosecond=0)
    minute = [float(m) for m in (bars.index - open_) // pd.Timedelta(minutes=1) + 1]
    return minute, minute[-1]


# The gate's speed limit: mean wall-clock seconds per call. Machine load counts against it, so the
# tests relax it (conftest) except in the one test about it.
SPEED_BUDGET_S = 0.005


def evaluate(names: list[str], sessions: list[tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]],
             budget_s: float | None = None) -> dict[str, str]:
    """sessions: (bars, prev_day, spy) per day. Returns name -> error for failures. `budget_s`:
    the speed limit, SPEED_BUDGET_S by default (the sandbox passes the runner's)."""
    budget_s = SPEED_BUDGET_S if budget_s is None else budget_s
    errors = {}
    for name in names:
        fn = F.REGISTRY[name]
        finite = total = 0
        elapsed = 0.0
        try:
            for bars, prev, spy in sessions:
                minute, length = _session_clock(bars)
                for i in range(30, len(bars)):
                    # As the engine sees bar i at the tick after it: minute 1 is the 09:30 bar.
                    ctx = F.FeatureContext(prev, spy.iloc[: i + 1], minute[i], length - minute[i])
                    t = time.perf_counter()
                    v = fn(bars.iloc[: i + 1], ctx)
                    elapsed += time.perf_counter() - t
                    total += 1
                    if isinstance(v, (int, float)) and math.isfinite(v):
                        finite += 1
        except Exception as e:
            errors[name] = f"raised {e!r}"
            continue
        if total == 0:
            errors[name] = "no bars to evaluate"
        elif finite / total < 0.8:
            errors[name] = f"only {finite}/{total} finite values"
        elif elapsed / total > budget_s:
            errors[name] = f"too slow: {elapsed / total * 1000:.1f} ms/call"
    return errors


def run_gate(directory: Path, sessions, alert=lambda lvl, msg: None) -> HarnessReport:
    """Start the sandbox, gate the custom features in it, and install it as F.SANDBOX."""
    from trader.features.sandbox import FeatureSandbox

    if F.SANDBOX is not None:
        F.SANDBOX.close()
    sb = FeatureSandbox(directory, alert)
    sb.start(sessions)
    F.SANDBOX = sb
    errors = dict(sb.errors)
    if sb.broken:
        errors["_sandbox"] = sb.broken
    return HarnessReport(ok=not errors, features=sorted(sb.names), errors=errors, docs=sb.docs)
