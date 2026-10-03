"""Read-only dashboard (plus STOP), served behind `tailscale serve`.

All data comes from files: the runner's RUNTIME_DIR (status, books, replays) and
the strategist's state/ and journal/. Every route requires a Tailscale identity in
config.yaml's dashboard.users (TRADER_DASHBOARD_USERS overrides it, for development only);
with no identity header, only TRADER_DASHBOARD_ALLOW_LOCAL=1 (dev) lets requests through.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse

from trader import config, safeio

STATIC = config.CODE_ROOT / "dashboard" / "static"
JOURNAL_KINDS = ("daily", "weekly", "monthly", "yearly")
MAX_POINTS = 2000
USERS = {u.strip() for u in os.environ.get("TRADER_DASHBOARD_USERS", ",".join(config.SETTINGS.dashboard.users)).split(",") if u.strip()}
ALLOW_LOCAL = os.environ.get("TRADER_DASHBOARD_ALLOW_LOCAL") == "1"

app = FastAPI(title="trader dashboard", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def tailscale_auth(request: Request, call_next):
    login = request.headers.get("Tailscale-User-Login")
    if (login is None and not ALLOW_LOCAL) or (login is not None and login not in USERS):
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    return await call_next(request)


# ---- file helpers --------------------------------------------------------------


def _json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _csv(path: Path) -> list[dict]:
    try:
        with path.open(newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _strategist_text(path: Path) -> str | None:
    """A file from the strategist's checkout, or None if it's missing, unreadable or refused. safeio
    never follows a symlink there, so a planted one can't make the dashboard (running as runner)
    show another file runner can read."""
    try:
        return safeio.read_text(path)
    except (OSError, ValueError):  # UnsafePath and UnicodeDecodeError are ValueErrors
        return None


def _journal_names(kind: str) -> list[str]:
    """The *.md entries in journal/<kind>/, newest first; none if the directory is missing or refused."""
    try:
        names = safeio.listdir(config.STRATEGIST_ROOT / "journal" / kind)
    except (OSError, ValueError):
        return []
    return sorted((n for n in names if n.endswith(".md") and not n.startswith(".")), reverse=True)


def _replay_ids() -> list[str]:
    root = config.REPLAY_DIR
    if not root.is_dir():
        return []
    dirs = [d for d in root.iterdir() if d.is_dir()]
    return [d.name for d in sorted(dirs, key=lambda d: d.stat().st_mtime, reverse=True)]


def _source_dir(source: str) -> tuple[Path, list[Path]]:
    """(run dir, book dirs) for 'live' or a replay id."""
    if source == "live":
        base = config.RUNTIME_DIR
        books_root = base / "books"
        # sim/ holds one account per sim classifier: see _sim_dirs, shown as one combined "sim" book.
        books = sorted(d for d in books_root.iterdir() if d.is_dir() and d.name != "sim") if books_root.is_dir() else []
        return base, books
    if source not in _replay_ids():
        raise HTTPException(404, "unknown source")
    base = config.REPLAY_DIR / source
    books = sorted(d for d in base.iterdir() if d.is_dir() and ((d / "equity.csv").exists() or (d / "trades.csv").exists()))
    return base, books


def _sim_dirs(source: str) -> list[Path]:
    """The live runner's sim accounts (books/sim/<classifier id>/), one per `mode: sim` classifier."""
    root = config.RUNTIME_DIR / "books" / "sim"
    return sorted(d for d in root.iterdir() if d.is_dir()) if source == "live" and root.is_dir() else []


def _sim_trades(source: str) -> list[dict]:
    rows = [r for d in _sim_dirs(source) for r in _csv(d / "trades.csv")]
    return sorted(rows, key=lambda r: r.get("time") or "")


def _downsample(rows: list, n: int = MAX_POINTS) -> list:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)] + [rows[-1]]


DETAIL_DAYS = 7  # the performance chart's longest intraday range (7D); longer ranges plot daily closes


