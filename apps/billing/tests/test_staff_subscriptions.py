"""The subscriptions and billing screen.

What it has to get right, in order of how much it would cost to get
wrong:

1. **Contracted is not received.** The totals are sums of what active
   subscriptions are priced at. Nothing on this screen may present that
   as money collected, and the totals must not move when the operator
   filters the list — a total that changes with a filter is not a total.
2. **Amounts come from the provider or not at all.** An event whose
   payload carries no trustworthy figure reports None, never 0 and never
   our own list price dressed up as a confirmation.
3. It survives being empty, which is the state of this installation:
   Asaas has no credentials, so no payment event can have arrived.

Nothing here asserts on rendered wording or markup — the interface
renders in pt-BR and the words are not a contract. Assertions are on
context state and HTTP status.
"""

from __future__ import annotations

import pytest
from django.db.models.signals import post_save
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.signals import create_organization_on_user_create
from apps.billing import services
from apps.billing.models import BillingEvent, WorkspaceAddon, WorkspaceSubscription
from apps.billing.views import _event_amount_cents
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

GB = WorkspaceAddon.BYTES_PER_GB
URL = "billing_staff:subscriptions"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def operator(db):
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


def _subscribe(workspace, status, *, price_cents=5000):
    """A subscription in a given state, priced explicitly.

    The price is a parameter because these tests are about the totals,
    and a total built from an implicit default is a total nobody can
    check by reading the test.
    """
    sub = services.ensure_subscription(workspace)
    sub.status = status
    sub.price_cents = price_cents
    sub.save(update_fields=["status", "price_cents"])
    return sub


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAccess:
    def test_an_operator_reaches_it(self, client, operator):
        client.force_login(operator)

        assert client.get(reverse(URL)).status_code == 200

    def test_a_customer_is_forbidden(self, client, customer):
        client.force_login(customer)

        assert client.get(reverse(URL)).status_code == 403

    def test_anonymous_is_sent_to_login(self, client):
        response = client.get(reverse(URL))

        assert response.status_code == 302
        assert "/accounts/login" in response.headers["Location"]

    def test_an_impersonated_session_is_forbidden(self, client, operator, customer):
        """Billing figures for every account must not be readable from
        inside one customer's session."""
        client.force_login(operator)
        client.post(reverse("accounts:start_impersonation", kwargs={"user_id": customer.pk}))

        assert client.get(reverse(URL)).status_code == 403

    def test_it_is_read_only(self, client, operator):
        """Every action on a subscription lives on Customers. A second
        place to comp a workspace is a second rule to keep in agreement
        with the first."""
        client.force_login(operator)

        assert client.post(reverse(URL)).status_code == 405


