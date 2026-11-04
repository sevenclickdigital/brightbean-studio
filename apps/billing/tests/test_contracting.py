"""Tests for the contracting flow.

The gateway is a stub that counts calls, because the property that
matters here is not what JSON we send (``test_asaas_client`` covers
that) but how many times we send it: contracting twice must not bill
twice. Behaviour and database state only.
"""

from __future__ import annotations

from django.test import TestCase, override_settings

from apps.billing import contracting, services
from apps.billing.gateways.asaas import AsaasAPIError, AsaasTransportError
from apps.billing.models import (
    OrganizationBillingProfile,
    WorkspaceAddon,
    WorkspaceStorageAddon,
    WorkspaceSubscription,
)
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


class StubAsaas:
    """A fake Asaas that behaves like the real one's happy path.

    Keeps the objects it was asked to create, so a test can assert both
    "a subscription exists" and "exactly one was created" — the second
    being the actual idempotency claim.
    """

    def __init__(self, *, existing_customer=None, existing_subscription=None):
        self.customers = []
        self.subscriptions = []
        self.canceled = []
        self.existing_customer = existing_customer
        self.existing_subscription = existing_subscription
        self._next = 0

    def _id(self, prefix):
        self._next += 1
        return f"{prefix}_{self._next}"

    def create_customer(self, *, name, external_reference, email="", **_kwargs):
        record = {"id": self._id("cus"), "name": name, "externalReference": str(external_reference)}
        self.customers.append(record)
        return record

    def find_customer_by_reference(self, external_reference):
        return self.existing_customer

    def create_subscription(self, *, customer_id, value_cents, external_reference, **_kwargs):
        record = {
            "id": self._id("sub"),
            "customer": customer_id,
            "value_cents": value_cents,
            "externalReference": str(external_reference),
        }
        self.subscriptions.append(record)
        return record

    def find_subscription_by_reference(self, external_reference):
        return self.existing_subscription

    def cancel_subscription(self, subscription_id):
        self.canceled.append(subscription_id)
        return {"deleted": True, "id": subscription_id}


class BrokenAsaas(StubAsaas):
    """A gateway that fails on create, to test the local row is unharmed."""

    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def create_customer(self, **_kwargs):
        raise self.exc

    def create_subscription(self, **_kwargs):
        raise self.exc


def _workspace(name="Workspace", org=None):
    org = org or Organization.objects.create(name="Acme")
    return Workspace.objects.create(organization=org, name=name)


@override_settings(BILLING_WORKSPACE_PRICE_CENTS=5000)
class CustomerTests(TestCase):
    def test_creates_customer_and_stores_id_on_the_org_profile(self):
        """The payer is the Organization, not the workspace."""
        org = Organization.objects.create(name="Acme")
        client = StubAsaas()
        profile = contracting.ensure_customer(org, client=client)
        self.assertTrue(profile.gateway_customer_id.startswith("cus_"))
        self.assertEqual(profile.gateway, "asaas")
        self.assertEqual(len(client.customers), 1)

    def test_second_call_reuses_the_stored_customer(self):
        org = Organization.objects.create(name="Acme")
        client = StubAsaas()
        first = contracting.ensure_customer(org, client=client)
        second = contracting.ensure_customer(org, client=client)
        self.assertEqual(first.gateway_customer_id, second.gateway_customer_id)
        self.assertEqual(len(client.customers), 1)

    def test_adopts_a_customer_asaas_already_has(self):
        """Recovers from a create whose response we never received."""
        org = Organization.objects.create(name="Acme")
        client = StubAsaas(existing_customer={"id": "cus_orphan"})
        profile = contracting.ensure_customer(org, client=client)
        self.assertEqual(profile.gateway_customer_id, "cus_orphan")
        self.assertEqual(client.customers, [])

    def test_external_reference_is_the_organization_id(self):
        org = Organization.objects.create(name="Acme")
        client = StubAsaas()
        contracting.ensure_customer(org, client=client)
        self.assertEqual(client.customers[0]["externalReference"], str(org.pk))

    def test_one_profile_serves_every_workspace_in_the_org(self):
        """Card details entered once cover the org's other workspaces."""
        org = Organization.objects.create(name="Acme")
        client = StubAsaas()
        contracting.subscribe_workspace(_workspace("A", org), client=client)
        contracting.subscribe_workspace(_workspace("B", org), client=client)
        self.assertEqual(len(client.customers), 1)
        self.assertEqual(len(client.subscriptions), 2)
        self.assertEqual(OrganizationBillingProfile.objects.filter(organization=org).count(), 1)


