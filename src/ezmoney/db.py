"""SQLite storage shared by the trader daemon and the dashboard.

Each call opens its own short-lived connection, so the store is safe to use
from scheduler threads and from Streamlit reruns at the same time. WAL mode
lets the dashboard read while the daemon writes.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    symbol    TEXT PRIMARY KEY,
    dollars   REAL NOT NULL CHECK (dollars > 0),
    enabled   INTEGER NOT NULL DEFAULT 1,
    added_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT NOT NULL,
    mode           TEXT NOT NULL,
    status         TEXT NOT NULL,
    session_date   TEXT NOT NULL,
    dollars        REAL NOT NULL,
    buy_order_id   TEXT,
    buy_ts         TEXT,
    qty            REAL NOT NULL DEFAULT 0,
    buy_price      REAL,
    sell_order_id  TEXT,
    sell_ts        TEXT,
    sold_qty       REAL NOT NULL DEFAULT 0,
    proceeds       REAL NOT NULL DEFAULT 0,
    sell_price     REAL,
    pnl            REAL,
    sell_attempts  INTEGER NOT NULL DEFAULT 0,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS lots_status ON lots (mode, status);

CREATE TABLE IF NOT EXISTS runs (
    session_date  TEXT NOT NULL,
    side          TEXT NOT NULL,
    mode          TEXT NOT NULL,
    status        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    detail        TEXT,
    PRIMARY KEY (session_date, side, mode)
);

CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    level  TEXT NOT NULL,
    msg    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    status  TEXT NOT NULL DEFAULT 'pending',
    detail  TEXT
);
"""

DEFAULT_SETTINGS = {
    "paused": "0",
    "buy_offset_min": "2",
    "sell_delay_sec": "5",
    "skip_before_breaks": "0",
}

# Lot statuses
PENDING_BUY = "pending_buy"
OPEN = "open"
PENDING_SELL = "pending_sell"
CLOSED = "closed"
FAILED = "failed"


SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_symbol(raw: str) -> str:
    """Upper-case a ticker and check it looks like one (SPY, BRK.B)."""
    symbol = raw.strip().upper()
    if not SYMBOL_RE.match(symbol):
        raise ValueError(f"{raw!r} doesn't look like a ticker symbol")
    return symbol


@dataclass(frozen=True)
class WatchItem:
    symbol: str
    dollars: float
    enabled: bool


@dataclass(frozen=True)
class Lot:
    id: int
    symbol: str
    mode: str
    status: str
    session_date: date
    dollars: float
    buy_order_id: str | None
    buy_ts: str | None
    qty: float
    buy_price: float | None
    sell_order_id: str | None
    sell_ts: str | None
    sold_qty: float
    proceeds: float
    sell_price: float | None
    pnl: float | None
    sell_attempts: int
    error: str | None

    @property
    def remaining_qty(self) -> float:
        return max(self.qty - self.sold_qty, 0.0)

    @property
    def cost(self) -> float:
        return (self.buy_price or 0.0) * self.qty

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Lot:
        data = dict(row)
        data["session_date"] = date.fromisoformat(data["session_date"])
        return cls(**data)


@dataclass(frozen=True)
class Event:
    id: int
    ts: str
    level: str
    msg: str


