"""Exactly which sales an agreement's rate applies to.

An agreement's scope says:
  - brands:    "all", or only some brands, or all but some (NetSuite classes, by brand code);
  - categories: for particular brands only, which product categories count (the NetSuite item field
               "Item category", e.g. EUFY Security / Baby / Appliances): only some, or all but some.
               A brand with no category rule counts in full;
  - customers: every customer in the agreement's customer group, or only some of them, or all but
               some (NetSuite customer internal ids), e.g. JB Hi-Fi's airport stores;
  - journal_customer: the NetSuite customer the journal lines are tagged with, normally the
               retailer's head-office account that its rebate claims are credited to.

It is stored on the agreement as canonical JSON text, so a change to it is an ordinary agreement
change: proposed with a reason, approved by someone else, and kept in the change log.

    {"brands": {"mode": "all_but", "codes": ["MOVA", "WITHINGS"]},
     "categories": {"EUFY": {"mode": "all_but", "names": ["Appliances"]}},
     "customers": {"mode": "group"},
     "journal_customer": 12345}

"categories" is left out when there are no category rules, so scopes written before categories
existed read and compare exactly as before.
"""

import json
import re
from dataclasses import dataclass, field

NO_CLASS = "NO_CLASS"  # sales lines NetSuite carries without a class
BRAND_MODES = ("all", "only", "all_but")
CUSTOMER_MODES = ("group", "only", "all_but")
CATEGORY_MODES = ("only", "all_but")
CATEGORY_LINE = re.compile(r"^\s*([A-Za-z0-9_ :&.-]+?)\s*:\s*(only|except)\s+(.+?)\s*$", re.IGNORECASE)


class ScopeError(ValueError):
    """A scope that cannot be used, with a message fit to show on screen."""


@dataclass(frozen=True)
class CategoryRule:
    mode: str  # "only" or "all_but"
    names: frozenset[str]  # as written, e.g. "Security"; compared without regard to case

    def includes(self, category: str) -> bool:
        inside = (category or "").strip().lower() in {n.lower() for n in self.names}
        return inside if self.mode == "only" else not inside

    def describe(self) -> str:
        return ("only " if self.mode == "only" else "except ") + ", ".join(sorted(self.names))


@dataclass(frozen=True)
class Scope:
    brand_mode: str
    brand_codes: frozenset[str]
    customer_mode: str
    customer_ids: frozenset[int]
    journal_customer: int | None
    category_rules: tuple[tuple[str, CategoryRule], ...] = field(default=())  # (brand code, rule), sorted

    @property
    def categories(self) -> dict[str, CategoryRule]:
        return dict(self.category_rules)

    def includes_brand(self, code: str) -> bool:
        if self.brand_mode == "all":
            return True
        inside = code in self.brand_codes
        return inside if self.brand_mode == "only" else not inside

    def includes_category(self, code: str, category: str) -> bool:
        rule = self.categories.get(code)
        return rule is None or rule.includes(category)

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
        data = {"brands": brands, "customers": customers, "journal_customer": self.journal_customer}
        if self.category_rules:
            data["categories"] = {code: {"mode": rule.mode, "names": sorted(rule.names)} for code, rule in self.category_rules}
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    def categories_text(self) -> str:
        """One line per brand, as typed on the agreement page: 'EUFY: only Security, Baby'."""
        return "\n".join(f"{code}: {rule.describe()}" for code, rule in self.category_rules)

    def describe(self, customer_names: dict[int, str] | None = None) -> str:
        names = customer_names or {}
        brands = {"all": "all brands", "only": "only ", "all_but": "all brands except "}[self.brand_mode]
        if self.brand_mode != "all":
            brands += ", ".join(sorted(self.brand_codes))
        if self.category_rules:
            brands += " (" + "; ".join(f"{code} {rule.describe()}" for code, rule in self.category_rules) + ")"
        listed = ", ".join(names.get(i, str(i)) for i in sorted(self.customer_ids))
        customers = {"group": "every store in the group", "only": f"only {listed}",
                     "all_but": f"every store in the group except {listed}"}[self.customer_mode]
        tagged = names.get(self.journal_customer, str(self.journal_customer)) if self.journal_customer else "no customer"
        return f"{brands}; {customers}; journal tagged to {tagged}"


def describe_for_owner(scope: Scope, customer_names: dict[int, str]) -> str:
    """What an agreement covers, in words for a brand owner: brands, and stores only if not the whole retailer."""
    brands = {"all": "All brands", "only": "Only ", "all_but": "All brands except "}[scope.brand_mode]
    if scope.brand_mode != "all":
        brands += ", ".join(sorted(scope.brand_codes))
    if scope.category_rules:
        brands += " (" + "; ".join(f"{code} {rule.describe()}" for code, rule in scope.category_rules) + ")"
    stores = sorted(customer_names.get(i, "a store") for i in scope.customer_ids)
    if scope.customer_mode == "only":
        return f"{brands}; only at {', '.join(stores)}"
    if scope.customer_mode == "all_but":
        return f"{brands}; all stores except {', '.join(stores)}"
    return brands


def parse_category_lines(text: str) -> dict[str, tuple[str, list[str]]]:
    """'EUFY: only Security, Baby' / 'EUFY: except Appliances', one per line -> {code: (mode, names)}."""
    rules = {}
    for number, line in enumerate((text or "").splitlines(), start=1):
        if not line.strip():
            continue
        match = CATEGORY_LINE.match(line)
        if not match:
            raise ScopeError(f"Category line {number} should read like 'EUFY: only Security, Baby' or 'EUFY: except Appliances'.")
        code, word, names = match.groups()
        code = re.sub(r"[^A-Z0-9]+", "_", code.upper()).strip("_")
        if code in rules:
            raise ScopeError(f"{code} has more than one category line.")
        rules[code] = ("only" if word.lower() == "only" else "all_but", [n.strip() for n in names.split(",") if n.strip()])
    return rules


def make_scope(brand_mode: str, brand_codes, customer_mode: str = "group", customer_ids=(),
               journal_customer: int | None = None, categories: dict | None = None) -> Scope:
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
    rules = []
    for code, (mode, names) in sorted((categories or {}).items()):
        code = code.strip().upper()
        names = frozenset(n.strip() for n in names if n.strip())
        if mode not in CATEGORY_MODES or not names:
            raise ScopeError(f"The category rule for {code} needs 'only' or 'except' and at least one category.")
        if brand_mode == "only" and code not in codes or brand_mode == "all_but" and code in codes:
            raise ScopeError(f"There is a category rule for {code}, but the agreement does not cover {code}.")
        rules.append((code, CategoryRule(mode, names)))
    return Scope(brand_mode, codes, customer_mode, ids, int(journal_customer) if journal_customer else None, tuple(rules))


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
        categories = {code: (rule["mode"], rule["names"]) for code, rule in data.get("categories", {}).items()}
        return make_scope(brands["mode"], brands.get("codes", []), customers["mode"], customers.get("ids", []),
                          data.get("journal_customer"), categories)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ScopeError):
            raise
        raise ScopeError(f"The stored scope cannot be read: {exc}") from exc