def _equity_points(rows: list[dict]) -> list:
    """[time, equity, nav] marks for the performance chart: every mark from the last DETAIL_DAYS
    calendar days, and each earlier day's last mark (its close), so a year stays small enough to
    send and the short ranges keep full detail. Malformed rows are skipped."""
    pts = []
    for r in rows:
        try:
            dt.date.fromisoformat(str(r["time"])[:10])  # the chart dates every mark
            pts.append([str(r["time"]), float(r["equity"]), float(r["nav"])])
        except (KeyError, TypeError, ValueError):
            continue
    if not pts:
        return []
    cutoff = (dt.date.fromisoformat(pts[-1][0][:10]) - dt.timedelta(days=DETAIL_DAYS - 1)).isoformat()
    out = [p for i, p in enumerate(pts)
           if p[0][:10] >= cutoff or i + 1 == len(pts) or pts[i + 1][0][:10] != p[0][:10]]
    return _downsample(out)


SKIP_REASONS = {  # the engine's skip_* outcomes, as the dashboard says them
    "feed_stale": "market data stale",
    "paused": "Jev calls paused after errors",
    "no_time": "no time left in the minute",
    "book_blocked": "account blocked (kill switch, STOP or unverified start)",
    "symbol_busy": "another rule had the stock",
    "no_bars": "no price bars",
    "outside_window": "outside its time window",
}


def _times(n: int) -> str:
    return "once" if n == 1 else "twice" if n == 2 else f"{n} times"


def _num_text(v) -> str:
    return "unavailable" if v is None else f"{v:g}"


def why_summary(x: dict) -> str:
    """One plain-English line on what a rule did with a stock today, from the engine's counts
    (status.json: classifiers[].symbols[sym].counts and .last_trigger)."""
    counts = x.get("counts") if isinstance(x.get("counts"), dict) else {}
    n = {k: v for k, v in counts.items() if isinstance(v, int) and v > 0}
    checks, no_trig, errors = n.get("checks", 0), n.get("no_trigger", 0), n.get("jev_error", 0)
    asked = sum(v for k, v in n.items() if k.startswith("asked_"))
    skips = sorted(((k[5:], v) for k, v in n.items() if k.startswith("skip_")), key=lambda kv: -kv[1])
    if checks:  # once it has been checked, minutes outside the window say nothing
        skips = [(k, v) for k, v in skips if k != "outside_window"]
    if not checks:
        out = "Not checked yet today."
    elif asked:
        out = f"Checked {_times(checks)}; asked Jev {_times(asked)}."
        if no_trig:
            out += f" The trigger didn't pass on {no_trig} of the checks."
    elif no_trig == checks:
        out = f"Checked {_times(checks)}; the trigger never passed."
    else:
        out = f"Checked {_times(checks)}; Jev not asked."
        if no_trig:
            out += f" The trigger didn't pass on {no_trig} of them."
    if errors:
        out += f" Jev failed to answer {_times(errors)}."
    if skips:
        out += " Skipped: " + ", ".join(f"{SKIP_REASONS.get(k, k.replace('_', ' '))} ({v})" for k, v in skips) + "."
    trig = x.get("last_trigger")
    if no_trig and isinstance(trig, dict) and isinstance(trig.get("conditions"), list):
        failed = [c for c in trig["conditions"] if isinstance(c, list) and len(c) == 5 and not c[4]]
        if failed:
            out += f" Last miss at {trig.get('at', '?')}: " + "; ".join(
                f"{f} = {_num_text(v)} (needs {op} {need:g})" for f, op, need, v, _ in failed) + "."
    return out


