"""Tests for the billing screens.

Behaviour, permission and database state only. Nothing here asserts on
rendered text or markup: the UI renders in pt-BR, the wording is not a
contract, and the project already carries a pile of tests broken by
exactly that mistake. What *is* asserted is the stuff a wording change
must never break — who may reach a page, what the add-on arithmetic
produces, that a missing Asaas credential is not a 500, and what the
database holds afterwards.
"""

from __future__ import annotations

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing import services
from apps.billing.models import (
    OrganizationBillingProfile,
    WorkspaceAddon,
    WorkspaceSubscription,
)
from apps.billing.views import STORAGE_ADDON_MAX_GB, _storage_quote
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

GB = WorkspaceAddon.BYTES_PER_GB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _user(email, *, is_staff=False):
    return User.objects.create_user(
        email=email,
        password="testpass123",
        name=email.split("@")[0],
        tos_accepted_at=timezone.now(),
        is_staff=is_staff,
    )


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Acme")


@pytest.fixture
def workspace(db, org):
    return Workspace.objects.create(organization=org, name="Acme Social")


@pytest.fixture
def owner(db, org, workspace):
    """Workspace owner and org owner — may contract."""
    user = _user("owner@example.com")
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user


@pytest.fixture
def viewer(db, org, workspace):
    """Workspace member with no authority to spend money."""
    user = _user("viewer@example.com")
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
    )
    return user


@pytest.fixture
def outsider(db):
    """Authenticated user with no membership in the workspace's org."""
    other_org = Organization.objects.create(name="Other Co")
    user = _user("outsider@example.com")
    OrgMembership.objects.create(user=user, organization=other_org, org_role=OrgMembership.OrgRole.OWNER)
    return user


@pytest.fixture
def staff(db):
    return _user("ops@example.com", is_staff=True)


def _client_for(user):
    client = Client()
    client.force_login(user)
    return client


# ---------------------------------------------------------------------------
# Add-on pricing — the arithmetic the customer agrees to
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestStorageQuote:
    """Price of the chosen quantity, computed once and used everywhere."""

    @override_settings(BILLING_STORAGE_PRICE_PER_GB_CENTS=200)
    def test_total_is_quantity_times_unit_price(self):
        assert _storage_quote(5)["total_cents"] == 1000
        assert _storage_quote(10)["total_cents"] == 2000
        assert _storage_quote(50)["total_cents"] == 10_000

    @override_settings(BILLING_STORAGE_PRICE_PER_GB_CENTS=375)
    def test_odd_unit_price_stays_in_whole_cents(self):
        quote = _storage_quote(7)
        assert quote["unit_price_cents"] == 375
        assert quote["total_cents"] == 2625
        assert isinstance(quote["total_cents"], int)

    @override_settings(BILLING_STORAGE_PRICE_PER_GB_CENTS=200)
    def test_quote_endpoint_prices_the_requested_quantity(self, owner, workspace):
        client = _client_for(owner)
        response = client.get(
            reverse("billing:storage_quote", kwargs={"workspace_id": workspace.id}),
            {"quantity_gb": "25"},
        )
        assert response.status_code == 200
        assert response.context["quote"]["total_cents"] == 5000

    def test_quote_endpoint_rejects_out_of_range(self, owner, workspace):
        client = _client_for(owner)
        response = client.get(
            reverse("billing:storage_quote", kwargs={"workspace_id": workspace.id}),
            {"quantity_gb": str(STORAGE_ADDON_MAX_GB + 1)},
        )
        # Still a 200 fragment — this is an inline preview, not a failure
        # — but it carries no quote to confirm against.
        assert response.status_code == 200
        assert response.context["quote"] is None
        assert response.context["invalid"] is True