# ---------------------------------------------------------------------------
# One row per subscription
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheSubscriptionList:
    def test_one_row_per_subscription_not_per_account(self, client, operator, org):
        """The grain that distinguishes this screen from Customers."""
        first = Workspace.objects.create(organization=org, name="One")
        second = Workspace.objects.create(organization=org, name="Two")
        _subscribe(first, WorkspaceSubscription.Status.ACTIVE)
        _subscribe(second, WorkspaceSubscription.Status.PENDING)
        client.force_login(operator)

        rows = client.get(reverse(URL)).context["rows"]

        assert len(rows) == 2
        assert {row["workspace"].name for row in rows} == {"One", "Two"}

    def test_each_row_carries_its_account_and_monthly_charge(self, client, operator, workspace):
        _subscribe(workspace, WorkspaceSubscription.Status.ACTIVE, price_cents=7700)
        client.force_login(operator)

        row = client.get(reverse(URL)).context["rows"][0]

        assert row["organization"].name == "Acme"
        assert row["monthly_total_cents"] == 7700

    def test_the_monthly_charge_includes_active_addons(self, client, operator, workspace):
        _subscribe(workspace, WorkspaceSubscription.Status.ACTIVE, price_cents=5000)
        addon = services.contract_storage_addon(workspace, 5, unit_price_cents=200)
        addon.status = WorkspaceAddon.Status.ACTIVE
        addon.save(update_fields=["status"])
        client.force_login(operator)

        row = client.get(reverse(URL)).context["rows"][0]

        assert row["monthly_total_cents"] == 5000 + 5 * 200
        assert row["active_addon_gb"] == 5
        assert row["active_addon_count"] == 1

    def test_a_pending_addon_is_not_reported_as_disk_sold(self, client, operator, workspace):
        """A pending add-on grants no disk, so counting it would overstate
        both the charge and the space."""
        _subscribe(workspace, WorkspaceSubscription.Status.ACTIVE, price_cents=5000)
        services.contract_storage_addon(workspace, 50, unit_price_cents=200)  # left pending
        client.force_login(operator)

        row = client.get(reverse(URL)).context["rows"][0]

        assert row["active_addon_gb"] == 0
        assert row["monthly_total_cents"] == 5000

    def test_a_comped_subscription_bills_nothing(self, client, operator, workspace):
        _subscribe(workspace, WorkspaceSubscription.Status.COURTESY, price_cents=5000)
        client.force_login(operator)

        row = client.get(reverse(URL)).context["rows"][0]

        assert row["monthly_total_cents"] == 0
        assert row["subscription"].is_courtesy is True

    def test_filters_by_status(self, client, operator, org):
        """The reason to open this screen: find the past-due ones."""
        paying = Workspace.objects.create(organization=org, name="Paying")
        late = Workspace.objects.create(organization=org, name="Late")
        _subscribe(paying, WorkspaceSubscription.Status.ACTIVE)
        _subscribe(late, WorkspaceSubscription.Status.PAST_DUE)
        client.force_login(operator)

        context = client.get(reverse(URL), {"status": "past_due"}).context

        assert [row["workspace"].name for row in context["rows"]] == ["Late"]
        assert context["status_filter"] == "past_due"

    def test_an_unknown_status_falls_back_to_everything(self, client, operator, workspace):
        """A hand-edited URL or a stale bookmark. The useful answer is the
        unfiltered list, not a 400."""
        _subscribe(workspace, WorkspaceSubscription.Status.ACTIVE)
        client.force_login(operator)

        context = client.get(reverse(URL), {"status": "nonsense"}).context

        assert context["status_filter"] == ""
        assert len(context["rows"]) == 1

    def test_survives_an_empty_installation(self, client, operator):
        client.force_login(operator)

        context = client.get(reverse(URL)).context

        assert context["rows"] == []
        assert context["totals"]["contracted_cents"] == 0


# ---------------------------------------------------------------------------
# The totals, and the line they must not cross
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTotals:
    def test_contracted_counts_only_active_subscriptions(self, client, operator, org):
        """Pending and canceled have agreed to nothing, so including them
        would inflate the recurring figure with money nobody owes."""
        for name, status in (
            ("Paying", WorkspaceSubscription.Status.ACTIVE),
            ("AlsoPaying", WorkspaceSubscription.Status.ACTIVE),
            ("Fresh", WorkspaceSubscription.Status.PENDING),
            ("Gone", WorkspaceSubscription.Status.CANCELED),
            ("Comped", WorkspaceSubscription.Status.COURTESY),
            ("Late", WorkspaceSubscription.Status.PAST_DUE),
        ):
            _subscribe(Workspace.objects.create(organization=org, name=name), status, price_cents=5000)
        client.force_login(operator)

        totals = client.get(reverse(URL)).context["totals"]

        assert totals["contracted_cents"] == 2 * 5000
        assert totals["past_due_cents"] == 5000
        assert totals["exempt_cents"] == 5000

    def test_exempt_reports_what_the_comp_would_bill(self, client, operator, workspace):
        """A courtesy bills zero by definition, so the useful figure is
        what it WOULD bill — that is the question an owner asks about a
        comp."""
        _subscribe(workspace, WorkspaceSubscription.Status.COURTESY, price_cents=7700)
        client.force_login(operator)

        totals = client.get(reverse(URL)).context["totals"]

        assert totals["exempt_cents"] == 7700
        # ...and it is not counted as revenue.
        assert totals["contracted_cents"] == 0

    def test_the_totals_do_not_move_when_the_list_is_filtered(self, client, operator, org):
        """A total that changes with a filter is not a total.

        "R$ 0,00 overdue" while filtered to Active would be read as
        nobody being overdue, which is the sort of thing a business
        decision gets made on.
        """
        _subscribe(Workspace.objects.create(organization=org, name="Paying"), WorkspaceSubscription.Status.ACTIVE)
        _subscribe(Workspace.objects.create(organization=org, name="Late"), WorkspaceSubscription.Status.PAST_DUE)
        client.force_login(operator)

        unfiltered = client.get(reverse(URL)).context["totals"]
        filtered = client.get(reverse(URL), {"status": "active"}).context["totals"]

        assert filtered == unfiltered
        assert filtered["past_due_cents"] == 5000

    def test_the_counts_distinguish_shown_from_total(self, client, operator, org):
        """So the screen can say "2 of 5" rather than implying the filter
        is the whole population."""
        _subscribe(Workspace.objects.create(organization=org, name="A"), WorkspaceSubscription.Status.ACTIVE)
        _subscribe(Workspace.objects.create(organization=org, name="B"), WorkspaceSubscription.Status.PAST_DUE)
        client.force_login(operator)

        context = client.get(reverse(URL), {"status": "active"}).context

        assert context["subscription_count"] == 1
        assert context["total_subscription_count"] == 2

    def test_contracted_uses_the_sold_price_not_the_list_price(self, client, operator, workspace):
        """The whole no-reprice invariant, seen from the reporting side:
        a customer sold at the old price must be totalled at the old
        price."""
        _subscribe(workspace, WorkspaceSubscription.Status.ACTIVE, price_cents=5000)
        services.set_price(services.ProductPrice.Key.WORKSPACE_PRICE_CENTS, 99900)
        client.force_login(operator)

        totals = client.get(reverse(URL)).context["totals"]

        assert totals["contracted_cents"] == 5000


