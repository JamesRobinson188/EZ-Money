"""EZ-Money dashboard. Run with `streamlit run dashboard/app.py`.

The dashboard never talks to Robinhood. It edits the watchlist and settings
in the shared database and queues commands that the trader daemon carries out.
"""

from __future__ import annotations

import base64
import hmac
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from ezmoney import history
from ezmoney.config import get_settings
from ezmoney.db import OPEN, PENDING_BUY, PENDING_SELL, Lot, Store, normalize_symbol
from ezmoney.market_calendar import ET, MarketCalendar, now_et
from ezmoney.quotes import QuoteError, price_source, prices
from ezmoney.strategy import money, signed

HERE = Path(__file__).parent
GREEN, ORANGE, MUTED, GRID = "#46D39A", "#FF8F4D", "#A1ABB9", "#222A35"
HEARTBEAT_STALE = timedelta(minutes=3)
RANGES = {"7 nights": 7, "30 nights": 30, "90 nights": 90, "All": None}
# st.html strips inline <svg>, so the logo goes in as an image.
MOON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" viewBox="0 0 28 28" fill="none">'
    '<circle cx="14" cy="14" r="12.5" stroke="#F2B13C" stroke-width="1.5"/>'
    '<path d="M18.5 7.5a7.5 7.5 0 1 0 0 13 6 6 0 0 1 0-13Z" fill="#F2B13C"/></svg>'
)
MOON_SRC = "data:image/svg+xml;base64," + base64.b64encode(MOON_SVG.encode()).decode()


def moon(size: int = 28) -> str:
    return f'<img src="{MOON_SRC}" width="{size}" height="{size}" alt="">'

st.set_page_config(page_title="EZ-Money", page_icon=":material/bedtime:", layout="wide")
st.html(HERE / "style.css")

settings = get_settings()
MODE = settings.trading_mode


@st.cache_resource
def get_store() -> Store:
    return Store(settings.db_path)


@st.cache_resource
def get_calendar() -> MarketCalendar:
    return MarketCalendar()


@st.cache_data(ttl=60, show_spinner=False)
def live_prices(symbols: tuple[str, ...]) -> dict[str, float | None]:
    return prices(list(symbols), price_source(settings.quote_source))


store = get_store()
calendar = get_calendar()


# --- formatting ------------------------------------------------------------


class Html(str):
    """A table cell that is already HTML and must not be escaped."""


def pct(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}{abs(x) * 100:.2f}%"


def tone(x: float) -> str:
    return "ez-gain" if x >= 0 else "ez-loss"


def colored(text: str, x: float) -> Html:
    return Html(f'<span class="{tone(x)}">{escape(text)}</span>')


