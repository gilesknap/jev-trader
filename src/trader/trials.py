"""The trial ledger: every evaluation of a classifier spec, kept by the tools rather than by hand.

Try enough variants on the same few days and one will look good by luck, so a result means little
without the number of attempts behind it. The strategist can't be trusted to count its own:
backtests nobody wrote down leave no trace once their replay directory is deleted. So the tools
append one row per classifier per evaluation to `logs/trials.csv` in the strategist's checkout,
and the strategist wrapper refuses any change to that file other than rows added at its end.

- `trader replay`: a `replay` row per classifier in the run (`stub` = 1 for the stub decider,
  whose results are plumbing checks, not evidence).
- `trader probe-report --replay X`: a `probe_report` row per probe scored, carrying the spec hash
  the replay recorded for it. Its trades and net return are the ENTER answers at the first horizon.
- The runner: a `shadow_start` row the first time a session runs a spec hash. The runner can't
  write the strategist's checkout, so it appends to `trials.csv` in its runtime directory and
  `trader archive` copies new rows across, as it does for trades.

`spec_hash` is `golive.spec_hash`, the promotion record's identity: a reworded criterion or a new
threshold is a new spec, a `mode`/`enabled`/`family` change is not. Writing is best-effort: a
ledger failure never stops a replay, a probe report or a session.
"""

from __future__ import annotations

import csv
import datetime as dt
import errno
import fcntl
import io
import os
import stat
import sys
import time
from pathlib import Path

from trader import config

COLS = (
    "time",
    "kind",
    "run_name",
    "classifier_id",
    "spec_hash",
    "family",
    "days",
    "start",
    "end",
    "trades",
    "net_pct_after_slip",
    "stub",
)
EVALUATIONS = ("replay", "probe_report")
HEADER = ",".join(COLS) + "\n"
LOCK_WAIT_S = 10.0


def ledger_path() -> Path:
    return config.STRATEGIST_ROOT / "logs" / "trials.csv"


def runtime_path() -> Path:
    return config.RUNTIME_DIR / "trials.csv"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def append(rows: list[dict], path: Path | None = None, lock: bool = True) -> int:
    """Append `rows` (dicts keyed by COLS) in one write, with a header if the file is new or empty.
    Never rewrites what is there: a last line without its newline gets one first. Symlinks are
    refused. Raises OSError on failure; callers that must not fail use record(). `lock=False` for
    a file with one writer (the runner's: other users can read it, so could hold a lock on it)."""
    if not rows:
        return 0
    path = path or ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.DictWriter(buf, COLS, restval="", extrasaction="ignore", lineterminator="\n")
    w.writerows(rows)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        # Writers of the same file wait for each other briefly; past that, append anyway (one
        # O_APPEND write of whole rows doesn't interleave with another) rather than hang a tool.
        deadline = time.monotonic() + LOCK_WAIT_S
        while lock:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() > deadline:
                    break
                time.sleep(0.05)
        size = os.fstat(fd).st_size
        head = HEADER if size == 0 else ""
        if size and os.pread(fd, len(HEADER), 0) != HEADER.encode():
            raise OSError(f"{path} doesn't start with the ledger's header; not appending")
        if size and os.pread(fd, 1, size - 1) != b"\n":
            head = "\n"
        data = memoryview((head + buf.getvalue()).encode())
        while data:  # O_APPEND: each write lands at the end, whatever else was written meanwhile
            data = data[os.write(fd, data) :]
    finally:
        os.close(fd)
    return len(rows)


def record(build, path: Path | None = None) -> int:
    """append(build()), but any failure, building the rows included, is a note on stderr, never an error."""
    try:
        return append(build(), path)
    except Exception as e:
        print(f"note: trial ledger not updated ({type(e).__name__}: {e})", file=sys.stderr)
        return 0


def read(path: Path | None = None) -> list[dict]:
    """The ledger's complete rows (a missing file has none; a last line still being written is
    left out). Raises ValueError if the file isn't a ledger (its header isn't COLS)."""
    path = path or ledger_path()
    try:
        text = path.read_bytes().decode()
    except FileNotFoundError:
        return []
    text = text[: text.rfind("\n") + 1]
    if text and not text.startswith(HEADER):
        raise ValueError(f"{path} doesn't start with the ledger's header ({HEADER.strip()})")
    return [r for r in csv.DictReader(io.StringIO(text)) if r.get("kind")]


def _spec_hashes(specs) -> dict[str, str]:
    from trader.provenance import Provenance

    return Provenance.build(specs, None).specs  # best-effort: "" for any it can't hash


def replay_rows(specs, run_dir: Path, summary: dict, stub: bool) -> list[dict]:
    """One row per classifier in a finished replay: its closed round trips and mean net return
    (replay fills already include the slippage)."""
    from trader.scoreboard import closed_trades

    tp = run_dir / "sim" / "trades.csv"
    with tp.open(newline="") if tp.exists() else io.StringIO() as f:
        closed = closed_trades(list(csv.DictReader(f)), slippage_per_side_pct=0.0)
    hashes, now = _spec_hashes(specs), _now()
    out = []
    for s in specs:
        nets = [t["net_pct"] for t in closed if t["classifier"] == s.id]
        out.append(
            {
                "time": now,
                "kind": "replay",
                "run_name": run_dir.name,
                "classifier_id": s.id,
                "spec_hash": hashes.get(s.id, ""),
                "family": s.family_label,
                "days": len(summary.get("days") or {}),
                "start": summary.get("start", ""),
                "end": summary.get("end", ""),
                "trades": len(nets),
                "net_pct_after_slip": round(sum(nets) / len(nets), 4) if nets else "",
                "stub": int(stub),
            }
        )
    return out


