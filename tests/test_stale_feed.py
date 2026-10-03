"""A stale IEX stream keeps held positions' stops working from REST bars (#50, item 1)."""

import datetime as dt
import threading
import time

import pandas as pd

from trader import engine as E
from trader.broker import SimBroker
from trader.data import ET
from trader.engine import Book, Engine
from trader import runner as R
from trader.runner import RestBars, stream_bars, tick_bars

from test_engine import Always, spec

DAY = dt.date(2026, 9, 21)
OPEN = dt.datetime.combine(DAY, dt.time(9, 30), ET)
CLOSE = dt.datetime.combine(DAY, dt.time(16), ET)


class PaperSim(SimBroker):
    """A SimBroker the engine treats as a paper account (the only books polled over REST)."""

    name = "paper"


def at(hh, mm):
    return dt.datetime.combine(DAY, dt.time(hh, mm), ET)


def rows_of(bars, until):
    """Websocket rows as `on_bar` appends them, for bars that started before `until`."""
    b = bars[bars.index < until]
    return [(ts, *r) for ts, r in zip(b.index, b.itertuples(index=False))]


class Fetch:
    """Fake REST IEX fetch over the full day's bars, recording each call."""

    def __init__(self, day_bars, fail=None, delay=0.0, hold=None):
        self.day_bars, self.fail, self.delay, self.hold, self.calls = day_bars, fail, delay, hold, []

    def __call__(self, syms, start, end):
        self.calls.append((tuple(syms), start, end))
        if self.delay:
            time.sleep(self.delay)
        if self.hold is not None:  # a hung call: answers only once the test releases it
            self.hold.wait(30)
        if self.fail:
            raise self.fail
        return {s: b[(b.index >= start) & (b.index <= end)] for s, b in self.day_bars.items() if s in syms}


class Alerts:
    def __init__(self):
        self.sent, self.last = [], {}

    def every(self, key, level, msg, seconds=600):  # Engine._alert_every's throttle, on a fake clock
        if key not in self.last:
            self.last[key] = True
            self.sent.append(msg)


def paper_engine(tmp_path, held="XLV", specs=(), decider=None):
    book = Book("paper", PaperSim(1000.0), tmp_path / "paper")
    eng = Engine(list(specs), {"live": book, "shadow": book}, decider or Always(), {"SPY", "XLV", "QQQ"}, tmp_path)
    eng.start_day(DAY, {})
    t0 = at(9, 40)
    book.broker.buy_notional(held, 100, 100.0, t0, "x")
    book.entries[held] = E.Entry("t", book.broker.positions[held].qty, 100.0, 99.5, 101.0, t0)
    return eng, book


def drive(eng, book, rest, rows, ticks):
    """The runner's tick loop: stream bars, REST poll, merge, tick."""
    for tick in ticks:
        live = stream_bars(rows(tick), tick)
        mtc = (CLOSE - tick).total_seconds() / 60
        bars, feed_spy = tick_bars(eng, rest, live, tick, mtc)
        eng.tick(tick, bars, mtc, feed_spy=feed_spy)


