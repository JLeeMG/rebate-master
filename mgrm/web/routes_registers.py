"""The registers this platform is master of: customers and customer groups, brands; and loading files."""

import hashlib
import re
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mgrm.auth.roles import Permission, can
from mgrm.auth.users import audit
from mgrm.data.errors import AlreadyLoaded, LoadRejected
from mgrm.data.rebate_workbooks import load_workbook
from mgrm.data.registers import LOADERS, FileInput
from mgrm.domain.entities import TRADING_ENTITIES
from mgrm.models import AppUser, Brand, Customer, CustomerGroup, ForecastTier, LoadBatch, LoadKind
from mgrm.web.app import render
from mgrm.web.security import get_db, require, verify_csrf

router = APIRouter()
reader = require(Permission.VIEW)
registrar = require(Permission.MANAGE_REGISTERS)

CUSTOMERS_SHOWN = 500
RECENT_LOADS = 50
UPLOAD_KINDS = {
    "customers": "Customer register (CSV from the MGFP Register - Customers search)",
    "classes": "Class register (CSV from the MGFP Register - Classes search)",
    "rebate_workbook": "Rebate reconciliation workbook (.xlsx; first load only)",
}


def _decode(raw: bytes, name: str) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise LoadRejected(name, ["The file is not text in a known encoding; export it from NetSuite as CSV."])


# ---------------------------------------------------------------- loads


def _loads_page(request: Request, db: Session, status_code: int = 200, **context):
    batches = db.scalars(select(LoadBatch).order_by(LoadBatch.id.desc()).limit(RECENT_LOADS)).all()
    return render(request, "loads.html", status_code=status_code, batches=batches, kinds=UPLOAD_KINDS, **context)


@router.get("/loads")
def loads(request: Request, user: AppUser = Depends(registrar), db: Session = Depends(get_db)):
    return _loads_page(request, db)


@router.post("/loads", dependencies=[Depends(verify_csrf)])
async def upload(
    request: Request,
    kind: str = Form(...),
    upload: UploadFile = File(...),
    user: AppUser = Depends(registrar),
    db: Session = Depends(get_db),
):
    if kind not in UPLOAD_KINDS:
        return _loads_page(request, db, 400, error="Choose what kind of file this is.")
    raw = await upload.read()
    name = Path(upload.filename or "upload").name
    try:
        with db.begin_nested():
            if kind == "rebate_workbook":
                if not can(user.role, Permission.EDIT_REBATES):
                    raise LoadRejected(name, ["Your role does not enter rebate agreements."])
                with tempfile.TemporaryDirectory() as folder:
                    path = Path(folder) / re.sub(r"[^\w .&()-]", "_", name)
                    path.write_bytes(raw)
                    batch = load_workbook(db, path, user)
            else:
                batch = LOADERS[LoadKind(kind)](db, FileInput(name, _decode(raw, name), hashlib.sha256(raw).hexdigest()), user)
            audit(db, user, "data.load", name, kind=kind, batch=batch.id, rows=batch.row_count)
    except AlreadyLoaded as exc:
        return _loads_page(request, db, notice=str(exc))
    except LoadRejected as exc:
        audit(db, user, "data.load_rejected", name, kind=kind, problems=len(exc.problems))
        return _loads_page(request, db, 400, rejected=exc)
    return _loads_page(request, db, notice=f"Loaded {name}: {batch.row_count} rows (load {batch.id}).")


# ---------------------------------------------------------------- brands


@router.get("/brands")
def brands(request: Request, user: AppUser = Depends(reader), db: Session = Depends(get_db)):
    rows = db.scalars(select(Brand).order_by(Brand.is_brand.desc(), Brand.is_inactive, Brand.name)).all()
    return render(request, "brands.html", brands=rows, tiers=list(ForecastTier))