def _with_why(status: dict | None) -> dict | None:
    """status.json with a `why` line on each trading rule's stocks (probes are summarised elsewhere)."""
    for c in (status.get("classifiers") or [] if isinstance(status, dict) else []):
        if isinstance(c, dict) and c.get("mode") != "probe" and isinstance(c.get("symbols"), dict):
            for x in c["symbols"].values():
                if isinstance(x, dict):
                    try:
                        x["why"] = why_summary(x)
                    except (TypeError, ValueError):  # a malformed count is shown as nothing, never an error
                        x["why"] = ""
    return status


def _age_seconds(path: Path) -> float | None:
    try:
        return round(dt.datetime.now().timestamp() - path.stat().st_mtime)
    except OSError:
        return None


# ---- overview: useful links -----------------------------------------------------


def _git(*args: str, root: Path | None = None) -> str | None:
    """Output of a read-only git command in the deployed checkout (or `root`), or None."""
    try:
        r = subprocess.run(["git", "-C", str(root or config.CODE_ROOT), *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def github_repo(remote: str | None) -> str | None:
    """'owner/name' from a remote URL: git@github-trading:o/n, github-trading:o/n, https://github.com/o/n.git."""
    m = re.search(r"[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", remote or "")
    return m.group(1) if m else None


def deployed_commit(root: Path | None = None) -> dict | None:
    """The commit this dashboard (and so the runner) is running, or the deployed config's with `root`."""
    out = _git("log", "-1", "--format=%H%x1f%cs%x1f%s", root=root)
    if not out or out.count("\x1f") != 2:
        return None
    sha, date, subject = out.split("\x1f")
    return {"sha": sha, "short": sha[:7], "date": date, "subject": subject}


def data_root() -> Path | None:
    """The deployed config checkout in the split layout (#169: public code repo, private data repo), or None
    in the monorepo. Read locally for now; the DATA_ROOT PR (A1) adds a config helper. Set to the code root
    itself, it is still the monorepo."""
    root = os.environ.get("TRADER_DATA_ROOT")
    if not root or Path(root).resolve() == Path(config.CODE_ROOT).resolve():
        return None
    return Path(root)


def links(repo: str | None, sha: str | None, data: dict | None = None) -> list[dict]:
    """Every external link on the page, grouped as shown. Edit here: (label, url, what it's for).
    The GitHub group is built from the git remote, so the repo is never typed twice. In the split layout,
    `data` is {"repo", "sha"} for the private data repo and its deployed config commit; `repo` is the code."""
    groups = [
        {"title": "Alpaca (the broker)", "hint": "Positions, orders and activity are in each dashboard's left menu; "
                                                 "Account › Configure and Funds & Wallet are on the live one.", "links": [
            ("Paper dashboard", "https://app.alpaca.markets/paper/dashboard/overview", "practice account: positions, orders, activity"),
            ("Live dashboard", "https://app.alpaca.markets/brokerage/dashboard/overview", "real money: deposits, withdrawals, settings"),
            ("Alpaca status", "https://status.alpaca.markets/", "is Alpaca itself having problems?"),
        ]},
        {"title": "OpenRouter (pays for Jev)", "hint": "Each Jev decision is billed here, a fraction of a cent.", "links": [
            ("Credits", "https://openrouter.ai/settings/credits", "balance and top-up"),
            ("Activity", "https://openrouter.ai/activity", "spend per day and per key"),
            ("API keys", "https://openrouter.ai/settings/keys", "limits and expiry"),
        ]},
    ]
    if data is None and repo:  # monorepo: code and notes in one repo
        gh = f"https://github.com/{repo}"
        groups.append({"title": "GitHub (code and the strategist's notes)", "hint": "Your weekly job: merge the weekly PR "
                                                                              "and answer needs-human issues.", "links": [
            ("Repository", gh, repo),
            ("Docs: operations", f"{gh}/blob/main/docs/how-to/daily-operations.md", "deploying, controls, alerts"),
            ("Pull requests", f"{gh}/pulls", "weekly PR and proposal/* changes to review"),
            ("Issues: needs-human", f"{gh}/issues?q=is%3Aissue+is%3Aopen+label%3Aneeds-human", "things the strategist asked you for"),
            ("Weekly journal", f"{gh}/tree/strategist/journal/weekly", "the strategist's retrospectives"),
            ("Current strategy", f"{gh}/blob/strategist/state/strategy.md", "on the strategist branch"),
            *([("Not yet deployed", f"{gh}/compare/{sha}...main", "commits on main the runner isn't running")] if sha else []),
        ]})
    if data is not None:
        if repo:
            gh = f"https://github.com/{repo}"
            groups.append({"title": "GitHub: the code", "hint": "Public. Code changes are reviewed and merged here, "
                                                               "then deployed with trading-deploy.", "links": [
                ("Repository", gh, repo),
                ("Docs: operations", f"{gh}/blob/main/docs/how-to/daily-operations.md", "deploying, controls, alerts"),
                *([("Not yet deployed", f"{gh}/compare/{sha}...main", "code on main the runner isn't running")] if sha else []),
            ]})
        dgh = f"https://github.com/{data['repo']}"
        groups.append({"title": "GitHub: your data (config and the strategist's notes)",
                       "hint": "Your weekly job: read the weekly issue and answer needs-human issues.", "links": [
            ("Repository", dgh, data["repo"]),
            ("Issues: weekly", f"{dgh}/issues?q=is%3Aissue+label%3Aweekly", "the strategist's weekly reports"),
            ("Issues: needs-human", f"{dgh}/issues?q=is%3Aissue+is%3Aopen+label%3Aneeds-human", "things the strategist asked you for, including code proposals"),
            ("Pull requests", f"{dgh}/pulls", "config changes to main to review"),
            ("Weekly journal", f"{dgh}/tree/strategist/journal/weekly", "the strategist's retrospectives"),
            ("Current strategy", f"{dgh}/blob/strategist/state/strategy.md", "on the strategist branch"),
            *([("Config not yet deployed", f"{dgh}/compare/{data['sha']}...main", "config on main the runner isn't using")]
              if data.get("sha") else []),
        ]})
    groups.append({"title": "Other", "hint": "", "links": [
        ("Tailscale admin", "https://login.tailscale.com/admin/machines", "who and what can reach this dashboard"),
        ("Claude usage (Max plan)", "https://claude.ai/settings/usage", "the strategist runs on your subscription"),
        ("Anthropic console", "https://platform.claude.com/usage", "API usage, if a key is ever used"),
    ]})
    for g in groups:
        g["links"] = [{"label": label, "url": url, "hint": hint} for label, url, hint in g["links"]]
    return groups


# ---- routes ----------------------------------------------------------------------


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/sources")
def sources():
    """Viewable sources with human-readable descriptions for the picker."""
    replays = []
    for rid in _replay_ids():
        d = config.REPLAY_DIR / rid
        summ = _json(d / "summary.json") or {}
        st = _json(d / "status.json") or {}
        replays.append({
            "id": rid,
            # actual sessions traded, not the requested range (which may include weekends)
            "start": min(summ.get("days") or {"": 0}) or summ.get("start"),
            "end": max(summ.get("days") or {"": 0}) or summ.get("end"),
            "days": len(summ.get("days", {})) or None,
            "pnl": round(summ["final_equity"] - summ["start_equity"], 2) if "final_equity" in summ else None,
            "classifiers": [c["id"] for c in st.get("classifiers", [])],
            "running": not summ,
        })
    return {"sources": ["live", *(r["id"] for r in replays)], "replays": replays, "mode": config.account_mode()}


@app.get("/api/data")
def data(source: str = "live"):
    base, book_dirs = _source_dir(source)
    books = {}
    for d in book_dirs:
        eq = _csv(d / "equity.csv")
        books[d.name] = {
            "equity": _equity_points(eq),
            "trades": _csv(d / "trades.csv")[-60:],
            "nav": _json(d / "nav.json"),
            "risk": _json(d / "risk.json"),
        }
    sims = _sim_dirs(source)
    if sims:  # every sim account's trades together; each account's own state is in status.json
        books["sim"] = {"equity": [], "trades": _sim_trades(source)[-60:], "nav": None, "risk": None, "accounts": len(sims)}
    return {
        "source": source,
        "status": _with_why(_json(base / "status.json")),
        "status_age_s": _age_seconds(base / "status.json"),
        "summary": _json(base / "summary.json"),
        "benchmark": _csv(base / "benchmark.csv"),
        "books": books,
    }


@app.get("/api/scoreboard")
def scoreboard(source: str = "live", all_days: bool | None = None):
    """Per-classifier and per-family results for each book, net of slippage (see scoreboard.py).
    The live board starts at the experiment's first day; `all_days` also shows the test sessions
    before it (display only: the go-live gate and promotion never count them). Unset, it's on
    until the start date (before it there's nothing else to show) and off from then on."""
    from trader import scoreboard as SB

    base, book_dirs = _source_dir(source)
    status = _json(base / "status.json") or {}
    # Probes never trade, so they aren't on the scoreboard.
    today = [c for c in status.get("classifiers") or [] if isinstance(c, dict) and c.get("id") and c.get("mode") != "probe"]
    if source == "live" and not today:  # before the open the runner hasn't loaded them yet
        today = _classifiers_file()
    families = {c["id"]: c.get("family") for c in today}
    since = None
    if source == "live":  # the runner's promotion record remembers retired classifiers' families
        promo = _json(config.RUNTIME_DIR / "promotion.json") or {}
        families = {k: v.get("family") for k, v in promo.items() if isinstance(v, dict)} | families
        since = {k: v["since"] for k, v in promo.items() if isinstance(v, dict) and v.get("since")}
    sim_ids = {c["id"] for c in today if c.get("mode") == "sim"}
    if all_days is None:
        from zoneinfo import ZoneInfo

        all_days = dt.datetime.now(ZoneInfo("America/New_York")).date() < SB.EXPERIMENT_START
    from_date = SB.EXPERIMENT_START.isoformat() if source == "live" and not all_days else None
    books = {}
    for d in book_dirs:
        eq = _csv(d / "equity.csv")
        # On the live board sim classifiers trade only their own accounts, so they're listed under "sim".
        current = {c["id"] for c in today} - (sim_ids if source == "live" else set())
        books[d.name] = SB.build(
            _csv(d / "trades.csv"), families, current, _num(eq[0].get("equity")) if eq else None,
            since=since if d.name == "paper" else None,  # the promotion record is kept on paper
            slippage_per_side_pct=SB.SLIPPAGE_PER_SIDE_PCT if source == "live" else 0.0,  # sim fills include it
            from_date=from_date,
        )
    if _sim_dirs(source) or (source == "live" and sim_ids):
        from trader.broker import SIM_START_CASH

        # Every sim account starts with the same cash, so % of it compares rules fairly. The
        # simulated fills already include slippage, and none of this counts towards promotion.
        books["sim"] = SB.build(_sim_trades(source), families, sim_ids, SIM_START_CASH, since=None,
                                slippage_per_side_pct=0.0, from_date=from_date)
    return {"source": source, "books": books,
            "experiment_start": SB.EXPERIMENT_START.isoformat() if source == "live" else None, "all_days": all_days}


def _num(v) -> float | None:
    try:
        return float(v) or None
    except (TypeError, ValueError):
        return None


def _classifiers_file() -> list[dict]:
    """Enabled classifiers' ids and families straight from classifiers.yaml (no validation)."""
    import yaml

    try:
        raw = yaml.safe_load(safeio.read_text(config.CLASSIFIERS_FILE)) or {}
    except (OSError, ValueError, yaml.YAMLError):  # ValueError: refused by safeio, or not UTF-8
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("classifiers") or [], list):
        return []
    return [{"id": c["id"], "family": "control" if c.get("control") else c.get("family"), "mode": c.get("mode", "shadow")}
            for c in raw.get("classifiers") or []
            if isinstance(c, dict) and c.get("id") and c.get("enabled", True) and c.get("mode") != "probe"]


PROBE_REPORT = "logs/probe_report.json"  # written by the post-close wrapper (trader probe-report --out)


@app.get("/api/probes")
def probes():
    """The latest probe report from the strategist's checkout, or null before the first one.
    Today's probe answers come with /api/data (status.json); this is the scored history."""
    path = config.STRATEGIST_ROOT / PROBE_REPORT
    try:
        report = json.loads(_strategist_text(path) or "null")
    except ValueError:
        report = None
    if not isinstance(report, dict) or not isinstance(report.get("probes"), dict):
        report = None
    return {"report": report, "age_s": _age_seconds(path) if report else None}


@app.get("/api/rules")
def rules():
    """Each classifier in classifiers.yaml with its defaults filled in, for the Rules page.
    A spec that doesn't validate is still shown, raw, with the reason. `problems` lists everything
    that makes the runner reject the whole file (it then trades nothing that day), except unknown
    features: those need the runner's sandboxed feature gate, which this process doesn't run."""
    import yaml
    from pydantic import ValidationError

    from trader.classifier import ClassifierSpec

    try:
        raw = yaml.safe_load(safeio.read_text(config.CLASSIFIERS_FILE)) or {}
    except (OSError, ValueError, yaml.YAMLError) as e:  # ValueError: refused by safeio, or not UTF-8
        return {"date": None, "classifiers": [], "problems": [], "error": f"couldn't read classifiers.yaml: {e}"}
    if not isinstance(raw, dict) or not isinstance(raw.get("classifiers") or [], list):
        return {"date": None, "classifiers": [], "problems": [],
                "error": "classifiers.yaml should be a mapping with a `classifiers:` list"}
    out, problems, seen = [], [], set()
    try:
        universe = set(config.universe())
    except Exception as e:  # a broken universe file breaks the runner too: say so rather than skip the check
        universe = None
        problems.append(f"couldn't read config/universe.yaml: {e}")
    for c in raw.get("classifiers") or []:
        if not isinstance(c, dict) or not c.get("id"):
            problems.append("an entry has no id")
            continue
        cid = str(c["id"])
        if cid in seen:
            problems.append(f"{cid}: the id is used twice")
        seen.add(cid)
        try:
            spec = ClassifierSpec.model_validate(c)
        except ValidationError as e:
            reason = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'spec'}: {err['msg']}" for err in e.errors())
            out.append({**jsonable_encoder(c), "invalid": reason})
            problems.append(f"{cid}: {reason}")
            continue
        outside = [sym for sym in spec.symbols if universe is not None and sym not in universe]
        if outside:
            problems.append(f"{cid}: {', '.join(outside)} not in config/universe.yaml")
        out.append(spec.model_dump(mode="json"))
    return {"date": str(raw["date"]) if raw.get("date") else None, "classifiers": out, "problems": problems}


