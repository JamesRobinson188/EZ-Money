from datetime import date, timedelta

from ezmoney.scheduler import Daemon, next_times, plan_for

from .conftest import FakeNotifier, et


def test_thanksgiving_is_closed_and_the_next_day_closes_early(calendar):
    assert not calendar.is_session(date(2026, 11, 26))
    assert calendar.next_session(date(2026, 11, 26)) == date(2026, 11, 27)
    assert calendar.open_at(date(2026, 11, 27)) == et(2026, 11, 27, 9, 30)
    assert calendar.close_at(date(2026, 11, 27)) == et(2026, 11, 27, 13, 0)
    assert calendar.close_at(date(2026, 12, 24)) == et(2026, 12, 24, 13, 0)


def test_friday_rolls_to_monday_across_the_dst_change(calendar):
    assert calendar.next_session(date(2026, 10, 30)) == date(2026, 11, 2)
    monday_open = calendar.open_at(date(2026, 11, 2))
    assert monday_open == et(2026, 11, 2, 9, 30)
    assert monday_open.utcoffset() == timedelta(hours=-5)


def test_plan_uses_the_early_close(calendar):
    plan = plan_for(date(2026, 11, 27), calendar, buy_offset_min=2, sell_delay_sec=5)
    assert plan.buy_at == et(2026, 11, 27, 12, 58)
    assert plan.sell_at == et(2026, 11, 27, 9, 30, 5)


def test_next_times_after_friday_close(calendar, store):
    next_sell, next_buy = next_times(et(2026, 10, 30, 20, 0), calendar, store)
    assert next_sell == et(2026, 11, 2, 9, 30, 5)
    assert next_buy == et(2026, 11, 2, 15, 58)


def test_next_times_midday(calendar, store):
    next_sell, next_buy = next_times(et(2026, 10, 29, 12, 0), calendar, store)
    assert next_sell == et(2026, 10, 30, 9, 30, 5)
    assert next_buy == et(2026, 10, 29, 15, 58)


def daemon_at(make_trader, store, calendar, at):
    trader, broker, notifier, clock = make_trader(at)
    return Daemon(trader, store, calendar, FakeNotifier(), now=clock.now), broker


def test_planner_schedules_the_sell_and_the_buy(make_trader, store, calendar):
    daemon, _ = daemon_at(make_trader, store, calendar, et(2026, 10, 29, 7, 0))
    daemon.plan_today()
    assert daemon.scheduler.get_job("sell").trigger.run_date == et(2026, 10, 29, 9, 30, 5)
    assert daemon.scheduler.get_job("buy").trigger.run_date == et(2026, 10, 29, 15, 58)


def test_planner_does_nothing_on_a_weekend(make_trader, store, calendar):
    daemon, _ = daemon_at(make_trader, store, calendar, et(2026, 10, 31, 7, 0))
    assert daemon.plan_today() is None
    assert daemon.scheduler.get_job("sell") is None and daemon.scheduler.get_job("buy") is None


def test_planner_started_late_runs_the_buy_right_away(make_trader, store, calendar):
    at = et(2026, 10, 29, 15, 59)
    daemon, _ = daemon_at(make_trader, store, calendar, at)
    daemon.plan_today()
    assert daemon.scheduler.get_job("sell") is None
    assert daemon.scheduler.get_job("buy").trigger.run_date == at


def test_planner_uses_the_dashboard_settings(make_trader, store, calendar):
    store.set_setting("buy_offset_min", 5)
    store.set_setting("sell_delay_sec", 30)
    daemon, _ = daemon_at(make_trader, store, calendar, et(2026, 10, 29, 7, 0))
    daemon.plan_today()
    assert daemon.scheduler.get_job("sell").trigger.run_date == et(2026, 10, 29, 9, 30, 30)
    assert daemon.scheduler.get_job("buy").trigger.run_date == et(2026, 10, 29, 15, 55)


def test_commands_from_the_dashboard(make_trader, store, calendar):
    store.upsert_watch("SPY", 500)
    daemon, broker = daemon_at(make_trader, store, calendar, et(2026, 10, 29, 15, 58))
    daemon.trader.buy_at_close()

    store.add_command("sell_all")
    store.add_command("replan")
    store.add_command("bogus")
    daemon.poll_commands()

    statuses = {c.kind: c.status for c in _all_commands(store)}
    assert statuses == {"sell_all": "done", "replan": "done", "bogus": "error"}
    assert ("sell", "SPY", 1.0) in broker.placed


def _all_commands(store):
    with store._conn() as conn:
        rows = conn.execute("SELECT * FROM commands").fetchall()
    from ezmoney.db import Command

    return [Command(**dict(r)) for r in rows]
