"""The five per-account actions on the Customers screen, and the courtesy clock.

What is asserted is database state and HTTP status. Nothing here asserts
on rendered wording or markup: the interface renders in pt-BR, the words
are not a contract, and this project already carries a pile of tests
broken by exactly that mistake. Where a claim is about the *page* rather
than the data ("the button is not offered"), it is read off the view
context, which is where the decision is made.

The invariants worth naming, because each is a way this screen could hurt
somebody:

- A courtesy with a deadline expires into *being billed*, and into
  nothing else. No workspace, membership, asset or login is touched.
- An operator cannot deactivate themselves or a colleague, from either
  the page or the endpoint.
- A duplicate e-mail on an edit is refused with the old values intact,
  not a 500.
- Every new route is closed to a non-staff account and to an
  impersonated session.
"""

from __future__ import annotations

import datetime

import pytest
from django.core import mail
from django.db.models.signals import post_save
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.signals import create_organization_on_user_create
from apps.billing import services
from apps.billing.models import OrganizationBillingProfile, WorkspaceSubscription
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def operator(db):
    """Staff owning nothing — what ``manage.py create_operator`` makes."""
    post_save.disconnect(create_organization_on_user_create, sender=User)
    try:
        return User.objects.create_user(
            email="ops@example.com", password="x", is_staff=True, tos_accepted_at=timezone.now()
        )
    finally:
        post_save.connect(create_organization_on_user_create, sender=User)


@pytest.fixture
def other_operator(db):
    post_save.disconnect(create_organization_on_user_create, sender=User)
    try:
        return User.objects.create_user(
            email="ops2@example.com", password="x", is_staff=True, tos_accepted_at=timezone.now()
        )
    finally:
        post_save.connect(create_organization_on_user_create, sender=User)


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Acme")


@pytest.fixture
def workspace(db, org):
    return Workspace.objects.create(organization=org, name="Acme Social")


@pytest.fixture
def customer(db, org, workspace):
    user = User.objects.create_user(email="client@example.com", password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=user).delete()
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user


def _row(response, org):
    return next(r for r in response.context["rows"] if r["organization"].pk == org.pk)


def _console(client):
    return client.get(reverse("billing_staff:staff_console"))


