"""The trading entities (spec §1.2). Fixed by the business, not configuration."""

from enum import StrEnum

from mgrm.domain.currency import Currency


class Entity(StrEnum):
    MGAU = "MGAU"
    MGNZ = "MGNZ"
    GROUP = "GROUP"

    @property
    def is_trading(self) -> bool:
        return self is not Entity.GROUP


TRADING_ENTITIES: tuple[Entity, ...] = (Entity.MGAU, Entity.MGNZ)

FUNCTIONAL_CURRENCY: dict[Entity, Currency] = {
    Entity.MGAU: Currency.AUD,
    Entity.MGNZ: Currency.NZD,
}

# Group presents in NZ$ by default; A$ is selectable.
GROUP_PRESENTATION_CURRENCIES: tuple[Currency, ...] = (Currency.NZD, Currency.AUD)

NETSUITE_SUBSIDIARY: dict[Entity, int] = {Entity.MGAU: 2, Entity.MGNZ: 3}
