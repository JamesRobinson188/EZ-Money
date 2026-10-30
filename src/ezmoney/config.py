"""Settings that come from the environment (or a .env file).

Anything that should only be changed by whoever runs the server lives here:
credentials, the paper/live switch and the hard dollar caps. Things the user
tweaks day to day (watchlist, timing, pause) live in the database instead.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    trading_mode: Literal["paper", "live"] = "paper"
    data_dir: Path = Path("data")

    # Robinhood (live mode only)
    rh_username: str = ""
    rh_password: str = ""
    rh_totp_secret: str = ""

    # Hard caps. The dashboard cannot raise these.
    max_dollars_per_ticker: float = 1000.0
    max_dollars_per_night: float = 5000.0

    # Paper mode
    paper_starting_cash: float = 10_000.0
    quote_source: Literal["yahoo", "demo"] = "yahoo"

    # Notifications (any that are set are used)
    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    dashboard_password: str = ""

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

    def clamp_dollars(self, dollars: float) -> float:
        """Keep a per-ticker amount between Robinhood's $1 fractional minimum and the cap."""
        return round(min(max(dollars, 1.0), self.max_dollars_per_ticker), 2)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ezmoney.db"

    @property
    def session_dir(self) -> Path:
        return self.data_dir / "robinhood-session"


@lru_cache
def get_settings() -> Settings:
    return Settings()
