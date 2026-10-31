from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from ezmoney.brokers.base import BrokerError, OrderResult
from ezmoney.config import Settings
from ezmoney.db import Store
from ezmoney.market_calendar import ET, MarketCalendar
from ezmoney.strategy import Trader


class FakeBroker:
    """Fills instantly by default. Per-symbol scripts make orders go through other states."""

    name = "fake"

    def __init__(self, prices: dict[str, float], buying_power: float = 10_000.0):
        self.prices = dict(prices)
        self.bp = buying_power
        self.orders: dict[str, list[OrderResult]] = {}
        self.placed: list[tuple[str, str, float]] = []
        self.cancelled: list[str] = []
        self.buy_script: dict[str, list[tuple[str, float]]] = {}
        self.sell_script: dict[str, list[tuple[str, float]]] = {}
        self.fail_sell: dict[str, int] = {}
        self.cancel_fails = False
        self.logins = 0

    def login(self) -> None:
        self.logins += 1

    def latest_price(self, symbol: str) -> float:
        return self.prices[symbol]

    def buying_power(self) -> float:
        return self.bp

    def buy_dollars(self, symbol: str, dollars: float) -> OrderResult:
        price = self.prices[symbol]
        self.placed.append(("buy", symbol, dollars))
        return self._new(math.floor(dollars / price * 1e6) / 1e6, price, self.buy_script.pop(symbol, None))

    def sell_qty(self, symbol: str, qty: float) -> OrderResult:
        if self.fail_sell.get(symbol):
            self.fail_sell[symbol] -= 1
            raise BrokerError("sell rejected")
        self.placed.append(("sell", symbol, qty))
        return self._new(qty, self.prices[symbol], self.sell_script.pop(symbol, None))

    def get_order(self, order_id: str) -> OrderResult:
        steps = self.orders[order_id]
        if len(steps) > 1:
            steps.pop(0)
        return steps[0]

    def cancel_order(self, order_id: str) -> None:
        if self.cancel_fails:
            raise BrokerError("cancel failed")
        self.cancelled.append(order_id)
        cur = self.orders[order_id][0]
        self.orders[order_id] = [OrderResult(order_id, "cancelled", cur.filled_qty, cur.avg_price)]

    def finish(self, order_id: str) -> None:
        """Make a working order fill completely."""
        cur = self.orders[order_id][-1]
        qty = self._qty[order_id]
        self.orders[order_id] = [OrderResult(order_id, "filled", qty, cur.avg_price or self._price[order_id])]

    def _new(self, qty: float, price: float, script: list[tuple[str, float]] | None) -> OrderResult:
        script = script or [("filled", 1.0)]
        order_id = f"o{len(self.orders) + 1}"
        self.orders[order_id] = [
            OrderResult(order_id, state, round(qty * frac, 6), price if frac else None) for state, frac in script
        ]
        self.__dict__.setdefault("_qty", {})[order_id] = qty
        self.__dict__.setdefault("_price", {})[order_id] = price
        return self.orders[order_id][0]


class FakeNotifier:
    channels = ["fake"]

    def __init__(self) -> None:
        self.sent: list[tuple[str, list[str]]] = []

    def send(self, title: str, lines: list[str] | None = None) -> list[str]:
        self.sent.append((title, list(lines or [])))
        return []

    @property
    def titles(self) -> list[str]:
        return [title for title, _ in self.sent]


class FakeClock:
    """Wall clock and monotonic clock that only move when something sleeps (or the test moves them)."""

    def __init__(self, start: datetime):
        self.at = start
        self.slept = 0.0

    def now(self) -> datetime:
        return self.at

    def set(self, at: datetime) -> None:
        self.at = at

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.at += timedelta(seconds=seconds)

    def monotonic(self) -> float:
        return self.at.timestamp()


def et(*args: int) -> datetime:
    return datetime(*args, tzinfo=ET)


@pytest.fixture(scope="session")
def calendar() -> MarketCalendar:
    return MarketCalendar()


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "test.db")


@pytest.fixture
def make_trader(store, calendar):
    def factory(start: datetime, *, mode: str = "paper", prices: dict[str, float] | None = None, **settings):
        clock = FakeClock(start)
        broker = FakeBroker(prices or {"SPY": 500.0, "QQQ": 400.0, "AAPL": 200.0})
        notifier = FakeNotifier()
        cfg = Settings(_env_file=None, trading_mode=mode, **settings)
        trader = Trader(
            store, broker, notifier, calendar, cfg, now=clock.now, sleep=clock.sleep, monotonic=clock.monotonic
        )
        return trader, broker, notifier, clock

    return factory
