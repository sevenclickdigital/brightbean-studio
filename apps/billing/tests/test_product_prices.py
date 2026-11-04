"""The editable price list, and the one guarantee it must never break.

The guarantee, which is what most of this file is about: **changing a
list price must never re-price anything already sold.** Every
``WorkspaceSubscription`` carries its own ``price_cents`` and
``base_storage_bytes``, every ``WorkspaceAddon`` its own
``unit_price_cents``, and those columns exist for precisely this. The
tests in ``TestExistingContractsAreNeverRepriced`` contract first, move
the list, and then assert the sold rows did not budge — including the
derived figures (monthly total, storage quota) that a leak would show up
in even if the column itself looked right.

The rest covers the read path (stored value wins, setting is the
fallback, the cache does not outlive a write), validation (nothing
invalid is ever written and nothing produces a 500), permissions (the
write is as closed as every other write in the admin area), and the
audit trail.

Nothing here asserts on rendered wording or markup. The interface
renders in pt-BR, the words are not a contract, and this project already
carries a pile of tests broken by exactly that mistake. What is asserted
is database state, returned values and HTTP status.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db.models.signals import post_save
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.signals import create_organization_on_user_create
from apps.billing import services
from apps.billing.models import (
    ProductPrice,
    ProductPriceChange,
    WorkspaceAddon,
    WorkspaceSubscription,
)
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

GB = WorkspaceAddon.BYTES_PER_GB
KEY = ProductPrice.Key


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_price_cache():
    """The resolved price list is cached process-wide.

    Autouse because a value cached by one test would otherwise decide
    what the next one reads — including the ``_PRICE_ABSENT`` sentinel,
    which would make a freshly written row look unset.
    """
    services.invalidate_price_cache()
    yield
    services.invalidate_price_cache()


@pytest.fixture
def operator(db):
    """Staff owning nothing — what ``manage.py create_operator`` makes."""
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


def _post_prices(client, **overrides):
    """Submit the price form, defaulting every field to something valid.

    The money fields are in REAIS, as the screen asks for them — that is
    the unit under test, and a helper that posted cents would be testing
    a form the operator never sees. Disk is whole GB.

    All three fields go in every submission because the view validates
    all three before writing any — a partial POST is a different test
    (and is covered as a validation case), not the way to exercise one
    field.
    """
    data = {
        "workspace_price_brl": "50,00",
        "base_storage_gb": "10",
        "storage_price_per_gb_brl": "2,00",
    }
    data.update({key: str(value) for key, value in overrides.items()})
    return client.post(reverse("billing_staff:update_prices"), data)


# ---------------------------------------------------------------------------
# The read path: stored value, else the setting
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPriceResolution:
    """The three service functions are the only price readers in the app.

    So these tests are the whole contract: everything else in the
    codebase inherits whatever they return.
    """

    def test_falls_back_to_settings_when_nothing_is_stored(self, settings):
        """The state of a fresh installation, and of this one until
        somebody opens the screen."""
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        settings.BILLING_WORKSPACE_BASE_STORAGE_BYTES = 10 * GB
        settings.BILLING_STORAGE_PRICE_PER_GB_CENTS = 200

        assert not ProductPrice.objects.exists()
        assert services.workspace_price_cents() == 5000
        assert services.base_storage_bytes() == 10 * GB
        assert services.storage_price_per_gb_cents() == 200

    def test_stored_value_wins_over_the_setting(self, settings):
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        settings.BILLING_WORKSPACE_BASE_STORAGE_BYTES = 10 * GB
        settings.BILLING_STORAGE_PRICE_PER_GB_CENTS = 200

        services.set_price(KEY.WORKSPACE_PRICE_CENTS, 7700)
        services.set_price(KEY.BASE_STORAGE_BYTES, 25 * GB)
        services.set_price(KEY.STORAGE_PRICE_PER_GB_CENTS, 350)

        assert services.workspace_price_cents() == 7700
        assert services.base_storage_bytes() == 25 * GB
        assert services.storage_price_per_gb_cents() == 350

    def test_one_stored_price_does_not_drag_the_others_off_their_defaults(self, settings):
        """Each key falls back independently, so setting one price does
        not require setting all three."""
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        settings.BILLING_STORAGE_PRICE_PER_GB_CENTS = 200

        services.set_price(KEY.STORAGE_PRICE_PER_GB_CENTS, 499)

        assert services.storage_price_per_gb_cents() == 499
        assert services.workspace_price_cents() == 5000

    def test_a_stored_zero_is_a_price_and_not_a_fallback(self, settings):
        """0 is a decision — "this is free" — and must not read as unset.

        The bug this guards against is the obvious implementation
        (``value or setting``), which would charge R$ 50,00 for a plan
        the owner deliberately made free.
        """
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000

        services.set_price(KEY.WORKSPACE_PRICE_CENTS, 0)

        assert services.workspace_price_cents() == 0

    def test_a_write_invalidates_the_cache(self, settings):
        """The readers are cached; a price the operator just saved has to
        be the price the next contract uses."""
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000

        assert services.workspace_price_cents() == 5000  # populates the cache
        services.set_price(KEY.WORKSPACE_PRICE_CENTS, 9900)

        assert services.workspace_price_cents() == 9900

    def test_the_absent_case_is_cached_without_becoming_a_value(self, settings):
        """A fresh installation reads "no row" constantly; caching that
        must not turn into caching a number that then ignores the
        setting."""
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        assert services.workspace_price_cents() == 5000

        settings.BILLING_WORKSPACE_PRICE_CENTS = 6100

        assert services.workspace_price_cents() == 6100

    def test_a_row_written_behind_the_cache_is_picked_up_after_invalidation(self, settings):
        """Covers the manual escape hatch: a price changed by a shell or
        a second process, then invalidated."""
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        assert services.workspace_price_cents() == 5000

        ProductPrice.objects.create(key=KEY.WORKSPACE_PRICE_CENTS, value=4242)
        services.invalidate_price_cache()

        assert services.workspace_price_cents() == 4242


# ---------------------------------------------------------------------------
# THE GUARANTEE
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestExistingContractsAreNeverRepriced:
    """Contract first, move the list, assert nothing sold moved.

    The most important class in this file. A failure here is not a
    cosmetic bug: it is an existing customer's invoice changing because
    somebody typed a number on an admin screen.
    """

    def test_an_existing_subscription_keeps_its_price(self, workspace, settings):
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        sub = services.ensure_subscription(workspace)
        assert sub.price_cents == 5000

        services.set_price(KEY.WORKSPACE_PRICE_CENTS, 9900)

        sub.refresh_from_db()
        assert sub.price_cents == 5000

    def test_an_existing_subscription_keeps_its_included_disk(self, workspace, settings):
        """Disk is the same invariant as price: a quota a workspace is
        already storing files against must not shrink under it."""
        settings.BILLING_WORKSPACE_BASE_STORAGE_BYTES = 10 * GB
        sub = services.ensure_subscription(workspace)
        assert sub.base_storage_bytes == 10 * GB

        services.set_price(KEY.BASE_STORAGE_BYTES, 2 * GB)

        sub.refresh_from_db()
        assert sub.base_storage_bytes == 10 * GB

    def test_an_existing_addon_keeps_its_unit_price(self, workspace, settings):
        settings.BILLING_STORAGE_PRICE_PER_GB_CENTS = 200
        addon = services.contract_storage_addon(workspace, 10)
        assert addon.unit_price_cents == 200

        services.set_price(KEY.STORAGE_PRICE_PER_GB_CENTS, 900)

        addon.refresh_from_db()
        assert addon.unit_price_cents == 200
        assert addon.total_price_cents == 10 * 200

    def test_the_monthly_bill_of_an_existing_customer_does_not_move(self, workspace, settings):
        """The derived figure, not just the columns.

        A leak could hide in any of the three properties that compute a
        bill — so this asserts the number the customer would actually be
        charged, with a subscription and an add-on both in play.
        """
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        settings.BILLING_STORAGE_PRICE_PER_GB_CENTS = 200
        sub = services.ensure_subscription(workspace)
        sub.status = WorkspaceSubscription.Status.ACTIVE
        sub.save(update_fields=["status"])
        addon = services.contract_storage_addon(workspace, 5)
        addon.status = WorkspaceAddon.Status.ACTIVE
        addon.save(update_fields=["status"])

        before = services.workspace_monthly_total_cents(workspace)
        assert before == 5000 + 5 * 200

        services.set_price(KEY.WORKSPACE_PRICE_CENTS, 20000)
        services.set_price(KEY.STORAGE_PRICE_PER_GB_CENTS, 5000)

        workspace.refresh_from_db()
        assert services.workspace_monthly_total_cents(workspace) == before

    def test_the_storage_quota_of_an_existing_customer_does_not_move(self, workspace, settings):
        """Lowering the included disk on the list must not take space
        away from a workspace already using it."""
        settings.BILLING_WORKSPACE_BASE_STORAGE_BYTES = 10 * GB
        settings.BILLING_STORAGE_PRICE_PER_GB_CENTS = 200
        services.ensure_subscription(workspace)
        addon = services.contract_storage_addon(workspace, 5)
        addon.status = WorkspaceAddon.Status.ACTIVE
        addon.save(update_fields=["status"])

        before = services.total_storage_bytes(workspace)
        assert before == 10 * GB + 5 * GB

        services.set_price(KEY.BASE_STORAGE_BYTES, 1 * GB)

        workspace.refresh_from_db()
        assert services.total_storage_bytes(workspace) == before

    def test_only_a_new_contract_takes_the_new_price(self, org, settings):
        """The other half of the guarantee: the new price has to actually
        apply to somebody, or the screen does nothing."""
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        settings.BILLING_WORKSPACE_BASE_STORAGE_BYTES = 10 * GB
        old_workspace = Workspace.objects.create(organization=org, name="Old")
        old_sub = services.ensure_subscription(old_workspace)

        services.set_price(KEY.WORKSPACE_PRICE_CENTS, 9900)
        services.set_price(KEY.BASE_STORAGE_BYTES, 30 * GB)

        new_workspace = Workspace.objects.create(organization=org, name="New")
        new_sub = services.ensure_subscription(new_workspace)

        old_sub.refresh_from_db()
        assert old_sub.price_cents == 5000
        assert old_sub.base_storage_bytes == 10 * GB
        assert new_sub.price_cents == 9900
        assert new_sub.base_storage_bytes == 30 * GB

    def test_only_a_new_addon_takes_the_new_per_gb_price(self, workspace, settings):
        settings.BILLING_STORAGE_PRICE_PER_GB_CENTS = 200
        old_addon = services.contract_storage_addon(workspace, 5)

        services.set_price(KEY.STORAGE_PRICE_PER_GB_CENTS, 450)
        new_addon = services.contract_storage_addon(workspace, 5)

        old_addon.refresh_from_db()
        assert old_addon.unit_price_cents == 200
        assert new_addon.unit_price_cents == 450

    def test_ensure_subscription_does_not_reprice_on_a_later_call(self, workspace, settings):
        """``ensure_subscription`` runs on every render of the customers
        screen and the billing page.

        So if its ``get_or_create`` defaults ever leaked onto an existing
        row, a price change would reach every customer the next time an
        operator merely LOOKED at the console — the worst version of this
        bug, because nobody would have pressed anything.
        """
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        services.ensure_subscription(workspace)

        services.set_price(KEY.WORKSPACE_PRICE_CENTS, 12345)
        for _ in range(3):
            workspace.refresh_from_db()
            services.ensure_subscription(workspace)

        assert WorkspaceSubscription.objects.get(workspace=workspace).price_cents == 5000

    def test_a_comped_workspace_is_not_repriced_either(self, workspace, settings):
        """A courtesy bills nothing today, but the row keeps the price it
        would bill if the comp were revoked — so that figure must be as
        frozen as any other."""
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        services.grant_courtesy(workspace)

        services.set_price(KEY.WORKSPACE_PRICE_CENTS, 30000)
        services.revoke_courtesy(workspace)

        assert WorkspaceSubscription.objects.get(workspace=workspace).price_cents == 5000

    def test_the_posted_form_does_not_reprice_a_sold_row(self, client, operator, workspace, settings):
        """End to end, through the actual endpoint rather than the
        service — a view that did its own UPDATE would pass every test
        above and still break this one.
        """
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        settings.BILLING_STORAGE_PRICE_PER_GB_CENTS = 200
        sub = services.ensure_subscription(workspace)
        addon = services.contract_storage_addon(workspace, 10)
        client.force_login(operator)

        response = _post_prices(
            client,
            workspace_price_brl="88,00",
            base_storage_gb=50,
            storage_price_per_gb_brl="7,00",
        )

        assert response.status_code == 302
        sub.refresh_from_db()
        addon.refresh_from_db()
        assert sub.price_cents == 5000
        assert sub.base_storage_bytes == 10 * GB
        assert addon.unit_price_cents == 200
        # ...and the list did move, so the test cannot pass by doing
        # nothing at all.
        assert services.workspace_price_cents() == 8800
        assert services.base_storage_bytes() == 50 * GB
        assert services.storage_price_per_gb_cents() == 700


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestValidationRefusesBadInput:
    """Nothing invalid is written, and nothing invalid is a 500.

    Each case asserts both: the response is a redirect (the operator gets
    a message, not a stack trace) and no ``ProductPrice`` row exists.
    """

    @pytest.mark.parametrize(
        "field,value",
        [
            # Money: empty, blank, not a number, negative, fractions of a
            # cent, and absurd. "50,00" and "R$ 50,00" are NOT here —
            # they are what the field asks for and are covered as
            # accepted input below.
            ("workspace_price_brl", ""),
            ("workspace_price_brl", "   "),
            ("workspace_price_brl", "abc"),
            ("workspace_price_brl", "R$"),
            ("workspace_price_brl", "-1"),
            ("workspace_price_brl", "-50,00"),
            ("workspace_price_brl", "50,005"),
            ("workspace_price_brl", "NaN"),
            ("workspace_price_brl", "Infinity"),
            ("workspace_price_brl", "1e9"),
            ("workspace_price_brl", "50,00,00"),
            ("workspace_price_brl", "999999,00"),
            ("storage_price_per_gb_brl", ""),
            ("storage_price_per_gb_brl", "-100"),
            ("storage_price_per_gb_brl", "dois reais"),
            ("storage_price_per_gb_brl", "2,001"),
            ("storage_price_per_gb_brl", "99999,00"),
            # Disk: whole GB only.
            ("base_storage_gb", "-5"),
            ("base_storage_gb", ""),
            ("base_storage_gb", "ten"),
            ("base_storage_gb", "1.5"),
            ("base_storage_gb", "10,5"),
            ("base_storage_gb", "999999999"),
        ],
    )
    def test_refused_without_writing_anything(self, client, operator, field, value):
        client.force_login(operator)

        response = _post_prices(client, **{field: value})

        assert response.status_code == 302
        assert not ProductPrice.objects.exists()
        assert not ProductPriceChange.objects.exists()

    def test_a_missing_field_is_refused_rather_than_defaulted(self, client, operator):
        """An absent key is a tampered or stale form. Writing a default
        for it would be the screen inventing a price."""
        client.force_login(operator)

        response = client.post(reverse("billing_staff:update_prices"), {})

        assert response.status_code == 302
        assert not ProductPrice.objects.exists()

    def test_one_bad_field_writes_none_of_the_good_ones(self, client, operator):
        """The three figures are one decision. A half-applied plan — a
        raised price with the old included disk — is a state the operator
        never chose."""
        client.force_login(operator)

        response = _post_prices(client, workspace_price_brl="70,00", base_storage_gb="oops")

        assert response.status_code == 302
        assert not ProductPrice.objects.exists()

    def test_zero_is_accepted_on_every_field(self, client, operator):
        """Free is a decision the owner is allowed to make, so 0 must not
        be swept up with the invalid input above."""
        client.force_login(operator)

        response = _post_prices(
            client,
            workspace_price_brl="0,00",
            base_storage_gb=0,
            storage_price_per_gb_brl="0,00",
        )

        assert response.status_code == 302
        assert services.workspace_price_cents() == 0
        assert services.base_storage_bytes() == 0
        assert services.storage_price_per_gb_cents() == 0

    def test_the_service_refuses_an_unknown_key(self):
        """Guards the write path itself, not just the form."""
        with pytest.raises(services.PriceValidationError):
            services.set_price("not_a_price", 1)

        assert not ProductPrice.objects.exists()

    def test_the_gb_field_is_stored_as_bytes(self, client, operator):
        """The screen's convenience unit must not become the stored one —
        disk is bytes everywhere in this app."""
        client.force_login(operator)

        _post_prices(client, base_storage_gb=25)

        assert ProductPrice.objects.get(key=KEY.BASE_STORAGE_BYTES).value == 25 * GB
        assert services.base_storage_bytes() == 25 * GB

    def test_the_bound_is_checked_in_the_unit_the_operator_typed(self, client, operator):
        """A GB figure that is fine as GB must not be refused for being
        large as bytes, and vice versa."""
        client.force_login(operator)

        assert _post_prices(client, base_storage_gb=2000).status_code == 302
        assert services.base_storage_bytes() == 2000 * GB


# ---------------------------------------------------------------------------
# The money fields take reais, not cents
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCurrencyInput:
    """The operator types R$ 50,00; the database stores 5000.

    Asking for cents on screen — which the first version of this form did
    — exposes the storage layer to somebody who thinks in reais. So the
    field is masked reais and the conversion is the backend's, which
    makes "what exactly does the parser accept" a contract worth pinning
    down rather than an implementation detail.
    """

    @pytest.mark.parametrize(
        "typed,expected_cents",
        [
            # What the mask itself produces.
            ("50,00", 5000),
            ("R$ 50,00", 5000),
            ("1.234,56", 123456),
            # What a person types without the mask's help.
            ("50", 5000),
            ("0,99", 99),
            ("0", 0),
            ("0,00", 0),
            ("2,5", 250),
            # Tolerated: a numeric keypad and an English spreadsheet both
            # produce a dot where pt-BR wants a comma.
            ("50.00", 5000),
            ("2.50", 250),
            # Unambiguously thousands, because three digits follow.
            ("1.234", 123400),
            ("R$ 1.234,00", 123400),
            # Noise the mask or a paste leaves behind.
            (" 50,00 ", 5000),
            ("R$50,00", 5000),
        ],
    )
    def test_accepts_what_a_person_naturally_types(self, client, operator, typed, expected_cents):
        client.force_login(operator)

        response = _post_prices(client, workspace_price_brl=typed)

        assert response.status_code == 302
        assert services.workspace_price_cents() == expected_cents

    def test_the_cents_stored_are_an_integer_not_a_rounded_float(self, client, operator):
        """``float("0.07") * 100`` is 7.000000000000001.

        Parsed with ``Decimal`` precisely so the stored value does not
        depend on which way that error happened to go. Exercised on the
        amounts where a float round-trip is known to drift.
        """
        client.force_login(operator)

        for typed, expected in (("0,07", 7), ("0,29", 29), ("8,15", 815), ("1,10", 110)):
            _post_prices(client, workspace_price_brl=typed)
            stored = ProductPrice.objects.get(key=KEY.WORKSPACE_PRICE_CENTS).value
            assert stored == expected
            assert isinstance(stored, int)

    def test_a_value_round_trips_through_the_form(self, client, operator):
        """What the screen redisplays has to be what the parser accepts,
        or an operator who saves twice without touching anything changes
        the price."""
        client.force_login(operator)
        _post_prices(client, workspace_price_brl="1.234,56")

        shown = services.price_input_value(KEY.WORKSPACE_PRICE_CENTS, services.workspace_price_cents())
        reparsed = services.parse_price_value(KEY.WORKSPACE_PRICE_CENTS, str(shown))

        assert reparsed == 123456

    def test_the_form_is_handed_reais_not_cents(self, client, operator):
        """Guards against the field being wired back to the stored unit:
        5000 in a reais field reads as R$ 5.000,00."""
        services.set_price(KEY.WORKSPACE_PRICE_CENTS, 5000)
        services.set_price(KEY.STORAGE_PRICE_PER_GB_CENTS, 250)
        client.force_login(operator)

        form = client.get(reverse("billing_staff:plans")).context["price_form"]

        assert form["workspace_price_brl"] == Decimal("50.00")
        assert form["storage_price_per_gb_brl"] == Decimal("2.50")

    def test_a_bad_amount_is_a_message_and_not_a_crash(self, client, operator):
        """The whole point of parsing in the service: every shape of bad
        money input ends as a redirect with a message, never a 500."""
        client.force_login(operator)

        for typed in ("", "R$", "abc", "--5", ",", ",,", "1,2,3", "NaN", "-0,01", "1e400"):
            response = _post_prices(client, workspace_price_brl=typed)
            assert response.status_code == 302, typed
            assert not ProductPrice.objects.exists(), typed


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOnlyAnOperatorMayChangePrices:
    def test_a_customer_is_forbidden(self, client, customer):
        client.force_login(customer)

        response = _post_prices(client, workspace_price_brl="99,00")

        assert response.status_code == 403
        assert not ProductPrice.objects.exists()

    def test_anonymous_is_sent_to_login_and_writes_nothing(self, client):
        response = _post_prices(client, workspace_price_brl="99,00")

        assert response.status_code == 302
        assert "/accounts/login" in response.headers["Location"]
        assert not ProductPrice.objects.exists()

    def test_an_impersonated_session_is_forbidden(self, client, operator, customer):
        """A price changed from inside a customer's session would be
        recorded in the trail against the customer's identity — worse
        than no trail at all."""
        client.force_login(operator)
        client.post(reverse("accounts:start_impersonation", kwargs={"user_id": customer.pk}))

        response = _post_prices(client, workspace_price_brl="99,00")

        assert response.status_code == 403
        assert not ProductPrice.objects.exists()

    def test_a_deactivated_operator_is_refused(self, client, operator):
        client.force_login(operator)
        operator.is_active = False
        operator.save(update_fields=["is_active"])

        response = _post_prices(client, workspace_price_brl="99,00")

        assert response.status_code in (302, 403)
        assert not ProductPrice.objects.exists()

    def test_get_is_not_a_way_to_write(self, client, operator):
        """POST-only, so a prefetch or a crawler cannot move a price."""
        client.force_login(operator)

        response = client.get(reverse("billing_staff:update_prices"))

        assert response.status_code == 405
        assert not ProductPrice.objects.exists()


# ---------------------------------------------------------------------------
# The audit trail
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheChangeTrail:
    def test_records_the_author_and_the_previous_value(self, client, operator, settings):
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        client.force_login(operator)

        _post_prices(client, workspace_price_brl="60,00")
        _post_prices(client, workspace_price_brl="70,00")

        changes = list(ProductPriceChange.objects.filter(key=KEY.WORKSPACE_PRICE_CENTS))
        assert [c.new_value for c in changes] == [7000, 6000]  # newest first; cents
        latest, first = changes
        assert first.previous_value is None  # came from the setting
        assert latest.previous_value == 6000
        assert latest.changed_by == operator
        assert latest.changed_by_email == operator.email

    def test_the_first_edit_reports_no_previous_row_rather_than_the_setting(self, settings):
        """NULL means "nobody had set this". Recording the setting's value
        would claim the operator changed a number somebody chose, which
        nobody had."""
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000

        services.set_price(KEY.WORKSPACE_PRICE_CENTS, 8000)

        change = ProductPriceChange.objects.get()
        assert change.previous_value is None
        assert change.is_first_setting is True

    def test_a_previous_zero_is_a_previous_value_and_not_an_absence(self):
        """0 is a real price, so the trail must distinguish "was free"
        from "was never set"."""
        services.set_price(KEY.WORKSPACE_PRICE_CENTS, 0)
        services.set_price(KEY.WORKSPACE_PRICE_CENTS, 100)

        latest = ProductPriceChange.objects.first()
        assert latest.previous_value == 0
        assert latest.is_first_setting is False

    def test_resubmitting_the_same_values_records_nothing(self, client, operator, settings):
        """A double-submitted form must not fill the history with no-ops
        — it would bury the edit that mattered."""
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        client.force_login(operator)

        _post_prices(client, workspace_price_brl="60,00")
        _post_prices(client, workspace_price_brl="60,00")

        assert ProductPriceChange.objects.filter(key=KEY.WORKSPACE_PRICE_CENTS).count() == 1

    def test_the_price_row_names_who_last_changed_it(self, client, operator):
        client.force_login(operator)

        _post_prices(client, workspace_price_brl="60,00")

        row = ProductPrice.objects.get(key=KEY.WORKSPACE_PRICE_CENTS)
        assert row.updated_by == operator

    def test_the_trail_survives_the_operator_being_deleted(self, client, operator):
        """A deleted account must not take the record of its pricing
        decisions with it."""
        client.force_login(operator)
        _post_prices(client, workspace_price_brl="60,00")
        email = operator.email

        operator.delete()

        change = ProductPriceChange.objects.get(key=KEY.WORKSPACE_PRICE_CENTS)
        assert change.changed_by is None
        assert change.changed_by_email == email
        assert change.new_value == 6000

    def test_the_first_submission_records_every_figure_it_pins_down(self, client, operator, settings):
        """Submitting a value equal to the *setting* is still a change.

        Not a no-op, and deliberately so: before the submission the
        figure was whatever the installation happened to be deployed
        with and would follow a later change to that setting. After it,
        the figure is pinned in the database. The effective number is the
        same; what it is bound to is not, and that is a decision worth
        recording.
        """
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        settings.BILLING_WORKSPACE_BASE_STORAGE_BYTES = 10 * GB
        settings.BILLING_STORAGE_PRICE_PER_GB_CENTS = 200
        client.force_login(operator)

        _post_prices(client, workspace_price_brl="60,00", base_storage_gb=10, storage_price_per_gb_brl="3,00")

        keys = set(ProductPriceChange.objects.values_list("key", flat=True))
        assert keys == {KEY.WORKSPACE_PRICE_CENTS, KEY.BASE_STORAGE_BYTES, KEY.STORAGE_PRICE_PER_GB_CENTS}
        assert ProductPrice.objects.get(key=KEY.BASE_STORAGE_BYTES).value == 10 * GB

    def test_a_later_submission_records_only_the_figures_that_moved(self, client, operator, settings):
        """Once every figure is stored, an unchanged one is a true no-op
        and must not appear in the history."""
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        settings.BILLING_WORKSPACE_BASE_STORAGE_BYTES = 10 * GB
        settings.BILLING_STORAGE_PRICE_PER_GB_CENTS = 200
        client.force_login(operator)
        _post_prices(client)  # pins all three at the deployed values
        ProductPriceChange.objects.all().delete()

        _post_prices(client, storage_price_per_gb_brl="3,50")

        keys = set(ProductPriceChange.objects.values_list("key", flat=True))
        assert keys == {KEY.STORAGE_PRICE_PER_GB_CENTS}


# ---------------------------------------------------------------------------
# What the screen is handed
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestThePlansScreenReflectsTheStoredPrices:
    def test_reports_the_stored_values_not_the_settings(self, client, operator, settings):
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        settings.BILLING_STORAGE_PRICE_PER_GB_CENTS = 200
        settings.BILLING_WORKSPACE_BASE_STORAGE_BYTES = 10 * GB
        services.set_price(KEY.WORKSPACE_PRICE_CENTS, 7700)
        services.set_price(KEY.BASE_STORAGE_BYTES, 20 * GB)
        client.force_login(operator)

        context = client.get(reverse("billing_staff:plans")).context

        assert context["workspace_price_cents"] == 7700
        assert context["base_storage_bytes"] == 20 * GB
        # Untouched, so still the setting.
        assert context["storage_price_per_gb_cents"] == 200

    def test_says_which_figures_nobody_has_set(self, client, operator):
        """The screen distinguishes "R$ 50,00 because nobody decided" from
        "R$ 50,00 because the owner chose it" — the per-GB price being
        undecided is the reason this feature exists."""
        services.set_price(KEY.WORKSPACE_PRICE_CENTS, 7700)
        client.force_login(operator)

        prices = client.get(reverse("billing_staff:plans")).context["prices"]

        assert prices[KEY.WORKSPACE_PRICE_CENTS]["is_configured"] is True
        assert prices[KEY.STORAGE_PRICE_PER_GB_CENTS]["is_configured"] is False

    def test_the_form_redisplays_disk_in_the_unit_it_asks_for(self, client, operator):
        services.set_price(KEY.BASE_STORAGE_BYTES, 40 * GB)
        client.force_login(operator)

        form = client.get(reverse("billing_staff:plans")).context["price_form"]

        assert form["base_storage_gb"] == 40

    def test_carries_the_recent_changes(self, client, operator):
        client.force_login(operator)
        _post_prices(client, workspace_price_brl="60,00")

        changes = client.get(reverse("billing_staff:plans")).context["price_changes"]

        assert [c.new_value for c in changes if c.key == KEY.WORKSPACE_PRICE_CENTS] == [6000]

    def test_the_screen_itself_is_still_read_only(self, client, operator):
        """The form posts to its own endpoint, so ``plans/`` stays
        GET-only and cannot be written to by a prefetch."""
        client.force_login(operator)

        assert client.post(reverse("billing_staff:plans")).status_code == 405
