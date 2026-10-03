"""Command-line entry point: `trader <command>`."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from trader import config
from trader import features as F


def _decider(name: str, secrets):
    from trader.jev import JevClient, StubDecider

    if name == "stub":
        return StubDecider()
    return JevClient(secrets["OPENROUTER_API_KEY"])


def _gate_samples(source: str = "alpaca"):
    """Recent SPY/QQQ sessions for the custom-feature gate: (bars, prev_day, spy) tuples."""
    from trader.replay import load_sessions

    secrets = config.load_secrets()
    end = dt.date.today() - dt.timedelta(days=1)
    # Count sessions, not calendar days: a run of holidays (or a data gap) can leave a short
    # window with too few sessions, and an empty sample set would reject every custom feature.
    for lookback in (7, 21, 60):
        sessions = load_sessions(["SPY", "QQQ"], end - dt.timedelta(days=lookback), end, secrets, source)
        spy = sessions.get("SPY", {})
        if len(spy) >= 3:
            break
    else:  # a data outage, not a bug in the features: the caller reports it, nothing is rejected
        raise RuntimeError(f"only {len(spy)} SPY session(s) in the last {lookback} days")
    days = sorted(spy)[-3:]
    samples = []
    for sym in ("SPY", "QQQ"):
        per = sessions.get(sym, {})
        for i, d in enumerate(days[1:], 1):
            if d in per and days[i - 1] in per:
                samples.append((per[d], per[days[i - 1]], spy[d]))
    return samples


def _specs(path: Path, source: str = "alpaca"):
    """Gate custom features in the sandbox (installed as F.SANDBOX), then validate the spec file."""
    from trader.classifier import load_specs
    from trader.features.harness import run_gate

    if F.SANDBOX is None:
        run_gate(config.CUSTOM_FEATURES_DIR, _gate_samples(source))
    return load_specs(path, F.known_features(), set(config.universe()))


def _has_custom_features(directory: Path) -> bool:
    """Whether the gate has anything to run (the sandbox's own test): a refused directory has
    nothing, and run_gate reports it without needing sample bars."""
    from trader.safeio import read_sources

    try:
        sources, _ = read_sources(directory)
    except (OSError, ValueError):
        return False
    return any(not name.startswith("_") for name in sources)


def cmd_validate(a):
    from trader.features.harness import run_gate

    samples = []
    if _has_custom_features(config.CUSTOM_FEATURES_DIR):  # no custom features: no fetch to fail
        try:
            samples = _gate_samples(a.source)
        except Exception as e:  # a data outage isn't a bug in the specs: one line, not a traceback
            sys.exit(f"custom features NOT gated: couldn't fetch {a.source} sample bars ({type(e).__name__}: {e})")
    report = run_gate(config.CUSTOM_FEATURES_DIR, samples)
    print(json.dumps({"custom_ok": report.ok, "custom_features": report.features, "errors": report.errors}, indent=1))
    try:
        specs = _specs(Path(a.file))
        labels = [p for p in (s.family_problem() for s in specs) if p]
        if labels:
            raise ValueError("; ".join(labels))
        from trader.golive import START_DATE

        plumbing = [s.id for s in specs if s.id.startswith("test_")]
        if plumbing and dt.date.today() >= START_DATE:  # the runner drops these: say so before it happens
            raise ValueError(f"the test_ prefix is reserved for pre-launch plumbing and the runner ignores it "
                             f"from {START_DATE}: rename or remove {plumbing}")
        print(f"classifiers OK: {[s.id for s in specs]}")
    except Exception as e:
        print(f"classifiers INVALID: {e}")
        sys.exit(1)
    sys.exit(0 if report.ok else 1)


def cmd_replay(a):
    from trader.replay import replay

    import re

    run_id = a.name or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}", run_id) or run_id in (".", ".."):
        sys.exit(f"invalid replay name {run_id!r}: letters, digits, '_', '-', '.' only")
    secrets = config.load_secrets()
    specs = _specs(Path(a.file), a.source)
    if a.only:
        specs = [s for s in specs if s.id in a.only.split(",")]
    run_dir = config.REPLAY_DIR / run_id
    end = dt.date.fromisoformat(a.end) if a.end else dt.date.today() - dt.timedelta(days=1)
    start = dt.date.fromisoformat(a.start) if a.start else end - dt.timedelta(days=a.days)
    summary = replay(
        specs, start, end, _decider(a.decider, secrets), set(config.universe()), run_dir,
        secrets, a.source, a.cash, a.pace,
    )
    print(json.dumps(summary, indent=1))
    print(f"run dir: {run_dir}")


def cmd_probe_report(a):
    import yaml

    from trader import probe
    from trader.classifier import ClassifierSpec
    from trader.data import ET, fetch, split_sessions
    from trader.market_calendar import load_calendar

    if a.replay:
        dirs = [config.REPLAY_DIR / a.replay / "decisions"]
    else:
        dirs = [config.RUNTIME_DIR / "decisions", config.STRATEGIST_ROOT / "logs" / "decisions"]
    end = dt.date.fromisoformat(a.end) if a.end else None
    start = dt.date.fromisoformat(a.start) if a.start else (None if a.replay else (end or dt.date.today()) - dt.timedelta(days=a.days))
    only = set(a.only.split(",")) if a.only else None
    rows = probe.load_rows(probe.decision_files(dirs, start, end), only)
    if rows.empty:
        sys.exit("no probe decisions found")
    horizons = [int(h) for h in a.horizons.split(",")]
    days = sorted(dt.date.fromisoformat(d) for d in rows.day.unique())
    t0 = dt.datetime.combine(days[0], dt.time(0), ET)
    # SIP's most recent 15 minutes aren't on the free plan: stop short of them during a session.
    t1 = min(dt.datetime.combine(days[-1] + dt.timedelta(days=1), dt.time(0), ET),
             dt.datetime.now(ET) - dt.timedelta(minutes=16))
    secrets = config.load_secrets()
    calendar = load_calendar(secrets, days[0], days[-1])  # early closes cut horizons at their own flatten
    raw = fetch(sorted(rows.s.unique()), t0, t1, secrets, "alpaca")
    rows = probe.forward_returns(rows, {s: calendar.trim(split_sessions(b)) for s, b in raw.items()}, horizons, calendar)
    thresholds = {}
    try:  # each spec on its own, so one bad spec doesn't cost the others their thresholds
        raw = (yaml.safe_load(Path(a.file).read_text()) or {}).get("classifiers") or []
    except Exception as e:
        raw = []
        print(f"note: couldn't read {a.file} ({e})", file=sys.stderr)
    for c in raw:
        try:
            spec = ClassifierSpec.model_validate(c)
            thresholds[spec.id] = spec.entry.threshold
        except Exception:
            pass
    missing = sorted(set(rows.c) - set(thresholds))
    if missing:  # scoring doesn't need the spec: fall back to the default threshold
        print(f"note: no valid spec for {missing}; scored at threshold {probe.DEFAULT_THRESHOLD}", file=sys.stderr)
    out = {"generated": dt.datetime.now(ET).isoformat(timespec="minutes"), "first_day": str(days[0]),
           "last_day": str(days[-1]), "horizons": horizons, "skipped_lines": rows.attrs.get("skipped", 0),
           "probes": probe.score(rows, horizons, thresholds)}
    if a.out:  # atomic, so the dashboard never reads half a report
        path = Path(a.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(probe.finite(out), indent=1, allow_nan=False))
        tmp.replace(path)
        print(f"probe report: {len(rows)} rows, {len(days)} days, {sorted(out['probes'])} -> {path}")
    else:
        print(json.dumps(probe.finite(out), indent=1, allow_nan=False))


SERVICES_ENV = Path.home() / ".config" / "trading" / "services.env"


def _require_runtime(stop: bool = False) -> None:
    """Refuse a state-changing command that would write to a stray runtime dir (#108).

    Without TRADER_RUNTIME, RUNTIME_DIR falls back to CODE_ROOT/runtime. On the production box
    that is never the real one: a command run without services.env loaded would create it,
    write there and report success. So refuse when this account has a services.env (production:
    load it) or when the fallback doesn't exist yet (it would be created). Development, with an
    existing CODE_ROOT/runtime and no services.env, is unchanged.
    """
    if os.environ.get("TRADER_RUNTIME"):  # empty would make RUNTIME_DIR the cwd
        return
    if SERVICES_ENV.exists():
        why = f"{SERVICES_ENV} exists, so this is a production account"
        fix = f"Load the services environment first:  set -a && . {SERVICES_ENV} && set +a"
    elif not config.RUNTIME_DIR.exists():
        why = f"the fallback {config.RUNTIME_DIR} doesn't exist and would be created"
        fix = "No services.env here: export TRADER_RUNTIME=<runtime dir> first."
    else:
        return
    sys.exit(f"REFUSING: TRADER_RUNTIME is not set and {why}; nothing was changed.\n{fix}"
             + ("\nIn an emergency, the dashboard's STOP button works without this." if stop else ""))


def cmd_config_get(a):
    try:
        v = config.setting(a.key)
    except KeyError:
        sys.exit(f"no such setting in {config.SETTINGS_FILE}: {a.key}")
    print(json.dumps(v) if isinstance(v, dict) else ",".join(map(str, v)) if isinstance(v, list) else v)


def cmd_config_render_deploy(a):
    """Regenerate the deploy files that can't read config.yaml, or --check that they match. Templates
    come from the code root; config.yaml is read, and the files written, under the data root
    (--data-root, else TRADER_DATA_ROOT, else the code root). The settings are loaded from that
    root's config.yaml, never from the import-time SETTINGS."""
    root = Path(a.data_root) if a.data_root else config.DATA_ROOT
    try:
        files = config.render_deploy(config.CODE_ROOT, root)
    except (OSError, config.SettingsError) as e:
        sys.exit(f"render-deploy: {e}")
    stale = [p for p, text in files.items() if not (root / p).exists() or (root / p).read_text() != text]
    if a.check:
        if stale:
            sys.exit(f"out of date with {root / 'config.yaml'}: " + ", ".join(stale)
                     + "\nRun: uv run trader config render-deploy" + (f" --data-root {root}" if a.data_root else ""))
        print("deploy files match config.yaml")
        return
    for p in stale:
        (root / p).parent.mkdir(parents=True, exist_ok=True)
        (root / p).write_text(files[p])
    print("updated: " + ", ".join(stale) if stale else "deploy files already match config.yaml")


def cmd_deploy_plan(a):
    from trader.deploy import plan

    pl = plan(Path(a.repo), a.base, a.target)
    print("\n".join(pl.lines()))
    sys.exit(pl.code)


def cmd_run(a):
    from trader.runner import run_session

    sys.exit(run_session(decider_name=a.decider, file=Path(a.file)))


def cmd_stop(a):
    from trader.runner import request_stop

    print(request_stop())


def cmd_rebase_paper(a):
    from trader.runner import rebase_paper

    print(rebase_paper())


def _book_name(v: str) -> str:
    import re

    if not re.fullmatch(r"paper|live|sim/[a-z0-9_]{1,40}", v):
        raise argparse.ArgumentTypeError("expected paper, live or sim/<classifier id>")
    return v


def cmd_clear_halt(a):
    from trader.runner import clear_halt

    print(clear_halt(a.book))


def cmd_session(a):
    from trader.runner import session_info

    info = session_info()
    print(json.dumps(info))
    sys.exit(0 if info else 2)


def cmd_watchdog(a):
    from trader.runner import watchdog

    print(watchdog())


def cmd_hold_live(a):
    from trader import golive
    from trader.alerts import notify

    print(golive.hold(notify, by="cli"))


def cmd_release_live(a):
    from trader import golive
    from trader.alerts import notify

    print(golive.release(notify))


def cmd_golive_status(a):
    from trader import config as C
    from trader import golive

    try:
        gate = golive.evaluate_gate(golive.PAPER_BOOK).__dict__
    except Exception as e:  # bad paper logs: show the state and what broke, not a traceback (#63)
        gate = {"error": f"{golive.EVIDENCE_ERROR} ({type(e).__name__}: {e})"}
    try:
        C.require_mode_file()  # split mode: a missing mode.yaml is an error, not a silent "auto"
    except C.SettingsError as e:
        sys.exit(str(e))
    print(json.dumps({"override": golive.override(), "effective": C.account_mode(), "state": golive.load_state(),
                      "gate_now": gate}, indent=1, default=str))


def cmd_housekeeping(a):
    from trader.housekeeping import run

    print("\n".join(run()))


def cmd_dashboard(a):
    import uvicorn

    if a.uds:
        # Unix socket in the runner's private runtime dir: only runner and tailscaled (root)
        # can connect, so other local users can't forge the Tailscale identity header.
        uvicorn.run("trader.dashboard:app", uds=a.uds, log_level="warning")
    else:
        uvicorn.run("trader.dashboard:app", host=a.host, port=a.port, log_level="warning")


def cmd_compact(a):
    from trader.compact import compact

    print(json.dumps(compact(dry_run=a.dry_run, scope=a.scope), indent=1))


def cmd_archive(a):
    from trader.compact import archive

    print(json.dumps(archive(), indent=1))


def cmd_features(a):
    from trader.features.harness import run_gate

    report = run_gate(config.CUSTOM_FEATURES_DIR, _gate_samples())
    for n in sorted(F.REGISTRY):
        doc = (F.REGISTRY[n].__doc__ or "").strip().splitlines()
        print(f"{n:28} [lib] {doc[0] if doc else ''}")
    for n in report.features:
        print(f"{n:28} [custom] {report.docs.get(n, '')}")
    for f, e in report.errors.items():
        print(f"REJECTED {f}: {e}")


def build_parser() -> argparse.ArgumentParser:
    """The `trader` command line (also rendered into the docs' CLI reference)."""
    p = argparse.ArgumentParser(prog="trader", description="Run, inspect and control the trading system.")
    sub = p.add_subparsers(required=True)
    default_file = str(config.CLASSIFIERS_FILE)

    s = sub.add_parser("validate", help="gate custom features and validate classifiers.yaml")
    s.add_argument("--file", default=default_file)
    s.add_argument("--source", default="alpaca", choices=["alpaca", "yfinance"])
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("replay", help="replay historical sessions through the engine")
    s.add_argument("--file", default=default_file)
    s.add_argument("--only", help="comma-separated classifier ids")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--days", type=int, default=5)
    s.add_argument("--source", default="alpaca", choices=["alpaca", "yfinance"])
    s.add_argument("--decider", default="jev", choices=["jev", "stub"])
    s.add_argument("--cash", type=float, help="starting cash (default: capital.replay_cash in config.yaml)")
    s.add_argument("--pace", type=float, default=0.0, help="seconds to sleep per simulated minute")
    s.add_argument("--name", help="run id (default: timestamp)")
    s.set_defaults(fn=cmd_replay)

    s = sub.add_parser("probe-report", help="score mode: probe decisions against forward returns")
    s.add_argument("--replay", help="score a replay run's decisions instead of the runner's")
    s.add_argument("--only", help="comma-separated probe ids")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--days", type=int, default=30, help="look-back when --start isn't given (runner logs)")
    s.add_argument("--horizons", default="15,30,60", help="forward-return horizons in minutes")
    s.add_argument("--file", default=default_file, help="classifiers file for each probe's entry threshold")
    s.add_argument("--out", help="write the report here (JSON) instead of printing it")
    s.set_defaults(fn=cmd_probe_report)

    s = sub.add_parser("run", help="run today's live/paper session (runner daemon)")
    s.add_argument("--file", default=default_file)
    s.add_argument("--decider", default="jev", choices=["jev", "stub"])
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("stop", help="STOP: flatten and halt for the rest of the day")
    s.set_defaults(fn=cmd_stop)

    s = sub.add_parser("clear-halt", help="human-only: clear a sticky halt on a book")
    s.add_argument("book", type=_book_name, help="paper, live, or sim/<classifier id>")
    s.set_defaults(fn=cmd_clear_halt)

    s = sub.add_parser("session", help="print today's session times; exit 2 if market closed")
    s.set_defaults(fn=cmd_session)

    s = sub.add_parser("watchdog", help="alert on stale runner or strategist")
    s.set_defaults(fn=cmd_watchdog)

    s = sub.add_parser("hold-live", help="veto automatic go-live (paper until release-live)")
    s.set_defaults(fn=cmd_hold_live)

    s = sub.add_parser("release-live", help="human: re-arm automatic go-live evaluation")
    s.set_defaults(fn=cmd_release_live)

    s = sub.add_parser("golive", help="show go-live gate and state")
    s.set_defaults(fn=cmd_golive_status)

    s = sub.add_parser("housekeeping", help="daily checks: API credit, token expiry, undeployed merges, disk")
    s.set_defaults(fn=cmd_housekeeping)

    s = sub.add_parser("rebase-paper", help="human: restart the paper book's NAV after an Alpaca paper reset")
    s.set_defaults(fn=cmd_rebase_paper)

    s = sub.add_parser("deploy-plan", help="deploy: list merged PRs since BASE; exit 3 if anything needs review")
    s.add_argument("--repo", default=".")
    s.add_argument("--base", default="HEAD")
    s.add_argument("--target", required=True)
    s.set_defaults(fn=cmd_deploy_plan)

    s = sub.add_parser("config", help="read deployment settings (config.yaml) or regenerate the deploy files from them")
    cs = s.add_subparsers(required=True)
    c = cs.add_parser("get", help="print one setting, e.g. owner.github_repo")
    c.add_argument("key")
    c.set_defaults(fn=cmd_config_get)
    c = cs.add_parser("render-deploy", help="regenerate deploy/ files from deploy/templates/ and config.yaml")
    c.add_argument("--check", action="store_true", help="only report whether they match (exit 1 if not)")
    c.add_argument("--data-root", help="data checkout holding config.yaml and the rendered files "
                                       "(default: TRADER_DATA_ROOT, else the code root)")
    c.set_defaults(fn=cmd_config_render_deploy)

    s = sub.add_parser("dashboard", help="serve the dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8321)
    s.add_argument("--uds", help="serve on this Unix socket instead of host:port (production)")
    s.set_defaults(fn=cmd_dashboard)

    s = sub.add_parser("compact", help="apply retention policy to journals and logs")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--scope", default="repo", choices=["repo", "runtime"])
    s.set_defaults(fn=cmd_compact)

    s = sub.add_parser("archive", help="copy the runner's day logs into the repo's logs/")
    s.set_defaults(fn=cmd_archive)

    s = sub.add_parser("features", help="list available features")
    s.set_defaults(fn=cmd_features)

    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    # Commands that write runtime state (the runner's own units always have TRADER_RUNTIME set).
    if a.fn in (cmd_run, cmd_stop, cmd_clear_halt, cmd_watchdog, cmd_hold_live, cmd_release_live,
                cmd_rebase_paper) or (a.fn is cmd_compact and a.scope == "runtime"):
        _require_runtime(stop=a.fn is cmd_stop)
    a.fn(a)


if __name__ == "__main__":
    main()
