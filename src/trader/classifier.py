"""Classifier specs (state/classifiers.yaml) and their per-symbol state machines.

A classifier watches one or more symbols. While flat it asks the decision model an
entry question (ENTER / WAIT / STAND_DOWN); while holding it asks an exit question
(HOLD / EXIT). Protective stop and take-profit are enforced by the engine regardless.

A `mode: sim` classifier trades its own simulated account (see broker.PersistentSimBroker), fed
by the live bars, so experiments never compete with each other or with paper for cash or symbols.
A `mode: probe` classifier never trades: whenever its trigger holds it asks the entry question
at its cadence and logs the answer, so `trader probe-report` can score the model's
probabilities against what price did next. It needs no exit question.
"""

from __future__ import annotations

import datetime as dt
import difflib
import operator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from trader import guardrails as G
from trader.safeio import read_text

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}


class Strict(BaseModel):
    """Unknown keys are errors, not silently dropped: a misspelt `trail_pc` must never mean
    "no trailing stop" without anyone noticing (#150)."""

    model_config = ConfigDict(extra="forbid")


class Condition(Strict):
    feature: str
    op: Literal[">", ">=", "<", "<="]
    value: float

    def holds(self, feats: dict[str, float]) -> bool:
        v = feats.get(self.feature, float("nan"))
        return v == v and OPS[self.op](v, self.value)


class Question(Strict):
    instructions: str
    criteria: dict[str, str]
    threshold: float = Field(0.6, ge=0.5, le=0.99)


class EntryOrder(Strict):
    """How the entry is placed. Market (default) fills now; a limit rests below the last
    price and is cancelled if not filled within `expire_min` (a non-fill isn't a trade)."""

    type: Literal["market", "limit"] = "market"
    offset_pct: float = Field(0.0, ge=0, le=2)  # limit price = last close x (1 - offset/100)
    expire_min: int = Field(5, ge=1, le=60)


class ScaleOut(Strict):
    """Sell `fraction` of the position when price first reaches +at_pct% from entry;
    the rest runs to the target, stop, trail or time stop."""

    at_pct: float = Field(gt=0, le=20)
    fraction: float = Field(ge=0.1, le=0.9)
    stop_to_breakeven: bool = False  # after scaling out, raise the stop to the entry price


# Optional execution fields (#38). When unset they are left out of the spec's identity, so
# adding them to the schema didn't restart any classifier's promotion record.
FAMILIES = ("novel", "conventional")

EXECUTION_FIELDS = ("trail_pct", "max_hold_min", "entry_order", "risk_pct", "stop_atr_mult", "scale_out")