@pytest.mark.django_db
class TestContractStorage:
    """Buying GB writes a row priced at the quantity the user chose."""

    @override_settings(BILLING_STORAGE_PRICE_PER_GB_CENTS=200, ASAAS_API_KEY="")
    def test_writes_addon_with_chosen_quantity_and_frozen_price(self, owner, workspace):
        client = _client_for(owner)
        response = client.post(
            reverse("billing:contract_storage", kwargs={"workspace_id": workspace.id}),
            {"quantity_gb": "10"},
        )
        assert response.status_code == 302

        addon = WorkspaceAddon.objects.get(workspace=workspace)
        assert addon.kind == WorkspaceAddon.Kind.EXTRA_STORAGE
        assert addon.quantity == 10
        assert addon.unit_price_cents == 200
        assert addon.total_price_cents == 2000
        assert addon.storage_bytes == 10 * GB

    @override_settings(ASAAS_API_KEY="")
    def test_pending_addon_grants_no_disk_yet(self, owner, workspace):
        """Only an *active* add-on raises the quota — see services.extra_storage_bytes."""
        services.ensure_subscription(workspace)
        before = services.total_storage_bytes(workspace)

        client = _client_for(owner)
        client.post(
            reverse("billing:contract_storage", kwargs={"workspace_id": workspace.id}),
            {"quantity_gb": "10"},
        )

        addon = WorkspaceAddon.objects.get(workspace=workspace)
        assert addon.status == WorkspaceAddon.Status.PENDING
        assert services.total_storage_bytes(workspace) == before

    @override_settings(ASAAS_API_KEY="")
    def test_invalid_quantity_writes_nothing(self, owner, workspace):
        client = _client_for(owner)
        for raw in ("0", "-5", "abc", "", str(STORAGE_ADDON_MAX_GB + 1)):
            response = client.post(
                reverse("billing:contract_storage", kwargs={"workspace_id": workspace.id}),
                {"quantity_gb": raw},
            )
            assert response.status_code == 302, raw
        assert not WorkspaceAddon.objects.filter(workspace=workspace).exists()

    @override_settings(ASAAS_API_KEY="")
    def test_member_without_authority_cannot_buy(self, viewer, workspace):
        client = _client_for(viewer)
        response = client.post(
            reverse("billing:contract_storage", kwargs={"workspace_id": workspace.id}),
            {"quantity_gb": "10"},
        )
        assert response.status_code == 403
        assert not WorkspaceAddon.objects.filter(workspace=workspace).exists()


# ---------------------------------------------------------------------------
# Asaas not configured — the normal state today, not a 500
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAsaasNotConfigured:
    """No credentials must never reach the user as an error page."""

    @override_settings(ASAAS_API_KEY="")
    def test_subscribing_redirects_instead_of_raising(self, owner, workspace):
        client = _client_for(owner)
        response = client.post(
            reverse("billing:subscribe_confirm", kwargs={"workspace_id": workspace.id}),
        )
        assert response.status_code == 302
        assert response.headers["Location"] == reverse(
            "billing:workspace_billing", kwargs={"workspace_id": workspace.id}
        )

    @override_settings(ASAAS_API_KEY="")
    def test_subscription_row_stays_pending_and_unlinked(self, owner, workspace):
        """A retry once Asaas is configured has to be a clean first attempt."""
        client = _client_for(owner)
        client.post(reverse("billing:subscribe_confirm", kwargs={"workspace_id": workspace.id}))

        sub = WorkspaceSubscription.objects.get(workspace=workspace)
        assert sub.status == WorkspaceSubscription.Status.PENDING
        assert sub.gateway_subscription_id == ""
        assert sub.gateway_customer_id == ""
        assert not sub.is_active

    @override_settings(ASAAS_API_KEY="")
    def test_message_is_informational_not_an_error(self, owner, workspace):
        """Nothing is broken, so the user must not be told something failed."""
        client = _client_for(owner)
        response = client.post(
            reverse("billing:subscribe_confirm", kwargs={"workspace_id": workspace.id}),
            follow=True,
        )
        tags = [m.level_tag for m in response.context["messages"]]
        assert "info" in tags
        assert "error" not in tags

    @override_settings(ASAAS_API_KEY="")
    def test_storage_addon_survives_as_a_pending_request(self, owner, workspace):
        """The row records what was asked for, so it can be finished later."""
        client = _client_for(owner)
        response = client.post(
            reverse("billing:contract_storage", kwargs={"workspace_id": workspace.id}),
            {"quantity_gb": "5"},
            follow=True,
        )
        addon = WorkspaceAddon.objects.get(workspace=workspace)
        assert addon.quantity == 5
        assert addon.gateway_subscription_id == ""
        assert "info" in [m.level_tag for m in response.context["messages"]]


