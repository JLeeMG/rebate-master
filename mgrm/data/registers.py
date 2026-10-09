"""Loaders for the NetSuite customer and class registers (docs/netsuite-saved-searches.md, searches 4 and 5).

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
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from mgrm.data.errors import AlreadyLoaded, LoadRejected
from mgrm.data.files import file_sha256
from mgrm.domain.entities import Entity
from mgrm.models import AppUser, Brand, Customer, LoadBatch, LoadKind

# The governed searches keep their original ids: they were specified before the split.
SEARCH_IDS: dict[LoadKind, str] = {
    LoadKind.CUSTOMERS: "customsearch_mgfp_customers",
    LoadKind.CLASSES: "customsearch_mgfp_classes",
}

ENTITY_BY_SUBSIDIARY = {"macgear au": Entity.MGAU, "macgear nz": Entity.MGNZ}
TRUE_TEXT = {"yes", "true", "t", "y", "1"}
FALSE_TEXT = {"no", "false", "f", "n", "0", ""}
MAX_PROBLEMS_PER_KIND = 25  # beyond this, one summary line; the file is rejected either way


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
}


def _normalise(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", header.strip().lower())


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
    for number, values in enumerate(reader, start=2):
        if not any(v.strip() for v in values):
            continue
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


LOADERS: dict[LoadKind, Callable[[Session, FileInput, AppUser | None], LoadBatch]] = {
    LoadKind.CLASSES: load_classes,
    LoadKind.CUSTOMERS: load_customers,
}