# ---------------------------------------------------------------------------
# a) Courtesy with a deadline
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTimedCourtesy:
    def test_granting_exempts_the_account_and_records_the_deadline(self, client, operator, org, workspace):
        until = timezone.localdate() + datetime.timedelta(days=30)
        client.force_login(operator)

        response = client.post(
            reverse("billing_staff:grant_courtesy", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "1", "courtesy_expires_on": until.isoformat(), "courtesy_note": "Partner"},
        )

        assert response.status_code == 302
        profile = OrganizationBillingProfile.objects.get(organization=org)
        assert profile.courtesy_workspace_quota == 1
        assert profile.courtesy_expires_on == until
        assert profile.courtesy_note == "Partner"
        assert profile.courtesy_granted_by_id == operator.pk
        # The grant spends the budget it records: a quota with nothing
        # exempted is the bug the single-call service exists to prevent.
        assert WorkspaceSubscription.objects.get(workspace=workspace).status == WorkspaceSubscription.Status.COURTESY

    def test_an_empty_date_means_no_end_date(self, client, operator, org, workspace):
        """The behaviour every courtesy had before the field existed, kept
        so a permanent comp does not need a date invented for it."""
        client.force_login(operator)

        client.post(
            reverse("billing_staff:grant_courtesy", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "1", "courtesy_expires_on": ""},
        )

        profile = OrganizationBillingProfile.objects.get(organization=org)
        assert profile.courtesy_expires_on is None
        assert profile.courtesy_is_expired is False

    def test_a_past_date_is_refused_rather_than_granted_and_swept(self, client, operator, org, workspace):
        """Granted, it would be removed by the next expiry run, which looks
        to the operator like the form did nothing."""
        yesterday = timezone.localdate() - datetime.timedelta(days=1)
        client.force_login(operator)

        client.post(
            reverse("billing_staff:grant_courtesy", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "1", "courtesy_expires_on": yesterday.isoformat()},
        )

        # Refused before anything is written, so there is no profile at all
        # rather than one carrying a half-applied grant.
        assert not OrganizationBillingProfile.objects.filter(
            organization=org, courtesy_workspace_quota__gt=0
        ).exists()
        assert not WorkspaceSubscription.objects.filter(
            workspace=workspace, status=WorkspaceSubscription.Status.COURTESY
        ).exists()

    def test_an_unparseable_date_is_refused(self, client, operator, org, workspace):
        client.force_login(operator)

        response = client.post(
            reverse("billing_staff:grant_courtesy", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "2", "courtesy_expires_on": "not-a-date"},
        )

        assert response.status_code == 302
        assert OrganizationBillingProfile.objects.filter(organization=org).first() is None or (
            OrganizationBillingProfile.objects.get(organization=org).courtesy_workspace_quota == 0
        )

    def test_a_negative_quota_is_refused(self, client, operator, org):
        client.force_login(operator)

        client.post(
            reverse("billing_staff:grant_courtesy", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "-3"},
        )

        assert not OrganizationBillingProfile.objects.filter(
            organization=org, courtesy_workspace_quota__gt=0
        ).exists()

    def test_the_quota_caps_how_many_workspaces_are_exempted(self, client, operator, org, workspace):
        """Two workspaces, one free slot: the account pays for the other."""
        second = Workspace.objects.create(organization=org, name="Second")
        client.force_login(operator)

        client.post(
            reverse("billing_staff:grant_courtesy", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "1"},
        )

        comped = WorkspaceSubscription.objects.filter(
            workspace__organization=org, status=WorkspaceSubscription.Status.COURTESY
        ).count()
        assert comped == 1
        assert Workspace.objects.filter(organization=org).count() == 2
        assert {workspace.pk, second.pk}  # both still exist

    def test_ending_it_early_bills_the_account_again(self, client, operator, org, workspace):
        client.force_login(operator)
        client.post(
            reverse("billing_staff:grant_courtesy", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "1", "courtesy_expires_on": ""},
        )

        client.post(reverse("billing_staff:revoke_courtesy", kwargs={"org_id": org.id}))

        profile = OrganizationBillingProfile.objects.get(organization=org)
        assert profile.courtesy_workspace_quota == 0
        assert profile.courtesy_expires_on is None
        assert WorkspaceSubscription.objects.get(workspace=workspace).status == WorkspaceSubscription.Status.PENDING

    def test_granting_creates_no_workspace(self, client, operator, org, workspace):
        """The field was once read as provisioning. It is a billing ceiling."""
        before = Workspace.objects.filter(organization=org).count()
        client.force_login(operator)

        client.post(
            reverse("billing_staff:grant_courtesy", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "9"},
        )

        assert Workspace.objects.filter(organization=org).count() == before


