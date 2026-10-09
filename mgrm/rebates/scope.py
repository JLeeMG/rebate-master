"""Exactly which sales an agreement's rate applies to.

An agreement's scope says:
  - brands:    "all", or only some brands, or all but some (NetSuite classes, by brand code);
  - customers: every customer in the agreement's customer group, or only some of them, or all but
               some (NetSuite customer internal ids), e.g. JB Hi-Fi's airport stores;
  - journal_customer: the NetSuite customer the journal lines are tagged with, normally the
               retailer's head-office account that its rebate claims are credited to.

It is stored on the agreement as canonical JSON text, so a change to it is an ordinary agreement
change: proposed with a reason, approved by someone else, and kept in the change log.

    {"brands": {"mode": "all_but", "codes": ["MOVA", "WITHINGS"]},
     "customers": {"mode": "group"},
     "journal_customer": 12345}
"""

import json
from dataclasses import dataclass

NO_CLASS = "NO_CLASS"  # sales lines NetSuite carries without a class
BRAND_MODES = ("all", "only", "all_but")
CUSTOMER_MODES = ("group", "only", "all_but")


class ScopeError(ValueError):
    """A scope that cannot be used, with a message fit to show on screen."""


@dataclass(frozen=True)
class Scope:
    brand_mode: str
    brand_codes: frozenset[str]
    customer_mode: str
    customer_ids: frozenset[int]
    journal_customer: int | None

    def includes_brand(self, code: str) -> bool:
        if self.brand_mode == "all":
            return True
        inside = code in self.brand_codes
        return inside if self.brand_mode == "only" else not inside

    def includes_customer(self, customer_id: int | None, in_group: bool) -> bool:
        if self.customer_mode == "only":
            return customer_id in self.customer_ids
        if not in_group:
            return False
        return self.customer_mode == "group" or customer_id not in self.customer_ids

    def to_text(self) -> str:
        brands = {"mode": self.brand_mode}
        if self.brand_mode != "all":
            brands["codes"] = sorted(self.brand_codes)
        customers = {"mode": self.customer_mode}
        if self.customer_mode != "group":
            customers["ids"] = sorted(self.customer_ids)
        return json.dumps({"brands": brands, "customers": customers, "journal_customer": self.journal_customer},
                          sort_keys=True, separators=(",", ":"))

    def describe(self, customer_names: dict[int, str] | None = None) -> str:
        names = customer_names or {}
        brands = {"all": "all brands", "only": "only ", "all_but": "all brands except "}[self.brand_mode]
        if self.brand_mode != "all":
            brands += ", ".join(sorted(self.brand_codes))
        listed = ", ".join(names.get(i, str(i)) for i in sorted(self.customer_ids))
        customers = {"group": "every store in the group", "only": f"only {listed}",
                     "all_but": f"every store in the group except {listed}"}[self.customer_mode]
        tagged = names.get(self.journal_customer, str(self.journal_customer)) if self.journal_customer else "no customer"
        return f"{brands}; {customers}; journal tagged to {tagged}"


def make_scope(brand_mode: str, brand_codes, customer_mode: str = "group", customer_ids=(),
               journal_customer: int | None = None) -> Scope:
    codes = frozenset(c.strip().upper() for c in brand_codes if str(c).strip())
    ids = frozenset(int(i) for i in customer_ids)
    if brand_mode not in BRAND_MODES:
        raise ScopeError(f"Brands must be one of: {', '.join(BRAND_MODES)}.")
    if customer_mode not in CUSTOMER_MODES:
        raise ScopeError(f"Customers must be one of: {', '.join(CUSTOMER_MODES)}.")
    if brand_mode == "only" and not codes:
        raise ScopeError("Name at least one brand, or choose all brands.")
    if brand_mode == "all" and codes:
        raise ScopeError("All brands takes no list of brands.")
    if customer_mode == "only" and not ids:
        raise ScopeError("Name at least one customer, or choose every store in the group.")
    if customer_mode == "group" and ids:
        raise ScopeError("Every store in the group takes no list of customers.")
    return Scope(brand_mode, codes, customer_mode, ids, int(journal_customer) if journal_customer else None)


def describe_text(text: str | None, customer_names: dict[int, str] | None = None) -> str:
    """A stored scope in words, for screens and the change log."""
    try:
        scope = parse_scope(text)
    except ScopeError:
        return "unreadable"
    return scope.describe(customer_names) if scope else "not yet defined"


def parse_scope_safe(text: str | None) -> Scope | None:
    try:
        return parse_scope(text)
    except ScopeError:
        return None


def parse_scope(text: str | None) -> Scope | None:
    if not text:
        return None
    try:
        data = json.loads(text)
        brands, customers = data["brands"], data["customers"]
        return make_scope(brands["mode"], brands.get("codes", []), customers["mode"], customers.get("ids", []),
                          data.get("journal_customer"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ScopeError(f"The stored scope cannot be read: {exc}") from exc
