"""Load who reviews each brand's rebate agreements: the Group Product Manager (GPM) and Product Manager (PM).

The input is the "Brand owners" workbook: one row per brand code, with GPM name and email, PM name and
email, and notes. Reference only: it gives nobody access to the platform. The whole file is checked
first and rejected, listing every problem, if anything is wrong. Each change is in the audit log.
Workbooks are opened read-only and never saved.
"""

import re
from pathlib import Path

import openpyxl
from sqlalchemy import select
from sqlalchemy.orm import Session

from mgrm.auth.users import audit
from mgrm.data.errors import AlreadyLoaded, LoadRejected
from mgrm.data.files import file_sha256, workbook_problem
from mgrm.models import AppUser, Brand, LoadBatch, LoadKind

SHEET = "Brand owners"
COLUMNS = {"Brand code": "code", "GPM name": "gpm_name", "GPM email": "gpm_email", "PM name": "pm_name",
           "PM email": "pm_email", "Notes": "owner_notes"}
OPTIONAL_COLUMNS = {"GPM title": "gpm_title"}  # the owner's actual title; kept as it is if the column is absent
EMAIL = re.compile(r"^[^@\s]+@macgeargroup\.com$", re.IGNORECASE)
OWNER_FIELDS = ("gpm_name", "gpm_email", "gpm_title", "pm_name", "pm_email", "owner_notes")


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def read_owners(path: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    """{brand code: owner fields}, and every problem found."""
    problem = workbook_problem(path)
    if problem:
        return {}, [problem]
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if SHEET not in workbook.sheetnames:
            return {}, [f"there is no '{SHEET}' sheet"]
        rows = list(workbook[SHEET].iter_rows(values_only=True))
    finally:
        workbook.close()
    if not rows:
        return {}, ["the sheet is empty"]
    header = [_text(h) for h in rows[0]]
    missing = [name for name in COLUMNS if name not in header]
    if missing:
        return {}, [f"missing column: {name}" for name in missing]
    at = {COLUMNS[name]: header.index(name) for name in COLUMNS}
    at |= {field: header.index(name) for name, field in OPTIONAL_COLUMNS.items() if name in header}
    owners, problems = {}, []
    for number, row in enumerate(rows[1:], start=2):
        values = {field: _text(row[i]) if i < len(row) else "" for field, i in at.items()}
        code = values.pop("code").upper()
        if not code:
            continue
        if code in owners:
            problems.append(f"row {number}: brand {code} appears twice")
        for name_field, email_field, who in (("gpm_name", "gpm_email", "GPM"), ("pm_name", "pm_email", "PM")):
            if bool(values[name_field]) != bool(values[email_field]):
                problems.append(f"row {number} ({code}): {who} name and email go together")
            if values[email_field] and not EMAIL.match(values[email_field]):
                problems.append(f"row {number} ({code}): '{values[email_field]}' is not a MacGear email address")
            values[email_field] = values[email_field].lower()
        if (values["pm_name"]) and not values["gpm_name"]:
            problems.append(f"row {number} ({code}): a PM without a GPM; the GPM receives the review pack")
        owners[code] = values
    return owners, problems


def load_brand_owners(session: Session, path: Path, actor: AppUser | None) -> LoadBatch:
    path = Path(path)
    owners, problems = read_owners(path)
    known = {b.code: b for b in session.scalars(select(Brand))}
    problems += [f"brand code {code} is not in the brand register" for code in owners if code not in known]
    if problems:
        raise LoadRejected(path.name, problems)
    sha = file_sha256(path)
    if session.scalar(select(LoadBatch).where(LoadBatch.kind == LoadKind.BRAND_OWNERS, LoadBatch.file_sha256 == sha)):
        raise AlreadyLoaded(f"{path.name} has already been loaded")
    batch = LoadBatch(kind=LoadKind.BRAND_OWNERS, source="Brand owners workbook", file_name=path.name, file_sha256=sha,
                      row_count=len(owners), loaded_by_id=actor.id if actor else None, summary={})
    session.add(batch)
    session.flush()
    changed = 0
    for code, values in owners.items():
        brand = known[code]
        values = {"gpm_title": brand.gpm_title if values["gpm_email"] == brand.gpm_email else "", **values}
        old = {f: getattr(brand, f) for f in OWNER_FIELDS}
        if old != values:
            audit(session, actor, "brand.owner", code, old={k: v for k, v in old.items() if k != "owner_notes"},
                  new={k: v for k, v in values.items() if k != "owner_notes"}, batch=batch.id)
            for field, value in values.items():
                setattr(brand, field, value)
            changed += 1
    owned = sorted({v["gpm_email"] for v in owners.values() if v["gpm_email"]})
    batch.summary = {"brands_changed": changed, "brands_with_owner": sum(1 for v in owners.values() if v["gpm_email"]),
                     "owners": len(owned)}
    session.flush()
    return batch
