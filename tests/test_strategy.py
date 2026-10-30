import pytest

from ezmoney.db import CLOSED, FAILED, OPEN, PENDING_BUY, PENDING_SELL

from .conftest import et

THU_BUY = et(2026, 10, 29, 15, 58)
FRI_SELL = et(2026, 10, 30, 9, 30, 5)
FRI_BUY = et(2026, 10, 30, 15, 58)


@pytest.fixture
def watch(store):
    store.upsert_watch("SPY", 500)
    store.upsert_watch("QQQ", 400)
    return store


def bought(trader):
    result = trader.buy_at_close()
    assert result.ok, result.lines
    return trader.store.lots("paper", (OPEN,))


def test_buy_creates_one_open_lot_per_enabled_ticker(watch, make_trader):
    watch.upsert_watch("AAPL", 300, enabled=False)
    trader, broker, notifier, clock = make_trader(THU_BUY)

    lots = bought(trader)

    assert [(lot.symbol, lot.qty, lot.buy_price) for lot in lots] == [("SPY", 1.0, 500.0), ("QQQ", 1.0, 400.0)]
    assert [p for p in broker.placed] == [("buy", "SPY", 500), ("buy", "QQQ", 400)]
    assert notifier.titles == ["Bought at close · Thu Oct 29"]


def test_buy_only_runs_once_per_session(watch, make_trader):
    trader, broker, _, clock = make_trader(THU_BUY)
    bought(trader)

    clock.set(THU_BUY.replace(minute=59))
    result = trader.buy_at_close()

    assert not result.ok
    assert len(broker.placed) == 2


def test_buy_outside_the_window_does_nothing(watch, make_trader):
    trader, broker, notifier, _ = make_trader(et(2026, 10, 29, 14, 0))
    result = trader.buy_at_close()
    assert "outside the buy window" in result.message
    assert broker.placed == [] and notifier.sent == []


def test_buy_on_a_holiday_does_nothing(watch, make_trader):
    trader, broker, _, _ = make_trader(et(2026, 11, 26, 15, 58))
    assert "closed" in trader.buy_at_close().message
    assert broker.placed == []


def test_early_close_moves_the_window(watch, make_trader):
    trader, broker, _, clock = make_trader(et(2026, 11, 27, 15, 58))
    assert "outside the buy window" in trader.buy_at_close().message

    clock.set(et(2026, 11, 27, 12, 58))
    assert trader.buy_at_close().ok
    assert len(broker.placed) == 2


def test_pause_blocks_the_buy_and_says_so(watch, make_trader):
    watch.set_setting("paused", True)
    trader, broker, notifier, _ = make_trader(THU_BUY)
    trader.buy_at_close()
    assert broker.placed == []
    assert notifier.sent == [("Skipped buy", ["trading is paused"])]


def test_dollars_are_clamped_to_the_per_ticker_cap(store, make_trader):
    store.upsert_watch("SPY", 5000)
    trader, broker, _, _ = make_trader(THU_BUY, max_dollars_per_ticker=1000)
    trader.buy_at_close()
    assert broker.placed == [("buy", "SPY", 1000)]


def test_over_the_nightly_cap_skips_everything(watch, make_trader):
    trader, broker, notifier, _ = make_trader(THU_BUY, max_dollars_per_night=800)
    result = trader.buy_at_close()
    assert broker.placed == []
    assert "nightly cap" in result.message
    assert notifier.titles == ["Skipped buy"]


def test_low_buying_power_skips_without_using_up_the_run(watch, make_trader):
    trader, broker, _, _ = make_trader(THU_BUY)
    broker.bp = 100
    assert "buying power" in trader.buy_at_close().message
    assert broker.placed == []

    broker.bp = 10_000
    assert trader.buy_at_close().ok
    assert len(broker.placed) == 2


def test_skip_before_breaks_skips_friday_but_not_thursday(watch, make_trader):
    watch.set_setting("skip_before_breaks", True)
    trader, broker, _, clock = make_trader(FRI_BUY)
    assert "next session is Mon Nov 2" in trader.buy_at_close().message

    clock.set(THU_BUY)
    assert trader.buy_at_close().ok


def test_buy_that_is_still_working_near_the_close_is_cancelled_and_keeps_the_partial_fill(watch, make_trader):
    trader, broker, _, _ = make_trader(et(2026, 10, 29, 15, 59, 30))
    broker.buy_script["SPY"] = [("partially_filled", 0.5)]

    result = trader.buy_at_close()

    spy = next(lot for lot in trader.store.lots("paper") if lot.symbol == "SPY")
    assert spy.status == OPEN and spy.qty == 0.5
    assert broker.cancelled == ["o1"]
    assert result.ok


def test_buy_that_fails_is_recorded(watch, make_trader):
    trader, broker, _, _ = make_trader(THU_BUY)
    broker.buy_script["QQQ"] = [("rejected", 0)]
    result = trader.buy_at_close()
    qqq = next(lot for lot in trader.store.lots("paper") if lot.symbol == "QQQ")
    assert qqq.status == FAILED and "rejected" in qqq.error
    assert not result.ok


def test_unsold_lot_blocks_buying_that_ticker_again(watch, make_trader):
    trader, broker, _, clock = make_trader(THU_BUY)
    bought(trader)
    clock.set(FRI_BUY)
    result = trader.buy_at_close()
    assert broker.placed.count(("buy", "SPY", 500)) == 1
    assert any("still holding" in line for line in result.lines)


