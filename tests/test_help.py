"""Help from the top menu: each role's guide, and only the parts that role can do."""

import pytest

from mgrm.auth.roles import Role
from mgrm.checks.text import scan_generated_text
from tests.conftest import sign_in

SECTIONS = {
    "Administering the platform": {Role.ADMIN},
    "Entering a change": {Role.ADMIN, Role.REBATE_MAINTAINER, Role.REBATE_EDITOR},
    "Reviewing someone else's proposal": {Role.REBATE_MAINTAINER, Role.REBATE_REVIEWER, Role.BRAND_APPROVER},
    "The monthly accrual journal": {Role.ADMIN, Role.REBATE_MAINTAINER, Role.REBATE_EDITOR, Role.REBATE_REVIEWER},
    "Customers, customer groups and brands": {Role.ADMIN, Role.REBATE_MAINTAINER, Role.REBATE_EDITOR},
    "Download review pack": {Role.ADMIN, Role.REBATE_MAINTAINER, Role.REBATE_EDITOR},
    "Finding things": set(Role),
    "For everyone": set(Role),
}


@pytest.mark.parametrize("role", list(Role))
def test_each_role_reads_its_own_guide_from_the_menu(client, make_user, role):
    make_user(role)
    sign_in(client, f"{role.value}@example.com")
    assert 'href="/help"' in client.get("/").text
    page = client.get("/help")
    assert page.status_code == 200 and f"<strong>{role.title}:</strong>" in page.text
    for heading, roles in SECTIONS.items():
        assert (heading in page.text) == (role in roles), heading
    assert scan_generated_text(page.text) == []


def test_any_role_can_read_another_roles_guide(client, make_user):
    make_user(Role.VIEWER)
    sign_in(client, "viewer@example.com")
    page = client.get("/help?role=rebate_maintainer")
    assert "Entering a change" in page.text and "You are reading the guide for" in page.text


def test_a_brand_approver_cannot_approve_a_journal(make_user):
    from mgrm.rebates.journal import journal_refusal
    from mgrm.models import RebateJournal, ReviewStatus

    gpm = make_user(Role.BRAND_APPROVER)
    journal = RebateJournal(status=ReviewStatus.PROPOSED, entered_by_id=gpm.id + 1)
    assert journal_refusal(gpm, journal) == "Your role does not approve journals."
