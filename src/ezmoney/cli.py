"""Command line entry point: `ez-money run | login | buy-now | sell-now | sell-all | status | watch`."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from .brokers import make_broker
from .config import Settings, get_settings
from .db import Store, normalize_symbol
from .market_calendar import ET, MarketCalendar
from .notify import Notifier
from .strategy import RunResult, Trader, money


def make_trader(settings: Settings) -> Trader:
    store = Store(settings.db_path)
    return Trader(store, make_broker(settings, store), Notifier(settings), MarketCalendar(), settings)


def _print_result(result: RunResult) -> int:
    print(result.message)
    for line in result.lines:
        print(f"  {line}")
    return 0 if result.ok else 1


def cmd_run(settings: Settings) -> int:
    from .scheduler import Daemon

    trader = make_trader(settings)
    Daemon(trader, trader.store, trader.calendar, trader.notifier).run()
    return 0


def cmd_login(settings: Settings) -> int:
    from .brokers.robinhood import RobinhoodBroker

    broker = RobinhoodBroker(settings)
    print("Logging in to Robinhood. Approve the device in the Robinhood app if asked.")
    broker.login()
    print(f"Logged in. Buying power: {money(broker.buying_power())}")
    print(f"Session saved in {settings.session_dir}")
    return 0


def cmd_status(settings: Settings) -> int:
    store = Store(settings.db_path)
    print(f"Mode:     {settings.trading_mode.upper()}{'  (PAUSED)' if store.paused() else ''}")
    heartbeat = store.get_setting("heartbeat")
    print(f"Daemon:   last heartbeat {heartbeat or 'never'}")
    for key in ("next_sell", "next_buy"):
        value = store.get_setting(key)
        when = datetime.fromisoformat(value).astimezone(ET).strftime("%a %b %-d %-I:%M:%S %p ET") if value else "-"
        print(f"{key.replace('_', ' ').capitalize():<9} {when}")
    print("\nWatchlist:")
    for item in store.watchlist():
        print(f"  {'on ' if item.enabled else 'off'}  {item.symbol:<6} {money(item.dollars)}")
    lots = store.lots(settings.trading_mode, ("pending_buy", "open", "pending_sell"))
    print(f"\nOpen lots: {len(lots)}")
    for lot in lots:
        print(f"  {lot.symbol:<6} {lot.qty:.6f} sh @ {money(lot.buy_price or 0)}  {lot.status}  bought {lot.session_date}")
    print("\nRecent activity:")
    for event in store.recent_events(10):
        print(f"  {event.ts}  {event.level:<5}  {event.msg}")
    return 0


def cmd_watch(settings: Settings, args: argparse.Namespace) -> int:
    store = Store(settings.db_path)
    if args.action == "add":
        symbol = normalize_symbol(args.symbol)
        dollars = settings.clamp_dollars(args.dollars)
        store.upsert_watch(symbol, dollars, enabled=True)
        print(f"Watching {symbol} at {money(dollars)} a night")
    elif args.action == "remove":
        store.remove_watch(normalize_symbol(args.symbol))
        print(f"Removed {args.symbol.upper()}")
    for item in store.watchlist():
        print(f"  {'on ' if item.enabled else 'off'}  {item.symbol:<6} {money(item.dollars)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ez-money", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="start the trader daemon")
    sub.add_parser("login", help="log in to Robinhood and save the session (live mode)")
    buy = sub.add_parser("buy-now", help="run the close buy now (must be within 15 minutes of the close)")
    buy.add_argument("--force", action="store_true", help="paper mode only: ignore the market clock")
    sub.add_parser("sell-now", help="run the open sell now (sells lots from earlier sessions)")
    sell_all = sub.add_parser("sell-all", help="sell every open bot position now")
    sell_all.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    sub.add_parser("status", help="show the current state")
    watch = sub.add_parser("watch", help="manage the watchlist")
    watch_sub = watch.add_subparsers(dest="action", required=True)
    add = watch_sub.add_parser("add")
    add.add_argument("symbol")
    add.add_argument("dollars", type=float)
    remove = watch_sub.add_parser("remove")
    remove.add_argument("symbol")
    watch_sub.add_parser("list")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("apscheduler").setLevel(logging.WARNING)  # it logs every 5-second poll otherwise
    settings = get_settings()

    try:
        if args.cmd == "run":
            return cmd_run(settings)
        if args.cmd == "login":
            return cmd_login(settings)
        if args.cmd == "status":
            return cmd_status(settings)
        if args.cmd == "watch":
            return cmd_watch(settings, args)
        if args.cmd == "buy-now":
            if args.force and settings.is_live:
                print("--force is only allowed in paper mode", file=sys.stderr)
                return 2
            return _print_result(make_trader(settings).buy_at_close(force=args.force))
        if args.cmd == "sell-now":
            return _print_result(make_trader(settings).sell_at_open())
        if args.cmd == "sell-all":
            if settings.is_live and not args.yes:
                if input("Sell every open bot position with REAL money? Type 'yes': ").strip() != "yes":
                    print("Cancelled")
                    return 1
            return _print_result(make_trader(settings).sell_all_now())
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
