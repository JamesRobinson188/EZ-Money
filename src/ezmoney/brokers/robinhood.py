"""Robinhood through the unofficial robin_stocks library.

Robinhood has no official stock-trading API, so this can break whenever
Robinhood changes its private endpoints. Fractional orders are always market
orders during regular hours; robin_stocks sends buys as limit orders with a
5% collar, which is what the Robinhood app does too.
"""

from __future__ import annotations

import math

import pyotp
import robin_stocks.robinhood as rh

from ..config import Settings
from .base import CANCELLED, FAILED, FILLED, PARTIALLY_FILLED, PENDING, REJECTED, BrokerError, OrderResult

_STATES = {
    "filled": FILLED,
    "partially_filled": PARTIALLY_FILLED,
    "cancelled": CANCELLED,
    "canceled": CANCELLED,
    "rejected": REJECTED,
    "failed": FAILED,
}


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_order(data: object) -> OrderResult:
    if not isinstance(data, dict) or "id" not in data:
        detail = (data.get("detail") or data.get("non_field_errors")) if isinstance(data, dict) else data
        raise BrokerError(f"Robinhood rejected the order: {detail or 'no response'}")

    filled = _to_float(data.get("cumulative_quantity")) or 0.0
    avg = _to_float(data.get("average_price"))
    if avg is None and filled:
        executions = data.get("executions") or []
        qty = sum(_to_float(e.get("quantity")) or 0.0 for e in executions)
        cost = sum((_to_float(e.get("price")) or 0.0) * (_to_float(e.get("quantity")) or 0.0) for e in executions)
        avg = cost / qty if qty else None

    return OrderResult(
        order_id=data["id"],
        state=_STATES.get(str(data.get("state")), PENDING),
        filled_qty=filled,
        avg_price=avg,
    )


class RobinhoodBroker:
    name = "robinhood"

    def __init__(self, settings: Settings):
        self._settings = settings

    def login(self) -> None:
        s = self._settings
        if not s.rh_username or not s.rh_password:
            raise BrokerError("RH_USERNAME and RH_PASSWORD must be set for live trading")
        mfa = pyotp.TOTP(s.rh_totp_secret).now() if s.rh_totp_secret else None
        s.session_dir.mkdir(parents=True, exist_ok=True)
        result = rh.login(
            s.rh_username,
            s.rh_password,
            mfa_code=mfa,
            store_session=True,
            pickle_path=str(s.session_dir.resolve()),
        )
        if not result or "access_token" not in result:
            raise BrokerError("Robinhood login failed. Run `ez-money login` to approve this device.")

    def latest_price(self, symbol: str, price_type: str | None = None) -> float:
        prices = rh.stocks.get_latest_price(symbol, priceType=price_type, includeExtendedHours=True)
        price = _to_float(prices[0]) if prices else None
        if not price:
            raise BrokerError(f"No Robinhood quote for {symbol}")
        return price

    def buying_power(self) -> float:
        value = _to_float(rh.profiles.load_account_profile(info="buying_power"))
        if value is None:
            raise BrokerError("Could not read buying power from Robinhood")
        return value

    def buy_dollars(self, symbol: str, dollars: float) -> OrderResult:
        # Size the order ourselves: robin_stocks' own dollar helper rounds the
        # share count to 2 decimals for shares over $1, which can overspend.
        ask = self.latest_price(symbol, "ask_price")
        qty = math.floor(dollars / ask * 1e6) / 1e6
        if qty <= 0:
            raise BrokerError(f"${dollars:.2f} is too small to buy {symbol}")
        return parse_order(rh.orders.order(symbol, qty, "buy", timeInForce="gfd", extendedHours=False))

    def sell_qty(self, symbol: str, qty: float) -> OrderResult:
        return parse_order(rh.orders.order(symbol, round(qty, 6), "sell", timeInForce="gfd", extendedHours=False))

    def get_order(self, order_id: str) -> OrderResult:
        return parse_order(rh.orders.get_stock_order_info(order_id))

    def cancel_order(self, order_id: str) -> None:
        rh.orders.cancel_stock_order(order_id)
