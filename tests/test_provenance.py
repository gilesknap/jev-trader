"""Provenance on decision and trade rows: model, code commit and the spec hash a trade opened under."""

import csv
import datetime as dt
import gzip
import json

import pandas as pd

from test_engine import Always, run, spec
from trader import compact, config, golive
from trader import scoreboard as SB
from trader.broker import Fill, SimBroker
from trader.data import ET
from trader.engine import TRADE_COLS, Book, Entry
from trader.provenance import Provenance, code_sha, model_id

OLD_COLS = "time,book,classifier,symbol,side,qty,price,notional,reason,pnl,pnl_pct"
OLD_ROWS = (
    "2026-09-21T10:00,paper,x,SPY,buy,1.000000,100.0000,100.00,ENTER,,\n"
    "2026-09-21T11:00,paper,x,SPY,sell,1.000000,101.0000,101.00,target,1.00,1.000\n"
)


def test_trade_and_decision_rows_carry_provenance(tmp_path, session):
    s = spec()
    _, trades = run(tmp_path, session(path=[100.0] * 390), [s], Always())
    h = golive.spec_hash(s, golive.custom_features_digest())
    assert list(trades.columns) == TRADE_COLS
    assert set(trades.spec_hash) == {h} and set(trades.model) == {"Always"}
    assert set(trades.code_sha) == {code_sha()}
    with gzip.open(tmp_path / "decisions" / "2026-09-21.jsonl.gz", "rt") as f:
        rows = [json.loads(line) for line in f]
    assert rows and all(r["h"] == h and r["mv"] == "Always" and r["cv"] == code_sha() for r in rows)


def test_code_sha_and_model_are_best_effort(tmp_path):
    code_sha.cache_clear()
    assert len(code_sha()) == 12  # the tests run from a git checkout
    assert code_sha(tmp_path) == ""  # not a repository: blank, never an error

    class M:
        model = "vendor/model-1"

    class Stub:
        offline = True

    assert (model_id(M()), model_id(Stub())) == ("vendor/model-1", "stub")


def test_sell_keeps_the_spec_hash_it_opened_under(tmp_path):
    """A restart on an edited spec stamps new buys with the new hash, but a position held across it
    closes under the spec it opened with (and one opened before provenance existed stays blank)."""
    book = Book("paper", SimBroker(1000.0), tmp_path)
    book.provenance = Provenance("m", "c0de", {"x": "new"})
    now = dt.datetime(2026, 9, 21, 11, 0, tzinfo=ET)
    for sym, h in (("SPY", "old"), ("QQQ", "")):
        e = Entry("x", 1.0, 100.0, 99.0, 102.0, now, spec_hash=h)
        book.entries[sym] = e
        book.record_exit(sym, e, Fill(sym, "sell", 1.0, 101.0, now), "target")
    book.append_trade({"time": "2026-09-21T11:05", "book": "paper", "classifier": "x", "symbol": "IWM", "side": "buy"})
    rows = list(csv.DictReader((tmp_path / "trades.csv").open()))
    assert [r["spec_hash"] for r in rows] == ["old", "", "new"]
    assert {r["model"] for r in rows} == {"m"} and {r["code_sha"] for r in rows} == {"c0de"}


def test_old_trades_file_gains_the_columns_and_old_readers_still_work(tmp_path):
    (tmp_path / "trades.csv").write_text(OLD_COLS + "\n" + OLD_ROWS)
    book = Book("paper", SimBroker(1000.0), tmp_path)
    book.provenance = Provenance("m", "c0de", {"x": "h1"})
    book.append_trade(
        {
            "time": "2026-09-22T10:00",
            "book": "paper",
            "classifier": "x",
            "symbol": "SPY",
            "side": "buy",
            "qty": "1",
            "price": "100",
            "notional": "100",
            "reason": "ENTER",
            "pnl": "",
            "pnl_pct": "",
        }
    )
    df = pd.read_csv(tmp_path / "trades.csv", dtype=str, keep_default_na=False)
    assert list(df.columns) == TRADE_COLS and len(df) == 3
    assert list(df.spec_hash) == ["", "", "h1"]
    rows = list(csv.DictReader((tmp_path / "trades.csv").open()))
    assert len(SB.closed_trades(rows)) == 1  # readers by column name are unaffected


def test_a_file_that_cant_be_upgraded_keeps_its_header(tmp_path):
    """An unrecognised header is left alone: new rows are written under it, never misaligned."""
    (tmp_path / "trades.csv").write_text(OLD_COLS + ",note\n")
    book = Book("paper", SimBroker(1000.0), tmp_path)
    book.provenance = Provenance("m", "c0de", {"x": "h1"})
    book.append_trade({"time": "2026-09-22T10:00", "book": "paper", "classifier": "x", "symbol": "SPY", "side": "buy"})
    rows = list(csv.reader((tmp_path / "trades.csv").open()))
    assert rows[0] == OLD_COLS.split(",") + ["note"] and len(rows[1]) == len(rows[0])


def test_archive_widens_the_repo_trades_log(tmp_path, monkeypatch):
    rt = tmp_path / "runtime"
    (rt / "books" / "paper").mkdir(parents=True)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "trades.csv").write_text(OLD_COLS + "\n" + OLD_ROWS)
    # the runner upgraded its own copy and added a row
    (rt / "books" / "paper" / "trades.csv").write_text(OLD_COLS + "\n" + OLD_ROWS)
    Book("paper", SimBroker(1000.0), rt / "books" / "paper").append_trade(
        {
            "time": "2026-09-22T10:00",
            "book": "paper",
            "classifier": "x",
            "symbol": "SPY",
            "side": "buy",
            "spec_hash": "h1",
        }
    )
    monkeypatch.setattr(config, "RUNTIME_DIR", rt)
    monkeypatch.setattr(compact, "LOGS", logs)
    assert compact.archive()["trades_added"] == 1
    assert compact.archive()["trades_added"] == 0  # idempotent across the header change
    df = pd.read_csv(logs / "trades.csv", dtype=str, keep_default_na=False)
    assert list(df.columns) == TRADE_COLS and list(df.spec_hash) == ["", "", "h1"]


def test_archive_leaves_a_row_still_being_written_for_next_time(tmp_path, monkeypatch):
    """The runner may be mid-append: a last line without its newline is torn. Archived now, its
    short copy would stay in the log beside the full row once that was archived too."""
    rt = tmp_path / "runtime"
    src = rt / "books" / "paper" / "trades.csv"
    src.parent.mkdir(parents=True)
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(config, "RUNTIME_DIR", rt)
    monkeypatch.setattr(compact, "LOGS", logs)
    full = OLD_COLS + "\r\n" + OLD_ROWS.replace("\n", "\r\n")  # as csv writes it
    last = full.rstrip("\r\n").rsplit("\r\n", 1)[1]
    src.write_text(full[: -len(last) - 2] + last[:20], newline="")  # torn mid-row
    assert compact.archive()["trades_added"] == 1
    src.write_text(full, newline="")  # the runner finished the line
    assert compact.archive()["trades_added"] == 1
    assert compact.archive()["trades_added"] == 0
    rows = list(csv.reader((logs / "trades.csv").open(newline="")))
    assert rows == [r.split(",") for r in [OLD_COLS] + OLD_ROWS.splitlines()]
    src.write_text(OLD_COLS[:10], newline="")  # even the header unfinished: nothing, no crash
    assert compact._merge_csv(src, tmp_path / "other.csv") == 0