def probe_rows(run_name: str, rows, report: dict, horizons: list[int]) -> list[dict]:
    """One row per probe scored from replay `run_name`'s decisions. The spec hash, family and stub
    flag come from the ledger's latest `replay` row for that run and probe (blank if none)."""
    replayed = {r["classifier_id"]: r for r in read() if r["kind"] == "replay" and r["run_name"] == run_name}
    now, out = _now(), []
    for c, p in sorted(report.items()):
        days = sorted(rows.loc[rows.c == c, "day"].unique())
        first = (p.get("horizons") or {}).get(horizons[0]) or {}
        src = replayed.get(c, {})
        out.append(
            {
                "time": now,
                "kind": "probe_report",
                "run_name": run_name,
                "classifier_id": c,
                "spec_hash": src.get("spec_hash", ""),
                "family": src.get("family", ""),
                "days": p.get("days", len(days)),
                "start": days[0] if days else "",
                "end": days[-1] if days else "",
                "trades": first.get("enter_n", 0),
                "net_pct_after_slip": "" if first.get("enter_mean_net_pct") is None else first["enter_mean_net_pct"],
                "stub": src.get("stub", ""),
            }
        )
    return out


def note_shadow_starts(specs, hashes: dict[str, str], day: dt.date, stub: bool, path: Path | None = None) -> list[str]:
    """Runner, at session start: a `shadow_start` row in the runtime ledger for each spec whose hash
    it hasn't logged under that id before. Returns the ids logged. Raises on failure: the caller decides."""
    path = path or runtime_path()
    seen = {(r["spec_hash"], r["classifier_id"]) for r in read(path) if r["kind"] == "shadow_start"}
    now, new = _now(), []
    for s in specs:
        h = hashes.get(s.id, "")
        if h and (h, s.id) not in seen:
            seen.add((h, s.id))
            new.append(
                {
                    "time": now,
                    "kind": "shadow_start",
                    "run_name": day.isoformat(),
                    "classifier_id": s.id,
                    "spec_hash": h,
                    "family": s.family_label,
                    "stub": int(stub),
                }
            )
    append(new, path, lock=False)
    return [r["classifier_id"] for r in new]


def merge_runtime(src: Path | None = None, dst: Path | None = None) -> int:
    """`trader archive`: append the runtime ledger's rows that the strategist's ledger lacks."""
    src, dst = src or runtime_path(), dst or ledger_path()
    have = {tuple(r.get(c) or "" for c in COLS) for r in read(dst)}
    new = [r for r in read(src) if tuple(r.get(c) or "" for c in COLS) not in have]
    return append(new, dst)


def report(rows: list[dict], cid: str | None = None, spec: str | None = None, include_stub: bool = False) -> str:
    """`trader trials`: distinct specs tried per classifier id (or for one id or spec hash, each
    spec), on how many different days trials were run (not the sessions each one replayed: that
    is the ledger's days/start/end), and the totals for each family shown."""
    stubs = sum(r.get("stub") == "1" for r in rows)
    if not include_stub:
        rows = [r for r in rows if r.get("stub") != "1"]
    every = rows
    if cid:
        rows = [r for r in rows if r["classifier_id"] == cid]
    if spec:
        rows = [r for r in rows if r["spec_hash"].startswith(spec)]
    if not rows:
        return "no matching trials in the ledger"

    def tally(rs):  # (distinct specs, distinct days with a trial, evaluations)
        return (
            len({r["spec_hash"] for r in rs if r["spec_hash"]}),
            len({r["time"][:10] for r in rs}),
            sum(r["kind"] in EVALUATIONS for r in rs),
        )

    def group(rs, key):
        out: dict[str, list[dict]] = {}
        for r in rs:
            out.setdefault(r[key] or "(none)", []).append(r)
        return out

    lines = []
    if cid or spec:
        if cid:
            n, d, e = tally(rows)
            lines.append(f"{cid}: {n} distinct spec(s) tried on {d} different day(s), {e} evaluation(s)")
        lines.append(f"{'spec_hash':16}  {'family':12}  {'first':10}  {'last':10}  evals  shadow  ids")
        for h, rs in sorted(group(rows, "spec_hash").items(), key=lambda kv: min(r["time"] for r in kv[1])):
            lines.append(
                f"{h:16}  {rs[-1]['family'] or '-':12}  {min(r['time'] for r in rs)[:10]}  "
                f"{max(r['time'] for r in rs)[:10]}  {sum(r['kind'] in EVALUATIONS for r in rs):5}  "
                f"{'yes' if any(r['kind'] == 'shadow_start' for r in rs) else 'no':6}  "
                f"{','.join(sorted({r['classifier_id'] for r in rs}))}"
            )
    else:
        lines.append(f"{'classifier_id':32}  {'family':12}  specs  trial_days  evals")
        for c, rs in sorted(group(rows, "classifier_id").items()):
            n, d, e = tally(rs)
            lines.append(f"{c:32}  {rs[-1]['family'] or '-':12}  {n:5}  {d:10}  {e:5}")
    lines.append("")
    for f in sorted({r["family"] for r in rows if r["family"]}):
        fam = [r for r in every if r["family"] == f]
        n, d, _ = tally(fam)
        lines.append(
            f"family {f}: {n} distinct spec(s) across {len({r['classifier_id'] for r in fam})} id(s), tried on {d} different day(s)"
        )
    if stubs and not include_stub:
        lines.append(f"({stubs} stub-decider row(s) not counted: --include-stub counts them)")
    return "\n".join(lines).rstrip()
