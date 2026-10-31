"""The broker interface the strategy talks to."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

FILLED = "filled"
PARTIALLY_FILLED = "partially_filled"
PENDING = "pending"
CANCELLED = "cancelled"
REJECTED = "rejected"
FAILED = "failed"

FINAL_STATES = frozenset({FILLED, CANCELLED, REJECTED, FAILED})


class BrokerError(RuntimeError):
    pass


class UnknownOrderError(BrokerError):
    """The broker has no record of this order id."""


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    state: str
    filled_qty: float = 0.0
    avg_price: float | None = None

    @property
    def is_final(self) -> bool:
        return self.state in FINAL_STATES


class Broker(Protocol):
    name: str

    def login(self) -> None: ...

    def latest_price(self, symbol: str) -> float: ...

    def buying_power(self) -> float: ...

    def buy_dollars(self, symbol: str, dollars: float) -> OrderResult: ...

    def sell_qty(self, symbol: str, qty: float) -> OrderResult: ...

    def get_order(self, order_id: str) -> OrderResult: ...

    def cancel_order(self, order_id: str) -> None: ...


def wait_for_fill(
    broker: Broker,
    order: OrderResult,
    timeout: float,
    *,
    poll: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> OrderResult:
    """Poll an order until it reaches a final state or `timeout` seconds pass."""
    deadline = clock() + timeout
    while not order.is_final and clock() < deadline:
        sleep(poll)
        order = broker.get_order(order.order_id)
    return order
