"""Unit-NAV accounting so deposits and withdrawals don't read as gains or losses."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class NavBook:
    units: float = 0.0
    hwm: float = 1.0  # high-water mark of NAV per unit
    last_equity: float = 0.0

    @property
    def nav_per_unit(self) -> float:
        return self.last_equity / self.units if self.units > 0 else 1.0

    def mark(self, equity: float) -> float:
        """Record current equity; returns NAV per unit and updates the HWM."""
        if self.units <= 0:
            self.units = equity  # first mark: NAV starts at 1.0
        self.last_equity = equity
        nav = self.nav_per_unit
        self.hwm = max(self.hwm, nav)
        return nav

    def cashflow(self, amount: float) -> None:
        """Deposit (+) or withdrawal (-) at the current NAV: issues or redeems units."""
        nav = self.nav_per_unit
        self.units += amount / nav
        self.last_equity += amount

    @classmethod
    def load(cls, path: Path) -> NavBook:
        if path.exists():
            return cls(**json.loads(path.read_text()))
        return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self)))