# ---------------------------------------------------------------------------
# Workspace page access
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWorkspaceBillingAccess:
    def test_anonymous_is_sent_to_login(self, client, workspace):
        response = client.get(reverse("billing:workspace_billing", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 302
        assert "/accounts/login" in response.headers["Location"]

    def test_non_member_is_forbidden(self, outsider, workspace):
        client = _client_for(outsider)
        response = client.get(reverse("billing:workspace_billing", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 403

    def test_member_may_view_but_not_manage(self, viewer, workspace):
        client = _client_for(viewer)
        response = client.get(reverse("billing:workspace_billing", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 200
        # The meter is for everyone; the purchase controls are not.
        assert response.context["can_manage_billing"] is False

    def test_owner_may_manage(self, owner, workspace):
        client = _client_for(owner)
        response = client.get(reverse("billing:workspace_billing", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 200
        assert response.context["can_manage_billing"] is True

    def test_viewing_creates_the_subscription_row(self, owner, workspace):
        """Opening the page must never 500 on a workspace that predates billing."""
        assert not WorkspaceSubscription.objects.filter(workspace=workspace).exists()
        client = _client_for(owner)
        response = client.get(reverse("billing:workspace_billing", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 200
        assert WorkspaceSubscription.objects.filter(workspace=workspace).exists()

    def test_subscribe_modal_redirects_when_already_active(self, owner, workspace):
        sub = services.ensure_subscription(workspace)
        sub.status = WorkspaceSubscription.Status.ACTIVE
        sub.save(update_fields=["status"])

        client = _client_for(owner)
        response = client.get(reverse("billing:subscribe_modal", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 302
        assert response.headers["Location"] == reverse(
            "billing:workspace_billing", kwargs={"workspace_id": workspace.id}
        )

    def test_courtesy_workspace_sees_the_modal_as_settled(self, owner, workspace):
        """A comped workspace is active, so it must not be asked to pay."""
        services.grant_courtesy(workspace, note="Partner")
        client = _client_for(owner)
        response = client.get(reverse("billing:subscribe_modal", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 302


@pytest.mark.django_db
class TestWorkspaceCreationFlow:
    def test_creating_a_workspace_lands_on_contracting(self, db, org):
        """The product rule: a new workspace shows the contracting modal."""
        user = _user("founder@example.com")
        OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)

        client = _client_for(user)
        response = client.post(reverse("workspaces:create"), {"name": "Brand New"})

        created = Workspace.objects.get(name="Brand New")
        assert response.status_code == 302
        assert response.headers["Location"] == reverse("billing:subscribe_modal", kwargs={"workspace_id": created.id})

    def test_creating_a_workspace_provisions_its_subscription_row(self, db, org):
        """So the staff console and the disk quota have something to read."""
        user = _user("founder2@example.com")
        OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)

        _client_for(user).post(reverse("workspaces:create"), {"name": "Fresh"})

        created = Workspace.objects.get(name="Fresh")
        sub = WorkspaceSubscription.objects.get(workspace=created)
        assert sub.status == WorkspaceSubscription.Status.PENDING
        assert sub.price_cents == services.workspace_price_cents()
        assert sub.base_storage_bytes == services.base_storage_bytes()


# ---------------------------------------------------------------------------
# Staff console — who may reach it
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestStaffConsoleAccess:
    def test_anonymous_is_sent_to_login(self, client):
        response = client.get(reverse("billing_staff:staff_console"))
        assert response.status_code == 302
        assert "/accounts/login" in response.headers["Location"]

    def test_org_owner_without_is_staff_is_forbidden(self, owner):
        """Being the top role in your own account grants nothing here.

        The console spans every organization, so no membership — however
        privileged — can be the thing that opens it.
        """
        client = _client_for(owner)
        response = client.get(reverse("billing_staff:staff_console"))
        assert response.status_code == 403

    def test_logged_in_non_staff_gets_403_not_a_login_loop(self, viewer):
        client = _client_for(viewer)
        response = client.get(reverse("billing_staff:staff_console"))
        assert response.status_code == 403

    def test_staff_may_view(self, staff, workspace):
        client = _client_for(staff)
        response = client.get(reverse("billing_staff:staff_console"))
        assert response.status_code == 200

    def test_inactive_staff_is_forbidden(self, staff):
        staff.is_active = False
        staff.save(update_fields=["is_active"])
        client = Client()
        client.force_login(staff)
        response = client.get(reverse("billing_staff:staff_console"))
        # force_login on an inactive user still sets the session, so the
        # staff check is what has to refuse — not a 200.
        assert response.status_code in (302, 403)

    def test_non_staff_cannot_grant_courtesy(self, owner, workspace):
        client = _client_for(owner)
        response = client.post(
            reverse("billing_staff:toggle_courtesy", kwargs={"target_workspace_id": workspace.id}),
            {"action": "grant"},
        )
        assert response.status_code == 403
        assert not WorkspaceSubscription.objects.filter(
            workspace=workspace, status=WorkspaceSubscription.Status.COURTESY
        ).exists()

    def test_non_staff_cannot_set_the_quota(self, owner, org):
        client = _client_for(owner)
        response = client.post(
            reverse("billing_staff:set_courtesy_quota", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "99"},
        )
        assert response.status_code == 403
        profile = OrganizationBillingProfile.objects.filter(organization=org).first()
        assert profile is None or profile.courtesy_workspace_quota == 0


@pytest.mark.django_db
class TestStaffCourtesy:
    """Granting and revoking a comp, checked in the database."""

    def test_grant_moves_status_to_courtesy_and_zeroes_the_bill(self, staff, workspace):
        services.ensure_subscription(workspace)
        client = _client_for(staff)
        response = client.post(
            reverse("billing_staff:toggle_courtesy", kwargs={"target_workspace_id": workspace.id}),
            {"action": "grant", "note": "Beta tester"},
        )
        assert response.status_code == 302

        sub = WorkspaceSubscription.objects.get(workspace=workspace)
        assert sub.status == WorkspaceSubscription.Status.COURTESY
        assert sub.courtesy_note == "Beta tester"
        assert sub.is_active

        # Re-fetch: this test's ``workspace`` instance cached its reverse
        # OneToOne before the request, and the view mutated the row in
        # another instance. Passing the id forces the service to read the
        # database instead of the stale cache.
        assert services.workspace_monthly_total_cents(workspace.pk) == 0
        assert services.is_workspace_active(workspace.pk) is True

    def test_grant_records_who_comped_it(self, staff, workspace, org):
        _client_for(staff).post(
            reverse("billing_staff:toggle_courtesy", kwargs={"target_workspace_id": workspace.id}),
            {"action": "grant"},
        )
        profile = OrganizationBillingProfile.objects.get(organization=org)
        assert profile.courtesy_granted_by_id == staff.id
        assert profile.courtesy_granted_at is not None

    def test_revoke_returns_to_pending_not_active(self, staff, workspace):
        """Removing a comp means it needs a real contract again."""
        services.grant_courtesy(workspace, note="Temporary")
        client = _client_for(staff)
        client.post(
            reverse("billing_staff:toggle_courtesy", kwargs={"target_workspace_id": workspace.id}),
            {"action": "revoke"},
        )
        sub = WorkspaceSubscription.objects.get(workspace=workspace)
        assert sub.status == WorkspaceSubscription.Status.PENDING
        assert sub.courtesy_note == ""
        # By id, not by instance — see the note in the grant test.
        assert services.is_workspace_active(workspace.pk) is False

    def test_grant_is_idempotent(self, staff, workspace):
        """A double-submitted form must not toggle the comp back off."""
        client = _client_for(staff)
        url = reverse("billing_staff:toggle_courtesy", kwargs={"target_workspace_id": workspace.id})
        client.post(url, {"action": "grant"})
        client.post(url, {"action": "grant"})
        sub = WorkspaceSubscription.objects.get(workspace=workspace)
        assert sub.status == WorkspaceSubscription.Status.COURTESY

    def test_unknown_action_changes_nothing(self, staff, workspace):
        services.ensure_subscription(workspace)
        _client_for(staff).post(
            reverse("billing_staff:toggle_courtesy", kwargs={"target_workspace_id": workspace.id}),
            {"action": "explode"},
        )
        sub = WorkspaceSubscription.objects.get(workspace=workspace)
        assert sub.status == WorkspaceSubscription.Status.PENDING

    def test_courtesy_keeps_disk_quota(self, staff, workspace):
        """A comped workspace still has the disk it would have paid for."""
        _client_for(staff).post(
            reverse("billing_staff:toggle_courtesy", kwargs={"target_workspace_id": workspace.id}),
            {"action": "grant"},
        )
        assert services.total_storage_bytes(workspace.pk) == services.base_storage_bytes()


@pytest.mark.django_db
class TestStaffCourtesyQuota:
    def test_setting_the_quota_persists_it(self, staff, org):
        client = _client_for(staff)
        response = client.post(
            reverse("billing_staff:set_courtesy_quota", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "3", "courtesy_note": "Agency partner"},
        )
        assert response.status_code == 302

        profile = OrganizationBillingProfile.objects.get(organization=org)
        assert profile.courtesy_workspace_quota == 3
        assert profile.courtesy_note == "Agency partner"
        assert profile.courtesy_workspaces_remaining == 3

    def test_negative_quota_is_refused(self, staff, org):
        services.ensure_billing_profile(org)
        _client_for(staff).post(
            reverse("billing_staff:set_courtesy_quota", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "-1"},
        )
        assert OrganizationBillingProfile.objects.get(organization=org).courtesy_workspace_quota == 0

    def test_non_numeric_quota_is_refused(self, staff, org):
        services.ensure_billing_profile(org)
        _client_for(staff).post(
            reverse("billing_staff:set_courtesy_quota", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "lots"},
        )
        assert OrganizationBillingProfile.objects.get(organization=org).courtesy_workspace_quota == 0

    def test_lowering_the_quota_keeps_existing_comps(self, staff, org, workspace):
        """Nobody starts being charged retroactively."""
        services.grant_courtesy(workspace)
        _client_for(staff).post(
            reverse("billing_staff:set_courtesy_quota", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "0"},
        )
        sub = WorkspaceSubscription.objects.get(workspace=workspace)
        assert sub.status == WorkspaceSubscription.Status.COURTESY
        assert services.courtesy_quota_exceeded(org) is True


# ---------------------------------------------------------------------------
# The 90% banner's context processor
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestStorageBannerContext:
    """``billing_storage`` appears only when there is something to say."""

    def _subscribe(self, workspace, *, base_bytes=10 * GB):
        sub = services.ensure_subscription(workspace)
        sub.status = WorkspaceSubscription.Status.ACTIVE
        sub.base_storage_bytes = base_bytes
        sub.save(update_fields=["status", "base_storage_bytes"])
        return sub

    def _asset(self, workspace, size):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.media_library.models import MediaAsset

        return MediaAsset.objects.create(
            organization=workspace.organization,
            workspace=workspace,
            filename="f.jpg",
            file=SimpleUploadedFile("f.jpg", b"x"),
            file_size=size,
            media_type=MediaAsset.MediaType.IMAGE,
        )

    def _context(self, user, workspace):
        from django.core.cache import cache

        cache.clear()
        client = _client_for(user)
        response = client.get(reverse("media_library:index", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 200
        return response.context

    def test_absent_well_under_the_limit(self, owner, workspace):
        self._subscribe(workspace)
        self._asset(workspace, 1 * GB)
        assert self._context(owner, workspace).get("billing_storage") is None

    def test_present_at_the_warning_threshold(self, owner, workspace):
        self._subscribe(workspace)
        self._asset(workspace, 9 * GB)
        status = self._context(owner, workspace).get("billing_storage")
        assert status is not None
        assert status.is_near_limit is True
        assert status.is_full is False

    def test_present_and_full_when_over(self, owner, workspace):
        self._subscribe(workspace)
        self._asset(workspace, 10 * GB)
        status = self._context(owner, workspace).get("billing_storage")
        assert status is not None
        assert status.is_full is True
        assert status.is_near_limit is False

    def test_absent_for_a_workspace_with_no_subscription(self, owner, workspace):
        """Limit 0 reads as full; a pre-billing workspace must not go red."""
        self._asset(workspace, 1 * GB)
        assert self._context(owner, workspace).get("billing_storage") is None
