"""Turns closed lots into the nightly results the dashboard shows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from .db import CLOSED, Lot
from .market_calendar import ET

LOT_COLUMNS = ["symbol", "bought", "sold", "qty", "buy_price", "sell_price", "cost", "pnl", "ret"]
NIGHT_COLUMNS = ["bought", "sold", "lots", "cost", "pnl", "ret"]


def lots_frame(lots: list[Lot]) -> pd.DataFrame:
    rows = []
    for lot in lots:
        if lot.status != CLOSED or not lot.buy_price or lot.pnl is None:
            continue
        cost = lot.qty * lot.buy_price
        sold = datetime.fromisoformat(lot.sell_ts).astimezone(ET).date() if lot.sell_ts else lot.session_date
        rows.append(
            {
                "symbol": lot.symbol,
                "bought": lot.session_date,
                "sold": sold,
                "qty": lot.qty,
                "buy_price": lot.buy_price,
                "sell_price": lot.sell_price,
                "cost": cost,
                "pnl": lot.pnl,
                "ret": lot.pnl / cost if cost else 0.0,
            }
        )
    return pd.DataFrame(rows, columns=LOT_COLUMNS)


def nights_frame(lots: pd.DataFrame) -> pd.DataFrame:
    """One row per night (the session the lots were bought in), oldest first."""
    if lots.empty:
        return pd.DataFrame(columns=NIGHT_COLUMNS)
    nights = (
        lots.groupby("bought")
        .agg(sold=("sold", "max"), lots=("symbol", "count"), cost=("cost", "sum"), pnl=("pnl", "sum"))
        .reset_index()
        .sort_values("bought")
    )
    nights["ret"] = nights["pnl"] / nights["cost"]
    return nights[NIGHT_COLUMNS].reset_index(drop=True)


def last_nights(nights: pd.DataFrame, count: int | None) -> pd.DataFrame:
    return nights if count is None else nights.tail(count).reset_index(drop=True)


@dataclass(frozen=True)
class Summary:
    nights: int
    realized: float
    avg_per_night: float
    avg_return: float
    wins: int
    best: float
    worst: float
    drawdown: float


def summarize(nights: pd.DataFrame) -> Summary:
    if nights.empty:
        return Summary(0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0)
    cumulative = nights["pnl"].cumsum()
    peak = cumulative.cummax().clip(lower=0.0)
    return Summary(
        nights=len(nights),
        realized=float(nights["pnl"].sum()),
        avg_per_night=float(nights["pnl"].mean()),
        avg_return=float(nights["ret"].mean()),
        wins=int((nights["pnl"] > 0).sum()),
        best=float(nights["pnl"].max()),
        worst=float(nights["pnl"].min()),
        drawdown=float(min((cumulative - peak).min(), 0.0)),
    )


def by_ticker(lots: pd.DataFrame, nights: pd.DataFrame) -> pd.DataFrame:
    """Per-ticker results over the nights in `nights`."""
    columns = ["symbol", "nights", "wins", "avg_ret", "total"]
    if lots.empty or nights.empty:
        return pd.DataFrame(columns=columns)
    subset = lots[lots["bought"].isin(set(nights["bought"]))]
    out = (
        subset.groupby("symbol")
        .agg(nights=("pnl", "count"), wins=("pnl", lambda s: int((s > 0).sum())), avg_ret=("ret", "mean"), total=("pnl", "sum"))
        .reset_index()
        .sort_values("total", ascending=False)
    )
    return out[columns].reset_index(drop=True)


def last_night_returns(lots: pd.DataFrame) -> dict[str, float]:
    """Each ticker's return on the most recent night it was held."""
    if lots.empty:
        return {}
    latest = lots.sort_values("bought").groupby("symbol").tail(1)
    return dict(zip(latest["symbol"], latest["ret"], strict=True))
