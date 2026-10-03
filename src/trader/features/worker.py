"""Sandboxed feature worker: `python -I -m trader.features.worker` under bwrap.

Reads one JSON request per line on stdin, writes one JSON reply per line on stdout.
  {"op": "load", "dir": ..., "samples": [[bars, prev, spy], ...]}
      -> {"features": [...], "docs": {...}, "errors": {...}}
  {"op": "compute", "names": [...], "bars": ..., "prev": ..., "spy": ..., "mso": m, "mtc": m}
      -> {"values": {name: float|null}}
"""

from __future__ import annotations

import json
import math
import os
import sys


def _limits() -> list[str]:
    """Cap memory and file descriptors before any strategist code is imported.
    Returns warnings (reported to the client, which alerts) instead of failing silently."""
    warnings = []
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (1536 * 2**20, 1536 * 2**20))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    except (ImportError, ValueError, OSError) as e:
        warnings.append(f"could not set resource limits: {e}")
    return warnings


def _private_channel():
    """Move the protocol off fds 0/1 before strategist code runs, so a feature that prints
    (or reads stdin) can't inject or consume protocol lines."""
    proto_in = os.fdopen(os.dup(0), "r", buffering=1)
    proto_out = os.fdopen(os.dup(1), "w", buffering=1)
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 0)
    os.dup2(null, 1)
    sys.stdin = open(os.devnull)
    sys.stdout = open(os.devnull, "w")
    return proto_in, proto_out


class Worker:
    def __init__(self, warnings: list[str] | None = None):
        self.custom: list[str] = []
        self.warnings = warnings or []

    def handle(self, req: dict) -> dict:
        from trader import features as F
        from trader.features import harness
        from trader.features.barcodec import decode_bars  # config-free: never import sandbox here

        try:
            if req["op"] == "load":
                names, errors = harness.load_custom_inprocess(__import__("pathlib").Path(req["dir"]))
                samples = [tuple(decode_bars(x) for x in s) for s in req.get("samples", [])]
                errors.update(harness.evaluate(names, samples, req.get("speed_budget_s")))
                good = [n for n in names if n not in errors]
                for bad in set(names) - set(good):
                    F.REGISTRY.pop(bad, None)
                self.custom = good
                docs = {n: ((F.REGISTRY[n].__doc__ or "").strip().splitlines() or [""])[0] for n in good}
                return {"features": good, "docs": docs, "errors": errors, "warnings": self.warnings}
            if req["op"] == "compute":
                ctx = F.FeatureContext(decode_bars(req["prev"]), decode_bars(req["spy"]), req["mso"], req["mtc"])
                bars = decode_bars(req["bars"])
                names = [n for n in req["names"] if n in self.custom]
                vals = F.compute_local(names, bars, ctx)
                return {"values": {k: (v if math.isfinite(v) else None) for k, v in vals.items()}}
            return {"error": f"unknown op {req.get('op')!r}"}
        except Exception as e:  # report, never crash the protocol
            return {"error": f"{type(e).__name__}: {e}"[:300]}


def main() -> None:
    proto_in, proto_out = _private_channel()
    w = Worker(_limits())
    for line in proto_in:
        rid = nonce = None
        try:
            req = json.loads(line)
            rid, nonce = req.pop("id", None), req.pop("nonce", None)
            reply = w.handle(req)
        except ValueError as e:
            reply = {"error": f"bad request: {e}"}
        reply["id"], reply["nonce"] = rid, nonce
        proto_out.write(json.dumps(reply, separators=(",", ":")) + "\n")
        proto_out.flush()


if __name__ == "__main__":
    main()
