from datetime import date

import pytest

from ezmoney import history
from ezmoney.db import CLOSED, Lot


def lot(symbol, bought, sold_ts, qty, buy, sell, status=CLOSED, lot_id=0):
    return Lot(
        id=lot_id,
        symbol=symbol,
        mode="paper",
        status=status,
        session_date=bought,
        dollars=qty * buy,
        buy_order_id="b",
        buy_ts=None,
        qty=qty,
        buy_price=buy,
        sell_order_id="s",
        sell_ts=sold_ts,
        sold_qty=qty,
        proceeds=qty * sell,
        sell_price=sell,
        pnl=round(qty * (sell - buy), 4),
        sell_attempts=1,
        error=None,
    )


LOTS = [
    lot("SPY", date(2026, 10, 27), "2026-10-28T13:30:06+00:00", 1, 500, 505),
    lot("QQQ", date(2026, 10, 27), "2026-10-28T13:30:07+00:00", 1, 400, 396),
    lot("SPY", date(2026, 10, 28), "2026-10-29T13:30:06+00:00", 1, 505, 495),
    lot("SPY", date(2026, 10, 30), "2026-11-02T14:30:06+00:00", 2, 100, 103),
    lot("QQQ", date(2026, 10, 30), None, 1, 400, 400, status="open"),
]


def test_nights_group_lots_by_the_session_they_were_bought():
    nights = history.nights_frame(history.lots_frame(LOTS))
    assert list(nights["bought"]) == [date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 30)]
    assert list(nights["pnl"]) == [1.0, -10.0, 6.0]
    assert list(nights["sold"]) == [date(2026, 10, 28), date(2026, 10, 29), date(2026, 11, 2)]
    assert nights["ret"].iloc[0] == pytest.approx(1 / 900)


def test_summary_and_drawdown():
    s = history.summarize(history.nights_frame(history.lots_frame(LOTS)))
    assert (s.nights, s.wins, s.realized, s.best, s.worst) == (3, 2, -3.0, 6.0, -10.0)
    assert s.drawdown == -10.0  # from +1 down to -9


def test_summary_of_nothing():
    assert history.summarize(history.nights_frame(history.lots_frame([]))).nights == 0


def test_by_ticker_and_last_night():
    lots = history.lots_frame(LOTS)
    nights = history.nights_frame(lots)
    tickers = history.by_ticker(lots, history.last_nights(nights, 2))
    assert list(tickers["symbol"]) == ["SPY"]
    assert tickers["total"].iloc[0] == -4.0
    assert history.last_night_returns(lots) == {"QQQ": pytest.approx(-0.01), "SPY": pytest.approx(0.03)}
