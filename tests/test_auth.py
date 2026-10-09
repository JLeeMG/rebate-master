"""The permission table, passwords, and who Microsoft sign-in admits."""

import pytest

from mgrm.auth.microsoft import resolve_microsoft_user
from mgrm.auth.passwords import hash_password, password_problem, verify_password
from mgrm.auth.roles import ROLE_PERMISSIONS, Permission, Role, can
from mgrm.auth.users import UserError, change_role, set_active
from mgrm.models import AuthMethod

P = Permission
TENANT = "11111111-2222-3333-4444-555555555555"


def test_permission_table_is_as_agreed():
    # Pinned: changing who can do what must be a deliberate edit to this test too.
    assert ROLE_PERMISSIONS[Role.ADMIN] == frozenset(Permission)
    assert ROLE_PERMISSIONS[Role.REBATE_EDITOR] == {P.VIEW, P.VIEW_SALES, P.EDIT_REBATES, P.MANAGE_REGISTERS}
    assert ROLE_PERMISSIONS[Role.REBATE_REVIEWER] == {P.VIEW, P.VIEW_SALES, P.APPROVE_REBATES}
    assert ROLE_PERMISSIONS[Role.BRAND_APPROVER] == {P.VIEW, P.APPROVE_REBATES}
    assert ROLE_PERMISSIONS[Role.VIEWER] == {P.VIEW}


def test_the_editor_cannot_approve_and_the_reviewer_cannot_edit():
    # Four-eyes by role as well as by person: Siobhan enters, Ken approves.
    assert not can(Role.REBATE_EDITOR, P.APPROVE_REBATES)
    assert not can(Role.REBATE_REVIEWER, P.EDIT_REBATES)


def test_only_admin_manages_users_and_the_feed():
    for role in Role:
        assert can(role, P.MANAGE_USERS) == (role is Role.ADMIN)


def test_passwords():
    assert password_problem("short") is not None
    hashed = hash_password("a long enough password")
    assert verify_password(hashed, "a long enough password")
    assert not verify_password(hashed, "a long enough passwore")


def test_microsoft_admits_only_registered_microsoft_users(db, make_user):
    ms = make_user(Role.VIEWER, email="ms@macgeargroup.com", auth_method=AuthMethod.MICROSOFT, password=None)
    claims = {"tid": TENANT, "preferred_username": "MS@macgeargroup.com"}
    assert resolve_microsoft_user(db, claims, TENANT) is ms
    assert resolve_microsoft_user(db, {**claims, "tid": "another-organisation"}, TENANT) is None
    assert resolve_microsoft_user(db, {"tid": TENANT, "email": "stranger@macgeargroup.com"}, TENANT) is None


def test_the_last_administrator_cannot_be_removed(db, make_user):
    admin = make_user(Role.ADMIN)
    with pytest.raises(UserError, match="only active administrator"):
        change_role(db, actor=admin, user=admin, role=Role.VIEWER)
    with pytest.raises(UserError, match="only active administrator"):
        set_active(db, actor=admin, user=admin, active=False)
