"""The isolated administrative area: who reaches it, and what it survives.

Three screens behind ``_staff_required`` — Customers (the old single
console), Plan and prices, Access log — rendered in
``templates/layouts/admin_shell.html`` instead of ``base.html``.

What is asserted here is behaviour, permission outcome and context
state. Nothing asserts on rendered wording or markup: the interface
renders in pt-BR, the words are not a contract, and this project already
carries a pile of tests broken by exactly that mistake. The one thing the
isolation claim needs is checked structurally rather than textually —
which *template* answered, and the fact that the page renders at all for
an account with no workspace, which is what a stray ``request.workspace``
dereference in the chrome would break.
"""

from __future__ import annotations

import pytest
from django.db.models.signals import post_save
from django.urls import reverse
from django.utils import timezone

from apps.accounts import impersonation
from apps.accounts.models import ImpersonationSession, User
from apps.accounts.signals import create_organization_on_user_create
from apps.billing import services
from apps.billing.models import WorkspaceAddon, WorkspaceSubscription
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

GB = WorkspaceAddon.BYTES_PER_GB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def operator(db):
    """Staff owning nothing — what ``manage.py create_operator`` makes.

    The provisioning signal is disconnected around creation for the same
    reason the command bypasses it: an operator with an organization is
    the coupling the separate account exists to undo, and a test fixture
    that quietly has one would hide every bug this file looks for.
    """
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
def org(db):
    return Organization.objects.create(name="Acme")


@pytest.fixture
def workspace(db, org):
    return Workspace.objects.create(organization=org, name="Acme Social")


