"""Loaders for the NetSuite exports: the customer and class registers, and the trading detail
(docs/netsuite-saved-searches.md in the forecasting platform, searches 3, 4 and 5).

The input is the saved search's own CSV export (Results -> Export -> CSV).
Each loader checks the columns are exactly the ones the search is defined
with, checks every row, and rejects the whole file - listing every problem -
if anything is wrong. Otherwise it updates the register in one transaction.

Loads never undo decisions made here: customer groups already assigned, and
whether a class is a brand, are kept. New customers arrive without a group
and new classes arrive as "not a brand" (spec §4.2, §4.3).
"""

import csv
import io
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from mgrm.data.errors import AlreadyLoaded, LoadRejected
from mgrm.data.files import file_sha256
from mgrm.domain.entities import Entity
from mgrm.models import AppUser, Brand, Customer, LoadBatch, LoadKind, SalesLine

# The governed searches keep their original ids: they were specified before the split.
SEARCH_IDS: dict[LoadKind, str] = {
    LoadKind.CUSTOMERS: "customsearch_mgfp_customers",
    LoadKind.CLASSES: "customsearch_mgfp_classes",
    LoadKind.TRADING_DETAIL: "customsearch_mgfp_trading_detail",
}
SALES_ACCOUNT = "40010"  # the rebate base; the search also returns rebates, cost of sales and freight
TRADING_ACCOUNTS = {"40010", "42010", "42020", "43010", "50010", "51030", "51040", "51080", "55010", "55050"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
AMOUNT_NOISE = re.compile(r"[()\s,]|[A-Za-z]{0,3}\$")  # brackets, spaces, thousands commas, A$ / NZ$ / US$

ENTITY_BY_SUBSIDIARY = {"macgear au": Entity.MGAU, "macgear nz": Entity.MGNZ}
TRUE_TEXT = {"yes", "true", "t", "y", "1"}
FALSE_TEXT = {"no", "false", "f", "n", "0", ""}
MAX_PROBLEMS_PER_KIND = 25  # beyond this, one summary line; the file is rejected either way
NETSUITE_TOTAL_ROW = "Overall Total"  # the last row of a summary search's CSV export


@dataclass(frozen=True)
class Column:
    key: str
    aliases: tuple[str, ...]  # header text NetSuite may use for this field
    required: bool = True


COLUMNS: dict[LoadKind, tuple[Column, ...]] = {
    LoadKind.CLASSES: (
        Column("internal_id", ("Internal ID",)),
        Column("name", ("Name",)),
        Column("parent", ("Parent",), False),
        Column("subsidiary", ("Subsidiary",), False),
        Column("inactive", ("Inactive",)),
    ),
    LoadKind.CUSTOMERS: (
        Column("internal_id", ("Internal ID",)),
        Column("entity_id", ("ID", "Customer ID")),
        Column("name", ("Name", "Company Name")),
        Column("subsidiary", ("Subsidiary", "Primary Subsidiary")),
        Column("category", ("Category",)),
        Column("parent", ("Parent", "Top Level Parent")),
        Column("terms", ("Terms",)),
        Column("inactive", ("Inactive",)),
    ),
    LoadKind.TRADING_DETAIL: (
        Column("subsidiary", ("Subsidiary", "Subsidiary (no hierarchy)")),
        Column("period", ("Start Date", "Accounting Period : Start Date", "Accounting Period Fields : Start Date", "Period")),
        Column("account", ("Number", "Account : Number", "Account Fields : Number", "Account Number")),
        Column("class", ("Class",)),
        Column("class_id", ("Class Fields : Internal ID", "Class : Internal ID", "Class Internal ID")),
        Column("name", ("Name",)),
        Column("customer_id", ("Customer Fields : Internal ID", "Customer : Internal ID", "Customer Internal ID")),
        Column("debit", ("Debit Amount", "Amount (Debit)", "Debit")),
        Column("credit", ("Credit Amount", "Amount (Credit)", "Credit")),
    ),
}


def _normalise(header: str) -> str:
    text = re.sub(r"^(sum of|group|count of|maximum of|minimum of)\s+", "", header.strip().lower())
    return re.sub(r"[^a-z0-9]+", "", text)


class Problems:
    """Collects every problem in a file, capped per kind so a systematic fault stays readable."""

    def __init__(self) -> None:
        self.items: list[str] = []
        self._counts: dict[str, int] = {}

    def add(self, kind: str, message: str) -> None:
        self._counts[kind] = self._counts.get(kind, 0) + 1
        if self._counts[kind] <= MAX_PROBLEMS_PER_KIND:
            self.items.append(message)

    def final(self) -> list[str]:
        extra = [
            f"... and {count - MAX_PROBLEMS_PER_KIND} more of the same kind ({kind})"
            for kind, count in self._counts.items()
            if count > MAX_PROBLEMS_PER_KIND
        ]
        return self.items + extra

    def __bool__(self) -> bool:
        return bool(self.items)


def read_csv(text: str, kind: LoadKind, problems: Problems) -> list[dict[str, str]]:
    """Rows keyed by column key, or [] with problems recorded."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        problems.add("empty", "The file is empty.")
        return []
    columns = COLUMNS[kind]
    by_alias = {_normalise(alias): column for column in columns for alias in column.aliases}
    positions: dict[str, int] = {}
    for index, name in enumerate(header):
        column = by_alias.get(_normalise(name))
        if column is None:
            problems.add("columns", f"Unexpected column '{name}'. The saved search has changed, or this is not a {kind.value} export.")
        elif column.key in positions:
            problems.add("columns", f"Column '{name}' appears twice.")
        else:
            positions[column.key] = index
    for column in columns:
        if column.required and column.key not in positions:
            problems.add("columns", f"Missing column: {column.aliases[0]}.")
    if problems:
        return []
    rows = []
    lines = [(number, values) for number, values in enumerate(reader, start=2) if any(v.strip() for v in values)]
    if lines and lines[-1][1][0].strip() == NETSUITE_TOTAL_ROW:
        lines.pop()  # a summary search's export ends with NetSuite's grand total; it is not a line of data
    for number, values in lines:
        values = values + [""] * (len(header) - len(values))
        row = {key: values[index].strip() for key, index in positions.items()}
        row["_line"] = str(number)
        rows.append(row)
    if not rows:
        problems.add("empty", "The file has a header but no rows.")
    return rows


def parse_subsidiary(text: str) -> Entity:
    leaf = text.split(":")[-1].strip().lower()
    if leaf not in ENTITY_BY_SUBSIDIARY:
        raise ValueError(f"subsidiary '{text}' is not MacGear AU or MacGear NZ; the search's subsidiary filter is wrong")
    return ENTITY_BY_SUBSIDIARY[leaf]


def parse_flag(text: str) -> bool:
    value = text.strip().lower()
    if value in TRUE_TEXT:
        return True
    if value in FALSE_TEXT:
        return False
    raise ValueError(f"'{text}' is not yes or no")


def parse_id(text: str) -> int:
    return int(Decimal(text.strip().replace(",", "")))


def parse_optional_id(text: str) -> int | None:
    text = text.strip()
    if not text or text in {"- None -", "-"}:
        return None
    return parse_id(text)


def parse_amount(text: str) -> Decimal:
    """'1,234.56', '(1,234.56)', '-1234.56', 'A$1,234.56', '' (blank = nil in a summary search)."""
    cleaned = text.strip()
    if not cleaned:
        return Decimal(0)
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    value = Decimal(AMOUNT_NOISE.sub("", cleaned))
    return -value if negative else value


def parse_period(text: str) -> date:
    """'1/04/2026' (day first, as NetSuite is set), '2026-04-01', or 'Apr 2026'. Must be the 1st."""
    text = text.strip()
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if match:
        day, month, year = map(int, match.groups())
        value = date(year, month, day)
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        value = date.fromisoformat(text)
    else:
        match = re.fullmatch(r"([A-Za-z]{3})[a-z]* (\d{4})", text)
        if not match or match.group(1).lower() not in MONTHS:
            raise ValueError(f"'{text}' is not a date")
        value = date(int(match.group(2)), MONTHS[match.group(1).lower()], 1)
    if value.day != 1:
        raise ValueError(f"'{text}' is not the first day of a month")
    return value


def _field(row: dict, key: str, parser: Callable, problems: Problems, kind: str):
    try:
        return parser(row[key])
    except InvalidOperation:
        problems.add(kind, f"Line {row['_line']}, {key.replace('_', ' ')}: '{row[key]}' is not a number")
    except (ValueError, ArithmeticError) as exc:
        problems.add(kind, f"Line {row['_line']}, {key.replace('_', ' ')}: {exc}")
    return None


@dataclass(frozen=True)
class FileInput:
    name: str
    text: str
    sha256: str

    @classmethod
    def from_path(cls, path: Path) -> "FileInput":
        path = Path(path)
        return cls(path.name, path.read_text(encoding="utf-8-sig"), file_sha256(path))


def _new_batch(session: Session, kind: LoadKind, file: FileInput, rows: int, actor: AppUser | None) -> LoadBatch:
    if session.scalar(select(LoadBatch).where(LoadBatch.kind == kind, LoadBatch.file_sha256 == file.sha256)):
        raise AlreadyLoaded(f"{file.name} has already been loaded")
    batch = LoadBatch(kind=kind, entity_code=None, source=SEARCH_IDS[kind], file_name=file.name,
                      file_sha256=file.sha256, row_count=rows, loaded_by_id=actor.id if actor else None, summary={})
    session.add(batch)
    session.flush()
    return batch


def brand_code(class_name: str) -> str:
    """The brand code for a NetSuite class: its full path, upper case.

    'MOVA' -> 'MOVA'; 'SALOMON : THOMAS KENT' -> 'SALOMON_THOMAS_KENT'. The full
    path, not the leaf, because NetSuite has two classes named THOMAS KENT.
    """
    return re.sub(r"[^A-Z0-9]+", "_", class_name.upper()).strip("_")


def load_classes(session: Session, file: FileInput, actor: AppUser | None) -> LoadBatch:
    problems = Problems()
    rows = read_csv(file.text, LoadKind.CLASSES, problems)
    parsed = []
    for row in rows:
        internal_id = _field(row, "internal_id", parse_id, problems, "internal id")
        inactive = _field(row, "inactive", parse_flag, problems, "flag")
        if internal_id is None or inactive is None:
            continue
        parsed.append((internal_id, row["name"], inactive))
    codes = [brand_code(name) for _, name, _ in parsed]
    for code in {c for c in codes if codes.count(c) > 1}:
        problems.add("duplicate", f"Two classes would share the brand code {code}; rename one in NetSuite")
    if problems:
        raise LoadRejected(file.name, problems.final())
    batch = _new_batch(session, LoadKind.CLASSES, file, len(parsed), actor)
    added = 0
    for internal_id, name, inactive in parsed:
        brand = session.scalar(select(Brand).where(Brand.netsuite_class_id == internal_id))
        if brand is None:
            brand = Brand(code=brand_code(name), netsuite_class_id=internal_id, is_brand=False)
            added += 1
        brand.name, brand.is_inactive = name.strip(), inactive
        session.add(brand)
    batch.summary = {"new_classes": added}
    session.flush()
    return batch


def load_customers(session: Session, file: FileInput, actor: AppUser | None) -> LoadBatch:
    problems = Problems()
    rows = read_csv(file.text, LoadKind.CUSTOMERS, problems)
    parsed = []
    for row in rows:
        internal_id = _field(row, "internal_id", parse_id, problems, "internal id")
        entity = _field(row, "subsidiary", parse_subsidiary, problems, "subsidiary")
        inactive = _field(row, "inactive", parse_flag, problems, "flag")
        if None in (internal_id, entity, inactive):
            continue
        parsed.append((internal_id, entity, inactive, row))
    ids = [p[0] for p in parsed]
    for internal_id in {i for i in ids if ids.count(i) > 1}:
        problems.add("duplicate", f"Customer internal ID {internal_id} appears more than once")
    if problems:
        raise LoadRejected(file.name, problems.final())
    batch = _new_batch(session, LoadKind.CUSTOMERS, file, len(parsed), actor)
    added = 0
    for internal_id, entity, inactive, row in parsed:
        customer = session.scalar(select(Customer).where(Customer.netsuite_customer_id == internal_id))
        if customer is None:
            customer = Customer(netsuite_customer_id=internal_id)
            added += 1
        customer.name, customer.entity_code, customer.is_inactive = row["name"], entity.value, inactive
        customer.netsuite_entity_id, customer.category = row["entity_id"], row["category"]
        customer.parent_name, customer.terms = row["parent"], row["terms"]
        session.add(customer)
    batch.summary = {"new_customers": added}
    session.flush()
    return batch


def load_trading_detail(session: Session, file: FileInput, actor: AppUser | None, today: date | None = None) -> LoadBatch:
    """Net sales by entity, month, class and customer. Replaces whatever was held for the months in the file.

    Anything already posted to a month after the current one is set aside and noted, never kept as sales.
    """
    problems = Problems()
    rows = read_csv(file.text, LoadKind.TRADING_DETAIL, problems)
    lines = []
    this_month = (today or date.today()).replace(day=1)
    ahead: dict[str, Decimal] = {}
    for row in rows:
        entity = _field(row, "subsidiary", parse_subsidiary, problems, "subsidiary")
        period = _field(row, "period", parse_period, problems, "period")
        debit = _field(row, "debit", parse_amount, problems, "amount")
        credit = _field(row, "credit", parse_amount, problems, "amount")
        class_id = _field(row, "class_id", parse_optional_id, problems, "internal id")
        customer_id = _field(row, "customer_id", parse_optional_id, problems, "internal id")
        account = row["account"].strip()
        if account not in TRADING_ACCOUNTS:
            problems.add("account", f"Line {row['_line']}: account '{account}' is not one this search should return")
        if None in (entity, period, debit, credit) or account != SALES_ACCOUNT:
            continue  # only sales are the rebate base; the other accounts are checked, not kept
        if period > this_month:
            key = f"{entity.value} {period:%b %Y}"
            ahead[key] = ahead.get(key, Decimal(0)) + credit - debit
            continue
        lines.append(SalesLine(entity_code=entity.value, period=period, account_code=account, netsuite_class_id=class_id,
                               class_name=row["class"][:200], netsuite_customer_id=customer_id,
                               customer_name=row["name"][:300], amount=credit - debit))
    if problems:
        raise LoadRejected(file.name, problems.final())
    if not lines:
        raise LoadRejected(file.name, [f"The file has no sales (account {SALES_ACCOUNT}) lines."])
    batch = _new_batch(session, LoadKind.TRADING_DETAIL, file, len(lines), actor)
    covered = sorted({(line.entity_code, line.period) for line in lines})
    for entity_code, period in covered:
        session.execute(delete(SalesLine).where(SalesLine.entity_code == entity_code, SalesLine.period == period))
    for line in lines:
        line.batch_id = batch.id
    session.add_all(lines)
    batch.summary = {"months": [f"{e} {p:%Y-%m}" for e, p in covered], "sales_lines": len(lines),
                     "notes": [f"{k}: sales of {v:,.2f} posted ahead, not loaded" for k, v in sorted(ahead.items())]}
    session.flush()
    return batch


LOADERS: dict[LoadKind, Callable[[Session, FileInput, AppUser | None], LoadBatch]] = {
    LoadKind.CLASSES: load_classes,
    LoadKind.CUSTOMERS: load_customers,
    LoadKind.TRADING_DETAIL: load_trading_detail,
}
