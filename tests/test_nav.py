from trader.nav import NavBook


def test_withdrawal_does_not_move_nav():
    n = NavBook()
    n.mark(250)
    n.mark(300)  # +20%
    assert round(n.nav_per_unit, 4) == 1.2
    n.cashflow(-100)  # withdraw profits
    assert round(n.nav_per_unit, 4) == 1.2
    assert round(n.mark(200), 4) == 1.2  # equity fell only because of the withdrawal
    assert round(n.hwm, 9) == 1.2


def test_deposit_and_drawdown():
    n = NavBook()
    n.mark(200)
    n.cashflow(200)
    n.mark(400)
    assert round(n.nav_per_unit, 4) == 1.0
    n.mark(300)
    assert round(n.nav_per_unit, 4) == 0.75


def test_roundtrip(tmp_path):
    n = NavBook()
    n.mark(250)
    n.save(tmp_path / "nav.json")
    assert NavBook.load(tmp_path / "nav.json") == n
