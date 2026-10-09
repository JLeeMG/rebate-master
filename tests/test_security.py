"""Hardening from the 9 October 2026 security audit: headers, hosts, request size, sign-in throttling,
sessions that can be ended, the audit trail, and file safety."""

import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mgrm.api.feed import new_token
from mgrm.auth.roles import Role
from mgrm.auth.users import set_password
from mgrm.config import deployment_problem
from mgrm.data import files
from mgrm.models import ApiToken, AuditEvent, AuthMethod, RebateAgreement, RebateRate
from mgrm.web.routes_auth import SIGN_IN_FAILED
from mgrm.web.security import MAX_FAILED_SIGN_INS, record_failed_sign_in, sign_in_refused
from tests.conftest import PDF_BYTES, TEST_PASSWORD, form_token, sign_in
from tests.test_web import load_workbook_through_screen


# ---------------------------------------------------------------- the browser's protections


def test_every_response_carries_the_security_headers(client):
    headers = client.get("/login").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in headers  # only when served over HTTPS


def test_https_only_adds_strict_transport_security(settings, db):
    from mgrm.web.app import create_app

    app = create_app(settings.model_copy(update={"session_https_only": True}))
    with TestClient(app, base_url="https://localhost") as client:
        assert client.get("/login").headers["strict-transport-security"].startswith("max-age=")


def test_requests_for_another_host_name_are_refused(app):
    with TestClient(app, base_url="http://evil.example") as client:
        assert client.get("/login").status_code == 400


def test_a_network_name_needs_https(settings):
    hosted = settings.model_copy(update={"allowed_hosts": ["localhost", "rebates.macgeargroup.com"]})
    assert "SESSION_HTTPS_ONLY" in deployment_problem(hosted)
    assert deployment_problem(hosted.model_copy(update={"session_https_only": True})) is None
    from mgrm.web.app import create_app

    with pytest.raises(RuntimeError, match="HTTPS"):
        create_app(hosted)


def test_an_oversized_request_is_refused_before_it_is_read(client, monkeypatch):
    monkeypatch.setattr("mgrm.web.hardening.MAX_REQUEST_BYTES", 1000)
    response = client.post("/login", data={"email": "x" * 2000, "password": "p", "csrf_token": "t"})
    assert response.status_code == 413 and "25 MB" in response.text


# ---------------------------------------------------------------- signing in


def test_failures_from_one_address_do_not_lock_the_person_out_elsewhere(db):
    for _ in range(MAX_FAILED_SIGN_INS):
        record_failed_sign_in(db, "cfo@example.com", "10.0.0.66")
    db.flush()
    assert sign_in_refused(db, "cfo@example.com", "10.0.0.66")
    assert not sign_in_refused(db, "cfo@example.com", "10.0.0.7")  # the CFO's own computer


def test_guessing_many_emails_from_one_address_is_slowed(db):
    for n in range(20):
        record_failed_sign_in(db, f"person{n}@example.com", "10.0.0.66")
    db.flush()
    assert sign_in_refused(db, "anyone@example.com", "10.0.0.66")


def test_after_five_wrong_passwords_even_the_right_one_waits(client, make_user):
    make_user(Role.VIEWER)
    for _ in range(MAX_FAILED_SIGN_INS):
        assert sign_in(client, "viewer@example.com", "wrong password!").status_code == 401
    assert sign_in(client, "viewer@example.com").status_code == 429


def test_one_message_whatever_went_wrong(client, make_user, db):
    make_user(Role.VIEWER, email="ms@example.com", auth_method=AuthMethod.MICROSOFT, password=None)
    gone = make_user(Role.VIEWER, email="gone@example.com")
    gone.is_active = False
    db.flush()
    for email in ("ms@example.com", "gone@example.com", "nobody@example.com"):
        response = sign_in(client, email)
        assert response.status_code == 401 and SIGN_IN_FAILED in response.text, email


# ---------------------------------------------------------------- sessions


def two_signed_in(app, email):
    first, second = TestClient(app, base_url="http://localhost"), TestClient(app, base_url="http://localhost")
    sign_in(first, email)
    sign_in(second, email)
    return first, second


def test_signing_out_ends_every_session(app, make_user):
    make_user(Role.VIEWER)
    first, second = two_signed_in(app, "viewer@example.com")
    assert second.get("/rebates").status_code == 200
    first.post("/logout", data={"csrf_token": form_token(first, "/")})
    assert second.get("/rebates", follow_redirects=False).headers["location"] == "/login"