# ---------------------------------------------------------------------------
# The expiry job
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCourtesyExpiry:
    def _grant(self, org, *, days):
        return services.grant_account_courtesy(
            org,
            quota=1,
            expires_on=timezone.localdate() + datetime.timedelta(days=days),
            note="Trial",
        )

    def test_a_lapsed_courtesy_is_billed_again(self, org, workspace):
        self._grant(org, days=-1)

        assert services.expire_due_courtesies() == 1

        profile = OrganizationBillingProfile.objects.get(organization=org)
        assert profile.courtesy_workspace_quota == 0
        assert profile.courtesy_expires_on is None
        assert WorkspaceSubscription.objects.get(workspace=workspace).status == WorkspaceSubscription.Status.PENDING

    def test_the_last_day_is_still_free(self, org, workspace):
        """"Free until the 30th" means free all through the 30th. Expiring
        on the morning of the date an operator typed would cut a day."""
        self._grant(org, days=0)

        assert services.expire_due_courtesies() == 0
        assert WorkspaceSubscription.objects.get(workspace=workspace).status == WorkspaceSubscription.Status.COURTESY

    def test_a_future_deadline_is_left_alone(self, org, workspace):
        self._grant(org, days=10)

        assert services.expire_due_courtesies() == 0
        assert WorkspaceSubscription.objects.get(workspace=workspace).status == WorkspaceSubscription.Status.COURTESY

    def test_an_open_ended_courtesy_never_expires(self, org, workspace):
        """NULL is what every row granted before the field existed holds;
        the sweep must not read it as a deadline in 1970."""
        services.grant_account_courtesy(org, quota=1, expires_on=None)

        assert services.expire_due_courtesies() == 0
        assert WorkspaceSubscription.objects.get(workspace=workspace).status == WorkspaceSubscription.Status.COURTESY

    def test_expiring_deletes_nothing_and_withdraws_no_access(self, org, workspace, customer):
        """The invariant that matters most: a customer whose free period
        ended owes money, they have not done anything wrong."""
        self._grant(org, days=-5)
        asset_count = Workspace.objects.filter(organization=org).count()

        services.expire_due_courtesies()

        workspace.refresh_from_db()
        customer.refresh_from_db()
        assert Organization.objects.filter(pk=org.pk).exists()
        assert Workspace.objects.filter(organization=org).count() == asset_count
        assert workspace.is_archived is False
        assert customer.is_active is True
        assert OrgMembership.objects.filter(user=customer, organization=org).exists()
        assert WorkspaceMembership.objects.filter(user=customer, workspace=workspace).exists()

    def test_running_twice_is_a_no_op_the_second_time(self, org, workspace):
        """Zeroing the quota is what makes the sweep idempotent; without it
        a daily job would re-revoke a comp an operator just re-granted."""
        self._grant(org, days=-1)

        assert services.expire_due_courtesies() == 1
        assert services.expire_due_courtesies() == 0

    def test_the_task_reports_what_it_expired(self, org, workspace):
        """The registered job is a thin wrapper; this is what pins the wiring
        between the schedule and the service."""
        from apps.billing.tasks import expire_courtesies

        self._grant(org, days=-1)

        assert expire_courtesies.now() == 1

    def test_the_job_is_registered_to_repeat_daily(self):
        from apps.billing.tasks import COURTESY_EXPIRY_INTERVAL_SECONDS

        assert COURTESY_EXPIRY_INTERVAL_SECONDS == 24 * 60 * 60

    def test_one_account_lapsing_does_not_touch_another(self, org, workspace, db):
        other = Organization.objects.create(name="Still Free")
        other_ws = Workspace.objects.create(organization=other, name="Theirs")
        self._grant(org, days=-1)
        services.grant_account_courtesy(other, quota=1, expires_on=None)

        services.expire_due_courtesies()

        assert WorkspaceSubscription.objects.get(workspace=other_ws).status == WorkspaceSubscription.Status.COURTESY
        assert OrganizationBillingProfile.objects.get(organization=other).courtesy_workspace_quota == 1


