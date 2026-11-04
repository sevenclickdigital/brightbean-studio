"""An operator account — staff, no organization — must be able to navigate.

The risk this covers is not the permission check, which is simple, but
the chrome: almost every page in this product assumes a current workspace
(``request.workspace``), and an operator has none. A template that
dereferences it would 500 on the console the operator logged in to use.

Status codes and redirect targets only. Nothing asserts on rendered
wording — the interface is pt-BR and the words are not a contract.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

STAFF_CONSOLE = "/billing/staff/"


@pytest.fixture
def operator(db):
    """Staff, and deliberately owning nothing. Created the way the
    management command creates one, bypassing the provisioning signal."""
    from django.db.models.signals import post_save

    from apps.accounts.signals import create_organization_on_user_create

    post_save.disconnect(create_organization_on_user_create, sender=User)
    try:
        return User.objects.create_user(
            email="ops@example.com",
            password="x",
            is_staff=True,
            tos_accepted_at=timezone.now(),
        )
    finally:
        post_save.connect(create_organization_on_user_create, sender=User)


@pytest.fixture
def customer(db):
    user = User.objects.create_user(
        email="client@example.com", password="x", tos_accepted_at=timezone.now()
    )
    org = Organization.objects.create(name="Client Co")
    workspace = Workspace.objects.create(organization=org, name="Client Social")
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user


@pytest.mark.django_db
class TestOperatorWithNoOrganization:
    def test_has_no_org_or_workspace(self, operator):
        assert OrgMembership.objects.filter(user=operator).count() == 0
        assert WorkspaceMembership.objects.filter(user=operator).count() == 0

    def test_reaches_the_staff_console(self, client, operator):
        client.force_login(operator)

        assert client.get(STAFF_CONSOLE).status_code == 200

    def test_console_renders_with_customer_data_present(self, client, operator, customer):
        """The console reads every org; none of them is the operator's."""
        client.force_login(operator)

        response = client.get(STAFF_CONSOLE)

        assert response.status_code == 200

    def test_root_sends_the_operator_to_the_console(self, client, operator):
        """Not to the create-a-workspace screen: an operator is not a customer."""
        client.force_login(operator)

        response = client.get("/")

        assert response.status_code == 302
        assert response.url == reverse("billing_staff:staff_console")

    def test_account_settings_does_not_break_without_an_org(self, client, operator):
        """A page whose chrome assumes a workspace must still render."""
        client.force_login(operator)

        assert client.get(reverse("accounts:settings")).status_code == 200


@pytest.mark.django_db
class TestCustomerIsUnaffected:
    def test_customer_still_lands_on_their_workspace(self, client, customer):
        client.force_login(customer)

        response = client.get("/")

        assert response.status_code == 302
        assert reverse("billing_staff:staff_console") not in response.url

    def test_customer_is_refused_the_console(self, client, customer):
        client.force_login(customer)

        assert client.get(STAFF_CONSOLE).status_code == 403

    def test_customer_with_no_workspace_still_sees_the_dashboard(self, client, db):
        """A real customer with nothing yet gets the create-workspace screen,
        which is right for them and wrong for an operator."""
        user = User.objects.create_user(
            email="newbie@example.com", password="x", tos_accepted_at=timezone.now()
        )
        WorkspaceMembership.objects.filter(user=user).delete()
        OrgMembership.objects.filter(user=user).delete()
        user.last_workspace_id = None
        user.save(update_fields=["last_workspace_id"])
        client.force_login(user)

        response = client.get("/")

        assert response.status_code == 200
