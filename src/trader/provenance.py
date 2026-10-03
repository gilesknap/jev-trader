"""Where a piece of evidence came from: the decision model, the code commit and each classifier's
spec hash, stamped on decision-log and trade rows so results can be split into cohorts later
(a same-ID spec edit, a model bump or a deploy each start a new cohort, without restarting any
promotion record: `golive.spec_hash` is unchanged and never includes the code version).

Best-effort and computed once, when the engine is built (before the session trades): anything
that can't be read is "" and never stops a session or a replay.
"""

from __future__ import annotations

import functools
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from trader import config

COLS = ("model", "code_sha", "spec_hash")  # extra trade-row columns, in this order


@functools.cache
def code_sha(root: Path | None = None) -> str:
    """The commit the code runs from (12 hex), or "". Any user may read it: the strategist's
    replays run against the deployed checkout it doesn't own."""
    try:
        r = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(root or config.CODE_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    sha = r.stdout.strip()
    return sha[:12] if r.returncode == 0 and len(sha) >= 12 else ""


def model_id(decider) -> str:
    model = getattr(decider, "model", None)
    if isinstance(model, str) and model:
        return model
    return "stub" if getattr(decider, "offline", False) else type(decider).__name__


@dataclass
class Provenance:
    model: str = ""
    code: str = ""
    specs: dict[str, str] = field(default_factory=dict)  # classifier id -> golive.spec_hash

    @classmethod
    def build(cls, specs, decider) -> Provenance:
        from trader import golive

        hashes = {}
        try:
            digest = golive.custom_features_digest()
            for s in specs:
                hashes[s.id] = golive.spec_hash(s, digest)
        except Exception:  # evidence only: never a reason not to trade
            pass
        return cls(model_id(decider), code_sha(), hashes)

    def trade(self, classifier: str) -> dict:
        """Trade-row fields. A sell's spec_hash is its entry's, set by the caller."""
        return {"model": self.model, "code_sha": self.code, "spec_hash": self.specs.get(classifier, "")}

    def decision(self, classifier: str | None) -> dict:
        """Decision-log fields (short keys, like the rest of the row)."""
        return {"mv": self.model, "cv": self.code, "h": self.specs.get(classifier or "", "")}