@override_settings(BILLING_WORKSPACE_PRICE_CENTS=5000)
class SubscribeWorkspaceTests(TestCase):
    def test_stores_both_gateway_ids(self):
        ws = _workspace()
        client = StubAsaas()
        sub = contracting.subscribe_workspace(ws, client=client)
        self.assertTrue(sub.gateway_subscription_id.startswith("sub_"))
        self.assertTrue(sub.gateway_customer_id.startswith("cus_"))
        self.assertEqual(sub.gateway, "asaas")

    def test_subscription_starts_pending_not_active(self):
        """An agreement to pay is not a payment; the webhook activates."""
        sub = contracting.subscribe_workspace(_workspace(), client=StubAsaas())
        self.assertEqual(sub.status, WorkspaceSubscription.Status.PENDING)
        self.assertFalse(sub.is_active)

    def test_charges_the_price_snapshotted_on_the_row(self):
        ws = _workspace()
        client = StubAsaas()
        contracting.subscribe_workspace(ws, client=client)
        self.assertEqual(client.subscriptions[0]["value_cents"], 5000)

    def test_contracting_twice_creates_one_subscription(self):
        """The double-clicked modal must not bill twice."""
        ws = _workspace()
        client = StubAsaas()
        first = contracting.subscribe_workspace(ws, client=client)
        second = contracting.subscribe_workspace(ws, client=client)
        self.assertEqual(first.gateway_subscription_id, second.gateway_subscription_id)
        self.assertEqual(len(client.subscriptions), 1)
        self.assertEqual(WorkspaceSubscription.objects.filter(workspace=ws).count(), 1)

    def test_already_contracted_workspace_makes_no_gateway_call(self):
        ws = _workspace()
        contracting.subscribe_workspace(ws, client=StubAsaas())
        fresh = StubAsaas()
        contracting.subscribe_workspace(ws, client=fresh)
        self.assertEqual(fresh.subscriptions, [])
        self.assertEqual(fresh.customers, [])

    def test_adopts_a_subscription_asaas_already_has(self):
        ws = _workspace()
        client = StubAsaas(existing_subscription={"id": "sub_orphan"})
        sub = contracting.subscribe_workspace(ws, client=client)
        self.assertEqual(sub.gateway_subscription_id, "sub_orphan")
        self.assertEqual(client.subscriptions, [])

    def test_external_reference_is_the_workspace_id(self):
        ws = _workspace()
        client = StubAsaas()
        contracting.subscribe_workspace(ws, client=client)
        self.assertEqual(client.subscriptions[0]["externalReference"], str(ws.pk))

    def test_courtesy_workspace_is_never_charged(self):
        """A comped workspace has no gateway subscription by design."""
        ws = _workspace()
        services.grant_courtesy(ws, note="founding customer")
        sub = contracting.subscribe_workspace(ws, client=(client := StubAsaas()))
        self.assertEqual(sub.status, WorkspaceSubscription.Status.COURTESY)
        self.assertEqual(sub.gateway_subscription_id, "")
        self.assertEqual(client.subscriptions, [])

    def test_two_workspaces_get_distinct_subscriptions(self):
        org = Organization.objects.create(name="Acme")
        client = StubAsaas()
        a = contracting.subscribe_workspace(_workspace("A", org), client=client)
        b = contracting.subscribe_workspace(_workspace("B", org), client=client)
        self.assertNotEqual(a.gateway_subscription_id, b.gateway_subscription_id)
        # Same payer, different subscriptions.
        self.assertEqual(a.gateway_customer_id, b.gateway_customer_id)


@override_settings(BILLING_WORKSPACE_PRICE_CENTS=5000)
class GatewayFailureTests(TestCase):
    def test_transport_failure_leaves_no_gateway_ids_behind(self):
        """A clean retry must be possible after a timeout."""
        ws = _workspace()
        with self.assertRaises(AsaasTransportError):
            contracting.subscribe_workspace(ws, client=BrokenAsaas(AsaasTransportError("down")))
        sub = WorkspaceSubscription.objects.get(workspace=ws)
        self.assertEqual(sub.gateway_subscription_id, "")
        self.assertEqual(sub.status, WorkspaceSubscription.Status.PENDING)

    def test_retry_after_failure_succeeds(self):
        ws = _workspace()
        with self.assertRaises(AsaasAPIError):
            contracting.subscribe_workspace(ws, client=BrokenAsaas(AsaasAPIError("bad", status_code=400)))
        sub = contracting.subscribe_workspace(ws, client=StubAsaas())
        self.assertTrue(sub.gateway_subscription_id.startswith("sub_"))

    def test_api_error_propagates_to_the_caller(self):
        with self.assertRaises(AsaasAPIError):
            contracting.subscribe_workspace(
                _workspace(),
                client=BrokenAsaas(AsaasAPIError("bad", status_code=400)),
            )