def test_a_password_reset_ends_the_persons_sessions(app, make_user, db):
    admin = make_user(Role.ADMIN)
    viewer = make_user(Role.VIEWER)
    client = TestClient(app, base_url="http://localhost")
    sign_in(client, "viewer@example.com")
    set_password(db, actor=admin, user=viewer, password="a temporary password")
    db.flush()
    assert client.get("/rebates", follow_redirects=False).headers["location"] == "/login"


def test_changing_your_own_password_keeps_you_signed_in_here_only(app, make_user):
    make_user(Role.VIEWER)
    here, elsewhere = two_signed_in(app, "viewer@example.com")
    here.post("/account/password", data={"csrf_token": form_token(here, "/account/password"),
                                         "current_password": TEST_PASSWORD, "new_password": "another long password",
                                         "confirm_password": "another long password"})
    assert here.get("/rebates").status_code == 200
    assert elsewhere.get("/rebates", follow_redirects=False).headers["location"] == "/login"


def test_a_session_ends_after_twelve_hours_however_busy(client, make_user, monkeypatch):
    make_user(Role.VIEWER)
    sign_in(client, "viewer@example.com")
    monkeypatch.setattr("mgrm.web.security.SESSION_ABSOLUTE_SECONDS", -1)
    assert client.get("/rebates", follow_redirects=False).headers["location"] == "/login"


# ---------------------------------------------------------------- the audit trail


def test_the_audit_log_records_the_network_address(client, make_user, db):
    make_user(Role.VIEWER)
    sign_in(client, "viewer@example.com")
    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "sign_in"))
    assert event.address == "testclient"


def test_a_refused_page_is_recorded(client, make_user, db):
    make_user(Role.VIEWER)
    sign_in(client, "viewer@example.com")
    assert client.get("/admin/users").status_code == 403
    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "access.refused"))
    assert event.subject == "/admin/users" and event.detail["permission"] == "manage_users"


# ---------------------------------------------------------------- evidence, the feed and files


def test_only_the_proposer_adds_evidence_to_a_pending_rate(client, make_user, db, tmp_path):
    make_user(Role.REBATE_MAINTAINER, email="siobhan@example.com")
    make_user(Role.REBATE_MAINTAINER, email="ken@example.com")
    sign_in(client, "siobhan@example.com")
    load_workbook_through_screen(client, tmp_path)
    agreement = db.query(RebateAgreement).one()
    rate = db.query(RebateRate).first()
    page = f"/rebates/agreement/{agreement.id}"
    client.post("/logout", data={"csrf_token": form_token(client, "/")})
    sign_in(client, "ken@example.com")
    refused = client.post(f"{page}/evidence", data={"csrf_token": form_token(client, page), "rate_id": rate.id},
                          files=[("evidence", ("extra.pdf", PDF_BYTES, "application/pdf"))])
    assert refused.status_code == 400 and "Only the person who proposed" in refused.text


def test_an_expired_feed_token_is_refused(client, db):
    token, token_hash = new_token()
    db.add(ApiToken(name="old", token_hash=token_hash, expires_at=datetime.now(UTC) - timedelta(days=1)))
    db.flush()
    assert client.get("/api/v1/brands", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_a_workbook_that_unpacks_too_large_is_not_opened(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "MAX_WORKBOOK_UNPACKED_BYTES", 10_000)
    path = tmp_path / "MGNZ 2608 bomb.xlsx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"0" * 100_000)
    assert "unpacks" in files.workbook_problem(path)
    (tmp_path / "not.xlsx").write_bytes(b"plain text")
    assert "not an Excel workbook" in files.workbook_problem(tmp_path / "not.xlsx")


def test_an_oversized_load_is_refused(client, make_user, monkeypatch):
    monkeypatch.setattr("mgrm.web.routes_registers.MAX_UPLOAD_BYTES", 100)
    make_user(Role.REBATE_EDITOR)
    sign_in(client, "rebate_editor@example.com")
    response = client.post("/loads", data={"kind": "customers", "csrf_token": form_token(client, "/loads")},
                           files={"upload": ("customers.csv", io.BytesIO(b"x" * 1000))})
    assert response.status_code == 400 and "Nothing was loaded" in response.text
