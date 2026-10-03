"""Replay fill timing: the engine decides on a completed bar, so a market order it sends
then executes at the next bar's open, not at the close it has just seen. A target is not a
resting order (live sells at market once it sees the touch), so a bar's transient high is never
filled retroactively. A stop is a resting order, filled where it was hit.

Each case also runs the same session the way the live runner drives the engine (fills at the
latest close, as paper and sim accounts do), so the two differ only in the fill prices."""

import pandas as pd
import pytest

from trader.replay import replay

from test_engine import Always, run, spec

S = 0.0005  # SimBroker.SLIPPAGE


def replayed(tmp_path, bars, specs, decider=None):
    day = bars.index[0].date()
    summary = replay(
        specs,
        day,
        day,
        decider or Always(),
        {"SPY"},
        tmp_path / "replay",
        {},
        cash=250.0,
        sessions={"SPY": {day: bars}},
    )
    return summary, pd.read_csv(tmp_path / "replay" / "sim" / "trades.csv")


def live(tmp_path, bars, specs, decider=None):
    return run(tmp_path / "live", bars, specs, decider or Always())[1]


def same_decisions(a, b):
    """Replay and the live-style run made the same trades at the same times, for the same reasons."""
    cols = ["time", "side", "reason"]
    assert a[cols].to_dict("records") == b[cols].to_dict("records")


def bar(bars, i, **v):
    for k, x in v.items():
        bars.iloc[i, bars.columns.get_loc(k)] = x


def test_a_gap_after_the_signal_bar_moves_the_entry(tmp_path, session):
    bars = session(path=[100.0] * 390)
    bar(bars, 5, open=100.3, high=100.32)  # the bar after 09:34's signal opens higher
    _, r = replayed(tmp_path, bars, [spec(stop_pct=1.0)])
    t = live(tmp_path, bars, [spec(stop_pct=1.0)])
    same_decisions(r, t)
    assert r.time.iloc[0].startswith("2026-09-21T09:35")
    assert r.price.iloc[0] == pytest.approx(100.3 * (1 + S))  # the next open, plus slippage
    assert t.price.iloc[0] == pytest.approx(100.0 * (1 + S))  # live-style: the close it saw


def test_a_target_touch_then_reversal_gets_no_retroactive_target_fill(tmp_path, session):
    bars = session(path=[100.0] * 390)
    bar(bars, 10, high=101.5)  # 09:40 spikes through the 1% target (101.05) and closes at 100
    bar(bars, 11, open=99.9)  # and the next bar opens lower
    _, r = replayed(tmp_path, bars, [spec()])
    t = live(tmp_path, bars, [spec()])
    same_decisions(r, t)
    sell = r.iloc[1]
    assert sell.reason == "target" and sell.time.startswith("2026-09-21T09:41")
    assert sell.price == pytest.approx(99.9 * (1 - S))  # sold at market once seen: the next open
    assert sell.pnl < 0  # the transient high was never available to the live policy
    assert t.price.iloc[1] == pytest.approx(100.0 * (1 - S))  # live-style: the close, never the target


def test_a_stop_still_fills_at_its_level(tmp_path, session):
    bars = session(path=[100.0] * 390)
    bar(bars, 10, low=99.0)  # through the 0.5% stop (99.55) inside 09:40
    bar(bars, 11, open=98.0)  # a later gap must not worsen a stop that already filled
    _, r = replayed(tmp_path, bars, [spec()])
    t = live(tmp_path, bars, [spec()])
    same_decisions(r, t)
    stop = 100.0 * (1 + S) * (1 - 0.005)
    assert r.reason.iloc[1] == "stop" and r.price.iloc[1] == pytest.approx(stop * (1 - S))
    assert t.price.iloc[1] == pytest.approx(r.price.iloc[1])


def test_model_exit_and_eod_flatten_fill_at_the_next_open(tmp_path, session):
    bars = session(path=[100.0] * 390)
    bar(bars, 6, open=100.2, high=100.22)  # after 09:35 is seen, the exit sells at 09:36's open
    _, r = replayed(tmp_path, bars, [spec()], Always(exit="EXIT"))
    t = live(tmp_path, bars, [spec()], Always(exit="EXIT"))
    same_decisions(r, t)
    assert r.reason.iloc[1] == "classifier EXIT" and r.price.iloc[1] == pytest.approx(100.2 * (1 - S))

    bars = session(path=[100.0] * 390)
    bar(bars, 375, open=100.4, high=100.42)  # 15:45: the bar after the flatten window is seen
    _, r = replayed(tmp_path / "eod", bars, [spec(stop_pct=1.0)])
    t = live(tmp_path / "eod", bars, [spec(stop_pct=1.0)])
    same_decisions(r, t)
    assert r.reason.iloc[-1] == "eod flatten" and r.price.iloc[-1] == pytest.approx(100.4 * (1 - S))
    assert t.price.iloc[-1] == pytest.approx(100.0 * (1 - S))


def test_scale_out_sells_at_the_next_open(tmp_path, session):
    so = {"at_pct": 0.5, "fraction": 0.5}
    bars = session(path=[100.0] * 10 + [100.6] * 20 + [100.0] * 360)
    bar(bars, 11, open=100.7, high=100.72)  # crossed at 09:40; 09:41 opens higher
    _, r = replayed(tmp_path, bars, [spec(scale_out=so, target_pct=2, stop_pct=1.0)])
    t = live(tmp_path, bars, [spec(scale_out=so, target_pct=2, stop_pct=1.0)])
    same_decisions(r, t)
    part = r[r.side == "sell_part"].iloc[0]
    assert part.time.startswith("2026-09-21T09:41") and part.price == pytest.approx(100.7 * (1 - S))


def test_a_market_order_after_the_last_bar_fills_at_the_close(tmp_path, session):
    """No bar after the one just seen (the session's last): nothing better to go on than its close."""
    from trader.broker import SimBroker
    from trader.engine import Book, Engine

    b = Book("sim", SimBroker(250.0), tmp_path / "sim")
    b.broker.next_open = {"QQQ": 50.0}  # another symbol's next open is no price for SPY
    assert Engine._market_ref(b, "SPY", 100.0) == 100.0
    assert Engine._market_ref(b, "QQQ", 49.0) == 50.0
