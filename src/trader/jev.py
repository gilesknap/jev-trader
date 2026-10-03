"""Decision-model adapter. Jev via OpenRouter's Decisions API, plus an offline stub.

The classifier only ever asks `choice` questions and reads back probabilities, so
swapping in a local model (NanoJev, Laya) means implementing `decide`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from trader import config

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = config.SETTINGS.models.jev  # pinned in config.yaml; bump deliberately after checking behaviour


class DecisionError(Exception):
    pass


@dataclass
class Decision:
    choice: str
    probabilities: dict[str, float]
    cost: float = 0.0
    input_tokens: int = 0


class JevClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 5.0):
        self.model = model
        self.total_cost = 0.0
        self.calls = 0
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Title": "trader",
            },
        )

    def decide(self, state: dict | str, instructions: str, criteria: dict[str, str]) -> Decision:
        body = {
            "model": self.model,
            "state": state,
            "questions": {
                "action": {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": criteria,
                }
            },
        }
        last: Exception | None = None
        for _attempt in range(2):  # one retry; the engine's circuit breaker handles outages
            try:
                r = self._http.post(DECISIONS_URL, json=body)
                if r.status_code in (429, 500, 502, 503, 524, 529):
                    raise DecisionError(f"HTTP {r.status_code}: {r.text[:200]}")
                if r.status_code != 200:
                    # 4xx other than 429 won't improve on retry
                    raise DecisionError(f"HTTP {r.status_code}: {r.text[:300]}")
                data = r.json()
                ans = data["answers"]["action"]
                usage = data.get("usage", {})
                cost = float(usage.get("cost") or 0.0)
                self.total_cost += cost
                self.calls += 1
                return Decision(
                    choice=ans["choice"],
                    probabilities={k: float(v) for k, v in ans["probabilities"].items()},
                    cost=cost,
                    input_tokens=int(usage.get("input_tokens") or 0),
                )
            except (httpx.HTTPError, DecisionError, KeyError, ValueError) as e:
                last = e
                if isinstance(e, DecisionError) and not str(e).startswith(("HTTP 429", "HTTP 5")):
                    break
                time.sleep(0.5)
        raise DecisionError(str(last))


class StubDecider:
    """Offline stand-in for dev replays without Jev: enters whenever the classifier's
    trigger passed, exits once unrealised loss exceeds 0.3%, otherwise holds."""

    offline = True

    def __init__(self):
        self.total_cost = 0.0
        self.calls = 0

    def decide(self, state, instructions, criteria) -> Decision:
        self.calls += 1
        keys = list(criteria)
        feats = state.get("features", {}) if isinstance(state, dict) else {}
        pos = state.get("position") if isinstance(state, dict) else None
        if pos is None:
            pick = "ENTER" if feats.get("_trigger", 0) > 0 else keys[-1]
            if pick not in keys:
                pick = keys[0]
        else:
            pick = "EXIT" if pos.get("unrealised_pct", 0) < -0.3 else "HOLD"
            if pick not in keys:
                pick = keys[0]
        probs = {k: (0.8 if k == pick else 0.2 / max(1, len(keys) - 1)) for k in keys}
        return Decision(choice=pick, probabilities=probs)
