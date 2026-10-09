"""Periods. A period is the first day of a month; the financial year runs April-March."""

from datetime import date

FY_START_MONTH = 4


def month_start(value: date) -> date:
    return value.replace(day=1)


def require_period(value: date) -> date:
    """Reject anything that is not the first day of a month."""
    if value.day != 1:
        raise ValueError(f"A period is the first day of a month; got {value.isoformat()}")
    return value


def add_months(period: date, months: int) -> date:
    require_period(period)
    index = period.year * 12 + (period.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def fiscal_year(period: date) -> int:
    """The calendar year in which the financial year ends. April 2026 is FY27."""
    return period.year + 1 if period.month >= FY_START_MONTH else period.year


def fiscal_year_label(period: date) -> str:
    return f"FY{fiscal_year(period) % 100:02d}"


def months_between(start: date, end: date) -> list[date]:
    """Every period from start to end inclusive."""
    require_period(start)
    require_period(end)
    if end < start:
        raise ValueError(f"Period range ends before it starts: {start.isoformat()} to {end.isoformat()}")
    periods = [start]
    while periods[-1] < end:
        periods.append(add_months(periods[-1], 1))
    return periods
