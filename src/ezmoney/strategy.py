"""The overnight strategy: buy the watchlist at the close, sell it at the next open.

Every trading action goes through `Trader`, which holds a lock so the
scheduler, the reconcile job and dashboard commands never trade at once.
The bot only ever sells the quantity it bought itself (tracked as "lots"),
so any other shares of the same stock in the account are never touched.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .brokers.base import Broker, OrderResult, UnknownOrderError, wait_for_fill
from .config import Settings
from .db import CLOSED, FAILED, OPEN, PENDING_BUY, PENDING_SELL, Lot, Store, utcnow
from .market_calendar import MarketCalendar, now_et
from .notify import Notifier

log = logging.getLogger(__name__)

BUY_WINDOW = timedelta(minutes=15)  # a buy may only start this close to the close
BUY_CUTOFF = timedelta(seconds=20)  # stop waiting for buy fills this long before the close
FORCED_FILL_TIMEOUT = 60.0
SELL_TIMEOUT = 90.0
SELL_ATTEMPTS = 3
RETRY_BACKOFF = (5.0, 15.0)
MIN_PRICE = 1.0  # Robinhood fractional shares need a price of at least $1
QTY_EPSILON = 1e-6


def money(x: float) -> str:
    return f"${x:,.2f}"


def signed(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}${abs(x):,.2f}"


@dataclass
class RunResult:
    ok: bool
    message: str
    lines: list[str] = field(default_factory=list)


class Trader:
    def __init__(
        self,
        store: Store,
        broker: Broker,
        notifier: Notifier,
        calendar: MarketCalendar,
        settings: Settings,
        *,
        now: Callable[[], datetime] = now_et,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.broker = broker
        self.notifier = notifier
        self.calendar = calendar
        self.settings = settings
        self._now = now
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = threading.RLock()

    @property
    def mode(self) -> str:
        return self.settings.trading_mode

    # --- buying ----------------------------------------------------------

    def buy_at_close(self, session: date | None = None, *, force: bool = False) -> RunResult:
        """Buy every enabled watchlist ticker. `force` (paper only) skips the time checks."""
        with self._lock:
            now = self._now()
            session = session or now.date()
            if force and self.settings.is_live:
                return self._skip("buy", "--force is only allowed in paper mode", notify=False)

            reason, notify = self._buy_skip_reason(session, now, force)
            if reason:
                return self._skip("buy", reason, notify=notify)

            try:
                result, started = self._buy(session, force)
            except Exception as exc:
                log.exception("Buy at close failed")
                self.store.finish_run(session, "buy", self.mode, "error", str(exc), only_running=True)
                return self._fail("Buy at close FAILED", str(exc))
            if started:
                self.store.finish_run(session, "buy", self.mode, "done" if result.ok else "error", result.message)
            return result

    def _buy_skip_reason(self, session: date, now: datetime, force: bool) -> tuple[str, bool]:
        if self.store.paused():
            return "trading is paused", True
        if force:
            return "", False
        if not self.calendar.is_session(session):
            return f"the market is closed on {session}", False
        close = self.calendar.close_at(session)
        if not (close - BUY_WINDOW <= now < close - BUY_CUTOFF):
            return f"outside the buy window (the market closes at {close:%-I:%M %p} ET)", False
        if self.store.skip_before_breaks():
            nxt = self.calendar.next_session(session)
            if (nxt - session).days > 1:
                return f"the next session is {nxt:%a %b %-d}, so tonight's hold is skipped", True
        return "", False

    def _buy(self, session: date, force: bool) -> tuple[RunResult, bool]:
        """Returns the result and whether orders were attempted (the run was recorded)."""
        cap_ticker = self.settings.max_dollars_per_ticker
        cap_night = self.settings.max_dollars_per_night
        holding = {lot.symbol for lot in self.store.lots(self.mode, (PENDING_BUY, OPEN, PENDING_SELL))}

        planned: list[tuple[str, float]] = []
        problems: list[str] = []
        for item in self.store.watchlist():
            if not item.enabled:
                continue
            if item.symbol in holding:
                problems.append(f"{item.symbol}: still holding an unsold lot, not buying more")
                continue
            dollars = round(min(item.dollars, cap_ticker), 2)
            if dollars >= 1:
                planned.append((item.symbol, dollars))

        if not planned:
            if problems:
                return self._report("Buy at close skipped", [], problems, ok=False), False
            return self._skip("buy", "the watchlist has no enabled tickers", notify=False), False

        total = sum(d for _, d in planned)
        if total > cap_night:
            return self._skip("buy", f"{money(total)} is over the {money(cap_night)} nightly cap"), False

        self.broker.login()
        buying_power = self.broker.buying_power()
        if buying_power < total:
            reason = f"buying power {money(buying_power)} is less than tonight's {money(total)}"
            return self._skip("buy", reason), False

        # Recorded atomically in the database, so the buy can't run twice for a
        # session even if the dashboard, CLI and daemon race each other.
        if not self.store.start_run(session, "buy", self.mode):
            if not force:
                return self._skip("buy", f"the buy for {session} already ran", notify=False), False
            self.store.clear_run(session, "buy", self.mode)
            self.store.start_run(session, "buy", self.mode)

        self.store.log("info", f"Buy run started · {len(planned)} tickers · {money(total)} · buying power OK")

        placed: list[tuple[int, str, OrderResult]] = []
        for symbol, dollars in planned:
            lot_id = None
            try:
                price = self.broker.latest_price(symbol)
                if price < MIN_PRICE:
                    problems.append(f"{symbol}: price {money(price)} is under $1, skipped")
                    continue
                lot_id = self.store.create_lot(symbol, self.mode, session, dollars)
                order = self.broker.buy_dollars(symbol, dollars)
                self.store.update_lot(lot_id, buy_order_id=order.order_id)
                placed.append((lot_id, symbol, order))
            except Exception as exc:
                log.warning("Buy %s failed: %s", symbol, exc)
                if lot_id is not None:
                    self.store.update_lot(lot_id, status=FAILED, error=str(exc))
                problems.append(f"{symbol}: {exc}")

        deadline = None if force else self.calendar.close_at(session) - BUY_CUTOFF
        lines: list[str] = []
        spent = 0.0
        for lot_id, symbol, order in placed:
            timeout = FORCED_FILL_TIMEOUT if deadline is None else max((deadline - self._now()).total_seconds(), 0.0)
            order = self._wait(order, timeout)
            if not order.is_final:
                # Don't let a buy linger past the close.
                try:
                    self.broker.cancel_order(order.order_id)
                    order = self.broker.get_order(order.order_id)
                except Exception as exc:
                    log.warning("Cancel of %s buy failed: %s", symbol, exc)
            lot = self._apply_buy(lot_id, order)
            if lot.status == OPEN:
                spent += lot.cost
                lines.append(f"{symbol}  {lot.qty:.6f} sh @ {money(lot.buy_price or 0)}  ({money(lot.cost)})")
                self.store.log("ok", f"{symbol} filled {lot.qty:.6f} sh at {money(lot.buy_price or 0)}")
            elif lot.status == PENDING_BUY:
                problems.append(f"{symbol}: order still working at the close, will check again")
            else:
                problems.append(f"{symbol}: {lot.error}")

        filled = len(lines)
        if filled:
            lines.append(f"Total {money(spent)} · {filled} of {len(planned)} filled")
        title = f"Bought at close · {session:%a %b %-d}" if filled else "Buy at close FAILED"
        return self._report(title, lines, problems, ok=not problems), True

    def _apply_buy(self, lot_id: int, order: OrderResult) -> Lot:
        lot = self.store.get_lot(lot_id)
        if lot.status != PENDING_BUY:
            return lot
        if not order.is_final:
            return lot  # reconcile will look again
        if order.filled_qty > 0 and order.avg_price:
            self.store.update_lot(
                lot_id, status=OPEN, qty=order.filled_qty, buy_price=order.avg_price, buy_ts=utcnow(), error=None
            )
        else:
            self.store.update_lot(lot_id, status=FAILED, error=f"buy order {order.state}")
        return self.store.get_lot(lot_id)

    # --- selling ---------------------------------------------------------

    def sell_at_open(self, session: date | None = None) -> RunResult:
        """Sell every lot bought in an earlier session."""
        with self._lock:
            now = self._now()
            session = session or now.date()
            if not self.calendar.is_session(session):
                return self._skip("sell", f"the market is closed on {session}", notify=False)
            if now < self.calendar.open_at(session):
                return self._skip("sell", "the market is not open yet", notify=False)

            lots = [lot for lot in self.store.lots(self.mode, (OPEN,)) if lot.session_date < session]
            if not lots:
                self.store.log("info", "Sell run: nothing to sell")
                return RunResult(True, "nothing to sell")

            self.store.start_run(session, "sell", self.mode)
            try:
                self.broker.login()
                result = self._sell_lots(lots, f"Sold at open · {session:%a %b %-d}", wait=True)
            except Exception as exc:
                log.exception("Sell at open failed")
                self.store.finish_run(session, "sell", self.mode, "error", str(exc))
                return self._fail("Sell at open FAILED, sell manually in Robinhood", str(exc))
            self.store.finish_run(session, "sell", self.mode, "done" if result.ok else "error", result.message)
            return result

    def sell_all_now(self) -> RunResult:
        """Emergency sell of every open bot lot. Outside market hours the sells queue for the open."""
        with self._lock:
            lots = self.store.lots(self.mode, (OPEN,))
            if not lots:
                return RunResult(True, "no open positions")
            self.store.log("warn", f"Emergency sell requested for {len(lots)} lots")
            wait = self.calendar.is_open(self._now()) or not self.settings.is_live
            try:
                self.broker.login()
                return self._sell_lots(lots, "Emergency sell", wait=wait)
            except Exception as exc:
                log.exception("Emergency sell failed")
                return self._fail("Emergency sell FAILED, sell manually in Robinhood", str(exc))

    def _sell_lots(self, lots: list[Lot], title: str, *, wait: bool) -> RunResult:
        lines: list[str] = []
        problems: list[str] = []
        realized = 0.0
        closed = 0
        for lot in lots:
            lot = self._sell_lot(lot, wait=wait)
            if lot.status == CLOSED:
                closed += 1
                pct = (lot.sell_price or 0) / (lot.buy_price or 1) * 100 - 100
                realized += lot.pnl or 0.0
                lines.append(f"{lot.symbol}  {signed(lot.pnl or 0)} ({pct:+.2f}%)")
                self.store.log(
                    "ok", f"{lot.symbol} sold {lot.qty:.6f} sh at {money(lot.sell_price or 0)} · {signed(lot.pnl or 0)}"
                )
            elif lot.status == PENDING_SELL:
                lines.append(f"{lot.symbol}  sell order queued")
            else:
                problems.append(f"{lot.symbol}: {lot.error}")
        if closed:
            lines.append(f"Total {signed(realized)} · {closed} lots closed")
        if problems:
            title += " · ACTION NEEDED"
        return self._report(title, lines, problems, ok=not problems)

    def _sell_lot(self, lot: Lot, *, wait: bool) -> Lot:
        for attempt in range(SELL_ATTEMPTS):
            if attempt:
                self._sleep(RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)])
                self.store.log("warn", f"{lot.symbol} sell retry {attempt} of {SELL_ATTEMPTS - 1}")
            self.store.update_lot(lot.id, status=PENDING_SELL, sell_attempts=lot.sell_attempts + 1, error=None)
            try:
                order = self.broker.sell_qty(lot.symbol, lot.remaining_qty)
            except Exception as exc:
                log.warning("Sell %s failed: %s", lot.symbol, exc)
                self.store.update_lot(lot.id, status=OPEN, error=str(exc))
                lot = self.store.get_lot(lot.id)
                continue
            self.store.update_lot(lot.id, sell_order_id=order.order_id)
            if wait:
                order = self._wait(order, SELL_TIMEOUT)
            lot = self._apply_sell(lot.id, order)
            if lot.status != OPEN:
                return lot
        return lot

    def _apply_sell(self, lot_id: int, order: OrderResult) -> Lot:
        lot = self.store.get_lot(lot_id)
        if lot.status != PENDING_SELL or not order.is_final:
            return lot
        if order.filled_qty > 0 and order.avg_price:
            sold = lot.sold_qty + order.filled_qty
            proceeds = lot.proceeds + order.filled_qty * order.avg_price
            fields: dict[str, object] = {"sold_qty": sold, "proceeds": proceeds, "sell_ts": utcnow()}
            if lot.qty - sold <= QTY_EPSILON:
                fields.update(
                    status=CLOSED,
                    sell_price=proceeds / sold,
                    pnl=round(proceeds - lot.qty * (lot.buy_price or 0), 4),
                    error=None,
                )
            else:
                fields.update(status=OPEN, error=f"partial sell, {lot.qty - sold:.6f} sh left")
            self.store.update_lot(lot_id, **fields)
        else:
            self.store.update_lot(lot_id, status=OPEN, error=f"sell order {order.state}")
        return self.store.get_lot(lot_id)

    # --- recovery --------------------------------------------------------

    def reconcile(self) -> RunResult:
        """Refresh orders left pending and sell any lot that missed its sell at the open."""
        with self._lock:
            pending_buys = self.store.lots(self.mode, (PENDING_BUY,))
            pending_sells = self.store.lots(self.mode, (PENDING_SELL,))
            if not (pending_buys or pending_sells or self._missed_sells()):
                return RunResult(True, "nothing to reconcile")
            try:
                self.broker.login()
                for lot in pending_buys:
                    self._refresh(lot, buy=True)
                for lot in pending_sells:
                    self._refresh(lot, buy=False)
                missed = self._missed_sells()
                if missed:
                    return self._sell_lots(missed, "Sold missed positions", wait=True)
            except Exception as exc:
                log.exception("Reconcile failed")
                return self._fail("Reconcile FAILED", str(exc))
            return RunResult(True, "reconciled")

    def _missed_sells(self) -> list[Lot]:
        now = self._now()
        if not self.calendar.is_open(now):
            return []
        session = now.date()
        if now < self.calendar.open_at(session) + timedelta(seconds=self.store.sell_delay_sec()):
            return []
        return [lot for lot in self.store.lots(self.mode, (OPEN,)) if lot.session_date < session]

    def _refresh(self, lot: Lot, *, buy: bool) -> None:
        order_id = lot.buy_order_id if buy else lot.sell_order_id
        if not order_id:
            if buy:
                self.store.update_lot(lot.id, status=FAILED, error="no order id was saved, check Robinhood")
            else:
                self.store.update_lot(lot.id, status=OPEN, error="no sell order id was saved")
            return
        try:
            order = self.broker.get_order(order_id)
        except UnknownOrderError as exc:
            self.store.update_lot(lot.id, status=FAILED if buy else OPEN, error=str(exc))
            return
        if buy:
            self._apply_buy(lot.id, order)
        else:
            self._apply_sell(lot.id, order)

    # --- helpers ---------------------------------------------------------

    def _wait(self, order: OrderResult, timeout: float) -> OrderResult:
        return wait_for_fill(self.broker, order, timeout, sleep=self._sleep, clock=self._monotonic)

    def _skip(self, side: str, reason: str, *, notify: bool = True) -> RunResult:
        self.store.log("info", f"{side.capitalize()} skipped: {reason}")
        if notify:
            self._notify(f"Skipped {side}", [reason])
        return RunResult(False, reason)

    def _fail(self, title: str, detail: str) -> RunResult:
        self.store.log("error", f"{title}: {detail}")
        self._notify(title, [detail])
        return RunResult(False, detail)

    def _report(self, title: str, lines: list[str], problems: list[str], *, ok: bool) -> RunResult:
        for problem in problems:
            self.store.log("warn", problem)
        body = lines + [f"PROBLEM {p}" for p in problems]
        self._notify(title, body)
        return RunResult(ok, title, body)

    def _notify(self, title: str, lines: list[str]) -> None:
        failed = self.notifier.send(title, lines)
        if failed:
            self.store.log("warn", f"Notification failed on {', '.join(failed)}")
        elif self.notifier.channels:
            self.store.log("info", f"{title} sent to {', '.join(self.notifier.channels)}")