@app.get("/api/overview")
def overview():
    """Links for the overview card, plus the deployed commit. Local git reads only: no network."""
    repo = github_repo(_git("config", "--get", "remote.origin.url"))  # the code repo
    deployed = deployed_commit()
    root = data_root()
    if root is None:  # monorepo: the data is in the code repo
        return {"repo": repo, "data_repo": repo, "split": False, "deployed": deployed, "config": None,
                "links": links(repo, deployed and deployed["sha"])}
    data_repo = config.SETTINGS.owner.github_repo
    cfg = deployed_commit(root)
    return {"repo": repo, "data_repo": data_repo, "split": True, "deployed": deployed, "config": cfg,
            "links": links(repo, deployed and deployed["sha"], {"repo": data_repo, "sha": cfg and cfg["sha"]})}


@app.get("/api/docs")
def docs():
    root = config.STRATEGIST_ROOT
    return {
        "strategy": _strategist_text(root / "state" / "strategy.md"),
        "classifiers": _strategist_text(config.CLASSIFIERS_FILE),
        "watchlist": _strategist_text(root / "state" / "watchlist.md"),
        "journal": {kind: _journal_names(kind) for kind in JOURNAL_KINDS},
    }


@app.get("/api/journal/{kind}/{name}")
def journal(kind: str, name: str):
    if kind not in JOURNAL_KINDS or name not in _journal_names(kind):
        raise HTTPException(404, "no such journal entry")
    return {"kind": kind, "name": name, "text": _strategist_text(config.STRATEGIST_ROOT / "journal" / kind / name)}


