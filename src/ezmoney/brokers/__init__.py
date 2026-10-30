from ..config import Settings
from ..db import Store
from ..quotes import price_source
from .base import Broker, BrokerError, OrderResult, wait_for_fill
from .paper import PaperBroker

__all__ = ["Broker", "BrokerError", "OrderResult", "PaperBroker", "make_broker", "wait_for_fill"]


def make_broker(settings: Settings, store: Store) -> Broker:
    if settings.is_live:
        from .robinhood import RobinhoodBroker

        return RobinhoodBroker(settings)
    return PaperBroker(store, price_source(settings.quote_source), settings.paper_starting_cash)
