"""The feed: customer groups, customers, brands and APPROVED rates, read-only, version 1.

Callers present a token created under Admin -> Feed tokens:
    Authorization: Bearer <token>
Only the token's SHA-256 is stored. Every response names its source and the
moment it was produced, so a consumer can stamp what it used.

The contract (field names and meanings) is pinned by tests/test_feed.py.
Changing it means a new version (/api/v2), not an edit to v1.
"""

import secrets
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from mgrm.data.files import bytes_sha256
from mgrm.models import ApiToken, Brand, Customer, CustomerGroup, RebateAgreement, RebateRate, ReviewStatus
from mgrm.web.security import get_db

router = APIRouter(prefix="/api/v1")
FEED_VERSION = "1"
SOURCE = "MacGear Rebate Master"
TOKEN_BYTES = 32


def new_token() -> tuple[str, str]:
    """(the token to give the caller once, the hash to store)."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, bytes_sha256(token.encode())


def authorised(authorization: str = Header(default=""), db: Session = Depends(get_db)) -> ApiToken:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="A feed token is required: Authorization: Bearer <token>")
    record = db.scalar(select(ApiToken).where(ApiToken.token_hash == bytes_sha256(token.strip().encode())))
    if record is None or record.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Unknown or revoked feed token")
    record.last_used_at = datetime.now(UTC)
    return record


def envelope(request: Request, data: list[dict], **extra) -> dict:
    return {"source": SOURCE, "feed_version": FEED_VERSION, "endpoint": request.url.path,
            "generated_at": datetime.now(UTC).isoformat(), "count": len(data), **extra, "data": data}


@router.get("/customer-groups")
def customer_groups(request: Request, token: ApiToken = Depends(authorised), db: Session = Depends(get_db)):
    rows = db.scalars(select(CustomerGroup).order_by(CustomerGroup.code))
    return envelope(request, [{"code": g.code, "name": g.name, "is_intercompany": g.is_intercompany} for g in rows])


@router.get("/customers")
def customers(request: Request, token: ApiToken = Depends(authorised), db: Session = Depends(get_db)):
    rows = db.scalars(select(Customer).order_by(Customer.netsuite_customer_id))
    return envelope(request, [
        {"netsuite_customer_id": c.netsuite_customer_id, "netsuite_entity_id": c.netsuite_entity_id, "name": c.name,
         "entity": c.entity_code, "customer_group_code": c.customer_group_code, "category": c.category,
         "parent_name": c.parent_name, "terms": c.terms, "is_inactive": c.is_inactive}
        for c in rows
    ])


@router.get("/brands")
def brands(request: Request, token: ApiToken = Depends(authorised), db: Session = Depends(get_db)):
    rows = db.scalars(select(Brand).order_by(Brand.code))
    return envelope(request, [
        {"code": b.code, "name": b.name, "netsuite_class_id": b.netsuite_class_id, "is_brand": b.is_brand,
         "forecast_tier": b.forecast_tier.value if b.forecast_tier else None, "is_inactive": b.is_inactive}
        for b in rows
    ])


@router.get("/rates")
def rates(request: Request, as_of: date | None = None, token: ApiToken = Depends(authorised), db: Session = Depends(get_db)):
    """Approved rates only. With as_of, those in force on that date; without, the full approved history."""
    query = (select(RebateRate, RebateAgreement).join(RebateAgreement, RebateAgreement.id == RebateRate.agreement_id)
             .where(RebateRate.status == ReviewStatus.APPROVED).order_by(RebateAgreement.code, RebateRate.effective_from))
    if as_of is not None:
        query = query.where(RebateRate.effective_from <= as_of,
                            (RebateRate.effective_to.is_(None)) | (RebateRate.effective_to >= as_of))
    data = [
        {"agreement_code": a.code, "entity": a.entity_code, "customer_group_code": a.customer_group_code,
         "customer_label": a.customer_label, "brand_code": a.brand_code, "product_scope": a.product_scope,
         "rate_type": a.rate_type.value, "basis": a.basis.value, "accrual_mode": a.accrual_mode.value,
         "rate": str(r.rate), "effective_from": r.effective_from.isoformat(),
         "effective_to": r.effective_to.isoformat() if r.effective_to else None,
         "band_from": str(r.band_from) if r.band_from is not None else None,
         "band_to": str(r.band_to) if r.band_to is not None else None,
         "rate_id": r.id, "approved_at": r.reviewed_at.isoformat() if r.reviewed_at else None}
        for r, a in db.execute(query).all()
    ]
    return envelope(request, data, as_of=as_of.isoformat() if as_of else None)