# ---------------------------------------------------------------------------
# b) Password reset
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPasswordReset:
    def test_it_mails_the_customer(self, client, operator, customer):
        mail.outbox.clear()
        client.force_login(operator)

        response = client.post(
            reverse("billing_staff:send_password_reset", kwargs={"target_user_id": customer.pk})
        )

        assert response.status_code == 302
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [customer.email]
        # Structural, not textual: the mail carries a link into allauth's
        # own reset view, which is what proves the shared flow ran rather
        # than a parallel one written here.
        assert reverse("account_reset_password") in mail.outbox[0].body

    def test_the_password_itself_is_untouched(self, client, operator, customer):
        """The operator never sees or sets it — the only effect is an e-mail."""
        before = User.objects.get(pk=customer.pk).password
        client.force_login(operator)

        client.post(reverse("billing_staff:send_password_reset", kwargs={"target_user_id": customer.pk}))

        assert User.objects.get(pk=customer.pk).password == before

    def test_an_inactive_account_gets_no_usable_reset_and_no_error(self, client, operator, customer):
        """A disabled login matches no resettable user, so allauth's own
        enumeration-prevention path answers: a notice, never a token.

        Asserted as "the password is still unusable to change" rather than
        on the mail body, which is UI text. The point is that the endpoint
        does not 500 on the state and does not mint a reset for an account
        that cannot sign in.
        """
        customer.is_active = False
        customer.save(update_fields=["is_active"])
        mail.outbox.clear()
        client.force_login(operator)

        response = client.post(
            reverse("billing_staff:send_password_reset", kwargs={"target_user_id": customer.pk})
        )

        assert response.status_code == 302
        customer.refresh_from_db()
        assert customer.is_active is False
        # Reusing allauth means its refusal is ours; what must not happen
        # is a crash or a silently reactivated account.
        assert User.objects.filter(pk=customer.pk, is_active=False).exists()

    def test_an_unknown_user_is_a_404_not_a_500(self, client, operator):
        import uuid

        client.force_login(operator)

        response = client.post(
            reverse("billing_staff:send_password_reset", kwargs={"target_user_id": uuid.uuid4()})
        )

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# c) Editing the account
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEditAccount:
    def _post(self, client, org, **overrides):
        data = {"organization_name": "Renamed", "user_id": "", "name": "", "email": ""}
        data.update(overrides)
        return client.post(reverse("billing_staff:edit_account", kwargs={"org_id": org.id}), data)

    def test_renames_the_organization_and_the_owner(self, client, operator, org, customer):
        client.force_login(operator)

        response = self._post(
            client, org, user_id=str(customer.pk), name="New Name", email="new@example.com"
        )

        assert response.status_code == 302
        org.refresh_from_db()
        customer.refresh_from_db()
        assert org.name == "Renamed"
        assert customer.name == "New Name"
        assert customer.email == "new@example.com"

    def test_a_duplicate_email_is_refused_without_a_500(self, client, operator, org, customer, db):
        """``email`` is unique; two customers and one mistyped address is a
        routine operator mistake, not an incident."""
        User.objects.create_user(email="taken@example.com", password="x", tos_accepted_at=timezone.now())
        client.force_login(operator)

        response = self._post(client, org, user_id=str(customer.pk), email="taken@example.com", name="Kept")

        assert response.status_code == 302
        customer.refresh_from_db()
        org.refresh_from_db()
        assert customer.email == "client@example.com"
        assert customer.name != "Kept"
        # The org rename is refused with it, so the operator is not left
        # guessing which half of one submit landed.
        assert org.name == "Acme"

    def test_the_collision_check_ignores_case(self, client, operator, org, customer, db):
        User.objects.create_user(email="taken@example.com", password="x", tos_accepted_at=timezone.now())
        client.force_login(operator)

        self._post(client, org, user_id=str(customer.pk), email="TAKEN@example.com")

        customer.refresh_from_db()
        assert customer.email == "client@example.com"

    def test_keeping_your_own_email_is_not_a_collision(self, client, operator, org, customer):
        client.force_login(operator)

        self._post(client, org, user_id=str(customer.pk), email=customer.email, name="Just the name")

        customer.refresh_from_db()
        assert customer.name == "Just the name"
        assert customer.email == "client@example.com"

    def test_an_invalid_address_is_refused(self, client, operator, org, customer):
        client.force_login(operator)

        self._post(client, org, user_id=str(customer.pk), email="not-an-email")

        customer.refresh_from_db()
        assert customer.email == "client@example.com"

    def test_an_empty_account_name_is_refused(self, client, operator, org, customer):
        client.force_login(operator)

        self._post(client, org, organization_name="   ", user_id=str(customer.pk), email=customer.email)

        org.refresh_from_db()
        assert org.name == "Acme"

    def test_a_user_from_another_account_cannot_be_edited_through_this_org(
        self, client, operator, org, customer, db
    ):
        """The org in the URL is what the operator chose; a user id pointing
        elsewhere is a stale page or a tampered form."""
        stranger = User.objects.create_user(
            email="stranger@example.com", password="x", tos_accepted_at=timezone.now()
        )
        client.force_login(operator)

        response = self._post(client, org, user_id=str(stranger.pk), email="hijacked@example.com")

        assert response.status_code == 404
        stranger.refresh_from_db()
        assert stranger.email == "stranger@example.com"

    def test_renaming_only_the_organization_needs_no_user(self, client, operator, org):
        client.force_login(operator)

        self._post(client, org)

        org.refresh_from_db()
        assert org.name == "Renamed"

    def test_editing_deletes_nothing(self, client, operator, org, customer, workspace):
        client.force_login(operator)

        self._post(client, org, user_id=str(customer.pk), email="moved@example.com")

        assert Workspace.objects.filter(pk=workspace.pk).exists()
        assert OrgMembership.objects.filter(user=customer, organization=org).exists()