class ClassifierSpec(Strict):
    id: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    # sim: its own simulated account on live bars; probe: ask and log only, never order
    mode: Literal["shadow", "live", "sim", "probe"] = "shadow"
    enabled: bool = True
    control: bool = False  # benchmark classifier, not a strategy idea
    # The strategist's own label, for the dashboard scoreboard: "novel" = its own hypothesis (not a
    # textbook indicator strategy), "conventional" = anything else. A label, not behaviour: it's left
    # out of the spec's identity, and a missing or bad one never invalidates the file (that would stop
    # all trading over a label). `trader validate` rejects it; the runner alerts and shows "unlabelled".
    family: str | None = None
    symbols: list[str]
    window: tuple[str, str] = ("09:45", "15:30")  # ET, HH:MM
    cadence_min: int = Field(2, ge=1, le=60)
    trigger: list[Condition] = []  # all must hold before the entry question is asked
    features: list[str]
    context: str = ""  # strategy note passed to the decision model
    entry: Question
    exit: Question | None = None  # required unless mode is probe
    size_fraction: float = Field(0.2, gt=0, le=G.MAX_POSITION_FRACTION)
    max_trades: int = Field(1, ge=1, le=20)  # per symbol per day
    after_exit: Literal["rearm", "retire"] = "retire"
    stop_pct: float = Field(0.5, gt=0, le=G.MAX_STOP_DISTANCE * 100)  # % below entry
    target_pct: float = Field(1.0, gt=0, le=20)  # % above entry
    # ---- optional execution toolkit (all engine-enforced, inside the guardrails) ----
    trail_pct: float | None = Field(None, gt=0, le=G.MAX_STOP_DISTANCE * 100)  # stop trails the high by this %, only rising
    max_hold_min: int | None = Field(None, ge=1, le=390)  # time stop: exit after this many minutes
    entry_order: EntryOrder | None = None  # default: market
    risk_pct: float | None = Field(None, gt=0, le=2)  # size so a stop-out loses ~this % of equity (capped by size_fraction)
    stop_atr_mult: float | None = Field(None, gt=0, le=20)  # stop distance = mult x atr_14_pct, capped at stop_pct
    scale_out: ScaleOut | None = None

    @field_validator("entry")
    @classmethod
    def _entry_keys(cls, q: Question):
        if "ENTER" not in q.criteria or len(q.criteria) < 2:
            raise ValueError("entry criteria must include ENTER plus at least one alternative")
        if not set(q.criteria) <= {"ENTER", "WAIT", "STAND_DOWN"}:
            raise ValueError("entry criteria keys must be ENTER/WAIT/STAND_DOWN")
        return q

    @field_validator("exit")
    @classmethod
    def _exit_keys(cls, q: Question | None):
        if q is not None and set(q.criteria) != {"HOLD", "EXIT"}:
            raise ValueError("exit criteria keys must be exactly HOLD and EXIT")
        return q

    @model_validator(mode="after")
    def _control_prefix(self):
        # The go-live gate excludes control_* trades, so the prefix is reserved for controls.
        if self.control != self.id.startswith("control_"):
            raise ValueError("control classifiers must, and only they may, use ids starting with control_")
        return self

    @model_validator(mode="after")
    def _probe(self):
        if self.mode in ("probe", "sim") and self.control:
            raise ValueError(f"control classifiers are the benchmark and can't be {self.mode}s")
        if self.mode != "probe" and self.exit is None:
            raise ValueError("an exit question is required unless mode is probe")
        return self

    @property
    def probe(self) -> bool:
        return self.mode == "probe"

    @property
    def book_key(self) -> str:
        """The engine's key for the book this classifier trades: each sim classifier has its own."""
        return f"sim:{self.id}" if self.mode == "sim" else self.mode

    @property
    def family_label(self) -> str:
        if self.control:
            return "control"
        return self.family if self.family in FAMILIES else "unlabelled"

    def family_problem(self) -> str | None:
        """Why this spec's family label is wrong, or None. Checked by `trader validate` and the runner."""
        if self.control:
            return f"{self.id}: control classifiers have no family (they're the benchmark)" if self.family else None
        if self.family not in FAMILIES:
            return f"{self.id}: family must be one of {'/'.join(FAMILIES)} (got {self.family!r})"
        return None

    @model_validator(mode="after")
    def _scale_below_target(self):
        if self.scale_out and self.scale_out.at_pct >= self.target_pct:
            raise ValueError("scale_out.at_pct must be below target_pct")
        return self

    @model_validator(mode="after")
    def _window(self):
        a, b = (dt.time.fromisoformat(t) for t in self.window)
        if not (dt.time(9, 30) <= a < b <= dt.time(16, 0)):
            raise ValueError("window must lie within 09:30-16:00 ET")
        return self

    def in_window(self, t: dt.datetime) -> bool:
        a, b = (dt.time.fromisoformat(x) for x in self.window)
        return a <= t.time() < b


class ClassifierFile(BaseModel):
    date: str | None = None  # the session this file was written for (informational)
    classifiers: list[ClassifierSpec]

    @field_validator("date", mode="before")
    @classmethod
    def _date(cls, v):
        # YAML reads an unquoted `date: 2026-10-06` (the charter's own example) as a date.
        return v.isoformat() if isinstance(v, dt.date) else v

    @model_validator(mode="after")
    def _unique(self):
        ids = [c.id for c in self.classifiers]
        if len(ids) != len(set(ids)):
            raise ValueError("classifier ids must be unique")
        return self