def _strategist_stamp() -> Path:
    return config.STRATEGIST_STAMP


@app.get("/api/health")
def health():
    status = _json(config.RUNTIME_DIR / "status.json") or {}
    stamp = _strategist_stamp()
    try:
        last_run = dt.datetime.fromtimestamp(stamp.stat().st_mtime).isoformat(timespec="minutes")
    except OSError:
        last_run = None
    disk = shutil.disk_usage(config.RUNTIME_DIR if config.RUNTIME_DIR.exists() else config.CODE_ROOT)
    return {
        "status_age_s": _age_seconds(config.RUNTIME_DIR / "status.json"),
        "market_time": status.get("market_time"),
        "decision_calls": status.get("decision_calls"),
        "decision_cost_usd": status.get("decision_cost_usd"),
        "decision_errors": status.get("decision_errors"),
        "strategist_last_run": last_run,
        "disk_free_gb": round(disk.free / 1e9, 1),
        "disk_used_pct": round(disk.used / disk.total * 100, 1),
        "mode": config.account_mode(),
        "golive": _golive_summary(),
    }


def _golive_summary() -> str:
    from trader import golive

    ov = golive.override()
    if ov != "auto":
        return f"forced {ov} (config/mode.yaml)"
    st = golive.load_state()
    s = st["status"]
    if s == "corrupt":
        return f"CORRUPT golive.json ({st['error']}): paper until HOLD LIVE or release-live"
    if s == "armed":
        return f"ARMED: live after {st.get('sessions_left', '?')} more session(s)"
    if s == "pending" and st.get("last_gate"):
        return "pending: " + (", ".join(st["last_gate"]["blocking"]) or "gate passing")
    return s