def minutes(a, b):
    return [a + dt.timedelta(minutes=i) for i in range(int((b - a).total_seconds() // 60) + 1)]


def test_stop_hit_during_an_outage_is_enforced_from_rest_bars(tmp_path, session):
    spy = session(path=[100.0] * 120)
    xlv = session(path=[100.0] * 35 + [99.0] * 85)  # falls through the 99.5 stop at 10:05
    outage = at(10, 0)  # the stream delivers nothing from 10:00
    eng, book = paper_engine(tmp_path)
    fetch, alerts = Fetch({"SPY": spy, "XLV": xlv}), Alerts()
    rest = RestBars(fetch, OPEN, alerts.every)
    rows = lambda tick: {"SPY": rows_of(spy, min(tick, outage)), "XLV": rows_of(xlv, min(tick, outage))}
    drive(eng, book, rest, rows, minutes(at(9, 41), at(10, 10)))
    assert "XLV" not in book.entries
    t = pd.read_csv(tmp_path / "paper" / "trades.csv")
    assert t.reason.iloc[-1] == "stop" and t.time.iloc[-1].startswith("2026-09-21T10:06")
    assert fetch.calls and all(c[0] == ("XLV",) for c in fetch.calls)  # held symbols only
    assert not alerts.sent


def test_without_rest_bars_the_same_outage_leaves_the_stop_unenforced(tmp_path, session):
    """The control for the test above: the stream alone never shows the fall."""
    spy = session(path=[100.0] * 120)
    xlv = session(path=[100.0] * 35 + [99.0] * 85)
    eng, book = paper_engine(tmp_path)
    rest = RestBars(Fetch({}, fail=RuntimeError("down")), OPEN, Alerts().every)
    rows = lambda tick: {"SPY": rows_of(spy, min(tick, at(10, 0))), "XLV": rows_of(xlv, min(tick, at(10, 0)))}
    drive(eng, book, rest, rows, minutes(at(9, 41), at(10, 10)))
    assert "XLV" in book.entries


def test_fetch_failure_falls_back_without_raising_and_alerts_once(tmp_path, session):
    spy = session(path=[100.0] * 120)
    xlv = session(path=[100.0] * 120)
    eng, book = paper_engine(tmp_path)
    fetch, alerts = Fetch({}, fail=ConnectionError("503")), Alerts()
    rest = RestBars(fetch, OPEN, alerts.every)
    rows = lambda tick: {"SPY": rows_of(spy, min(tick, at(10, 0))), "XLV": rows_of(xlv, min(tick, at(10, 0)))}
    drive(eng, book, rest, rows, minutes(at(9, 41), at(10, 20)))
    assert len(fetch.calls) > 5 and len(alerts.sent) == 1 and "503" in alerts.sent[0]
    assert "XLV" in book.entries and not rest.bars  # as before: nothing invented, nothing exited


def test_a_hung_fetch_is_cut_off_at_the_timeout(tmp_path, session):
    spy = session(path=[100.0] * 120)
    release = threading.Event()
    fetch, alerts = Fetch({"XLV": spy}, hold=release), Alerts()
    rest = RestBars(fetch, OPEN, alerts.every, timeout=0.1)
    tick = at(10, 10)
    live = stream_bars({"SPY": rows_of(spy, at(10, 0))}, tick)
    try:
        rest.poll(tick, live, {"XLV"})  # returns although the fetch never answers: cut off at the timeout
        rest.poll(tick + dt.timedelta(minutes=1), live, {"XLV"})  # still running: not started twice
        assert not release.is_set() and len(fetch.calls) == 1
        assert len(alerts.sent) == 1 and "no answer in 0.1 s" in alerts.sent[0] and not rest.bars
    finally:
        release.set()


def test_no_duplicate_or_future_bars_when_the_stream_resumes(session):
    spy = session(path=[100.0] * 120)
    xlv = session(path=[100.0 + i / 100 for i in range(120)])
    fetch = Fetch({"SPY": spy, "XLV": xlv})
    rest = RestBars(fetch, OPEN, Alerts().every)
    for tick in minutes(at(10, 4), at(10, 8)):  # stale from 10:04; REST fills 10:00-10:07
        rest.poll(tick, stream_bars({"SPY": rows_of(spy, at(10, 0))}, tick), {"XLV"})
    assert rest.bars["XLV"].index[-1] == pd.Timestamp(at(10, 7))  # never the bar still forming at the tick
    # The stream resumes, replaying 10:05 on with different prices: it wins those minutes.
    resumed = xlv.copy()
    resumed.loc[resumed.index >= pd.Timestamp(at(10, 5)), "close"] += 1
    rows = {
        "SPY": rows_of(spy, at(10, 12)),
        "XLV": rows_of(xlv, at(10, 0)) + rows_of(resumed[resumed.index >= pd.Timestamp(at(10, 5))], at(10, 12)),
    }
    tick = at(10, 12)
    live = stream_bars(rows, tick)
    rest.poll(tick, live, {"XLV"})
    assert len(fetch.calls) == 5  # healthy again: no more polling
    merged = rest.merge(live)["XLV"]
    assert merged.index.is_unique and merged.index.is_monotonic_increasing
    assert merged.index[-1] < pd.Timestamp(tick)
    assert list(merged.index) == list(xlv.index[xlv.index < pd.Timestamp(tick)])  # 10:00-10:04 filled from REST
    assert (
        merged.close[merged.index >= pd.Timestamp(at(10, 5))]
        == resumed.close[(resumed.index >= pd.Timestamp(at(10, 5))) & (resumed.index < pd.Timestamp(tick))]
    ).all()


def test_nothing_is_fetched_when_the_feed_is_healthy_or_nothing_is_held(session):
    spy = session(path=[100.0] * 120)
    fetch = Fetch({"SPY": spy})
    rest = RestBars(fetch, OPEN, Alerts().every)
    for tick in minutes(at(10, 0), at(10, 20)):
        rest.poll(tick, stream_bars({"SPY": rows_of(spy, tick)}, tick), {"XLV"})  # healthy, held
        rest.poll(tick, stream_bars({"SPY": rows_of(spy, at(9, 50))}, tick), set())  # stale, nothing held
    assert not fetch.calls


def test_a_quiet_symbol_with_a_healthy_spy_triggers_nothing(tmp_path, session):
    spy = session(path=[100.0] * 120)
    xlv = session(path=[100.0] * 120)
    msgs = []
    eng, book = paper_engine(tmp_path)
    eng.alert = lambda level, msg: msgs.append(msg)
    fetch = Fetch({"SPY": spy, "XLV": xlv})
    rest = RestBars(fetch, OPEN, eng._alert_every)
    # XLV prints nothing after 09:45 (thin on IEX); SPY is fine.
    rows = lambda tick: {"SPY": rows_of(spy, tick), "XLV": rows_of(xlv, min(tick, at(9, 45)))}
    drive(eng, book, rest, rows, minutes(at(9, 41), at(10, 30)))
    assert not fetch.calls and "XLV" in book.entries
    assert not any("stale" in m for m in msgs)


def test_entries_stay_blocked_while_the_stream_is_stale_even_with_polled_spy(tmp_path, session):
    spy = session(path=[100.0] * 120)
    qqq = session(path=[100.0] * 120)
    d = Always()
    eng, book = paper_engine(tmp_path, held="SPY", specs=[spec(symbols=["QQQ"])], decider=d)
    msgs = []
    eng.alert = lambda level, msg: msgs.append(msg)
    fetch = Fetch({"SPY": spy, "QQQ": qqq})
    rest = RestBars(fetch, OPEN, eng._alert_every)
    rows = lambda tick: {"SPY": rows_of(spy, min(tick, at(10, 0))), "QQQ": rows_of(qqq, min(tick, at(10, 0)))}
    drive(eng, book, rest, rows, minutes(at(10, 10), at(10, 20)))
    assert fetch.calls and fetch.calls[-1][0] == ("SPY",)  # SPY is held, so it's polled
    assert "QQQ" not in book.entries and d.calls == 0  # but entries are still blocked
    assert any("Market data stale" in m for m in msgs)


def test_a_stream_that_never_delivered_spy_stays_stale_when_spy_is_polled(tmp_path, session):
    """Live holds SPY and polls it over REST; the stream has never delivered a SPY bar. The
    shadow book's SPY classifier must not read the polled bars as a live feed."""
    spy = session(path=[100.0] * 120)
    live = Book("live", PaperSim(1000.0), tmp_path / "live")
    shadow = Book("paper", PaperSim(1000.0), tmp_path / "paper")
    d = Always()
    eng = Engine([spec(mode="shadow")], {"live": live, "shadow": shadow}, d, {"SPY"}, tmp_path)
    eng.start_day(DAY, {})
    live.broker.buy_notional("SPY", 100, 100.0, at(9, 40), "x")
    live.entries["SPY"] = E.Entry("t", live.broker.positions["SPY"].qty, 100.0, 90.0, 110.0, at(9, 40))
    msgs = []
    eng.alert = lambda level, msg: msgs.append(msg)
    fetch = Fetch({"SPY": spy})
    rest = RestBars(fetch, OPEN, eng._alert_every)
    drive(eng, shadow, rest, lambda tick: {}, minutes(at(9, 41), at(10, 0)))
    assert fetch.calls and "SPY" in rest.bars  # polled, and the merged bars hold SPY
    assert "SPY" not in shadow.entries and d.calls == 0
    assert any("Market data stale" in m and "no SPY bars" in m for m in msgs)


def test_each_poll_fetches_from_the_oldest_latest_bar_not_the_open(session):
    spy = session(path=[100.0] * 120)
    xlv, unh = session(path=[50.0] * 120), session(path=[70.0] * 120)
    fetch = Fetch({"XLV": xlv, "UNH": unh})
    rest = RestBars(fetch, OPEN, Alerts().every)
    stream = {"SPY": rows_of(spy, at(10, 0)), "XLV": rows_of(xlv, at(10, 0)), "UNH": rows_of(unh, at(9, 50))}
    for tick in minutes(at(10, 5), at(10, 7)):
        rest.poll(tick, stream_bars(stream, tick), {"XLV", "UNH"})
    starts = [c[1] for c in fetch.calls]
    assert starts == [pd.Timestamp(at(9, 49)), pd.Timestamp(at(10, 4)), pd.Timestamp(at(10, 5))]


def test_a_symbol_without_bars_fetches_from_the_open(session):
    spy = session(path=[100.0] * 120)
    fetch = Fetch({"XLV": spy.iloc[:0]})  # nothing printed today
    rest = RestBars(fetch, OPEN, Alerts().every)
    for tick in minutes(at(10, 5), at(10, 6)):
        rest.poll(tick, stream_bars({"SPY": rows_of(spy, at(10, 0))}, tick), {"XLV"})
    assert [c[1] for c in fetch.calls] == [OPEN, OPEN]


def test_a_hung_fetch_is_replaced_after_a_while_with_bounded_threads(session):
    spy = session(path=[100.0] * 120)
    gate, clock = threading.Event(), [0.0]

    class Hang(Fetch):
        def __call__(self, syms, start, end):
            self.calls.append((tuple(syms), start, end))
            gate.wait(5)
            return {}

    fetch, alerts = Hang({}), Alerts()
    rest = RestBars(fetch, OPEN, alerts.every, timeout=0.05, clock=lambda: clock[0])
    live = stream_bars({"SPY": rows_of(spy, at(10, 0))}, at(10, 10))
    try:
        for t, expect in [(0, 1), (60, 1), (R.REST_HUNG_S + 1, 2), (3 * R.REST_HUNG_S, 2)]:
            clock[0] = t
            rest.poll(at(10, 10), live, {"XLV"})
            assert len(fetch.calls) == expect, t  # a young hung call blocks; an old one is replaced
        assert sum(th.is_alive() for th, _ in rest._calls) <= R.REST_MAX_CALLS
    finally:
        gate.set()


def test_the_eod_flatten_never_waits_on_rest(tmp_path, session):
    spy = session(path=[100.0] * 120)
    eng, book = paper_engine(tmp_path)
    fetch = Fetch({"XLV": spy})
    rest = RestBars(fetch, OPEN, Alerts().every)
    tick = dt.datetime.combine(DAY, dt.time(15, 50), ET)
    live = stream_bars({"SPY": rows_of(spy, at(10, 0))}, tick)
    tick_bars(eng, rest, live, tick, 10.0)
    assert not fetch.calls
    tick_bars(eng, rest, live, tick, 20.0)  # outside the flatten window it polls
    assert fetch.calls