# ---------------------------------------------------------------------------
# What can be trusted out of a BillingEvent payload
# ---------------------------------------------------------------------------


class TestEventAmountExtraction:
    """``payment.value`` only, and None whenever it is not trustworthy.

    Not a Django test: a pure function over a payload dict, which is
    exactly the shape that lets every malformed case be enumerated
    cheaply. None is the important return — it renders as an em dash,
    whereas 0 would read as a free charge.
    """

    def test_reads_the_gross_amount(self):
        assert _event_amount_cents({"payment": {"value": 50.0}}) == 5000

    def test_handles_an_integer_and_a_string(self):
        """Asaas sends a JSON number; a replayed or hand-made payload may
        carry a string."""
        assert _event_amount_cents({"payment": {"value": 50}}) == 5000
        assert _event_amount_cents({"payment": {"value": "50.00"}}) == 5000

    def test_rounds_to_whole_cents(self):
        assert _event_amount_cents({"payment": {"value": 0.07}}) == 7
        assert _event_amount_cents({"payment": {"value": 1234.56}}) == 123456

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"payment": None},
            {"payment": {}},
            {"payment": "sub_123"},
            {"payment": {"value": None}},
            {"payment": {"value": ""}},
            {"payment": {"value": "fifty"}},
            {"payment": {"value": True}},
            # A subscription event, which has no payment object at all.
            {"event": "SUBSCRIPTION_DELETED", "subscription": {"id": "sub_1"}},
        ],
    )
    def test_reports_none_rather_than_zero_when_untrustworthy(self, payload):
        assert _event_amount_cents(payload) is None

    def test_net_value_is_not_used_as_a_substitute(self):
        """``netValue`` is gross minus the provider's fee — a different
        question from "what was this customer billed". Absent a gross
        amount the honest answer is none."""
        assert _event_amount_cents({"payment": {"netValue": 48.5}}) is None

    def test_a_non_dict_payload_does_not_raise(self):
        """``BillingEvent.payload`` is a JSONField and defaults to {}, but
        a replayed row could hold anything."""
        assert _event_amount_cents([]) is None
        assert _event_amount_cents("nope") is None


