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
    try:  # the runner's shadow_start rows; never a reason for the archive to fail
        from trader import trials

        trials_added = trials.merge_runtime()
    except Exception as e:
        trials_added = f"failed: {type(e).__name__}: {e}"
    return {"decisions_copied": copied, "trades_added": trades_added, "trials_added": trials_added}


def _merge_csv(src, dst) -> int:
    """Rows of `src` not already in `dst`. Columns `src` has that `dst` lacks (new ones added at the
    end, e.g. trade provenance) are added to `dst` first, blank on its existing rows."""
    with src.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        cols = list(reader.fieldnames or [])
    old_cols, existing = [], []
    if dst.exists():
        with dst.open(newline="") as f:
            reader = csv.DictReader(f)
            existing = list(reader)
            old_cols = list(reader.fieldnames or [])
    all_cols = old_cols + [c for c in cols if c not in old_cols]

    def key(r):
        return tuple(r.get(c) or "" for c in all_cols)

    seen = {key(r) for r in existing}
    new = [r for r in rows if key(r) not in seen]
    if new:
        if old_cols and all_cols != old_cols:  # rewrite under the wider header, atomically
            tmp = dst.with_name(dst.name + ".tmp")
            with tmp.open("w", newline="") as f:
                w = csv.DictWriter(f, all_cols, restval="", extrasaction="ignore")
                w.writeheader()
                w.writerows(existing + new)
            tmp.replace(dst)
        else:
            with dst.open("a", newline="") as f:
                w = csv.DictWriter(f, all_cols, restval="", extrasaction="ignore")
                if not old_cols:
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