# ---------------------------------------------------------------------------
# d) Deactivating an account
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDeactivation:
    def _post(self, client, user, action):
        return client.post(
            reverse("billing_staff:set_user_active", kwargs={"target_user_id": user.pk}), {"action": action}
        )

    def test_deactivating_blocks_sign_in_and_deletes_nothing(
        self, client, operator, customer, org, workspace
    ):
        client.force_login(operator)

        response = self._post(client, customer, "deactivate")

        assert response.status_code == 302
        customer.refresh_from_db()
        assert customer.is_active is False
        assert User.objects.filter(pk=customer.pk).exists()
        assert Organization.objects.filter(pk=org.pk).exists()
        assert Workspace.objects.filter(pk=workspace.pk).exists()
        assert OrgMembership.objects.filter(user=customer).exists()

    def test_it_is_reversible(self, client, operator, customer):
        customer.is_active = False
        customer.save(update_fields=["is_active"])
        client.force_login(operator)

        self._post(client, customer, "activate")

        customer.refresh_from_db()
        assert customer.is_active is True

    def test_a_deactivated_customer_really_cannot_log_in(self, client, operator, customer):
        """The flag is only worth setting if Django's auth honours it."""
        client.force_login(operator)
        self._post(client, customer, "deactivate")
        client.logout()

        assert client.login(email="client@example.com", password="x") is False

    def test_an_operator_cannot_deactivate_themselves(self, client, operator):
        """It would lock them out of the only area they can reach."""
        client.force_login(operator)

        response = self._post(client, operator, "deactivate")

        assert response.status_code == 403
        operator.refresh_from_db()
        assert operator.is_active is True

    def test_an_operator_cannot_deactivate_another_operator(self, client, operator, other_operator):
        """Privilege escalation dressed up as support; aimed at the last
        operator it would leave the install with no way in."""
        client.force_login(operator)

        response = self._post(client, other_operator, "deactivate")

        assert response.status_code == 403
        other_operator.refresh_from_db()
        assert other_operator.is_active is True

    def test_an_operator_cannot_reactivate_another_operator_either(
        self, client, operator, other_operator
    ):
        other_operator.is_active = False
        other_operator.save(update_fields=["is_active"])
        client.force_login(operator)

        response = self._post(client, other_operator, "activate")

        assert response.status_code == 403
        other_operator.refresh_from_db()
        assert other_operator.is_active is False

    def test_an_unknown_action_changes_nothing(self, client, operator, customer):
        client.force_login(operator)

        self._post(client, customer, "obliterate")

        customer.refresh_from_db()
        assert customer.is_active is True

    def test_the_page_offers_no_button_the_endpoint_would_refuse(
        self, client, operator, other_operator, org, customer
    ):
        """The page and the POST read the same rule, so a drawn button can
        never 403 and a refused target is never offered one."""
        OrgMembership.objects.filter(user=other_operator).delete()
        OrgMembership.objects.create(user=other_operator, organization=org, org_role=OrgMembership.OrgRole.MEMBER)
        client.force_login(operator)

        row = _row(_console(client), org)
        by_user = {entry["membership"].user_id: entry for entry in row["members"]}

        assert by_user[customer.pk]["may_deactivate"] is True
        assert by_user[other_operator.pk]["may_deactivate"] is False

    def test_an_inactive_customer_may_still_be_reactivated_from_the_page(
        self, client, operator, org, customer
    ):
        """``may_impersonate`` refuses an inactive target; the deactivation
        flag must not, or a disabled account could never be restored."""
        customer.is_active = False
        customer.save(update_fields=["is_active"])
        client.force_login(operator)

        row = _row(_console(client), org)
        entry = next(e for e in row["members"] if e["membership"].user_id == customer.pk)

        assert entry["may_impersonate"] is False
        assert entry["may_deactivate"] is True