# ---------------------------------------------------------------------------
# The financial history
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheChargeHistory:
    def test_lists_events_with_the_account_they_belong_to(self, client, operator, workspace):
        sub = _subscribe(workspace, WorkspaceSubscription.Status.ACTIVE)
        BillingEvent.objects.create(
            gateway="asaas",
            gateway_event_id="evt_1",
            event_type="PAYMENT_CONFIRMED",
            subscription=sub,
            status=BillingEvent.Status.PROCESSED,
            payload={"payment": {"value": 50.0}},
        )
        client.force_login(operator)

        entry = client.get(reverse(URL)).context["event_rows"][0]

        assert entry["organization"].name == "Acme"
        assert entry["workspace"].name == "Acme Social"
        assert entry["amount_cents"] == 5000

    def test_an_event_matched_to_no_local_row_is_still_listed(self, client, operator):
        """Precisely the row somebody will need to explain — a charge
        created straight in the provider's dashboard. Dropping it would
        lose the audit trail when it is most needed."""
        BillingEvent.objects.create(
            gateway="asaas",
            gateway_event_id="evt_orphan",
            event_type="PAYMENT_CONFIRMED",
            status=BillingEvent.Status.IGNORED,
            payload={"payment": {"value": 50.0}},
        )
        client.force_login(operator)

        entry = client.get(reverse(URL)).context["event_rows"][0]

        assert entry["organization"] is None
        assert entry["workspace"] is None
        assert entry["amount_cents"] == 5000

    def test_an_addon_event_resolves_through_the_addon(self, client, operator, workspace):
        addon = services.contract_storage_addon(workspace, 5, unit_price_cents=200)
        BillingEvent.objects.create(
            gateway="asaas",
            gateway_event_id="evt_addon",
            event_type="PAYMENT_CONFIRMED",
            addon=addon,
            status=BillingEvent.Status.PROCESSED,
            payload={"payment": {"value": 10.0}},
        )
        client.force_login(operator)

        entry = client.get(reverse(URL)).context["event_rows"][0]

        assert entry["workspace"].name == "Acme Social"
        assert entry["amount_cents"] == 1000

    def test_an_event_with_no_amount_reports_none(self, client, operator, workspace):
        sub = _subscribe(workspace, WorkspaceSubscription.Status.ACTIVE)
        BillingEvent.objects.create(
            gateway="asaas",
            gateway_event_id="evt_sub",
            event_type="SUBSCRIPTION_DELETED",
            subscription=sub,
            status=BillingEvent.Status.PROCESSED,
            payload={"subscription": {"id": "sub_1"}},
        )
        client.force_login(operator)

        entry = client.get(reverse(URL)).context["event_rows"][0]

        assert entry["amount_cents"] is None

    def test_newest_first(self, client, operator):
        for index in range(3):
            BillingEvent.objects.create(gateway_event_id=f"evt_{index}", event_type=f"E{index}")
        client.force_login(operator)

        entries = client.get(reverse(URL)).context["event_rows"]

        assert [e["event"].event_type for e in entries] == ["E2", "E1", "E0"]

    def test_caps_the_window_and_reports_the_true_count(self, client, operator):
        """A fixed window, not pagination — the question is a recency
        one. The true count is still reported so the cap is visible
        rather than looking like the whole history."""
        from apps.billing.views import BILLING_EVENT_LIMIT

        BillingEvent.objects.bulk_create(
            [BillingEvent(gateway_event_id=f"evt_{i}") for i in range(BILLING_EVENT_LIMIT + 5)]
        )
        client.force_login(operator)

        context = client.get(reverse(URL)).context

        assert len(context["event_rows"]) == BILLING_EVENT_LIMIT
        assert context["event_count"] == BILLING_EVENT_LIMIT + 5


# ---------------------------------------------------------------------------
# The empty state, which is this installation's actual state
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheEmptyState:
    def test_reports_the_provider_as_unconfigured(self, client, operator, settings):
        """The honest reason there are no charges. "No charges yet" on its
        own would be read as customers not paying; the truth is that no
        charge could have been created at all."""
        settings.ASAAS_API_KEY = ""
        client.force_login(operator)

        context = client.get(reverse(URL)).context

        assert context["gateway_configured"] is False
        assert context["event_rows"] == []
        assert context["event_count"] == 0

    def test_reports_the_provider_as_configured_when_it_is(self, client, operator, settings):
        """Then an empty history means the provider has said nothing yet,
        which is a different sentence on screen."""
        settings.ASAAS_API_KEY = "key"
        client.force_login(operator)

        assert client.get(reverse(URL)).context["gateway_configured"] is True

    def test_renders_with_subscriptions_but_no_events(self, client, operator, workspace):
        """The exact state of this installation: contracts recorded,
        nothing ever charged."""
        _subscribe(workspace, WorkspaceSubscription.Status.PENDING)
        client.force_login(operator)

        response = client.get(reverse(URL))

        assert response.status_code == 200
        assert len(response.context["rows"]) == 1
        assert response.context["event_rows"] == []

    def test_renders_through_the_admin_shell(self, client, operator):
        """Under base.html this page would carry the whole product
        interface, pointing at a workspace an operator does not have."""
        client.force_login(operator)

        templates = [t.name for t in client.get(reverse(URL)).templates if t.name]

        assert "layouts/admin_shell.html" in templates

    def test_marks_itself_in_the_navigation(self, client, operator):
        client.force_login(operator)

        assert client.get(reverse(URL)).context["admin_active"] == "subscriptions"
