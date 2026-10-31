"""NYSE trading calendar: which days the market is open and when."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(ET)


class MarketCalendar:
    def __init__(self) -> None:
        self._cal = xcals.get_calendar("XNYS")

    def is_session(self, day: date) -> bool:
        return bool(self._cal.is_session(day.isoformat()))

    def open_at(self, session: date) -> datetime:
        return self._cal.session_open(session.isoformat()).to_pydatetime().astimezone(ET)

    def close_at(self, session: date) -> datetime:
        """Regular close, which is 1 PM ET on early-close days."""
        return self._cal.session_close(session.isoformat()).to_pydatetime().astimezone(ET)

    def next_session(self, day: date) -> date:
        """The first session strictly after `day`."""
        if self.is_session(day):
            return self._cal.next_session(day.isoformat()).date()
        return self._cal.date_to_session(day.isoformat(), direction="next").date()

    def previous_session(self, day: date) -> date:
        """The last session strictly before `day`."""
        if self.is_session(day):
            return self._cal.previous_session(day.isoformat()).date()
        return self._cal.date_to_session(day.isoformat(), direction="previous").date()

    def is_open(self, at: datetime) -> bool:
        day = at.astimezone(ET).date()
        return self.is_session(day) and self.open_at(day) <= at < self.close_at(day)