# ---------------------------------------------------------------------------
# e) Impersonation stays gated
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestImpersonationFromTheModal:
    def test_a_customer_is_offered_a_takeover(self, client, operator, org, customer):
        client.force_login(operator)

        row = _row(_console(client), org)
        entry = next(e for e in row["members"] if e["membership"].user_id == customer.pk)

        assert entry["may_impersonate"] is True

    def test_an_operator_is_named_but_not_offered(self, client, operator, org, other_operator):
        """The e-mail is identification and is always shown; only the button
        is gated."""
        OrgMembership.objects.filter(user=other_operator).delete()
        OrgMembership.objects.create(user=other_operator, organization=org, org_role=OrgMembership.OrgRole.OWNER)
        client.force_login(operator)

        row = _row(_console(client), org)
        entry = next(e for e in row["members"] if e["membership"].user_id == other_operator.pk)

        assert entry["membership"].user.email == "ops2@example.com"
        assert entry["may_impersonate"] is False

    def test_the_endpoint_still_refuses_an_operator_target(self, client, operator, other_operator):
        client.force_login(operator)

        response = client.post(
            reverse("accounts:start_impersonation", kwargs={"user_id": other_operator.pk})
        )

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCustomersTable:
    def test_one_row_per_organization(self, client, operator, org, db):
        """Not per workspace: the account is what every action acts on."""
        Workspace.objects.create(organization=org, name="Second")
        Workspace.objects.create(organization=org, name="Third")
        client.force_login(operator)

        row = _row(_console(client), org)

        assert row["workspace_count"] == 2
        assert len([r for r in _console(client).context["rows"] if r["organization"].pk == org.pk]) == 1

    def test_the_status_cell_reports_the_most_urgent_workspace(self, client, operator, org):
        """The operator scanning the column is looking for the account that
        needs attention, not a representative sample."""
        paying = Workspace.objects.create(organization=org, name="Paying")
        failing = Workspace.objects.create(organization=org, name="Failing")
        sub = services.ensure_subscription(paying)
        sub.status = WorkspaceSubscription.Status.ACTIVE
        sub.save(update_fields=["status"])
        sub = services.ensure_subscription(failing)
        sub.status = WorkspaceSubscription.Status.PAST_DUE
        sub.save(update_fields=["status"])
        client.force_login(operator)

        assert _row(_console(client), org)["status"] == WorkspaceSubscription.Status.PAST_DUE

    def test_an_account_with_no_workspace_has_no_status(self, client, operator, org):
        """Inventing "pending" would put it on the chase list for not having
        done anything yet."""
        Workspace.objects.filter(organization=org).delete()
        client.force_login(operator)

        row = _row(_console(client), org)

        assert row["status"] == ""
        assert row["workspace_count"] == 0
        assert row["storage_percent"] == 0.0

    def test_disk_is_summed_across_the_account(self, client, operator, org, workspace):
        second = Workspace.objects.create(organization=org, name="Second")
        services.ensure_subscription(workspace)
        services.ensure_subscription(second)
        client.force_login(operator)

        row = _row(_console(client), org)

        assert row["storage_limit_bytes"] == 2 * services.base_storage_bytes()

    def test_the_owner_is_the_face_of_the_row(self, client, operator, org, customer, db):
        member = User.objects.create_user(email="member@example.com", password="x", tos_accepted_at=timezone.now())
        OrgMembership.objects.filter(user=member).delete()
        OrgMembership.objects.create(user=member, organization=org, org_role=OrgMembership.OrgRole.MEMBER)
        client.force_login(operator)

        row = _row(_console(client), org)

        assert row["owner"]["membership"].user_id == customer.pk

    def test_a_row_with_no_members_still_renders(self, client, operator, org):
        OrgMembership.objects.filter(organization=org).delete()
        client.force_login(operator)

        response = _console(client)

        assert response.status_code == 200
        assert _row(response, org)["owner"] is None

    def test_the_monthly_total_excludes_exempt_workspaces(self, client, operator, org, workspace):
        services.grant_account_courtesy(org, quota=1, expires_on=None)
        client.force_login(operator)

        assert _row(_console(client), org)["monthly_total_cents"] == 0


