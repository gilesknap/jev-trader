"""Runs strategist-authored features in a bubblewrap sandbox, never in the caller's process.

The runner holds live keys and owns the go-live state, so it must never import code the
strategist wrote. Instead one long-lived worker (`trader.features.worker`) runs under
bwrap with: no network, a cleared environment, a private /tmp, and only /usr, /etc, the
code checkout, the running interpreter's environment and a runner-owned copy of the
custom-feature files mounted read-only (no /home, no runtime dir, no secrets). The worker
loads and gates the features, then answers compute requests over a JSON-lines pipe.

Failure is closed: if the worker dies, times out or can't start, every custom feature
returns NaN for the rest of the session (triggers fail, so no entries; stops still work).

There is deliberately no in-process fallback: without bwrap, custom features are disabled.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from trader import config
from trader.features.barcodec import decode_bars, encode_bars  # noqa: F401  (decode_bars: re-export)
from trader.safeio import read_sources

REQUEST_TIMEOUT_S = 5.0
GATE_TIMEOUT_S = 180.0


class SandboxError(Exception):
    pass


def _refuse_unsafe_bind(path: Path) -> None:
    """Raise unless binding `path` read-only keeps home, the runtime dir and secrets hidden."""
    if path == Path("/"):
        raise SandboxError(f"refusing to bind {path} into the feature sandbox")
    protected = [Path.home(), Path(config.RUNTIME_DIR), *map(Path, config.SECRETS_FILES)]
    for p in protected:
        if p.resolve().is_relative_to(path):
            raise SandboxError(f"refusing to bind {path} into the feature sandbox: it would expose {p}")


def bwrap_cmd(custom_dir: Path, code_root: Path = config.CODE_ROOT,
              prefix: Path | None = None, base_prefix: Path | None = None) -> list[str]:
    """The sandbox command line. Only what Python and the features need is visible.

    The worker runs on the caller's own interpreter environment (`sys.prefix`), so the
    strategist's venv works as well as the runner's, wherever it lives (#169). That venv is
    bound read-only only when it isn't already visible (under `code_root` or /usr), and so is
    its base interpreter (`sys.base_prefix`, where the venv's `bin/python` symlink and the
    stdlib point). `prefix`/`base_prefix` exist for tests.

    Fail-closed floor: a bind that would expose `/`, the home directory, the runtime dir or
    a secrets file (the bind is an ancestor of, or equal to, one of them) raises
    SandboxError, which `start()` turns into "custom features disabled".
    """
    code_root, custom_dir = Path(code_root).resolve(), Path(custom_dir).resolve()
    prefix = Path(sys.prefix if prefix is None else prefix).resolve()
    base_prefix = Path(sys.base_prefix if base_prefix is None else base_prefix).resolve()
    cmd = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all", "--clearenv",
           "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
           "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for link in ("bin", "lib", "lib64", "sbin"):  # merged-/usr layout
        p = Path("/") / link
        if p.is_symlink():
            cmd += ["--symlink", os.readlink(p), str(p)]
        elif p.exists():
            cmd += ["--ro-bind", str(p), str(p)]
    cmd += ["--ro-bind", str(code_root), str(code_root)]
    visible = [Path("/usr"), code_root]
    for env in (prefix, base_prefix):
        if env.is_dir() and not any(env.is_relative_to(v) for v in visible):
            _refuse_unsafe_bind(env)
            cmd += ["--ro-bind", str(env), str(env)]
            visible.append(env)
    if custom_dir.exists() and not custom_dir.is_relative_to(code_root):
        cmd += ["--ro-bind", str(custom_dir), str(custom_dir)]
    cmd += ["--setenv", "PATH", "/usr/bin", "--setenv", "HOME", "/tmp",
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--setenv", "OMP_NUM_THREADS", "1",
            "--chdir", "/tmp"]
    python = str(prefix / "bin" / "python")
    if not Path(python).exists():
        python = sys.executable
    return cmd + [python, "-I", "-m", "trader.features.worker"]


class FeatureSandbox:
    """Client for the sandboxed worker. `names` are the custom features that passed the gate."""

    def __init__(self, custom_dir: Path, alert: Callable[[str, str], None] = lambda lvl, msg: None,
                 gate_timeout: float = GATE_TIMEOUT_S):
        self.custom_dir = Path(custom_dir)
        self.gate_timeout = gate_timeout
        self.alert = alert
        self.names: set[str] = set()
        self.docs: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self.broken: str | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._seq = 0

    # ---- lifecycle ---------------------------------------------------------------

    def start(self, samples: list[tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]]) -> None:
        """Start the worker, load and gate the custom features against sample sessions."""
        try:  # symlinked files are refused one by one; a symlinked directory disables them all
            sources, self.errors = read_sources(self.custom_dir)
        except (OSError, ValueError) as e:
            self._fail(f"custom features disabled: {e}")
            return
        if not any(not name.startswith("_") for name in sources):
            return  # nothing to run
        if not shutil.which("bwrap"):
            self._fail("bwrap not installed: custom features disabled")
            return
        # The worker loads a runner-owned copy of exactly what was just read, never the
        # strategist's path, so nothing swapped in there afterwards reaches the sandbox (#49).
        try:
            with tempfile.TemporaryDirectory(prefix="trader-features-") as snapshot:
                for name, source in sources.items():
                    (Path(snapshot) / name).write_bytes(source)
                self._proc = subprocess.Popen(
                    bwrap_cmd(Path(snapshot)), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, bufsize=1,
                )
                rep = self._call({"op": "load", "dir": str(Path(snapshot).resolve()),
                                  "samples": [[encode_bars(b), encode_bars(p), encode_bars(s)] for b, p, s in samples]},
                                 timeout=self.gate_timeout, expect="features")
        except (OSError, SandboxError) as e:  # e.g. a full /tmp: this alert, and library features still trade
            self._fail(f"custom-feature gate failed: {e}")
            return
        for w in rep.get("warnings", []):
            self.alert("urgent", f"custom-feature sandbox: {w}")
        self.names = set(rep.get("features", []))
        self.docs = rep.get("docs", {})
        self.errors |= rep.get("errors", {})

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
        self._proc = None

    # ---- requests ----------------------------------------------------------------

    def compute(self, names: list[str], bars: pd.DataFrame, ctx) -> dict[str, float]:
        nan = {n: float("nan") for n in names}
        if self.broken or not names:
            return nan
        req = {"op": "compute", "names": names, "bars": encode_bars(bars), "prev": encode_bars(ctx.prev_day),
               "spy": encode_bars(ctx.spy), "mso": ctx.minutes_since_open, "mtc": ctx.minutes_to_close}
        try:
            vals = self._call(req, timeout=REQUEST_TIMEOUT_S, expect="values")["values"]
        except SandboxError as e:
            self._fail(f"custom-feature worker failed mid-session: {e}")
            return nan
        out = {}
        for n in names:
            v = vals.get(n)
            out[n] = float(v) if isinstance(v, (int, float)) and math.isfinite(v) else float("nan")
        return out

    def _call(self, req: dict, timeout: float, expect: str) -> dict:
        with self._lock:
            self._seq += 1
            # A fresh random nonce per request, echoed in the reply: code in the worker that
            # got at the protocol pipe still couldn't forge a reply by guessing the sequence id.
            nonce = secrets.token_hex(16)
            req = req | {"id": self._seq, "nonce": nonce}
            p = self._proc
            if p is None or p.poll() is not None:
                raise SandboxError("worker not running")
            result: list = []

            def talk():
                try:
                    p.stdin.write(json.dumps(req, separators=(",", ":")) + "\n")
                    p.stdin.flush()
                    result.append(p.stdout.readline())
                except (OSError, ValueError) as e:
                    result.append(e)

            t = threading.Thread(target=talk, daemon=True)
            t.start()
            t.join(timeout)
            if t.is_alive() or not result or isinstance(result[0], Exception) or not result[0]:
                self.close()
                raise SandboxError("timed out" if t.is_alive() else f"no reply ({result[0] if result else 'none'})")
            try:
                reply = json.loads(result[0])
            except ValueError as e:
                self.close()
                raise SandboxError(f"bad reply: {e}") from e
            if not isinstance(reply, dict) or reply.get("id") != self._seq:
                self.close()
                raise SandboxError("reply out of sequence")
            if not hmac.compare_digest(str(reply.get("nonce", "")), nonce):
                self.close()
                raise SandboxError("reply nonce mismatch")
            if "error" in reply:
                raise SandboxError(reply["error"])
            if expect not in reply:
                self.close()
                raise SandboxError(f"reply missing {expect!r}")
            return reply

    def _fail(self, msg: str) -> None:
        if not self.broken:
            self.broken = msg
            self.alert("urgent", msg + " (custom features return NaN, so dependent entries are blocked; stops still work)")
        self.close()