@router.post("/brands/{brand_id}", dependencies=[Depends(verify_csrf)])
def update_brand(
    brand_id: int,
    is_brand: bool = Form(False),
    tier: str = Form(""),
    reason: str = Form(""),
    user: AppUser = Depends(registrar),
    db: Session = Depends(get_db),
):
    brand = db.get(Brand, brand_id)
    if brand is not None:
        new_tier = ForecastTier(tier) if tier else None
        if (brand.is_brand, brand.forecast_tier) != (is_brand, new_tier):
            audit(db, user, "brand.update", brand.code, reason=reason.strip(),
                  old={"is_brand": brand.is_brand, "tier": brand.forecast_tier and brand.forecast_tier.value},
                  new={"is_brand": is_brand, "tier": tier or None})
            brand.is_brand, brand.forecast_tier = is_brand, new_tier
    return RedirectResponse("/brands", status_code=303)


# ---------------------------------------------------------------- customers and groups


def _customer_query(search: str, unmapped: bool, entity: str, group: str):
    query = select(Customer)
    if search:
        query = query.where(Customer.name.ilike(f"%{search}%") | Customer.parent_name.ilike(f"%{search}%"))
    if unmapped:
        query = query.where(Customer.customer_group_code.is_(None))
    if entity in {e.value for e in TRADING_ENTITIES}:
        query = query.where(Customer.entity_code == entity)
    if group:
        query = query.where(Customer.customer_group_code == group)
    return query


@router.get("/customers")
def customers(
    request: Request,
    search: str = "",
    unmapped: bool = False,
    entity: str = "",
    group: str = "",
    user: AppUser = Depends(reader),
    db: Session = Depends(get_db),
):
    query = _customer_query(search.strip(), unmapped, entity, group)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    shown = db.scalars(query.order_by(Customer.name).limit(CUSTOMERS_SHOWN)).all()
    unmapped_count = db.scalar(select(func.count()).select_from(Customer).where(Customer.customer_group_code.is_(None)))
    counts = dict(db.execute(select(Customer.customer_group_code, func.count()).group_by(Customer.customer_group_code)).all())
    return render(
        request, "customers.html", customers=shown, total=total, search=search, unmapped=unmapped, entity=entity,
        group=group, groups=db.scalars(select(CustomerGroup).order_by(CustomerGroup.name)).all(),
        unmapped_count=unmapped_count, counts=counts, limit=CUSTOMERS_SHOWN,
    )


@router.post("/customer-groups", dependencies=[Depends(verify_csrf)])
def add_customer_group(
    code: str = Form(...),
    name: str = Form(...),
    reason: str = Form(""),
    user: AppUser = Depends(registrar),
    db: Session = Depends(get_db),
):
    code = re.sub(r"[^A-Z0-9]", "", code.upper())[:16]
    name = name.strip()
    if code and name and db.get(CustomerGroup, code) is None and not db.scalar(select(CustomerGroup).where(CustomerGroup.name == name)):
        db.add(CustomerGroup(code=code, name=name, is_intercompany=False))
        audit(db, user, "customer_group.add", code, name=name, reason=reason.strip())
    return RedirectResponse("/customers", status_code=303)


@router.post("/customers/assign", dependencies=[Depends(verify_csrf)])
def assign_group(
    group: str = Form(...),
    search: str = Form(""),
    unmapped: bool = Form(False),
    entity: str = Form(""),
    customer_id: int | None = Form(None),
    reason: str = Form(""),
    user: AppUser = Depends(registrar),
    db: Session = Depends(get_db),
):
    """Assign one customer, or every customer the current filter shows, to a group."""
    target = db.get(CustomerGroup, group) if group else None
    if customer_id is not None:
        chosen = [c for c in [db.get(Customer, customer_id)] if c is not None]
    else:
        if not search.strip():
            return RedirectResponse("/customers", status_code=303)  # never "assign everyone" without a filter
        chosen = db.scalars(_customer_query(search.strip(), unmapped, entity, "")).all()
    changed = [c for c in chosen if c.customer_group_code != (target.code if target else None)]
    if changed:
        audit(db, user, "customer.assign_group", f"{len(changed)} customers", group=target.code if target else None,
              reason=reason.strip(), filter=search,
              previous={str(c.netsuite_customer_id): c.customer_group_code for c in changed[:CUSTOMERS_SHOWN]})
    for customer in changed:
        customer.customer_group_code = target.code if target else None
    params = {"search": search, "entity": entity, **({"unmapped": "true"} if unmapped else {})}
    return RedirectResponse(f"/customers?{urlencode(params)}", status_code=303)