@pytest.fixture
def customer(db, org, workspace):
    user = User.objects.create_user(email="client@example.com", password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user


# Every GET-able screen in the area, so a new one added without a
# permission test shows up as a missing entry here rather than as an
# open door in production.
ADMIN_SCREENS = (
    "billing_staff:staff_console",
    "billing_staff:plans",
    "billing_staff:impersonation_log",
)


def _urls():
    return [reverse(name) for name in ADMIN_SCREENS]


# ---------------------------------------------------------------------------
# Who may reach the area
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAdminAreaAccess:
    @pytest.mark.parametrize("name", ADMIN_SCREENS)
    def test_operator_reaches_every_screen(self, client, operator, name):
        client.force_login(operator)

        assert client.get(reverse(name)).status_code == 200

    @pytest.mark.parametrize("name", ADMIN_SCREENS)
    def test_anonymous_is_sent_to_login(self, client, name):
        response = client.get(reverse(name))

        assert response.status_code == 302
        assert "/accounts/login" in response.headers["Location"]

    @pytest.mark.parametrize("name", ADMIN_SCREENS)
    def test_customer_is_forbidden(self, client, customer, name):
        """Being the owner of your own account grants nothing here.

        403 and not a redirect: ``_staff_required`` raises
        PermissionDenied so an authenticated member who found the URL
        gets a refusal instead of bouncing off the login page forever.
        """
        client.force_login(customer)

        assert client.get(reverse(name)).status_code == 403

    @pytest.mark.parametrize("name", ADMIN_SCREENS)
    def test_deactivated_operator_is_refused(self, client, operator, name):
        client.force_login(operator)
        operator.is_active = False
        operator.save(update_fields=["is_active"])

        # force_login already wrote the session, so the staff check is
        # what has to refuse — a 200 would mean the flag is read once at
        # login and cached for the life of the session.
        assert client.get(reverse(name)).status_code in (302, 403)


# ---------------------------------------------------------------------------
# Impersonation closes the area
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestImpersonatedSessionIsLockedOut:
    """``is_operator`` is False during a takeover, and must stay that way.

    The rule is in apps/accounts/impersonation; what this covers is that
    the two screens added alongside the console inherited it, rather than
    each re-deciding and one of them getting it wrong.
    """

    def _impersonate(self, client, operator, customer):
        client.force_login(operator)
        response = client.post(reverse("accounts:start_impersonation", kwargs={"user_id": customer.pk}))
        assert response.status_code in (302, 303)
        return response

    @pytest.mark.parametrize("name", ADMIN_SCREENS)
    def test_every_screen_refuses_an_impersonated_session(self, client, operator, customer, name):
        self._impersonate(client, operator, customer)

        assert client.get(reverse(name)).status_code == 403

    @pytest.mark.parametrize("name", ADMIN_SCREENS)
    def test_stopping_the_takeover_reopens_the_area(self, client, operator, customer, name):
        """Otherwise the lockout would be a one-way door for the operator."""
        self._impersonate(client, operator, customer)
        client.post(reverse("accounts:stop_impersonation"))

        assert client.get(reverse(name)).status_code == 200

    def test_the_write_actions_are_closed_too(self, client, operator, customer, workspace):
        """A comp granted from inside a customer's session would be
        recorded against the customer's identity."""
        self._impersonate(client, operator, customer)

        response = client.post(
            reverse("billing_staff:toggle_courtesy", kwargs={"target_workspace_id": workspace.id}),
            {"action": "grant"},
        )

        assert response.status_code == 403
        assert not WorkspaceSubscription.objects.filter(
            workspace=workspace, status=WorkspaceSubscription.Status.COURTESY
        ).exists()


# ---------------------------------------------------------------------------
# Who the account belongs to
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCustomersScreenIdentifiesTheAccount:
    """The e-mails are the identification, so they cannot be conditional.

    The product owner identifies a customer by e-mail — an organization
    name like "My Organization" names a product default, not a person.
    The list used to be pre-filtered to impersonable accounts, so an
    organization whose only member is staff showed a card with no human
    on it at all.

    Asserted on the context, never on the rendered HTML: what matters is
    that the data reaches the template for every member and that the
    takeover flag is per row.
    """

    def _entries(self, response):
        return [entry for row in response.context["rows"] for entry in row["members"]]

    def test_lists_every_member_of_an_account(self, client, operator, org, workspace):
        first = User.objects.create_user(email="one@example.com", password="x", tos_accepted_at=timezone.now())
        second = User.objects.create_user(email="two@example.com", password="x", tos_accepted_at=timezone.now())
        for user, role in ((first, OrgMembership.OrgRole.OWNER), (second, OrgMembership.OrgRole.MEMBER)):
            OrgMembership.objects.filter(user=user).delete()
            OrgMembership.objects.create(user=user, organization=org, org_role=role)
        client.force_login(operator)

        entries = self._entries(client.get(reverse("billing_staff:staff_console")))
        emails = {entry["membership"].user.email for entry in entries}

        assert "one@example.com" in emails
        assert "two@example.com" in emails

    def test_a_staff_member_is_still_listed_just_not_offered(self, client, operator, org, db):
        """The regression that motivated the change: an account whose only
        member is another operator used to render with nobody named."""
        other = User.objects.create_user(
            email="ops2@example.com", password="x", is_staff=True, tos_accepted_at=timezone.now()
        )
        OrgMembership.objects.filter(user=other).delete()
        OrgMembership.objects.create(user=other, organization=org, org_role=OrgMembership.OrgRole.OWNER)
        client.force_login(operator)

        response = client.get(reverse("billing_staff:staff_console"))
        row = next(r for r in response.context["rows"] if r["organization"].pk == org.pk)
        entry = next(e for e in row["members"] if e["membership"].user_id == other.pk)

        assert entry["membership"].user.email == "ops2@example.com"
        assert entry["may_impersonate"] is False
        # No button anywhere on the card, so the heading must be gone too.
        assert row["has_impersonable_members"] is False

    def test_a_customer_is_listed_and_offered(self, client, operator, customer, org):
        client.force_login(operator)

        response = client.get(reverse("billing_staff:staff_console"))
        row = next(r for r in response.context["rows"] if r["organization"].pk == org.pk)
        entry = next(e for e in row["members"] if e["membership"].user_id == customer.pk)

        assert entry["may_impersonate"] is True
        assert row["has_impersonable_members"] is True

    def test_the_operator_is_not_offered_their_own_account(self, client, operator, org, db):
        """``may_impersonate`` refuses self-takeover; the flag has to say so
        rather than the page offering a button that 403s."""
        OrgMembership.objects.filter(user=operator).delete()
        OrgMembership.objects.create(user=operator, organization=org, org_role=OrgMembership.OrgRole.OWNER)
        client.force_login(operator)

        response = client.get(reverse("billing_staff:staff_console"))
        row = next(r for r in response.context["rows"] if r["organization"].pk == org.pk)
        entry = next(e for e in row["members"] if e["membership"].user_id == operator.pk)

        assert entry["may_impersonate"] is False

    def test_an_account_with_no_members_still_renders(self, client, operator, org):
        """An organization can outlive its last member; the card must not
        500 on an empty list."""
        OrgMembership.objects.filter(organization=org).delete()
        client.force_login(operator)

        response = client.get(reverse("billing_staff:staff_console"))
        row = next(r for r in response.context["rows"] if r["organization"].pk == org.pk)

        assert response.status_code == 200
        assert row["members"] == []
        assert row["has_impersonable_members"] is False


# ---------------------------------------------------------------------------
# The exemption limit creates nothing
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestExemptionLimitIsNotProvisioning:
    """The field was read as granting workspaces. It is a billing ceiling.

    The wording fix is not testable without asserting on UI text, which
    this project forbids. What *is* asserted is the behaviour the wording
    now claims — that setting it creates no workspace, grants no
    exemption by itself, and never bills anyone retroactively. If a later
    change made the field actually provision something, the label would
    become true and these tests would fail.
    """

    def test_raising_the_limit_creates_no_workspace(self, client, operator, org, workspace):
        before = Workspace.objects.filter(organization=org).count()
        client.force_login(operator)

        client.post(
            reverse("billing_staff:set_courtesy_quota", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "9", "courtesy_note": "Partner"},
        )

        assert Workspace.objects.filter(organization=org).count() == before

    def test_raising_the_limit_exempts_nothing_on_its_own(self, client, operator, org, workspace):
        """The ceiling is permission to exempt, not an exemption. Each
        workspace still has to be toggled individually."""
        services.ensure_subscription(workspace)
        client.force_login(operator)

        client.post(
            reverse("billing_staff:set_courtesy_quota", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "5"},
        )

        assert not WorkspaceSubscription.objects.filter(
            workspace__organization=org, status=WorkspaceSubscription.Status.COURTESY
        ).exists()

    def test_lowering_the_limit_bills_nobody_retroactively(self, client, operator, org, workspace):
        services.grant_courtesy(workspace)
        client.force_login(operator)

        client.post(
            reverse("billing_staff:set_courtesy_quota", kwargs={"org_id": org.id}),
            {"courtesy_workspace_quota": "0"},
        )

        assert WorkspaceSubscription.objects.get(workspace=workspace).status == WorkspaceSubscription.Status.COURTESY

    def test_exempting_a_workspace_changes_only_its_billing(self, client, operator, workspace):
        """The toggle's own claim: the workspace keeps the disk it would
        have paid for, and nothing is provisioned."""
        client.force_login(operator)

        client.post(
            reverse("billing_staff:toggle_courtesy", kwargs={"target_workspace_id": workspace.id}),
            {"action": "grant"},
        )

        sub = WorkspaceSubscription.objects.get(workspace=workspace)
        assert sub.status == WorkspaceSubscription.Status.COURTESY
        assert sub.monthly_total_cents == 0
        assert services.total_storage_bytes(workspace.pk) == services.base_storage_bytes()


# ---------------------------------------------------------------------------
# The isolation itself
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheAreaIsIsolatedFromTheProduct:
    @pytest.mark.parametrize("name", ADMIN_SCREENS)
    def test_rendered_through_the_admin_shell_not_the_product_base(self, client, operator, name):
        """The product chrome lives in base.html, outside any block this
        area could override; the whole fix is that these pages no longer
        inherit from it."""
        client.force_login(operator)

        response = client.get(reverse(name))

        used = {template.name for template in response.templates}
        assert "layouts/admin_shell.html" in used
        assert "base.html" not in used

    @pytest.mark.parametrize("name", ADMIN_SCREENS)
    def test_renders_with_no_workspace_on_the_request(self, client, operator, name):
        """An operator has no workspace, so any ``request.workspace``
        dereference in the chrome is a 500 on the only area they can
        use."""
        client.force_login(operator)

        response = client.get(reverse(name))

        assert response.status_code == 200
        assert getattr(response.wsgi_request, "workspace", None) is None

    @pytest.mark.parametrize("name", ADMIN_SCREENS)
    def test_renders_with_customer_data_present(self, client, operator, customer, name):
        """Reading orgs none of which is the operator's is the normal case."""
        client.force_login(operator)

        assert client.get(reverse(name)).status_code == 200

    @pytest.mark.parametrize("name", ADMIN_SCREENS)
    def test_each_screen_marks_itself_in_the_navigation(self, client, operator, name):
        """``admin_active`` is what the shell highlights. A screen that
        forgets it renders a nav with nothing current."""
        client.force_login(operator)

        response = client.get(reverse(name))

        assert response.context["admin_active"] in {"accounts", "plans", "impersonations"}

    def test_the_three_screens_are_distinct_entries(self, client, operator):
        """Two screens sharing an ``admin_active`` would highlight both."""
        client.force_login(operator)

        actives = [client.get(url).context["admin_active"] for url in _urls()]

        assert len(set(actives)) == len(ADMIN_SCREENS)


# ---------------------------------------------------------------------------
# Plan and prices
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPlansScreen:
    def test_reports_the_configured_prices(self, client, operator, settings):
        """Read from the same services the customer-facing page reads, so
        the operator cannot be shown a price nobody is charged."""
        settings.BILLING_WORKSPACE_PRICE_CENTS = 7700
        settings.BILLING_STORAGE_PRICE_PER_GB_CENTS = 300
        settings.BILLING_WORKSPACE_BASE_STORAGE_BYTES = 20 * GB
        client.force_login(operator)

        context = client.get(reverse("billing_staff:plans")).context

        assert context["workspace_price_cents"] == 7700
        assert context["storage_price_per_gb_cents"] == 300
        assert context["base_storage_bytes"] == 20 * GB

    def test_counts_subscriptions_by_state(self, client, operator, org):
        paying = Workspace.objects.create(organization=org, name="Paying")
        comped = Workspace.objects.create(organization=org, name="Comped")
        fresh = Workspace.objects.create(organization=org, name="Fresh")

        sub = services.ensure_subscription(paying)
        sub.status = WorkspaceSubscription.Status.ACTIVE
        sub.save(update_fields=["status"])
        services.grant_courtesy(comped)
        services.ensure_subscription(fresh)

        client.force_login(operator)
        counts = client.get(reverse("billing_staff:plans")).context["counts"]

        assert counts["active"] == 1
        assert counts["courtesy"] == 1
        assert counts["pending"] == 1

    def test_sums_only_active_storage_addons(self, client, operator, workspace):
        """A pending add-on grants no disk, so counting it here would
        overstate what was sold."""
        active = services.contract_storage_addon(workspace, 5)
        active.status = WorkspaceAddon.Status.ACTIVE
        active.save(update_fields=["status"])
        services.contract_storage_addon(workspace, 50)  # left pending

        client.force_login(operator)
        counts = client.get(reverse("billing_staff:plans")).context["counts"]

        assert counts["active_storage_addons"] == 1
        assert counts["extra_gb_sold"] == 5

    def test_survives_an_empty_installation(self, client, operator):
        """No organizations, no subscriptions — a fresh deployment. The
        aggregate has to read 0, not None."""
        client.force_login(operator)

        counts = client.get(reverse("billing_staff:plans")).context["counts"]

        assert counts["extra_gb_sold"] == 0
        assert counts["active"] == 0

    def test_changes_nothing(self, client, operator, workspace):
        """Read-only by construction: GET-only, and the screen offers no
        form. Asserted because a later edit that adds one would silently
        be writing list prices no existing row reads."""
        services.ensure_subscription(workspace)
        client.force_login(operator)

        before = WorkspaceSubscription.objects.get(workspace=workspace).status
        client.get(reverse("billing_staff:plans"))

        assert WorkspaceSubscription.objects.get(workspace=workspace).status == before
        assert client.post(reverse("billing_staff:plans")).status_code == 405


# ---------------------------------------------------------------------------
# Access log
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestImpersonationLogScreen:
    def test_empty_when_nobody_has_signed_in_as_anyone(self, client, operator):
        client.force_login(operator)

        context = client.get(reverse("billing_staff:impersonation_log")).context

        assert list(context["sessions"]) == []
        assert context["open_count"] == 0

    def test_lists_a_takeover_that_happened(self, client, operator, customer):
        client.force_login(operator)
        client.post(reverse("accounts:start_impersonation", kwargs={"user_id": customer.pk}))
        client.post(reverse("accounts:stop_impersonation"))

        context = client.get(reverse("billing_staff:impersonation_log")).context
        rows = list(context["sessions"])

        assert len(rows) == 1
        assert rows[0].impersonated_id == customer.pk
        assert rows[0].impersonator_id == operator.pk
        assert rows[0].ended_at is not None
        assert context["open_count"] == 0

    def test_counts_a_takeover_left_open(self, client, operator, customer):
        """An operator who never pressed "return" — the state worth
        chasing, and the reason ``ended_at`` is nullable."""
        client.force_login(operator)
        client.post(reverse("accounts:start_impersonation", kwargs={"user_id": customer.pk}))

        # The impersonated session cannot read the log (it is a 403), so
        # the operator's own session has to be restored first. Stopping
        # would close the row, so the session is rebuilt instead.
        client.logout()
        client.force_login(operator)
        context = client.get(reverse("billing_staff:impersonation_log")).context

        assert context["open_count"] == 1
        assert list(context["sessions"])[0].ended_at is None

    def test_newest_first(self, client, operator, customer, db):
        """``Meta.ordering`` is -started_at; the view relies on it rather
        than re-sorting, so this is what pins the contract."""
        second = User.objects.create_user(email="client2@example.com", password="x", tos_accepted_at=timezone.now())
        for target in (customer, second):
            client.force_login(operator)
            client.post(reverse("accounts:start_impersonation", kwargs={"user_id": target.pk}))
            client.post(reverse("accounts:stop_impersonation"))

        client.force_login(operator)
        rows = list(client.get(reverse("billing_staff:impersonation_log")).context["sessions"])

        assert [row.impersonated_id for row in rows] == [second.pk, customer.pk]

    def test_caps_the_window(self, client, operator):
        """A fixed window, not pagination — see the view's docstring. The
        cap is what keeps the page off a growing table."""
        from apps.billing.views import IMPERSONATION_LOG_LIMIT

        ImpersonationSession.objects.bulk_create(
            ImpersonationSession(
                impersonator=operator,
                impersonated=None,
                impersonator_email=operator.email,
                impersonated_email=f"c{index}@example.com",
            )
            for index in range(IMPERSONATION_LOG_LIMIT + 5)
        )
        client.force_login(operator)

        rows = list(client.get(reverse("billing_staff:impersonation_log")).context["sessions"])

        assert len(rows) == IMPERSONATION_LOG_LIMIT

    def test_survives_a_deleted_account_on_either_side(self, client, operator, db):
        """The snapshotted e-mails are the whole point of the trail: a
        deleted customer must not take the record with them."""
        victim = User.objects.create_user(email="gone@example.com", password="x", tos_accepted_at=timezone.now())
        ImpersonationSession.objects.create(
            impersonator=operator,
            impersonated=victim,
            impersonator_email=operator.email,
            impersonated_email=victim.email,
        )
        victim.delete()
        client.force_login(operator)

        response = client.get(reverse("billing_staff:impersonation_log"))
        rows = list(response.context["sessions"])

        assert response.status_code == 200
        assert len(rows) == 1
        assert rows[0].impersonated_id is None
        assert rows[0].impersonated_email == "gone@example.com"

    def test_offers_no_way_to_edit_the_trail(self, client, operator):
        client.force_login(operator)

        assert client.post(reverse("billing_staff:impersonation_log")).status_code == 405


# ---------------------------------------------------------------------------
# The operator never lands on a product screen that needs a workspace
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOperatorCannotFallIntoTheProduct:
    def test_the_root_sends_the_operator_into_the_area(self, client, operator):
        client.force_login(operator)

        response = client.get("/")

        assert response.status_code == 302
        assert response.url == reverse("billing_staff:staff_console")

    def test_own_account_settings_still_render(self, client, operator):
        """The one product page an operator legitimately needs, and whose
        chrome assumes a workspace."""
        client.force_login(operator)

        assert client.get(reverse("accounts:settings")).status_code == 200

    def test_settings_chrome_offers_no_organization_links(self, client, operator):
        """Every link in that sidebar's Organization section is behind
        ``require_org_role``, so for an operator all of them 403. The
        section is keyed on the membership that makes them work."""
        client.force_login(operator)

        response = client.get(reverse("accounts:settings"))

        assert response.context["request"].org_membership is None

    @pytest.mark.parametrize(
        "name",
        [
            "organizations:settings",
            "organizations:workspaces",
            "organizations:cross_workspace_calendar",
            "members:list",
            "api_keys:list",
            "media_library_org:shared_index",
        ],
    )
    def test_org_scoped_product_screens_refuse_rather_than_break(self, client, operator, name):
        """A clear refusal, never a 500 on a missing organization."""
        client.force_login(operator)

        assert client.get(reverse(name)).status_code == 403

    def test_a_customer_is_not_redirected_into_the_area(self, client, customer):
        client.force_login(customer)

        response = client.get("/")

        assert response.status_code == 302
        assert reverse("billing_staff:staff_console") not in response.url

    def test_is_operator_is_the_single_predicate(self, client, operator, customer, rf):
        """Guards the invariant the three views share, at the source: the
        area is gated on ``impersonation.is_operator`` and nothing else,
        so a view that reads ``user.is_staff`` directly would diverge."""
        request = rf.get("/billing/staff/")
        request.user = operator
        request.session = {}
        assert impersonation.is_operator(request) is True

        request.user = customer
        assert impersonation.is_operator(request) is False