@override_settings(BILLING_STORAGE_PRICE_PER_GB_CENTS=200)
class StorageAddonContractingTests(TestCase):
    def test_addon_gets_its_own_gateway_subscription(self):
        """Asaas has no line items, so an add-on is a second charge."""
        ws = _workspace()
        addon = services.contract_storage_addon(ws, 10)
        client = StubAsaas()
        contracting.subscribe_storage_addon(addon, client=client)
        addon.refresh_from_db()
        self.assertTrue(addon.gateway_subscription_id.startswith("sub_"))
        self.assertEqual(client.subscriptions[0]["value_cents"], 2000)

    def test_addon_stays_pending_until_paid(self):
        ws = _workspace()
        WorkspaceSubscription.objects.create(workspace=ws, base_storage_bytes=0)
        addon = services.contract_storage_addon(ws, 10)
        contracting.subscribe_storage_addon(addon, client=StubAsaas())
        addon.refresh_from_db()
        self.assertEqual(addon.status, WorkspaceAddon.Status.PENDING)
        # Pending grants no disk — the rule from phase 2.
        self.assertEqual(services.total_storage_bytes(ws), 0)

    def test_contracting_addon_twice_creates_one_subscription(self):
        addon = services.contract_storage_addon(_workspace(), 10)
        client = StubAsaas()
        contracting.subscribe_storage_addon(addon, client=client)
        contracting.subscribe_storage_addon(addon, client=client)
        self.assertEqual(len(client.subscriptions), 1)

    def test_addon_reference_cannot_collide_with_the_workspace(self):
        ws = _workspace()
        addon = WorkspaceStorageAddon.objects.create(workspace=ws, quantity=5, unit_price_cents=200)
        client = StubAsaas()
        contracting.subscribe_workspace(ws, client=client)
        contracting.subscribe_storage_addon(addon, client=client)
        references = {s["externalReference"] for s in client.subscriptions}
        self.assertEqual(len(references), 2)
        self.assertIn(f"addon:{addon.pk}", references)


@override_settings(BILLING_WORKSPACE_PRICE_CENTS=5000)
class CancelTests(TestCase):
    def test_cancel_calls_the_gateway_and_marks_the_row(self):
        ws = _workspace()
        client = StubAsaas()
        sub = contracting.subscribe_workspace(ws, client=client)
        subscription_id = sub.gateway_subscription_id
        canceled = contracting.cancel_workspace_subscription(ws, client=client)
        self.assertEqual(canceled.status, WorkspaceSubscription.Status.CANCELED)
        self.assertIsNotNone(canceled.canceled_at)
        self.assertEqual(client.canceled, [subscription_id])
        self.assertFalse(canceled.is_active)

    def test_cancel_keeps_the_external_ids_for_audit(self):
        ws = _workspace()
        client = StubAsaas()
        contracting.subscribe_workspace(ws, client=client)
        canceled = contracting.cancel_workspace_subscription(ws, client=client)
        self.assertNotEqual(canceled.gateway_subscription_id, "")

    def test_cancel_without_a_gateway_subscription_still_records_intent(self):
        ws = _workspace()
        client = StubAsaas()
        canceled = contracting.cancel_workspace_subscription(ws, client=client)
        self.assertEqual(canceled.status, WorkspaceSubscription.Status.CANCELED)
        self.assertEqual(client.canceled, [])

    def test_gateway_404_is_treated_as_already_canceled(self):
        """Absent upstream is the state we were asking for."""
        ws = _workspace()
        client = StubAsaas()
        contracting.subscribe_workspace(ws, client=client)

        def _gone(subscription_id):
            raise AsaasAPIError("not found", status_code=404)

        client.cancel_subscription = _gone
        canceled = contracting.cancel_workspace_subscription(ws, client=client)
        self.assertEqual(canceled.status, WorkspaceSubscription.Status.CANCELED)

    def test_gateway_server_error_aborts_the_cancel(self):
        """Don't claim a cancel we could not actually perform."""
        ws = _workspace()
        client = StubAsaas()
        contracting.subscribe_workspace(ws, client=client)

        def _broken(subscription_id):
            raise AsaasAPIError("boom", status_code=500)

        client.cancel_subscription = _broken
        with self.assertRaises(AsaasAPIError):
            contracting.cancel_workspace_subscription(ws, client=client)
        sub = WorkspaceSubscription.objects.get(workspace=ws)
        self.assertNotEqual(sub.status, WorkspaceSubscription.Status.CANCELED)