def _require_same_origin_json(request: Request) -> None:
    """Refuse cross-site control requests (CSRF) before STOP or HOLD LIVE acts.

    The Tailscale identity header rides along on any request from the human's browser,
    so it doesn't show the page made the request. A cross-site HTML form can only send
    text/plain, form-urlencoded or multipart; a cross-site fetch with application/json
    needs a CORS preflight, which this app never approves. So requiring the JSON type
    is the defence, and Sec-Fetch-Site (sent by current browsers) refuses anything not
    same-origin as well. The Origin isn't compared with Host: behind `tailscale serve`
    Host is the socket's `localhost`, so that check would refuse the real page.
    """
    ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(415, "controls need Content-Type: application/json")
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        raise HTTPException(403, "cross-site control request refused")


@app.post("/api/hold-live")
async def hold_live(request: Request):
    _require_same_origin_json(request)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict) or body.get("confirm") != "HOLD":
        raise HTTPException(400, 'send {"confirm": "HOLD"}')
    from trader import golive
    from trader.alerts import notify

    return {"result": golive.hold(notify, by=request.headers.get("Tailscale-User-Login", "dashboard"))}


@app.post("/api/stop")
async def stop(request: Request):
    _require_same_origin_json(request)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict) or body.get("confirm") != "STOP":
        raise HTTPException(400, 'send {"confirm": "STOP"}')
    from trader.runner import request_stop

    return {"result": request_stop()}
