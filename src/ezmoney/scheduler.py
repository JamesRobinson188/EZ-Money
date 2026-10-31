"""The trader daemon: runs the strategy on the market's schedule.

A planner job runs every morning (and at startup) and schedules that day's
sell at the open and buy at the close from the NYSE calendar, so holidays and
1 PM early closes are handled automatically. Other jobs keep a heartbeat for
the dashboard, run commands queued from the dashboard, and reconcile orders.
"""

from __future__ import annotations

import logging
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .db import Store
from .market_calendar import ET, MarketCalendar, now_et
from .notify import Notifier
from .strategy import BUY_CUTOFF, BUY_WINDOW, Trader

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DayPlan:
    session: date
    sell_at: datetime
    buy_at: datetime


def plan_for(session: date, calendar: MarketCalendar, buy_offset_min: int, sell_delay_sec: int) -> DayPlan:
    return DayPlan(
        session=session,
        sell_at=calendar.open_at(session) + timedelta(seconds=sell_delay_sec),
        buy_at=calendar.close_at(session) - timedelta(minutes=buy_offset_min),
    )


def next_times(now: datetime, calendar: MarketCalendar, store: Store) -> tuple[datetime, datetime]:
    """The next scheduled sell and buy after `now`."""
    offset, delay = store.buy_offset_min(), store.sell_delay_sec()
    day = now.date() if calendar.is_session(now.date()) else calendar.next_session(now.date())
    today = plan_for(day, calendar, offset, delay)
    following = plan_for(calendar.next_session(day), calendar, offset, delay)
    next_sell = today.sell_at if today.sell_at > now else following.sell_at
    next_buy = today.buy_at if today.buy_at > now else following.buy_at
    return next_sell, next_buy


class Daemon:
    def __init__(
        self,
        trader: Trader,
        store: Store,
        calendar: MarketCalendar,
        notifier: Notifier,
        *,
        now: Callable[[], datetime] = now_et,
    ):
        self.trader = trader
        self.store = store
        self.calendar = calendar
        self.notifier = notifier
        self._now = now
        self.scheduler = BlockingScheduler(
            timezone=ET, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
        )

    def plan_today(self) -> DayPlan | None:
        now = self._now()
        today = now.date()
        for job_id in ("sell", "buy"):
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)

        plan = None
        if self.calendar.is_session(today):
            plan = plan_for(today, self.calendar, self.store.buy_offset_min(), self.store.sell_delay_sec())
            if plan.sell_at > now:
                self.scheduler.add_job(
                    self.trader.sell_at_open, DateTrigger(plan.sell_at), args=[today], id="sell", name="sell at open"
                )
            close = self.calendar.close_at(today)
            if plan.buy_at > now:
                buy_trigger = DateTrigger(plan.buy_at)
            elif close - BUY_WINDOW <= now < close - BUY_CUTOFF:
                buy_trigger = DateTrigger(now)  # started late but there's still time
            else:
                buy_trigger = None
            if buy_trigger:
                self.scheduler.add_job(
                    self.trader.buy_at_close, buy_trigger, args=[today], id="buy", name="buy at close"
                )
            self.store.log("info", f"Planner: sell {plan.sell_at:%-I:%M:%S %p} · buy {plan.buy_at:%-I:%M:%S %p} ET")
        self.publish_next_times()
        return plan

    def publish_next_times(self) -> None:
        next_sell, next_buy = next_times(self._now(), self.calendar, self.store)
        self.store.set_setting("next_sell", next_sell.isoformat())
        self.store.set_setting("next_buy", next_buy.isoformat())

    def heartbeat(self) -> None:
        self.store.set_setting("heartbeat", self._now().isoformat(timespec="seconds"))
        self.store.set_setting("daemon_mode", self.trader.mode)
        self.publish_next_times()

    def poll_commands(self) -> None:
        for cmd in self.store.pending_commands():
            self.store.finish_command(cmd.id, "running")
            try:
                if cmd.kind == "sell_all":
                    detail = self.trader.sell_all_now().message
                elif cmd.kind == "replan":
                    self.plan_today()
                    detail = "replanned"
                elif cmd.kind == "test_notify":
                    if not self.notifier.channels:
                        raise RuntimeError("no notification channels are configured")
                    failed = self.notifier.send("Test notification", ["EZ-Money can reach you here."])
                    if failed:
                        raise RuntimeError(f"failed on {', '.join(failed)}")
                    detail = f"sent to {', '.join(self.notifier.channels)}"
                else:
                    raise ValueError(f"unknown command {cmd.kind!r}")
                self.store.finish_command(cmd.id, "done", detail)
                self.store.log("info", f"Command {cmd.kind}: {detail}")
            except Exception as exc:
                log.exception("Command %s failed", cmd.kind)
                self.store.finish_command(cmd.id, "error", str(exc))
                self.store.log("error", f"Command {cmd.kind} failed: {exc}")

    def run(self) -> None:
        self.store.log("info", f"Daemon started in {self.trader.mode} mode")
        self.scheduler.add_job(self.plan_today, CronTrigger(hour=7, minute=0, timezone=ET), id="planner")
        self.scheduler.add_job(self.heartbeat, "interval", seconds=30, id="heartbeat")
        self.scheduler.add_job(self.poll_commands, "interval", seconds=5, id="commands")
        self.scheduler.add_job(self.trader.reconcile, "interval", minutes=5, id="reconcile")
        self.heartbeat()
        self.plan_today()
        self.trader.reconcile()
        # `docker stop` sends SIGTERM; turn it into SystemExit so running trades finish first.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("Shutting down; waiting for any running trade to finish")
            self.scheduler.shutdown(wait=True)
            self.store.log("info", "Daemon stopped")