_NESTED = {"trigger": Condition, "entry": Question, "exit": Question, "entry_order": EntryOrder, "scale_out": ScaleOut}


def _unknown_keys(c) -> list[str] | None:
    """If a raw classifier entry fails validation only because of unknown keys (and the required
    fields a misspelling leaves missing), describe each one, with a likely intended name; None if
    it validates or has any other problem."""
    try:
        ClassifierSpec.model_validate(c)
        return None
    except ValidationError as e:
        errs = e.errors()
    if not any(err["type"] == "extra_forbidden" for err in errs) or \
            any(err["type"] not in ("extra_forbidden", "missing") for err in errs):
        return None
    out = []
    for err in errs:
        if err["type"] == "missing":
            out.append(f"missing {'.'.join(map(str, err['loc']))}")
            continue
        loc = err["loc"]
        # The model that owns the bad key, for a "did you mean" hint from its field names.
        model = _NESTED.get(loc[0], ClassifierSpec) if len(loc) > 1 else ClassifierSpec
        key = str(loc[-1])
        close = difflib.get_close_matches(key, list(model.model_fields), n=1, cutoff=0.6)
        where = ".".join(map(str, loc))
        out.append(f"unknown key {where}" + (f" (did you mean {close[0]}?)" if close else ""))
    return out


def load_specs_report(path: Path, known_features: set[str],
                      universe: set[str]) -> tuple[list[ClassifierSpec], dict[str, str]]:
    """Parse and validate, returning (enabled specs, dropped): a classifier whose only fault is
    an unknown key is dropped on its own (#150), so the others still trade; `dropped` maps its
    id to the reason. Any other problem raises ValueError describing every problem found, as
    before. The file is read without following symlinks: it's the strategist's (issue #49)."""
    raw = yaml.safe_load(read_text(path)) or {}
    dropped: dict[str, str] = {}
    if isinstance(raw, dict) and isinstance(raw.get("classifiers"), list):
        keep = []
        for c in raw["classifiers"]:
            bad = _unknown_keys(c) if isinstance(c, dict) else None
            if bad:
                dropped[str(c.get("id") or "?")] = "; ".join(bad)
            else:
                keep.append(c)
        raw = {**raw, "classifiers": keep}
    cf = ClassifierFile.model_validate(raw)
    problems = []
    for c in cf.classifiers:
        for s in c.symbols:
            if s not in universe:
                problems.append(f"{c.id}: {s} not in universe")
        needed = set(c.features) | {t.feature for t in c.trigger}
        for f in sorted(needed - known_features):
            problems.append(f"{c.id}: unknown or failed feature {f}")
    if problems:
        raise ValueError("; ".join(problems))
    return [c for c in cf.classifiers if c.enabled], dropped


def load_specs(path: Path, known_features: set[str], universe: set[str]) -> list[ClassifierSpec]:
    """Strict form, for `trader validate` and replays: an unknown key is an error like any other."""
    specs, dropped = load_specs_report(path, known_features, universe)
    if dropped:
        raise ValueError("; ".join(f"{cid}: {why}" for cid, why in dropped.items()))
    return specs


@dataclass
class SymbolState:
    status: Literal["armed", "pending", "holding", "retired"] = "armed"  # pending: limit entry resting
    trades: int = 0
    last_eval: dt.datetime | None = None
    last_choice: str = ""
    last_probs: dict[str, float] = field(default_factory=dict)
    note: str = ""


@dataclass
class ClassifierState:
    spec: ClassifierSpec
    symbols: dict[str, SymbolState] = field(default_factory=dict)

    def __post_init__(self):
        for s in self.spec.symbols:
            self.symbols.setdefault(s, SymbolState())

    def due(self, sym: str, now: dt.datetime) -> bool:
        st = self.symbols[sym]
        if st.last_eval is None:
            return True
        return (now - st.last_eval).total_seconds() >= self.spec.cadence_min * 60 - 1

    def on_exit(self, sym: str) -> None:
        st = self.symbols[sym]
        if self.spec.after_exit == "rearm" and st.trades < self.spec.max_trades:
            st.status = "armed"
        else:
            st.status = "retired"