def test_sell_at_open_sells_exactly_the_bot_quantity_and_records_pnl(watch, make_trader):
    trader, broker, notifier, clock = make_trader(THU_BUY)
    bought(trader)

    broker.prices.update(SPY=505.0, QQQ=398.0)
    clock.set(FRI_SELL)
    result = trader.sell_at_open()

    assert result.ok
    assert ("sell", "SPY", 1.0) in broker.placed and ("sell", "QQQ", 1.0) in broker.placed
    lots = {lot.symbol: lot for lot in trader.store.lots("paper", (CLOSED,))}
    assert lots["SPY"].pnl == 5.0 and lots["QQQ"].pnl == -2.0
    assert notifier.sent[-1] == (
        "Sold at open · Fri Oct 30",
        ["SPY  +$5.00 (+1.00%)", "QQQ  −$2.00 (-0.50%)", "Total +$3.00 · 2 lots closed"],
    )


def test_sell_never_touches_lots_bought_today(watch, make_trader):
    trader, broker, _, clock = make_trader(FRI_BUY)
    bought(trader)

    clock.set(et(2026, 10, 30, 15, 59))
    trader.reconcile()
    assert trader.sell_at_open().message == "nothing to sell"
    assert [p for p in broker.placed if p[0] == "sell"] == []


def test_sell_retries_after_an_error(watch, make_trader):
    trader, broker, _, clock = make_trader(THU_BUY)
    bought(trader)
    broker.fail_sell["SPY"] = 1
    clock.set(FRI_SELL)

    assert trader.sell_at_open().ok
    assert all(lot.status == CLOSED for lot in trader.store.lots("paper"))
    assert clock.slept >= 5


def test_sell_that_keeps_failing_stays_open_and_asks_for_help(watch, make_trader):
    trader, broker, notifier, clock = make_trader(THU_BUY)
    bought(trader)
    broker.fail_sell["SPY"] = 99
    clock.set(FRI_SELL)

    result = trader.sell_at_open()

    spy = next(lot for lot in trader.store.lots("paper") if lot.symbol == "SPY")
    assert spy.status == OPEN and spy.sell_attempts == 3
    assert not result.ok and "ACTION NEEDED" in notifier.titles[-1]


def test_partial_sell_is_finished_on_the_retry(watch, make_trader):
    trader, broker, _, clock = make_trader(THU_BUY)
    bought(trader)
    broker.sell_script["SPY"] = [("cancelled", 0.25)]
    broker.prices["SPY"] = 510.0
    clock.set(FRI_SELL)

    trader.sell_at_open()

    spy = next(lot for lot in trader.store.lots("paper") if lot.symbol == "SPY")
    assert spy.status == CLOSED and spy.sold_qty == pytest.approx(1.0)
    assert ("sell", "SPY", 0.75) in broker.placed
    assert spy.pnl == pytest.approx(10.0)


def test_reconcile_sells_lots_that_missed_the_open(watch, make_trader):
    trader, broker, notifier, clock = make_trader(THU_BUY)
    bought(trader)

    clock.set(et(2026, 10, 30, 10, 15))
    trader.reconcile()

    assert all(lot.status == CLOSED for lot in trader.store.lots("paper"))
    assert notifier.titles[-1] == "Sold missed positions"


def test_reconcile_waits_for_the_sell_delay(watch, make_trader):
    trader, broker, _, clock = make_trader(THU_BUY)
    bought(trader)
    clock.set(et(2026, 10, 30, 9, 30, 1))
    trader.reconcile()
    assert [p for p in broker.placed if p[0] == "sell"] == []


def test_reconcile_resolves_a_buy_left_pending(watch, make_trader):
    trader, broker, _, clock = make_trader(et(2026, 10, 29, 15, 59, 30))
    broker.buy_script["SPY"] = [("pending", 0)]
    broker.cancel_fails = True
    trader.buy_at_close()
    spy = next(lot for lot in trader.store.lots("paper") if lot.symbol == "SPY")
    assert spy.status == PENDING_BUY

    broker.finish(spy.buy_order_id)
    trader.reconcile()

    assert trader.store.get_lot(spy.id).status == OPEN


def test_emergency_sell_outside_hours_queues_orders_in_live_mode(watch, make_trader):
    trader, broker, _, clock = make_trader(et(2026, 10, 29, 15, 58), mode="live")
    bought_live = trader.buy_at_close()
    assert bought_live.ok

    clock.set(et(2026, 10, 29, 20, 0))
    broker.sell_script.update(SPY=[("pending", 0)], QQQ=[("pending", 0)])
    slept_before = clock.slept
    trader.sell_all_now()

    assert {lot.status for lot in trader.store.lots("live")} == {PENDING_SELL}
    assert clock.slept == slept_before  # didn't sit waiting for a closed market

    for lot in trader.store.lots("live"):
        broker.finish(lot.sell_order_id)
    clock.set(FRI_SELL)
    trader.reconcile()
    assert {lot.status for lot in trader.store.lots("live")} == {CLOSED}


def test_force_is_refused_in_live_mode(watch, make_trader):
    trader, broker, _, _ = make_trader(et(2026, 10, 29, 12, 0), mode="live")
    assert "paper mode" in trader.buy_at_close(force=True).message
    assert broker.placed == []


def test_force_in_paper_mode_ignores_the_clock(watch, make_trader):
    trader, broker, _, _ = make_trader(et(2026, 10, 31, 12, 0))  # a Saturday
    assert trader.buy_at_close(force=True).ok
    assert len(broker.placed) == 2
