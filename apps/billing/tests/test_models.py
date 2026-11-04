"""Tests for the billing domain logic.

Behaviour only — quota arithmetic, the courtesy-counts-as-active rule,
the price snapshot on an add-on, and the webhook idempotency constraint.
Nothing here asserts on rendered text or markup; the billing UI arrives
in a later phase and its wording is not a contract.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing import services
from apps.billing.models import (
    BillingEvent,
    OrganizationBillingProfile,
    WorkspaceAddon,
    WorkspaceStorageAddon,
    WorkspaceSubscription,
)
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

GB = WorkspaceAddon.BYTES_PER_GB


def _workspace(name="Workspace", org=None):
    org = org or Organization.objects.create(name="Acme")
    return Workspace.objects.create(organization=org, name=name)


class WorkspaceSubscriptionStatusTests(TestCase):
    def test_default_status_is_pending(self):
        sub = WorkspaceSubscription.objects.create(workspace=_workspace())
        self.assertEqual(sub.status, WorkspaceSubscription.Status.PENDING)

    def test_one_subscription_per_workspace(self):
        ws = _workspace()
        WorkspaceSubscription.objects.create(workspace=ws)
        with self.assertRaises(IntegrityError), transaction.atomic():
            WorkspaceSubscription.objects.create(workspace=ws)

    def test_pending_is_not_active(self):
        sub = WorkspaceSubscription.objects.create(workspace=_workspace())
        self.assertFalse(sub.is_active)

    def test_active_is_active(self):
        sub = WorkspaceSubscription.objects.create(
            workspace=_workspace(),
            status=WorkspaceSubscription.Status.ACTIVE,
        )
        self.assertTrue(sub.is_active)

    def test_courtesy_counts_as_active(self):
        """The product rule: a comped workspace is fully usable."""
        sub = WorkspaceSubscription.objects.create(
            workspace=_workspace(),
            status=WorkspaceSubscription.Status.COURTESY,
        )
        self.assertTrue(sub.is_active)
        self.assertTrue(sub.is_courtesy)

    def test_past_due_is_not_active(self):
        sub = WorkspaceSubscription.objects.create(
            workspace=_workspace(),
            status=WorkspaceSubscription.Status.PAST_DUE,
        )
        self.assertFalse(sub.is_active)

    def test_canceled_is_not_active(self):
        sub = WorkspaceSubscription.objects.create(
            workspace=_workspace(),
            status=WorkspaceSubscription.Status.CANCELED,
        )
        self.assertFalse(sub.is_active)


class SubscriptionCurrentnessTests(TestCase):
    def test_active_within_period_is_current(self):
        sub = WorkspaceSubscription.objects.create(
            workspace=_workspace(),
            status=WorkspaceSubscription.Status.ACTIVE,
            current_period_end=timezone.now() + timezone.timedelta(days=5),
        )
        self.assertTrue(sub.is_current)

    def test_active_past_period_end_is_not_current(self):
        sub = WorkspaceSubscription.objects.create(
            workspace=_workspace(),
            status=WorkspaceSubscription.Status.ACTIVE,
            current_period_end=timezone.now() - timezone.timedelta(days=1),
        )
        self.assertFalse(sub.is_current)

    def test_active_without_period_end_is_current(self):
        """A webhook delay right after checkout must not read as a lapse."""
        sub = WorkspaceSubscription.objects.create(
            workspace=_workspace(),
            status=WorkspaceSubscription.Status.ACTIVE,
        )
        self.assertTrue(sub.is_current)

    def test_courtesy_is_always_current(self):
        sub = WorkspaceSubscription.objects.create(
            workspace=_workspace(),
            status=WorkspaceSubscription.Status.COURTESY,
            current_period_end=timezone.now() - timezone.timedelta(days=365),
        )
        self.assertTrue(sub.is_current)

    def test_past_due_is_not_current(self):
        sub = WorkspaceSubscription.objects.create(
            workspace=_workspace(),
            status=WorkspaceSubscription.Status.PAST_DUE,
            current_period_end=timezone.now() + timezone.timedelta(days=5),
        )
        self.assertFalse(sub.is_current)


class StorageQuotaTests(TestCase):
    def test_quota_without_addons_is_base_only(self):
        ws = _workspace()
        WorkspaceSubscription.objects.create(workspace=ws, base_storage_bytes=10 * GB)
        self.assertEqual(services.total_storage_bytes(ws), 10 * GB)

    def test_quota_adds_active_addon(self):
        ws = _workspace()
        WorkspaceSubscription.objects.create(workspace=ws, base_storage_bytes=10 * GB)
        WorkspaceStorageAddon.objects.create(
            workspace=ws,
            quantity=25,
            unit_price_cents=200,
            status=WorkspaceAddon.Status.ACTIVE,
        )
        self.assertEqual(services.total_storage_bytes(ws), 35 * GB)

    def test_quota_sums_multiple_addons(self):
        ws = _workspace()
        WorkspaceSubscription.objects.create(workspace=ws, base_storage_bytes=10 * GB)
        for qty in (5, 15, 30):
            WorkspaceStorageAddon.objects.create(
                workspace=ws,
                quantity=qty,
                unit_price_cents=200,
                status=WorkspaceAddon.Status.ACTIVE,
            )
        self.assertEqual(services.total_storage_bytes(ws), 60 * GB)

    def test_pending_addon_grants_no_disk(self):
        """Disk is granted on payment, not on intent to pay."""
        ws = _workspace()
        WorkspaceSubscription.objects.create(workspace=ws, base_storage_bytes=10 * GB)
        WorkspaceStorageAddon.objects.create(workspace=ws, quantity=50, unit_price_cents=200)
        self.assertEqual(services.total_storage_bytes(ws), 10 * GB)

    def test_canceled_addon_grants_no_disk(self):
        ws = _workspace()
        WorkspaceSubscription.objects.create(workspace=ws, base_storage_bytes=10 * GB)
        WorkspaceStorageAddon.objects.create(
            workspace=ws,
            quantity=50,
            unit_price_cents=200,
            status=WorkspaceAddon.Status.CANCELED,
        )
        self.assertEqual(services.total_storage_bytes(ws), 10 * GB)

    def test_workspace_without_subscription_has_no_quota(self):
        """No contract, no disk — including for pre-billing workspaces."""
        self.assertEqual(services.total_storage_bytes(_workspace()), 0)

    def test_addon_on_another_workspace_does_not_leak(self):
        org = Organization.objects.create(name="Acme")
        mine = _workspace("Mine", org)
        theirs = _workspace("Theirs", org)
        WorkspaceSubscription.objects.create(workspace=mine, base_storage_bytes=10 * GB)
        WorkspaceSubscription.objects.create(workspace=theirs, base_storage_bytes=10 * GB)
        WorkspaceStorageAddon.objects.create(
            workspace=theirs,
            quantity=100,
            unit_price_cents=200,
            status=WorkspaceAddon.Status.ACTIVE,
        )
        self.assertEqual(services.total_storage_bytes(mine), 10 * GB)

    def test_subscription_property_matches_service(self):
        ws = _workspace()
        sub = WorkspaceSubscription.objects.create(workspace=ws, base_storage_bytes=10 * GB)
        WorkspaceStorageAddon.objects.create(
            workspace=ws,
            quantity=5,
            unit_price_cents=200,
            status=WorkspaceAddon.Status.ACTIVE,
        )
        self.assertEqual(sub.total_storage_bytes, services.total_storage_bytes(ws))
        self.assertEqual(sub.extra_storage_bytes, 5 * GB)


class AddonPriceSnapshotTests(TestCase):
    @override_settings(BILLING_STORAGE_PRICE_PER_GB_CENTS=200)
    def test_contracting_snapshots_current_list_price(self):
        addon = services.contract_storage_addon(_workspace(), 10)
        self.assertEqual(addon.unit_price_cents, 200)

    @override_settings(BILLING_STORAGE_PRICE_PER_GB_CENTS=200)
    def test_list_price_change_does_not_reprice_existing_addon(self):
        """The whole reason unit_price_cents is a column."""
        addon = services.contract_storage_addon(_workspace(), 10)
        with override_settings(BILLING_STORAGE_PRICE_PER_GB_CENTS=900):
            addon.refresh_from_db()
            self.assertEqual(addon.unit_price_cents, 200)
            self.assertEqual(addon.total_price_cents, 2000)

    def test_explicit_price_overrides_list(self):
        addon = services.contract_storage_addon(_workspace(), 10, unit_price_cents=150)
        self.assertEqual(addon.unit_price_cents, 150)
        self.assertEqual(addon.total_price_cents, 1500)

    def test_total_price_is_quantity_times_unit(self):
        addon = WorkspaceStorageAddon(quantity=7, unit_price_cents=250)
        self.assertEqual(addon.total_price_cents, 1750)

    def test_storage_bytes_uses_decimal_gb(self):
        """We sell what the price list calls a GB, not a GiB."""
        addon = WorkspaceStorageAddon(quantity=1, unit_price_cents=200)
        self.assertEqual(addon.storage_bytes, 1_000_000_000)


class AddonProxyTests(TestCase):
    def test_proxy_sets_storage_kind(self):
        addon = WorkspaceStorageAddon.objects.create(
            workspace=_workspace(),
            quantity=5,
            unit_price_cents=200,
        )
        self.assertEqual(addon.kind, WorkspaceAddon.Kind.EXTRA_STORAGE)

    def test_proxy_queryset_excludes_other_kinds(self):
        """A future AI add-on shares the table but not this manager."""
        ws = _workspace()
        WorkspaceAddon.objects.create(workspace=ws, kind="ai_text", quantity=1, unit_price_cents=100)
        WorkspaceStorageAddon.objects.create(workspace=ws, quantity=5, unit_price_cents=200)
        self.assertEqual(WorkspaceAddon.objects.filter(workspace=ws).count(), 2)
        self.assertEqual(WorkspaceStorageAddon.objects.filter(workspace=ws).count(), 1)

    def test_non_storage_addon_grants_no_disk(self):
        ws = _workspace()
        WorkspaceSubscription.objects.create(workspace=ws, base_storage_bytes=10 * GB)
        WorkspaceAddon.objects.create(
            workspace=ws,
            kind="ai_text",
            quantity=50,
            unit_price_cents=100,
            status=WorkspaceAddon.Status.ACTIVE,
        )
        self.assertEqual(services.total_storage_bytes(ws), 10 * GB)


class MonthlyTotalTests(TestCase):
    def test_total_is_base_price_without_addons(self):
        ws = _workspace()
        WorkspaceSubscription.objects.create(
            workspace=ws,
            price_cents=5000,
            status=WorkspaceSubscription.Status.ACTIVE,
        )
        self.assertEqual(services.workspace_monthly_total_cents(ws), 5000)

    def test_total_includes_active_addons(self):
        ws = _workspace()
        WorkspaceSubscription.objects.create(
            workspace=ws,
            price_cents=5000,
            status=WorkspaceSubscription.Status.ACTIVE,
        )
        WorkspaceStorageAddon.objects.create(
            workspace=ws,
            quantity=10,
            unit_price_cents=200,
            status=WorkspaceAddon.Status.ACTIVE,
        )
        self.assertEqual(services.workspace_monthly_total_cents(ws), 7000)

    def test_total_excludes_inactive_addons(self):
        ws = _workspace()
        WorkspaceSubscription.objects.create(
            workspace=ws,
            price_cents=5000,
            status=WorkspaceSubscription.Status.ACTIVE,
        )
        WorkspaceStorageAddon.objects.create(workspace=ws, quantity=10, unit_price_cents=200)
        self.assertEqual(services.workspace_monthly_total_cents(ws), 5000)

    def test_courtesy_workspace_bills_nothing_even_with_addons(self):
        ws = _workspace()
        WorkspaceSubscription.objects.create(
            workspace=ws,
            price_cents=5000,
            status=WorkspaceSubscription.Status.COURTESY,
        )
        WorkspaceStorageAddon.objects.create(
            workspace=ws,
            quantity=10,
            unit_price_cents=200,
            status=WorkspaceAddon.Status.ACTIVE,
        )
        self.assertEqual(services.workspace_monthly_total_cents(ws), 0)


class AccessTests(TestCase):
    def test_workspace_without_subscription_is_not_active(self):
        self.assertFalse(services.is_workspace_active(_workspace()))

    def test_courtesy_workspace_is_active(self):
        ws = _workspace()
        WorkspaceSubscription.objects.create(workspace=ws, status=WorkspaceSubscription.Status.COURTESY)
        self.assertTrue(services.is_workspace_active(ws))

    def test_past_due_workspace_is_not_active(self):
        ws = _workspace()
        WorkspaceSubscription.objects.create(workspace=ws, status=WorkspaceSubscription.Status.PAST_DUE)
        self.assertFalse(services.is_workspace_active(ws))

    def test_accepts_workspace_id(self):
        ws = _workspace()
        WorkspaceSubscription.objects.create(workspace=ws, status=WorkspaceSubscription.Status.ACTIVE)
        self.assertTrue(services.is_workspace_active(ws.pk))

    def test_is_subscription_current_without_row(self):
        self.assertFalse(services.is_subscription_current(_workspace()))


class ProvisioningTests(TestCase):
    @override_settings(
        BILLING_WORKSPACE_PRICE_CENTS=5000,
        BILLING_WORKSPACE_BASE_STORAGE_BYTES=10 * GB,
    )
    def test_ensure_subscription_prices_from_settings(self):
        sub = services.ensure_subscription(_workspace())
        self.assertEqual(sub.price_cents, 5000)
        self.assertEqual(sub.base_storage_bytes, 10 * GB)
        self.assertEqual(sub.status, WorkspaceSubscription.Status.PENDING)

    def test_ensure_subscription_is_idempotent(self):
        ws = _workspace()
        first = services.ensure_subscription(ws)
        second = services.ensure_subscription(ws)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(WorkspaceSubscription.objects.filter(workspace=ws).count(), 1)

    @override_settings(BILLING_WORKSPACE_PRICE_CENTS=5000)
    def test_ensure_subscription_does_not_reprice_existing_row(self):
        ws = _workspace()
        services.ensure_subscription(ws)
        with override_settings(BILLING_WORKSPACE_PRICE_CENTS=9900):
            sub = services.ensure_subscription(ws)
            self.assertEqual(sub.price_cents, 5000)

    def test_grant_courtesy_activates_workspace(self):
        ws = _workspace()
        sub = services.grant_courtesy(ws, note="founding customer")
        self.assertEqual(sub.status, WorkspaceSubscription.Status.COURTESY)
        self.assertTrue(services.is_workspace_active(ws))

    def test_grant_courtesy_creates_missing_subscription(self):
        """Pre-billing workspaces can be comped without first subscribing."""
        ws = _workspace()
        services.grant_courtesy(ws)
        self.assertTrue(WorkspaceSubscription.objects.filter(workspace=ws).exists())

    def test_revoke_courtesy_returns_to_pending(self):
        ws = _workspace()
        services.grant_courtesy(ws)
        sub = services.revoke_courtesy(ws)
        self.assertEqual(sub.status, WorkspaceSubscription.Status.PENDING)
        self.assertFalse(services.is_workspace_active(ws))

    def test_revoke_courtesy_leaves_paying_subscription_alone(self):
        ws = _workspace()
        WorkspaceSubscription.objects.create(workspace=ws, status=WorkspaceSubscription.Status.ACTIVE)
        sub = services.revoke_courtesy(ws)
        self.assertEqual(sub.status, WorkspaceSubscription.Status.ACTIVE)


class OrganizationCourtesyQuotaTests(TestCase):
    def test_quota_defaults_to_zero(self):
        org = Organization.objects.create(name="Acme")
        profile = services.ensure_billing_profile(org)
        self.assertEqual(profile.courtesy_workspace_quota, 0)
        self.assertFalse(profile.has_courtesy_available)

    def test_one_profile_per_organization(self):
        org = Organization.objects.create(name="Acme")
        OrganizationBillingProfile.objects.create(organization=org)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationBillingProfile.objects.create(organization=org)

    def test_used_counts_only_courtesy_workspaces(self):
        org = Organization.objects.create(name="Acme")
        profile = OrganizationBillingProfile.objects.create(organization=org, courtesy_workspace_quota=3)
        services.grant_courtesy(_workspace("A", org))
        services.grant_courtesy(_workspace("B", org))
        WorkspaceSubscription.objects.create(
            workspace=_workspace("C", org),
            status=WorkspaceSubscription.Status.ACTIVE,
        )
        self.assertEqual(profile.courtesy_workspaces_used, 2)
        self.assertEqual(profile.courtesy_workspaces_remaining, 1)
        self.assertTrue(profile.has_courtesy_available)

    def test_remaining_is_floored_at_zero_when_quota_lowered(self):
        org = Organization.objects.create(name="Acme")
        profile = OrganizationBillingProfile.objects.create(organization=org, courtesy_workspace_quota=1)
        services.grant_courtesy(_workspace("A", org))
        services.grant_courtesy(_workspace("B", org))
        self.assertEqual(profile.courtesy_workspaces_remaining, 0)
        self.assertFalse(profile.has_courtesy_available)

    def test_quota_exceeded_reported_not_enforced(self):
        """Granting past the quota succeeds; the UI is told about it."""
        org = Organization.objects.create(name="Acme")
        OrganizationBillingProfile.objects.create(organization=org, courtesy_workspace_quota=1)
        services.grant_courtesy(_workspace("A", org))
        self.assertFalse(services.courtesy_quota_exceeded(org))
        services.grant_courtesy(_workspace("B", org))
        self.assertTrue(services.courtesy_quota_exceeded(org))

    def test_courtesy_without_profile_exceeds_zero_quota(self):
        org = Organization.objects.create(name="Acme")
        services.grant_courtesy(_workspace("A", org))
        self.assertTrue(services.courtesy_quota_exceeded(org))

    def test_quota_does_not_count_other_orgs(self):
        org = Organization.objects.create(name="Acme")
        other = Organization.objects.create(name="Other")
        profile = OrganizationBillingProfile.objects.create(organization=org, courtesy_workspace_quota=2)
        services.grant_courtesy(_workspace("Theirs", other))
        self.assertEqual(profile.courtesy_workspaces_used, 0)


class BillingEventIdempotencyTests(TestCase):
    def test_gateway_event_id_is_unique(self):
        BillingEvent.objects.create(gateway_event_id="evt_1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            BillingEvent.objects.create(gateway_event_id="evt_1")

    def test_record_event_claims_once(self):
        _event, created = services.record_event("evt_1", gateway="asaas", event_type="PAYMENT_CONFIRMED")
        self.assertTrue(created)

    def test_redelivered_event_is_not_reclaimed(self):
        """A retried webhook must be a no-op, not a second charge."""
        first, created_first = services.record_event("evt_1", gateway="asaas")
        second, created_second = services.record_event("evt_1", gateway="asaas")
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(BillingEvent.objects.filter(gateway_event_id="evt_1").count(), 1)

    def test_distinct_events_both_recorded(self):
        services.record_event("evt_1")
        services.record_event("evt_2")
        self.assertEqual(BillingEvent.objects.count(), 2)

    def test_record_event_stores_payload(self):
        event, _created = services.record_event(
            "evt_1",
            gateway="asaas",
            event_type="PAYMENT_CONFIRMED",
            payload={"id": "pay_123"},
        )
        event.refresh_from_db()
        self.assertEqual(event.payload, {"id": "pay_123"})
        self.assertEqual(event.event_type, "PAYMENT_CONFIRMED")

    def test_event_defaults_to_received_and_unprocessed(self):
        event, _created = services.record_event("evt_1")
        self.assertEqual(event.status, BillingEvent.Status.RECEIVED)
        self.assertIsNone(event.processed_at)

    def test_event_survives_subscription_deletion(self):
        """The audit trail must outlive the row it refers to."""
        ws = _workspace()
        sub = WorkspaceSubscription.objects.create(workspace=ws)
        event = BillingEvent.objects.create(gateway_event_id="evt_1", subscription=sub)
        sub.delete()
        event.refresh_from_db()
        self.assertIsNone(event.subscription_id)


class PriceListTests(TestCase):
    @override_settings(BILLING_WORKSPACE_PRICE_CENTS=5000)
    def test_workspace_price_reads_settings(self):
        self.assertEqual(services.workspace_price_cents(), 5000)

    @override_settings(BILLING_STORAGE_PRICE_PER_GB_CENTS=350)
    def test_storage_price_reads_settings(self):
        self.assertEqual(services.storage_price_per_gb_cents(), 350)

    @override_settings(BILLING_WORKSPACE_BASE_STORAGE_BYTES=20 * GB)
    def test_base_storage_reads_settings(self):
        self.assertEqual(services.base_storage_bytes(), 20 * GB)
