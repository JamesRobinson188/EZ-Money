"""Price lookups that don't need a Robinhood login.

Paper mode fills against these prices, and the dashboard uses them to show
unrealized P&L. "demo" prices are synthetic and only meant for trying the app
without network access.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Callable
from datetime import datetime, timezone

QuoteFn = Callable[[str], float]


class QuoteError(RuntimeError):
    pass


def yahoo_price(symbol: str) -> float:
    import yfinance as yf

    try:
        price = yf.Ticker(symbol).fast_info["last_price"]
    except Exception as exc:  # yfinance raises a wide variety of errors
        raise QuoteError(f"No Yahoo quote for {symbol}: {exc}") from exc
    if price is None or not math.isfinite(price) or price <= 0:
        raise QuoteError(f"No Yahoo quote for {symbol}")
    return float(price)


def demo_price(symbol: str, at: datetime | None = None) -> float:
    """A made-up but stable price that drifts a little from minute to minute."""
    at = at or datetime.now(timezone.utc)
    seed = int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)
    base = 20 + seed % 580
    minute = int(at.timestamp() // 60)
    rng = random.Random(f"{symbol}:{minute}")
    drift = 0.02 * math.sin(minute / 390 + seed)
    return round(base * (1 + drift + rng.uniform(-0.003, 0.003)), 2)


def price_source(name: str) -> QuoteFn:
    return demo_price if name == "demo" else yahoo_price


def prices(symbols: list[str], quote: QuoteFn) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for symbol in symbols:
        try:
            out[symbol] = quote(symbol)
        except QuoteError:
            out[symbol] = None
    return out
