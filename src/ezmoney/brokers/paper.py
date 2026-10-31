"""Simulated broker: every order fills immediately at the current quote."""

from __future__ import annotations

import math
import uuid

from ..db import OPEN, PENDING_SELL, Store
from ..quotes import QuoteError, QuoteFn
from .base import FILLED, BrokerError, OrderResult, UnknownOrderError


class PaperBroker:
    name = "paper"

    def __init__(self, store: Store, quote: QuoteFn, starting_cash: float):
        self._store = store
        self._quote = quote
        self._starting_cash = starting_cash
        self._orders: dict[str, OrderResult] = {}

    def login(self) -> None:
        pass

    def latest_price(self, symbol: str) -> float:
        try:
            return self._quote(symbol)
        except QuoteError as exc:
            raise BrokerError(str(exc)) from exc

    def buying_power(self) -> float:
        held = sum(lot.remaining_qty * (lot.buy_price or 0.0) for lot in self._store.lots("paper", (OPEN, PENDING_SELL)))
        return self._starting_cash + self._store.realized_pnl("paper") - held

    def buy_dollars(self, symbol: str, dollars: float) -> OrderResult:
        price = self.latest_price(symbol)
        qty = math.floor(dollars / price * 1e6) / 1e6
        return self._fill(qty, price)

    def sell_qty(self, symbol: str, qty: float) -> OrderResult:
        return self._fill(qty, self.latest_price(symbol))

    def get_order(self, order_id: str) -> OrderResult:
        try:
            return self._orders[order_id]
        except KeyError:
            raise UnknownOrderError(f"Unknown paper order {order_id} (the daemon restarted mid-order)") from None

    def cancel_order(self, order_id: str) -> None:
        pass

    def _fill(self, qty: float, price: float) -> OrderResult:
        order = OrderResult(order_id=f"paper-{uuid.uuid4()}", state=FILLED, filled_qty=qty, avg_price=price)
        self._orders[order.order_id] = order
        return order