@dataclass(frozen=True)
class Command:
    id: int
    ts: str
    kind: str
    status: str
    detail: str | None


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.executemany(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", DEFAULT_SETTINGS.items()
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- watchlist -------------------------------------------------------

    def watchlist(self) -> list[WatchItem]:
        with self._conn() as conn:
            rows = conn.execute("SELECT symbol, dollars, enabled FROM watchlist ORDER BY rowid").fetchall()
        return [WatchItem(r["symbol"], r["dollars"], bool(r["enabled"])) for r in rows]

    def upsert_watch(self, symbol: str, dollars: float, enabled: bool = True) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO watchlist (symbol, dollars, enabled, added_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT (symbol) DO UPDATE SET dollars = excluded.dollars, enabled = excluded.enabled""",
                (symbol, dollars, int(enabled), utcnow()),
            )

    def remove_watch(self, symbol: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))

    # --- settings --------------------------------------------------------

    def get_setting(self, key: str, default: str = "") -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: object) -> None:
        if isinstance(value, bool):
            value = int(value)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def paused(self) -> bool:
        return self.get_setting("paused") == "1"

    def buy_offset_min(self) -> int:
        return int(self.get_setting("buy_offset_min", "2"))

    def sell_delay_sec(self) -> int:
        return int(self.get_setting("sell_delay_sec", "5"))

    def skip_before_breaks(self) -> bool:
        return self.get_setting("skip_before_breaks") == "1"

    # --- runs ------------------------------------------------------------

    def start_run(self, session: date, side: str, mode: str) -> bool:
        """Record that a run started. Returns False if one already exists for this session."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO runs (session_date, side, mode, status, started_at) VALUES (?, ?, ?, 'running', ?)",
                (session.isoformat(), side, mode, utcnow()),
            )
            return cur.rowcount == 1

    def finish_run(
        self, session: date, side: str, mode: str, status: str, detail: str = "", *, only_running: bool = False
    ) -> None:
        sql = "UPDATE runs SET status = ?, finished_at = ?, detail = ? WHERE session_date = ? AND side = ? AND mode = ?"
        if only_running:
            sql += " AND status = 'running'"
        with self._conn() as conn:
            conn.execute(sql, (status, utcnow(), detail, session.isoformat(), side, mode))

    def runs(self, mode: str, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE mode = ? ORDER BY session_date DESC, side LIMIT ?", (mode, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_run(self, session: date, side: str, mode: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM runs WHERE session_date = ? AND side = ? AND mode = ?",
                (session.isoformat(), side, mode),
            )

    # --- lots ------------------------------------------------------------

    def create_lot(self, symbol: str, mode: str, session: date, dollars: float) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO lots (symbol, mode, status, session_date, dollars) VALUES (?, ?, ?, ?, ?)",
                (symbol, mode, PENDING_BUY, session.isoformat(), dollars),
            )
            return int(cur.lastrowid)

    def update_lot(self, lot_id: int, **fields: object) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as conn:
            conn.execute(f"UPDATE lots SET {cols} WHERE id = ?", (*fields.values(), lot_id))

    def get_lot(self, lot_id: int) -> Lot:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
        return Lot.from_row(row)

    def lots(self, mode: str, statuses: tuple[str, ...] | None = None) -> list[Lot]:
        sql = "SELECT * FROM lots WHERE mode = ?"
        args: list[object] = [mode]
        if statuses:
            sql += f" AND status IN ({', '.join('?' for _ in statuses)})"
            args.extend(statuses)
        sql += " ORDER BY id"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [Lot.from_row(r) for r in rows]

    def realized_pnl(self, mode: str) -> float:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS total FROM lots WHERE mode = ? AND status = ?", (mode, CLOSED)
            ).fetchone()
        return float(row["total"])

    # --- events ----------------------------------------------------------

    def log(self, level: str, msg: str) -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO events (ts, level, msg) VALUES (?, ?, ?)", (utcnow(), level, msg))

    def recent_events(self, limit: int = 50) -> list[Event]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [Event(**dict(r)) for r in rows]

    # --- commands (dashboard -> daemon) ----------------------------------

    def add_command(self, kind: str) -> int:
        with self._conn() as conn:
            cur = conn.execute("INSERT INTO commands (ts, kind) VALUES (?, ?)", (utcnow(), kind))
            return int(cur.lastrowid)

    def pending_commands(self) -> list[Command]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM commands WHERE status = 'pending' ORDER BY id").fetchall()
        return [Command(**dict(r)) for r in rows]

    def finish_command(self, command_id: int, status: str, detail: str = "") -> None:
        with self._conn() as conn:
            conn.execute("UPDATE commands SET status = ?, detail = ? WHERE id = ?", (status, detail, command_id))
