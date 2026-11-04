"""Tests for operator impersonation, the most dangerous feature here.

Every assertion is about session identity, permission outcome or the
audit row. Nothing asserts on rendered text — not even the banner, whose
wording is pt-BR and will change. What *is* asserted about the banner is
the thing a wording change must never break: that the context flag which
makes it render is set during a takeover and absent outside one.

The six rules under test, from the brief:

1. only ``is_staff`` starts one, checked against the *real* user;
2. no operator takes over another operator;
3. the session really becomes the customer;
4. stopping restores the operator without a re-login;
5. the audit trail records both start and end;
6. the impersonated session does not reach the staff console.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import SESSION_KEY
from django.urls import reverse
from django.utils import timezone

from apps.accounts import impersonation
from apps.accounts.models import ImpersonationSession, User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

STAFF_CONSOLE = "/billing/staff/"


def _staff_user(email):
    """Staff with nothing attached — an operator account."""
    from django.db.models.signals import post_save

    from apps.accounts.signals import create_organization_on_user_create

    post_save.disconnect(create_organization_on_user_create, sender=User)
    try:
        return User.objects.create_user(
            email=email, password="x", is_staff=True, tos_accepted_at=timezone.now()
        )
    finally:
        post_save.connect(create_organization_on_user_create, sender=User)


@pytest.fixture
def operator(db):
    return _staff_user("ops@example.com")


@pytest.fixture
def other_operator(db):
    return _staff_user("ops2@example.com")


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


def _start_url(user):
    return reverse("accounts:start_impersonation", kwargs={"user_id": user.pk})


STOP_URL = reverse("accounts:stop_impersonation")


def _session_user_id(client):
    """Whose identity the session cookie currently carries."""
    return client.session.get(SESSION_KEY)


# ---------------------------------------------------------------------------
# 1. Only an operator may start one
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOnlyStaffStarts:
    def test_customer_is_refused(self, client, customer, db):
        victim = User.objects.create_user(
            email="victim@example.com", password="x", tos_accepted_at=timezone.now()
        )
        client.force_login(customer)

        response = client.post(_start_url(victim))

        assert response.status_code == 403
        assert _session_user_id(client) == str(customer.pk)
        assert ImpersonationSession.objects.count() == 0

    def test_anonymous_is_sent_to_login(self, client, customer):
        response = client.post(_start_url(customer))

        assert response.status_code == 302
        assert "/accounts/login/" in response.url
        assert ImpersonationSession.objects.count() == 0

    def test_get_is_rejected(self, client, operator, customer):
        """A GET would let a link or an <img> perform a takeover."""
        client.force_login(operator)

        response = client.get(_start_url(customer))

        assert response.status_code == 405
        assert ImpersonationSession.objects.count() == 0

    def test_inactive_operator_is_refused(self, client, operator, customer):
        client.force_login(operator)
        operator.is_active = False
        operator.save(update_fields=["is_active"])

        response = client.post(_start_url(customer))

        assert response.status_code in (302, 403)
        assert ImpersonationSession.objects.count() == 0


# ---------------------------------------------------------------------------
# 2. No operator takes over another operator
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestNoStaffOnStaff:
    def test_refused(self, client, operator, other_operator):
        client.force_login(operator)

        response = client.post(_start_url(other_operator))

        assert response.status_code == 403
        assert _session_user_id(client) == str(operator.pk)
        assert ImpersonationSession.objects.count() == 0

    def test_service_refuses_too(self, operator, other_operator):
        assert impersonation.may_impersonate(operator, other_operator) is False

    def test_self_impersonation_is_refused(self, client, operator):
        client.force_login(operator)

        response = client.post(_start_url(operator))

        assert response.status_code == 403
        assert ImpersonationSession.objects.count() == 0

    def test_console_does_not_offer_a_staff_target(self, client, operator, other_operator, db):
        """A staff member who is also in a customer org must not be offered
        for takeover, or the page would show a button that 403s.

        They are still *listed*: the e-mail is how an operator identifies
        the account, so it is shown for every member and only the button
        is gated. ``may_impersonate`` is the per-row flag that gates it.
        """
        org = Organization.objects.create(name="Mixed Co")
        OrgMembership.objects.create(
            user=other_operator, organization=org, org_role=OrgMembership.OrgRole.OWNER
        )
        client.force_login(operator)

        response = client.get(STAFF_CONSOLE)

        rows = [entry for row in response.context["rows"] for entry in row["members"]]
        offered = {e["membership"].user_id for e in rows if e["may_impersonate"]}
        listed = {e["membership"].user_id for e in rows}

        assert other_operator.pk not in offered
        assert other_operator.pk in listed

    def test_console_offers_a_customer_target(self, client, operator, customer):
        client.force_login(operator)

        response = client.get(STAFF_CONSOLE)

        rows = [entry for row in response.context["rows"] for entry in row["members"]]
        offered = {e["membership"].user_id for e in rows if e["may_impersonate"]}
        assert customer.pk in offered


# ---------------------------------------------------------------------------
# 3. The session becomes the customer
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestImpersonatedSessionIdentity:
    def test_session_carries_the_customer(self, client, operator, customer):
        client.force_login(operator)

        client.post(_start_url(customer))

        assert _session_user_id(client) == str(customer.pk)

    def test_operator_is_remembered_in_the_session(self, client, operator, customer):
        client.force_login(operator)

        client.post(_start_url(customer))

        assert client.session[impersonation.IMPERSONATOR_SESSION_KEY] == str(operator.pk)

    def test_requests_are_served_as_the_customer(self, client, operator, customer):
        client.force_login(operator)
        client.post(_start_url(customer))

        response = client.get(reverse("accounts:settings"))

        assert response.status_code == 200
        assert response.context["user"].pk == customer.pk

    def test_banner_flag_is_set(self, client, operator, customer):
        """The context key the banner keys off, not its wording."""
        client.force_login(operator)
        client.post(_start_url(customer))

        response = client.get(reverse("accounts:settings"))

        assert response.context["impersonation"]["operator"].pk == operator.pk
        assert response.context["impersonation"]["impersonated"].pk == customer.pk

    def test_banner_flag_is_absent_outside_an_impersonation(self, client, operator):
        client.force_login(operator)

        response = client.get(reverse("accounts:settings"))

        assert "impersonation" not in response.context

    def test_takeover_cannot_be_chained(self, client, operator, customer, db):
        """An operator already inside an account must not jump to a second
        one — the original operator would be lost from the session."""
        second = User.objects.create_user(
            email="client2@example.com", password="x", tos_accepted_at=timezone.now()
        )
        client.force_login(operator)
        client.post(_start_url(customer))

        response = client.post(_start_url(second))

        assert response.status_code == 403
        assert _session_user_id(client) == str(customer.pk)
        assert client.session[impersonation.IMPERSONATOR_SESSION_KEY] == str(operator.pk)


# ---------------------------------------------------------------------------
# 4. Stopping restores the operator
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestStopRestoresTheOperator:
    def test_session_is_the_operator_again(self, client, operator, customer):
        client.force_login(operator)
        client.post(_start_url(customer))

        client.post(STOP_URL)

        assert _session_user_id(client) == str(operator.pk)

    def test_no_relogin_is_required(self, client, operator, customer):
        client.force_login(operator)
        client.post(_start_url(customer))
        client.post(STOP_URL)

        response = client.get(STAFF_CONSOLE)

        assert response.status_code == 200

    def test_impersonation_keys_are_cleared(self, client, operator, customer):
        client.force_login(operator)
        client.post(_start_url(customer))

        client.post(STOP_URL)

        assert impersonation.IMPERSONATOR_SESSION_KEY not in client.session
        assert impersonation.RECORD_SESSION_KEY not in client.session

    def test_stopping_twice_is_harmless(self, client, operator, customer):
        client.force_login(operator)
        client.post(_start_url(customer))
        client.post(STOP_URL)

        response = client.post(STOP_URL)

        assert response.status_code == 302
        assert _session_user_id(client) == str(operator.pk)

    def test_stop_without_impersonating_does_not_log_anybody_out(self, client, operator):
        client.force_login(operator)

        client.post(STOP_URL)

        assert _session_user_id(client) == str(operator.pk)


# ---------------------------------------------------------------------------
# 5. The audit trail
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditTrail:
    def test_start_is_recorded(self, client, operator, customer):
        client.force_login(operator)

        client.post(_start_url(customer))

        record = ImpersonationSession.objects.get()
        assert record.impersonator_id == operator.pk
        assert record.impersonated_id == customer.pk
        assert record.started_at is not None
        assert record.ended_at is None
        assert record.is_open is True

    def test_emails_are_snapshotted(self, client, operator, customer):
        """So the trail still names both parties after an account is deleted."""
        client.force_login(operator)

        client.post(_start_url(customer))

        record = ImpersonationSession.objects.get()
        assert record.impersonator_email == operator.email
        assert record.impersonated_email == customer.email

    def test_end_is_recorded(self, client, operator, customer):
        client.force_login(operator)
        client.post(_start_url(customer))

        client.post(STOP_URL)

        record = ImpersonationSession.objects.get()
        assert record.ended_at is not None
        assert record.is_open is False
        assert record.ended_at >= record.started_at

    def test_trail_survives_the_customer_being_deleted(self, client, operator, customer):
        client.force_login(operator)
        client.post(_start_url(customer))
        client.post(STOP_URL)

        customer.delete()

        record = ImpersonationSession.objects.get()
        assert record.impersonated is None
        assert record.impersonated_email == "client@example.com"

    def test_each_takeover_is_its_own_row(self, client, operator, customer):
        client.force_login(operator)
        client.post(_start_url(customer))
        client.post(STOP_URL)
        client.post(_start_url(customer))
        client.post(STOP_URL)

        assert ImpersonationSession.objects.count() == 2
        assert ImpersonationSession.objects.filter(ended_at__isnull=True).count() == 0

    def test_refused_attempt_leaves_no_row(self, client, operator, other_operator):
        client.force_login(operator)

        client.post(_start_url(other_operator))

        assert ImpersonationSession.objects.count() == 0


# ---------------------------------------------------------------------------
# 6. The impersonated session carries no operator privilege
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestImpersonatedSessionHasNoStaffPower:
    def test_staff_console_is_closed(self, client, operator, customer):
        client.force_login(operator)
        client.post(_start_url(customer))

        response = client.get(STAFF_CONSOLE)

        assert response.status_code == 403

    def test_courtesy_cannot_be_granted(self, client, operator, customer):
        """The money action, from inside a customer's identity. Must refuse."""
        from apps.billing.models import WorkspaceSubscription

        workspace = Workspace.objects.get(name="Client Social")
        client.force_login(operator)
        client.post(_start_url(customer))

        response = client.post(
            reverse("billing_staff:toggle_courtesy", kwargs={"target_workspace_id": workspace.pk}),
            {"action": "grant"},
        )

        assert response.status_code == 403
        subscription = WorkspaceSubscription.objects.filter(workspace=workspace).first()
        assert subscription is None or subscription.status != WorkspaceSubscription.Status.COURTESY

    def test_quota_cannot_be_changed(self, client, operator, customer):
        org = Organization.objects.get(name="Client Co")
        client.force_login(operator)
        client.post(_start_url(customer))

        response = client.post(
            reverse("billing_staff:set_courtesy_quota", kwargs={"org_id": org.pk}),
            {"courtesy_workspace_quota": "99"},
        )

        assert response.status_code == 403

    def test_is_operator_is_false_while_impersonating(self, client, operator, customer):
        client.force_login(operator)
        client.post(_start_url(customer))

        response = client.get(reverse("accounts:settings"))

        assert impersonation.is_operator(response.wsgi_request) is False

    def test_console_reopens_after_stopping(self, client, operator, customer):
        client.force_login(operator)
        client.post(_start_url(customer))
        assert client.get(STAFF_CONSOLE).status_code == 403

        client.post(STOP_URL)

        assert client.get(STAFF_CONSOLE).status_code == 200


# ---------------------------------------------------------------------------
# Privilege is re-read, never snapshotted
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPrivilegeIsNotSnapshotted:
    def test_demoting_the_operator_mid_takeover_removes_their_power(
        self, client, operator, customer
    ):
        """``real_user`` re-reads the operator every request, so a demotion
        takes effect on the next one instead of at session expiry."""
        client.force_login(operator)
        client.post(_start_url(customer))

        operator.is_staff = False
        operator.save(update_fields=["is_staff"])

        client.post(STOP_URL)

        assert client.get(STAFF_CONSOLE).status_code == 403

    def test_deactivating_the_operator_mid_takeover_ends_the_session(
        self, client, operator, customer
    ):
        client.force_login(operator)
        client.post(_start_url(customer))

        operator.is_active = False
        operator.save(update_fields=["is_active"])
        client.post(STOP_URL)

        # Neither the operator nor the customer: the session is gone.
        assert _session_user_id(client) is None