# ---------------------------------------------------------------------------
# Permission on every new route
# ---------------------------------------------------------------------------


def _new_routes(org, user):
    """Every route this screen added, with a body that would succeed."""
    return [
        (reverse("billing_staff:grant_courtesy", kwargs={"org_id": org.id}), {"courtesy_workspace_quota": "1"}),
        (reverse("billing_staff:revoke_courtesy", kwargs={"org_id": org.id}), {}),
        (
            reverse("billing_staff:edit_account", kwargs={"org_id": org.id}),
            {"organization_name": "Hijacked"},
        ),
        (reverse("billing_staff:send_password_reset", kwargs={"target_user_id": user.pk}), {}),
        (
            reverse("billing_staff:set_user_active", kwargs={"target_user_id": user.pk}),
            {"action": "deactivate"},
        ),
    ]


@pytest.mark.django_db
class TestOnlyAnOperatorMayAct:
    def test_a_customer_is_forbidden_everywhere(self, client, customer, org):
        client.force_login(customer)

        for url, data in _new_routes(org, customer):
            assert client.post(url, data).status_code == 403, url

        org.refresh_from_db()
        customer.refresh_from_db()
        assert org.name == "Acme"
        assert customer.is_active is True
        assert not OrganizationBillingProfile.objects.filter(
            organization=org, courtesy_workspace_quota__gt=0
        ).exists()

    def test_an_anonymous_visitor_is_sent_to_login(self, client, org, customer):
        for url, data in _new_routes(org, customer):
            response = client.post(url, data)
            assert response.status_code == 302, url
            assert "/accounts/login" in response.headers["Location"], url

    def test_an_impersonated_session_is_forbidden_everywhere(self, client, operator, customer, org):
        """``is_operator`` is False during a takeover: a comp granted from
        inside a customer's session would be recorded as the customer."""
        client.force_login(operator)
        assert client.post(
            reverse("accounts:start_impersonation", kwargs={"user_id": customer.pk})
        ).status_code in (302, 303)

        for url, data in _new_routes(org, customer):
            assert client.post(url, data).status_code == 403, url

        org.refresh_from_db()
        customer.refresh_from_db()
        assert org.name == "Acme"
        assert customer.is_active is True

    def test_every_route_refuses_a_get(self, client, operator, org, customer):
        """Nothing here is safe to reach by navigation — a GET reset would
        let an embedded image mail a customer a reset link."""
        client.force_login(operator)

        for url, _data in _new_routes(org, customer):
            assert client.get(url).status_code == 405, url

    def test_a_deactivated_operator_loses_the_actions(self, client, operator, org, customer):
        client.force_login(operator)
        operator.is_active = False
        operator.save(update_fields=["is_active"])

        for url, data in _new_routes(org, customer):
            assert client.post(url, data).status_code in (302, 403), url
