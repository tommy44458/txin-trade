"""Where the US stock market's day stands, for Binance perpetuals that track US stocks.

The perpetual trades around the clock, but the stock trades only in US sessions. Outside
regular hours volume is thin and the price follows a model of the stock, and the next
regular open can move the perpetual to a new level at once. The calendar is NYSE's
published holidays and early closes for the years listed; other years report no session.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .timeframes import FIXED_SECONDS

VERSION = "us_equity_session_v1"
EASTERN = ZoneInfo("America/New_York")
CALENDAR_YEARS = (2026, 2027)
HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
    date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
}
EARLY_CLOSES = {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}
PRE_MARKET = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
AFTER_HOURS_END = time(20, 0)
RECENT_CANDLES = 24
INTERPRETATION_LIMIT = (
    "The perpetual trades around the clock; the stock trades only in US sessions. Outside regular "
    "hours volume is thin and price follows a model of the stock, so the next regular open can gap "
    "the perpetual. Holidays and early closes follow NYSE's published calendar for calendar_years.")


def _trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in HOLIDAYS


def _regular_hours(day: date) -> tuple[datetime, datetime] | None:
    if not _trading_day(day):
        return None
    close = EARLY_CLOSE if day in EARLY_CLOSES else REGULAR_CLOSE
    return (datetime.combine(day, REGULAR_OPEN, EASTERN), datetime.combine(day, close, EASTERN))


def _covered(day: date) -> bool:
    return day.year in CALENDAR_YEARS


def regular_minutes(start: datetime, end: datetime) -> float:
    """Minutes of [start, end) that fall inside regular sessions."""
    total = 0.0
    day = start.astimezone(EASTERN).date()
    while datetime.combine(day, time(0), EASTERN) < end:
        hours = _regular_hours(day)
        if hours:
            overlap = min(end, hours[1]) - max(start, hours[0])
            total += max(0.0, overlap.total_seconds() / 60)
        day += timedelta(days=1)
    return total


def session_context(at: datetime, candles: list[dict] | None = None,
                    timeframe: str | None = None) -> dict:
    """The session at a cutoff, the next open and last close, and how much of the recent candles traded in it."""
    local = at.astimezone(EASTERN)
    day = local.date()
    if not (_covered(day) and _covered(day + timedelta(days=10)) and _covered(day - timedelta(days=10))):
        return {"version": VERSION, "status": "calendar_unavailable",
                "calendar_years": list(CALENDAR_YEARS), "interpretation_limit": INTERPRETATION_LIMIT}
    hours = _regular_hours(day)
    if hours is None:
        state = "weekend" if day.weekday() >= 5 else "holiday"
    elif hours[0] <= local < hours[1]:
        state = "regular"
    elif datetime.combine(day, PRE_MARKET, EASTERN) <= local < hours[0]:
        state = "pre_market"
    elif hours[1] <= local < datetime.combine(day, AFTER_HOURS_END, EASTERN):
        state = "after_hours"
    else:
        state = "overnight"
    next_open = last_close = None
    for offset in range(11):
        later = _regular_hours(day + timedelta(days=offset))
        if next_open is None and later and later[0] > local:
            next_open = later[0]
        earlier = _regular_hours(day - timedelta(days=offset))
        if last_close is None and earlier and earlier[1] <= local:
            last_close = earlier[1]
    context = {
        "version": VERSION, "status": "available", "exchange": "NYSE/Nasdaq",
        "timezone": "America/New_York", "state": state,
        "early_close_today": state != "weekend" and day in EARLY_CLOSES,
        "next_regular_open": next_open.astimezone(UTC).isoformat() if next_open else None,
        "hours_to_next_open": round((next_open - local).total_seconds() / 3600, 2) if next_open else None,
        "last_regular_close": last_close.astimezone(UTC).isoformat() if last_close else None,
        "calendar_years": list(CALENDAR_YEARS), "interpretation_limit": INTERPRETATION_LIMIT,
    }
    if candles and timeframe in FIXED_SECONDS:
        recent = candles[-RECENT_CANDLES:]
        span = sum(FIXED_SECONDS[timeframe] / 60 for _ in recent)
        inside = sum(regular_minutes(datetime.fromisoformat(row["open_time"]),
                                     datetime.fromisoformat(row["open_time"]) +
                                     timedelta(seconds=FIXED_SECONDS[timeframe])) for row in recent)
        context["recent_candles"] = {"timeframe": timeframe, "count": len(recent),
                                     "regular_session_share_pct": round(inside / span * 100, 1) if span else None}
    return context
