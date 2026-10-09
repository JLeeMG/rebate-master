"""Roles and what each may do in the rebate master.

This table is the whole permission model. Every screen asks
`can(role, permission)` rather than checking role names itself.
tests/test_auth.py pins the table so a change is deliberate.

The rebate master holds no P&L, balance sheet or facility figures; those live
in the forecasting platform. Sales amounts recorded by the legacy workbooks
are shown only to roles with VIEW_SALES.
"""

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    REBATE_EDITOR = "rebate_editor"
    REBATE_REVIEWER = "rebate_reviewer"
    BRAND_APPROVER = "brand_approver"
    VIEWER = "viewer"

    @property
    def title(self) -> str:
        return ROLE_TITLES[self]


ROLE_TITLES: dict[Role, str] = {
    Role.ADMIN: "Administrator",
    Role.REBATE_EDITOR: "Rebate editor",
    Role.REBATE_REVIEWER: "Rebate reviewer",
    Role.BRAND_APPROVER: "Brand rebate approver",
    Role.VIEWER: "Viewer",
}

ROLE_DESCRIPTIONS: dict[Role, str] = {
    Role.ADMIN: "Everything, including users, brand approvers and the feed to the forecasting platform.",
    Role.REBATE_EDITOR: (
        "Enters agreements and rate changes with their evidence, and maintains customers, customer groups "
        "and brands. Cannot approve their own entries."
    ),
    Role.REBATE_REVIEWER: "Approves or rejects rate changes entered by someone else.",
    Role.BRAND_APPROVER: (
        "Approves rate changes for the brands assigned to them (e.g. a Group Product Manager). Reads everything else."
    ),
    Role.VIEWER: "Reads agreements, rates, rate cards and the change log. Changes nothing.",
}


class Permission(StrEnum):
    VIEW = "view"
    VIEW_SALES = "view_sales"  # sales amounts recorded by the legacy workbooks
    EDIT_REBATES = "edit_rebates"  # agreements, rate proposals, evidence
    APPROVE_REBATES = "approve_rebates"  # never one's own entry; brand scope enforced in mgrm.rebates.service
    MANAGE_REGISTERS = "manage_registers"  # customers, customer groups, brands, NetSuite register loads
    MANAGE_USERS = "manage_users"  # users, brand approvers, feed tokens


P = Permission

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.REBATE_EDITOR: frozenset({P.VIEW, P.VIEW_SALES, P.EDIT_REBATES, P.MANAGE_REGISTERS}),
    Role.REBATE_REVIEWER: frozenset({P.VIEW, P.VIEW_SALES, P.APPROVE_REBATES}),
    Role.BRAND_APPROVER: frozenset({P.VIEW, P.APPROVE_REBATES}),
    Role.VIEWER: frozenset({P.VIEW}),
}


def can(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]
