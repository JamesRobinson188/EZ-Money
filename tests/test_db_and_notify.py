from datetime import date

import httpx
import pytest

from ezmoney.config import Settings
from ezmoney.db import normalize_symbol
from ezmoney.notify import Notifier


def test_watchlist_keeps_insertion_order_and_updates_in_place(store):
    store.upsert_watch("SPY", 500)
    store.upsert_watch("AAPL", 300)
    store.upsert_watch("SPY", 250, enabled=False)
    assert [(w.symbol, w.dollars, w.enabled) for w in store.watchlist()] == [
        ("SPY", 250, False),
        ("AAPL", 300, True),
    ]
    store.remove_watch("SPY")
    assert [w.symbol for w in store.watchlist()] == ["AAPL"]


def test_runs_are_recorded_once_per_session(store):
    day = date(2026, 10, 29)
    assert store.start_run(day, "buy", "paper")
    assert not store.start_run(day, "buy", "paper")
    assert store.start_run(day, "buy", "live")
    assert store.start_run(day, "sell", "paper")


def test_settings_defaults(store):
    assert not store.paused()
    assert store.buy_offset_min() == 2
    assert store.sell_delay_sec() == 5
    store.set_setting("paused", True)
    assert store.paused()


@pytest.mark.parametrize("raw, symbol", [(" spy ", "SPY"), ("brk.b", "BRK.B")])
def test_normalize_symbol(raw, symbol):
    assert normalize_symbol(raw) == symbol


@pytest.mark.parametrize("raw", ["", "TOOLONG", "SP Y", "<b>"])
def test_normalize_symbol_rejects_junk(raw):
    with pytest.raises(ValueError):
        normalize_symbol(raw)


def notifier_with(handler, **settings):
    cfg = Settings(_env_file=None, **settings)
    return Notifier(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_notifications_go_to_every_configured_channel():
    seen = []

    def handler(request):
        seen.append((request.url.host, request.read().decode()))
        return httpx.Response(200)

    notifier = notifier_with(
        handler,
        discord_webhook_url="https://discord.example/webhook",
        telegram_bot_token="tok",
        telegram_chat_id="42",
    )
    assert notifier.send("Bought at close", ["SPY 1 sh"]) == []
    assert [host for host, _ in seen] == ["discord.example", "api.telegram.org"]
    assert "[PAPER] Bought at close\\nSPY 1 sh" in seen[0][1]


def test_a_failing_channel_is_reported_not_raised(caplog):
    notifier = notifier_with(lambda request: httpx.Response(500), telegram_bot_token="secret-token", telegram_chat_id="1")
    assert notifier.send("hi") == ["telegram"]
    assert "secret-token" not in caplog.text


def test_no_channels_configured_sends_nothing():
    notifier = notifier_with(lambda request: pytest.fail("should not send"))
    assert notifier.channels == []
    assert notifier.send("hi") == []
