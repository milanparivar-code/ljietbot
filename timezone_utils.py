"""
Indian Standard Time (IST, UTC+05:30) Utility Module.
Ensures that all date, time, deadline calculations, and timestamps in the bot
strictly adhere to Indian Standard Time, regardless of host server timezone (e.g. UTC, US, Europe).
"""
from datetime import datetime, timezone, timedelta, date, time

# Indian Standard Time offset is permanently UTC+05:30 (no daylight saving time)
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def get_ist_now() -> datetime:
    """
    Returns the current datetime converted to Indian Standard Time (IST, UTC+05:30)
    as a timezone-naive datetime object.

    Returning naive datetime ensures 100% seamless compatibility with
    datetime.strptime, timedelta arithmetic, and database/store comparisons
    without triggering Python's 'can't compare offset-naive and offset-aware' TypeError.
    """
    return datetime.now(IST).replace(tzinfo=None)


def get_ist_today() -> date:
    """
    Returns today's date in Indian Standard Time (IST).
    """
    return get_ist_now().date()


def get_ist_today_str(fmt: str = "%d/%m/%Y") -> str:
    """
    Returns today's date string in Indian Standard Time (IST) in the requested format (default: 'DD/MM/YYYY').
    """
    return get_ist_now().strftime(fmt)


def get_ist_time() -> time:
    """
    Returns the current time in Indian Standard Time (IST).
    """
    return get_ist_now().time()
