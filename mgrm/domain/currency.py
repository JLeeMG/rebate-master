"""Currencies and amount formatting.

Spec §1.2: never a bare dollar sign. Every rendered amount carries A$, NZ$ or
US$, and this module is the only place that turns a number into text.
"""

from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

MINUS = "−"  # a true minus sign, as in the spec's figures
WHOLE = Decimal("1")
CENTS = Decimal("0.01")


class Currency(StrEnum):
    AUD = "AUD"
    NZD = "NZD"
    USD = "USD"

    @property
    def symbol(self) -> str:
        return {Currency.AUD: "A$", Currency.NZD: "NZ$", Currency.USD: "US$"}[self]


def format_amount(value: Decimal | int | float, currency: Currency, *, cents: bool = False) -> str:
    """Format an amount, e.g. A$268,000 or −NZ$1,309.

    Whole units by default. A non-zero amount that would round to zero is
    shown to the cent instead, so a sub-unit value is never displayed as nil
    (spec §9.2: a number format must not hide sub-unit values).
    """
    amount = Decimal(str(value))
    places = CENTS if cents else WHOLE
    rounded = amount.quantize(places, rounding=ROUND_HALF_UP)
    if rounded == 0 and amount != 0:
        places = CENTS
        rounded = amount.quantize(places, rounding=ROUND_HALF_UP)
    sign = MINUS if rounded < 0 else ""
    body = f"{abs(rounded):,.2f}" if places == CENTS else f"{abs(rounded):,.0f}"
    return f"{sign}{currency.symbol}{body}"
