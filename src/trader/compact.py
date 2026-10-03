"""Archive runner output into the repo, and apply the retention policy.

archive(): copies the runner's day files (runtime, read-only to the strategist)
into the repo's logs/ so they are committed. Idempotent.

compact(): retention —
- journal/daily older than 28 days, once the weekly covering that ISO week exists
- journal/monthly older than 13 months, once the yearly exists
- logs/decisions older than 90 days
- runtime replay runs older than 14 days (unless named keep-*), runtime decisions older than 14 days
"""

from __future__ import annotations

import csv
import datetime as dt
import shutil

from trader import config

REPO = config.STRATEGIST_ROOT
LOGS = REPO / "logs"
JOURNAL = REPO / "journal"


def archive() -> dict:
    LOGS.mkdir(exist_ok=True)
    (LOGS / "decisions").mkdir(exist_ok=True)
    copied = []
    for gz in sorted((config.RUNTIME_DIR / "decisions").glob("*.jsonl.gz")):
        dst = LOGS / "decisions" / gz.name
        if not dst.exists():
            shutil.copy2(gz, dst)
            copied.append(gz.name)
    trades_added = 0
    books = config.RUNTIME_DIR / "books"
    # Sim accounts' trades go in too (their `book` column says sim:<id>); their equity files don't.
    for book in sorted(books.glob("*")) + sorted((books / "sim").glob("*")):
        src = book / "trades.csv"
        if src.exists():
            trades_added += _merge_csv(src, LOGS / "trades.csv")
        for name in ("equity.csv", "cashflows.csv"):
            if book.parent.name != "sim" and (book / name).exists():
                shutil.copy2(book / name, LOGS / f"{book.name}_{name}")
    return {"decisions_copied": copied, "trades_added": trades_added}


def _merge_csv(src, dst) -> int:
    with src.open() as f:
        rows = list(csv.DictReader(f))
    existing = set()
    if dst.exists():
        with dst.open() as f:
            existing = {tuple(r.values()) for r in csv.DictReader(f)}
    new = [r for r in rows if tuple(r.values()) not in existing]
    if new:
        write_header = not dst.exists()
        with dst.open("a", newline="") as f:
            w = csv.DictWriter(f, list(rows[0]))
            if write_header:
                w.writeheader()
            w.writerows(new)
    return len(new)


def compact(dry_run: bool = False, today: dt.date | None = None, scope: str = "repo") -> dict:
    """scope "repo" runs as the strategist; scope "runtime" as the runner (owns runtime/)."""
    today = today or dt.date.today()
    removed: list[str] = []

    def rm(p):
        removed.append(str(p.relative_to(p.parents[2]) if len(p.parents) > 2 else p))
        if not dry_run:
            shutil.rmtree(p) if p.is_dir() else p.unlink()

    if scope == "runtime":
        return _compact_runtime(today, rm, removed, dry_run)
    for f in sorted((JOURNAL / "daily").glob("*.md")):
        d = _date(f.stem)
        if d and (today - d).days > 28:
            y, w, _ = d.isocalendar()
            if (JOURNAL / "weekly" / f"{y}-W{w:02d}.md").exists():
                rm(f)
    for f in sorted((JOURNAL / "monthly").glob("*.md")):
        try:
            d = dt.date.fromisoformat(f.stem + "-01")
        except ValueError:
            continue
        if (today - d).days > 400 and (JOURNAL / "yearly" / f"{d.year}.md").exists():
            rm(f)
    for f in sorted((LOGS / "decisions").glob("*.jsonl.gz")):
        d = _date(f.name[:10])
        if d and (today - d).days > 90:
            rm(f)
    for run in sorted((config.REPLAY_DIR).glob("*")):
        age = (dt.datetime.now().timestamp() - run.stat().st_mtime) / 86400
        if run.is_dir() and not run.name.startswith("keep-") and age > 14:
            rm(run)
    return {"removed": removed, "dry_run": dry_run}


def _compact_runtime(today, rm, removed, dry_run) -> dict:
    for f in sorted((config.RUNTIME_DIR / "decisions").glob("*.jsonl.gz")):
        d = _date(f.name[:10])
        if d and (today - d).days > 14:
            rm(f)
    return {"removed": removed, "dry_run": dry_run}


def _date(s: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None