def countdown(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    hours, minutes = divmod(minutes, 60)
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def setting_time(key: str) -> datetime | None:
    value = store.get_setting(key)
    return datetime.fromisoformat(value).astimezone(ET) if value else None


def when(at: datetime | None) -> str:
    return f"{at:%a %b %-d} · {at:%-I:%M:%S %p} ET" if at else "not scheduled (daemon not running?)"


def table(headers: list[str], rows: list[list[object]], footer: list[object] | None = None, min_width: int = 480) -> str:
    def cell(value: object, tag: str) -> str:
        return f"<{tag}>{value if isinstance(value, Html) else escape(str(value))}</{tag}>"

    def row(values: list[object], first: str = "th") -> str:
        return "<tr>" + cell(values[0], first) + "".join(cell(v, "td") for v in values[1:]) + "</tr>"

    head = "<tr>" + "".join(f'<th scope="col">{escape(h)}</th>' for h in headers) + "</tr>"
    body = "".join(row(r) for r in rows)
    foot = f"<tfoot>{row(footer)}</tfoot>" if footer else ""
    return (
        f'<div class="ez-scroll"><table class="ez-table" style="min-width:{min_width}px">'
        f"<thead>{head}</thead><tbody>{body}</tbody>{foot}</table></div>"
    )


def tile(label: str, value: str, sub: str = "", cls: str = "") -> str:
    sub_html = f'<p class="ez-sub">{escape(sub)}</p>' if sub else ""
    return (
        f'<div class="ez-card"><p class="ez-label">{escape(label)}</p>'
        f'<p class="ez-num {cls}">{escape(value)}</p>{sub_html}</div>'
    )


def card(title: str, body: str, aside: str = "") -> str:
    aside_html = f'<p class="ez-note">{aside}</p>' if aside else ""
    return f'<section class="ez-card"><div class="ez-row"><h2 class="ez-h">{escape(title)}</h2>{aside_html}</div>{body}</section>'


def banner(text: str, kind: str = "amber") -> None:
    st.html(f'<p class="ez-banner ez-banner-{kind}">{text}</p>')


# --- password gate ---------------------------------------------------------


def check_password() -> bool:
    if not settings.dashboard_password or st.session_state.get("authed"):
        return True
    _, middle, _ = st.columns([1, 1.2, 1])
    with middle:
        st.html(
            f'<div class="ez-login">{moon(48)}<h1>EZ-Money</h1>'
            '<p class="ez-sub">Buys your picks at the close, sells them at the open.</p></div>'
        )
        with st.form("login"):
            password = st.text_input("Dashboard password", type="password")
            if st.form_submit_button("Unlock", type="primary", width="stretch"):
                if hmac.compare_digest(password.encode(), settings.dashboard_password.encode()):
                    st.session_state.authed = True
                    st.rerun()
                st.error("That password isn't right.")
        st.html(
            '<p class="ez-note">The dashboard only listens on 127.0.0.1:8501. '
            "On a cloud server, open it through an SSH tunnel.</p>"
        )
    return False


# --- header ----------------------------------------------------------------


@st.fragment(run_every="30s")
def daemon_status() -> None:
    beat = setting_time("heartbeat")
    if beat and now_et() - beat < HEARTBEAT_STALE:
        age = int((now_et() - beat).total_seconds())
        st.html(f'<p class="ez-status"><span class="ez-dot ez-dot-ok"></span>Daemon online · heartbeat {age}s ago</p>')
    else:
        seen = f"last seen {beat:%b %-d %-I:%M %p}" if beat else "never seen"
        st.html(f'<p class="ez-status"><span class="ez-dot ez-dot-bad"></span>Daemon offline · {seen}</p>')


def set_paused() -> None:
    store.set_setting("paused", st.session_state.pause)


def queue_sell_all() -> None:
    store.add_command("sell_all")
    st.session_state.confirm_sell = False
    st.toast("Sell queued. The daemon picks it up within a few seconds.")


def header() -> None:
    brand, status, pause, sell = st.columns([5, 4, 2, 2], vertical_alignment="center")
    badge = '<span class="ez-badge ez-badge-live">LIVE</span>' if settings.is_live else '<span class="ez-badge">PAPER</span>'
    brand.html(f'<div class="ez-brand">{moon()}<span class="ez-name">EZ-Money</span>{badge}</div>')
    with status:
        daemon_status()
    pause.toggle("Pause trading", value=store.paused(), key="pause", on_change=set_paused)
    sell.button(
        "Sell all now", width="stretch", on_click=lambda: st.session_state.update(confirm_sell=True), icon=":material/logout:"
    )

    if st.session_state.get("confirm_sell"):
        count = len(store.lots(MODE, (OPEN,)))
        with st.container(border=True):
            st.html(
                f'<p class="ez-strong">Sell all {count} bot positions?</p>'
                '<p class="ez-sub">If the market is closed, the sells queue for the next open. '
                "Your other Robinhood holdings are not touched.</p>"
            )
            cancel, confirm, _ = st.columns([1, 1, 4])
            cancel.button("Cancel", on_click=lambda: st.session_state.update(confirm_sell=False), width="stretch")
            confirm.button("Queue sell", type="primary", on_click=queue_sell_all, width="stretch", disabled=not count)

    if store.paused():
        banner(
            '<strong>Trading paused.</strong> <span class="ez-muted">The next buy will be skipped. '
            "Open positions still sell at the open.</span>"
        )
    daemon_mode = store.get_setting("daemon_mode")
    if daemon_mode and daemon_mode != MODE:
        banner(
            f"The daemon is running in <strong>{escape(daemon_mode.upper())}</strong> mode but this dashboard is "
            f"<strong>{MODE.upper()}</strong>. Check TRADING_MODE in .env for both.",
            "warn",
        )
    if not settings.dashboard_password:
        banner("No DASHBOARD_PASSWORD is set. Keep the dashboard bound to 127.0.0.1.", "warn")


# --- dashboard tab -----------------------------------------------------------


def cycle_card(now: datetime, held: list[Lot]) -> str:
    next_sell, next_buy = setting_time("next_sell"), setting_time("next_buy")
    when_now = f"{now:%a %b %-d} · {'market open' if calendar.is_open(now) else 'market closed'} · {now:%-I:%M %p} ET"
    if not held:
        body = (
            '<p class="ez-p"><span class="ez-amber">Next buy</span> '
            f'<span class="ez-muted">{escape(when(next_buy))}</span>'
            + (f' · in <span class="ez-mono">{countdown(next_buy - now)}</span>' if next_buy else "")
            + "</p><p class=\"ez-note\">Nothing is held right now.</p>"
        )
        return card("Tonight's cycle", body, escape(when_now))

    stamps = [datetime.fromisoformat(lot.buy_ts).astimezone(ET) for lot in held if lot.buy_ts]
    bought_at = max(stamps) if stamps else now
    cost = sum(lot.cost for lot in held)
    progress = 0.0
    if next_sell and next_sell > bought_at:
        progress = min(max((now - bought_at) / (next_sell - bought_at), 0.0), 1.0)
    body = (
        f'<div class="ez-track" role="img" aria-label="Overnight hold {progress:.0%} complete">'
        '<div class="ez-track-line"></div>'
        f'<div class="ez-track-fill" style="width:{progress * 100:.1f}%"></div>'
        '<div class="ez-track-start"></div>'
        f'<div class="ez-track-now" style="left:{progress * 100:.1f}%"></div>'
        '<div class="ez-track-end"></div></div>'
        '<div class="ez-row">'
        f'<p class="ez-p"><span class="ez-amber">Bought</span> <span class="ez-muted">{bought_at:%a %-I:%M %p} ET · '
        f"{money(cost)} · {len(held)} lots</span></p>"
        f'<p class="ez-p"><span class="ez-blue">Sells</span> <span class="ez-muted">{escape(when(next_sell))}'
        + (f' · in</span> <span class="ez-mono">{countdown(next_sell - now)}</span>' if next_sell else "</span>")
        + "</p></div>"
    )
    return card("Tonight's cycle", body, escape(when_now))


def paper_buying_power() -> float | None:
    if settings.is_live:
        return None
    held = sum(lot.remaining_qty * (lot.buy_price or 0) for lot in store.lots(MODE, (OPEN, PENDING_SELL)))
    return settings.paper_starting_cash + store.realized_pnl(MODE) - held


def positions_card(held: list[Lot], quotes: dict[str, float | None]) -> str:
    if not held:
        return card("Open positions", '<p class="ez-note">No open positions.</p>', "Bot-owned lots only")
    rows, total_cost, total_pnl, priced = [], 0.0, 0.0, True
    for lot in held:
        price = quotes.get(lot.symbol)
        status = {PENDING_BUY: "buying", PENDING_SELL: "selling"}.get(lot.status)
        name = Html(escape(lot.symbol) + (f'<span class="ez-tag">{status}</span>' if status else ""))
        if price and lot.buy_price:
            pnl = (price - lot.buy_price) * lot.remaining_qty
            change = colored(f"{signed(pnl)} {pct(price / lot.buy_price - 1)}", pnl)
            total_pnl += pnl
        else:
            change, priced = "—", False
        total_cost += lot.cost
        rows.append([name, f"{lot.qty:.6f}", money(lot.buy_price or 0), money(price) if price else "—", change])
    footer = ["Total", "", money(total_cost), "", colored(signed(total_pnl), total_pnl) if priced else "—"]
    body = table(["Ticker", "Shares", "Bought at", "Now", "Unrealized"], rows, footer)
    body += '<p class="ez-note">Prices refresh every minute and may lag. The sell fills at the open, not at these prices.</p>'
    return card("Open positions", body, "Bot-owned lots only")


def activity_card() -> str:
    items = []
    for event in store.recent_events(30):
        at = datetime.fromisoformat(event.ts).astimezone(ET)
        items.append(
            f'<li><time datetime="{escape(event.ts)}" title="{at:%a %b %-d}">{at:%-I:%M:%S %p}</time>'
            # A word joiner after the minus sign keeps "−$20.58" from wrapping apart.
            f'<span class="ez-mark ez-mark-{escape(event.level)}"></span><span>{escape(event.msg).replace("−$", "−⁠$")}</span></li>'
        )
    body = f'<ol class="ez-events">{"".join(items)}</ol>' if items else '<p class="ez-note">Nothing yet.</p>'
    return card("Activity", body)


@st.fragment(run_every="60s")
def overview() -> None:
    now = now_et()
    lots = store.lots(MODE)
    held = [lot for lot in lots if lot.status in (PENDING_BUY, OPEN, PENDING_SELL)]
    nights = history.nights_frame(history.lots_frame(lots))
    last30 = history.summarize(history.last_nights(nights, 30))
    quotes = live_prices(tuple(sorted({lot.symbol for lot in held}))) if held else {}

    st.html(cycle_card(now, held))

    cost = sum(lot.cost for lot in held)
    unrealized = [
        (quotes[lot.symbol] - lot.buy_price) * lot.remaining_qty
        for lot in held
        if quotes.get(lot.symbol) and lot.buy_price
    ]
    bp = paper_buying_power()
    tiles = [
        tile("Held tonight", money(cost), f"Buying power {money(bp)}" if bp is not None else "Live buying power is checked at each buy"),
        tile(
            "Unrealized",
            signed(sum(unrealized)) if held and len(unrealized) == len(held) else "—",
            f"{pct(sum(unrealized) / cost)} on {money(cost)}" if held and cost and len(unrealized) == len(held) else "No price yet" if held else "Nothing held",
            tone(sum(unrealized)) if unrealized else "",
        ),
        tile(
            "Realized · last 30 nights",
            signed(last30.realized),
            f"Best {signed(last30.best)} · worst {signed(last30.worst)}" if last30.nights else "No closed nights yet",
            tone(last30.realized) if last30.nights else "",
        ),
        tile(
            "Winning nights",
            f"{last30.wins / last30.nights:.1%}" if last30.nights else "—",
            f"{last30.wins} of {last30.nights} nights" if last30.nights else "",
        ),
    ]
    st.html(f'<section class="ez-tiles" aria-label="Key numbers">{"".join(tiles)}</section>')

    left, right = st.columns([3, 2], gap="medium")
    left.html(positions_card(held, quotes))
    right.html(activity_card())


def save_watchlist(items: list) -> None:
    edits = st.session_state.get(f"watch_editor_{st.session_state.wl_version}", {}).get("edited_rows", {})
    for index, change in edits.items():
        item = items[int(index)]
        if change.get("Remove"):
            store.remove_watch(item.symbol)
            continue
        dollars = settings.clamp_dollars(float(change.get("Per night", item.dollars) or item.dollars))
        store.upsert_watch(item.symbol, dollars, bool(change.get("Trade", item.enabled)))
    st.session_state.wl_version += 1
    st.toast("Watchlist saved")


def watchlist_card(lots_df: pd.DataFrame) -> None:
    st.session_state.setdefault("wl_version", 0)
    cap = settings.max_dollars_per_ticker
    items = store.watchlist()
    last = history.last_night_returns(lots_df)
    with st.container(border=True):
        st.html(
            '<div class="ez-row"><h2 class="ez-h">Watchlist</h2>'
            f'<p class="ez-note">Bought every session at close − {store.buy_offset_min()} min</p></div>'
        )
        if items:
            frame = pd.DataFrame(
                [
                    {
                        "Trade": item.enabled,
                        "Ticker": item.symbol,
                        "Per night": item.dollars,
                        "Last night": pct(last[item.symbol]) if item.symbol in last else "—",
                        "Remove": False,
                    }
                    for item in items
                ]
            )
            st.data_editor(
                frame,
                key=f"watch_editor_{st.session_state.wl_version}",
                hide_index=True,
                disabled=["Ticker", "Last night"],
                column_config={
                    "Trade": st.column_config.CheckboxColumn("Trade", help="Buy this ticker at the close"),
                    "Per night": st.column_config.NumberColumn("Per night", min_value=1, max_value=cap, step=1, format="dollar"),
                    "Remove": st.column_config.CheckboxColumn("Remove"),
                },
                width="stretch",
            )
            st.button("Save watchlist", on_click=save_watchlist, args=(items,), icon=":material/save:")
        else:
            st.html('<p class="ez-note">No tickers yet. Add one below.</p>')

        with st.form("add_ticker", clear_on_submit=True, border=False):
            sym_col, amt_col, btn_col = st.columns([2, 2, 1.5], vertical_alignment="bottom")
            raw = sym_col.text_input("Ticker", placeholder="e.g. MSFT")
            dollars = amt_col.number_input("Dollars per night", min_value=1.0, max_value=cap, value=min(500.0, cap), step=50.0)
            added = btn_col.form_submit_button("Add ticker", width="stretch", type="primary")
        if added:
            add_ticker(raw, dollars, {item.symbol for item in items})

        enabled = [item for item in items if item.enabled]
        total = sum(settings.clamp_dollars(item.dollars) for item in enabled)
        st.html(
            f'<div class="ez-row"><p class="ez-p">Next buy <span class="ez-mono">{money(total)}</span> '
            f'<span class="ez-muted">across {len(enabled)} ticker{"s" if len(enabled) != 1 else ""}</span></p>'
            f'<p class="ez-note">Caps: {money(cap)} per ticker · {money(settings.max_dollars_per_night)} per night</p></div>'
        )
        if total > settings.max_dollars_per_night:
            banner("This is over the nightly cap, so the daemon will skip the buy. Turn off a ticker or lower an amount.", "warn")


def add_ticker(raw: str, dollars: float, existing: set[str]) -> None:
    try:
        symbol = normalize_symbol(raw)
    except ValueError as exc:
        st.error(str(exc))
        return
    if symbol in existing:
        st.warning(f"{symbol} is already on the watchlist.")
        return
    try:
        price_source(settings.quote_source)(symbol)
    except QuoteError:
        st.error(f"Couldn't get a price for {symbol}. Check the ticker.")
        return
    store.upsert_watch(symbol, settings.clamp_dollars(dollars))
    st.toast(f"Watching {symbol}")
    st.rerun()


def pnl_chart(nights: pd.DataFrame) -> None:
    recent = history.last_nights(nights, 30).copy()
    with st.container(border=True):
        st.html('<div class="ez-row"><h2 class="ez-h">Realized P&amp;L · last 30 nights</h2></div>')
        if recent.empty:
            st.html('<p class="ez-note">The chart fills in after the first round trip.</p>')
            return
        recent["cumulative"] = recent["pnl"].cumsum()
        recent["night"] = pd.to_datetime(recent["sold"])
        color = GREEN if recent["cumulative"].iloc[-1] >= 0 else ORANGE
        base = alt.Chart(recent).encode(
            x=alt.X("night:T", title=None, axis=alt.Axis(format="%b %-d", grid=False, labelColor=MUTED, domainColor=GRID, tickColor=GRID)),
            y=alt.Y("cumulative:Q", title=None, axis=alt.Axis(format="$,.2f", labelColor=MUTED, gridColor=GRID, domain=False, ticks=False)),
            tooltip=[
                alt.Tooltip("night:T", title="Sold", format="%a %b %-d"),
                alt.Tooltip("pnl:Q", title="Night", format="+$,.2f"),
                alt.Tooltip("cumulative:Q", title="Total", format="+$,.2f"),
            ],
        )
        chart = base.mark_area(color=color, opacity=0.12) + base.mark_line(color=color, strokeWidth=2, point=alt.OverlayMarkDef(color=color, size=24))
        st.altair_chart(chart.properties(height=240).configure_view(strokeWidth=0), theme="streamlit", width="stretch")


def schedule_card() -> None:
    with st.container(border=True):
        st.html('<h2 class="ez-h">Schedule &amp; safety</h2>')
        with st.form("schedule", border=False):
            offset_col, delay_col, skip_col = st.columns([1, 1, 2], vertical_alignment="bottom")
            offset = offset_col.number_input("Buy · minutes before close", 1, 10, store.buy_offset_min(), step=1)
            delay = delay_col.number_input("Sell · seconds after open", 0, 120, store.sell_delay_sec(), step=1)
            skip = skip_col.toggle("Skip buys before weekends and holidays", value=store.skip_before_breaks())
            saved = st.form_submit_button("Save schedule", icon=":material/schedule:")
        if saved:
            store.set_setting("buy_offset_min", int(offset))
            store.set_setting("sell_delay_sec", int(delay))
            store.set_setting("skip_before_breaks", skip)
            store.add_command("replan")
            st.toast("Schedule saved. The daemon replans today's times.")
        st.html(
            '<p class="ez-note">Early-close days (like the day after Thanksgiving) move the buy to before 1 PM ET '
            "automatically. The dollar caps and live trading are set in <code>.env</code> on the server and "
            "can't be changed here.</p>"
        )
        if st.button("Send test notification", icon=":material/notifications:"):
            store.add_command("test_notify")
            st.toast("Test queued. Check Discord or Telegram in a few seconds.")


def dashboard_tab() -> None:
    overview()
    lots_df = history.lots_frame(store.lots(MODE))
    left, right = st.columns([2, 3], gap="medium")
    with left:
        watchlist_card(lots_df)
    with right:
        pnl_chart(history.nights_frame(lots_df))
    schedule_card()


# --- history tab -------------------------------------------------------------


def history_tab() -> None:
    lots_df = history.lots_frame(store.lots(MODE))
    nights = history.nights_frame(lots_df)

    title, controls = st.columns([2, 3], vertical_alignment="center")
    title.html('<h2 class="ez-h" style="font-size:24px">History</h2>')
    with controls:
        range_col, export_col = st.columns([3, 1], vertical_alignment="center")
        choice = range_col.segmented_control("Range", list(RANGES), default="30 nights", key="range", label_visibility="collapsed")
    selected = history.last_nights(nights, RANGES[choice or "30 nights"])
    chosen_lots = lots_df[lots_df["bought"].isin(set(selected["bought"]))] if not selected.empty else lots_df.iloc[0:0]
    export_col.download_button(
        "Export CSV",
        chosen_lots.to_csv(index=False).encode(),
        file_name="ez-money-history.csv",
        mime="text/csv",
        icon=":material/download:",
        disabled=chosen_lots.empty,
        width="stretch",
    )

    s = history.summarize(selected)
    st.html(
        '<section class="ez-tiles" aria-label="Summary">'
        + tile("Realized P&L", signed(s.realized), cls=tone(s.realized) if s.nights else "")
        + tile("Average per night", signed(s.avg_per_night) if s.nights else "—")
        + tile("Average overnight return", pct(s.avg_return) if s.nights else "—")
        + tile("Winning nights", f"{s.wins} / {s.nights}" if s.nights else "—")
        + tile("Largest drawdown", signed(s.drawdown) if s.drawdown else "$0.00", cls="ez-loss" if s.drawdown else "")
        + "</section>"
    )
    if selected.empty:
        st.html('<p class="ez-note">No closed nights yet. Results show up here after the first sell at the open.</p>')
        return

    with st.container(border=True):
        st.html(
            '<div class="ez-row"><h2 class="ez-h">P&amp;L by night</h2>'
            '<p class="ez-note">Bought at close, sold at the next open</p></div>'
        )
        bars = selected.assign(label=pd.to_datetime(selected["sold"]).dt.strftime("%b %-d"))
        chart = (
            alt.Chart(bars)
            .mark_bar(cornerRadiusEnd=2)
            .encode(
                x=alt.X("label:N", sort=None, title=None, axis=alt.Axis(labelColor=MUTED, labelAngle=0, labelOverlap=True, domainColor=GRID, ticks=False)),
                y=alt.Y("pnl:Q", title=None, axis=alt.Axis(format="$,.2f", labelColor=MUTED, gridColor=GRID, domain=False, ticks=False)),
                color=alt.condition(alt.datum.pnl >= 0, alt.value(GREEN), alt.value(ORANGE)),
                tooltip=[alt.Tooltip("label:N", title="Sold"), alt.Tooltip("pnl:Q", title="P&L", format="+$,.2f"), alt.Tooltip("ret:Q", title="Return", format="+.2%")],
            )
            .properties(height=220)
            .configure_view(strokeWidth=0)
        )
        st.altair_chart(chart, theme="streamlit", width="stretch")

    left, right = st.columns([2, 3], gap="medium")
    tickers = history.by_ticker(lots_df, selected)
    left.html(
        card(
            "By ticker",
            table(
                ["Ticker", "Winning nights", "Avg return", "Total"],
                [[r.symbol, f"{r.wins} / {r.nights}", pct(r.avg_ret), colored(signed(r.total), r.total)] for r in tickers.itertuples()],
                min_width=380,
            ),
        )
    )
    rows = []
    for r in selected.iloc[::-1].head(15).itertuples():
        held_days = (r.sold - r.bought).days
        tag = f'<span class="ez-tag">{"weekend" if r.bought.weekday() == 4 else "holiday"}</span>' if held_days > 1 else ""
        rows.append(
            [f"{r.bought:%a %b %-d}", Html(f"{r.sold:%a %b %-d}{tag}"), r.lots, pct(r.ret), colored(signed(r.pnl), r.pnl)]
        )
    right.html(card("Recent nights", table(["Bought", "Sold", "Lots", "Return", "P&L"], rows, min_width=520)))


# --- page --------------------------------------------------------------------

if not check_password():
    st.stop()

header()
dashboard, past = st.tabs(["Dashboard", "History"])
with dashboard:
    dashboard_tab()
with past:
    history_tab()
st.html(
    f'<p class="ez-note">EZ-Money · {MODE} mode · Robinhood through the unofficial robin_stocks library. '
    "Not investment advice.</p>"
)
