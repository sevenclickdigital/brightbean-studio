"""Tests for the Asaas webhook.

Posts real requests through the URL conf with a fake token, and asserts
on database state and status codes. Nothing asserts on response text:
the bodies here are for Asaas, not a user, and the UI arrives in a later
phase.

The status code matters as much as the state change — Asaas retries
anything that is not a 200 and suspends the queue after 15 consecutive
failures, so "200 for an event we ignore" is a behavioural requirement.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.billing.models import (
    BillingEvent,
    WorkspaceAddon,
    WorkspaceStorageAddon,
    WorkspaceSubscription,
)
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

TOKEN = "fake-webhook-token"
GB = WorkspaceAddon.BYTES_PER_GB


def _workspace(name="Workspace", org=None):
    org = org or Organization.objects.create(name="Acme")
    return Workspace.objects.create(organization=org, name=name)


def _payment_event(event, *, event_id="evt_1", subscription="sub_1", due_date=None):
    payload = {
        "id": event_id,
        "event": event,
        "payment": {
            "id": "pay_1",
            "customer": "cus_1",
            "subscription": subscription,
            "value": 50.0,
            "status": "CONFIRMED",
        },
    }
    if due_date:
        payload["payment"]["dueDate"] = due_date
    return payload


@override_settings(ASAAS_WEBHOOK_TOKEN=TOKEN, RATELIMIT_ENABLE=False)
class WebhookAuthTests(TestCase):
    def setUp(self):
        self.url = reverse("asaas_webhook")

    def test_valid_token_is_accepted(self):
        response = self.client.post(
            self.url,
            data=_payment_event("PAYMENT_CONFIRMED"),
            content_type="application/json",
            headers={"asaas-access-token": TOKEN},
        )
        self.assertEqual(response.status_code, 200)

    def test_wrong_token_is_refused(self):
        response = self.client.post(
            self.url,
            data=_payment_event("PAYMENT_CONFIRMED"),
            content_type="application/json",
            headers={"asaas-access-token": "wrong"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(BillingEvent.objects.count(), 0)

    def test_missing_token_header_is_refused(self):
        """The endpoint is public; an anonymous POST must not be trusted."""
        response = self.client.post(
            self.url,
            data=_payment_event("PAYMENT_CONFIRMED"),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(BillingEvent.objects.count(), 0)

    @override_settings(ASAAS_WEBHOOK_TOKEN="")
    def test_unconfigured_secret_refuses_everything(self):
        """No token configured means nothing can be verified — so refuse."""
        response = self.client.post(
            self.url,
            data=_payment_event("PAYMENT_CONFIRMED"),
            content_type="application/json",
            headers={"asaas-access-token": "anything"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(BillingEvent.objects.count(), 0)

    def test_get_is_rejected(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_malformed_json_is_a_400(self):
        response = self.client.post(
            self.url,
            data="not json",
            content_type="application/json",
            headers={"asaas-access-token": TOKEN},
        )
        self.assertEqual(response.status_code, 400)

    def test_event_without_an_id_is_refused(self):
        """No event id means no idempotency key, so we must not process."""
        response = self.client.post(
            self.url,
            data={"event": "PAYMENT_CONFIRMED", "payment": {"subscription": "sub_1"}},
            content_type="application/json",
            headers={"asaas-access-token": TOKEN},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(BillingEvent.objects.count(), 0)


@override_settings(ASAAS_WEBHOOK_TOKEN=TOKEN, RATELIMIT_ENABLE=False)
class WebhookEventTests(TestCase):
    def setUp(self):
        self.url = reverse("asaas_webhook")
        self.workspace = _workspace()
        self.sub = WorkspaceSubscription.objects.create(
            workspace=self.workspace,
            price_cents=5000,
            base_storage_bytes=10 * GB,
            gateway="asaas",
            gateway_customer_id="cus_1",
            gateway_subscription_id="sub_1",
        )

    def _post(self, payload):
        return self.client.post(
            self.url,
            data=payload,
            content_type="application/json",
            headers={"asaas-access-token": TOKEN},
        )

    def _reload(self):
        self.sub.refresh_from_db()
        return self.sub

    # -- payment confirmed ------------------------------------------------

    def test_payment_confirmed_activates_the_subscription(self):
        response = self._post(_payment_event("PAYMENT_CONFIRMED"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._reload().status, WorkspaceSubscription.Status.ACTIVE)
        self.assertTrue(self._reload().is_active)

    def test_payment_received_also_activates(self):
        """Boleto sends RECEIVED separately from CONFIRMED."""
        self._post(_payment_event("PAYMENT_RECEIVED"))
        self.assertEqual(self._reload().status, WorkspaceSubscription.Status.ACTIVE)

    def test_payment_confirmed_records_the_period_end(self):
        self._post(_payment_event("PAYMENT_CONFIRMED", due_date="2026-12-09"))
        period_end = self._reload().current_period_end
        self.assertIsNotNone(period_end)
        self.assertEqual(period_end.date().isoformat(), "2026-12-09")

    def test_activation_without_a_due_date_leaves_period_null(self):
        """NULL already reads as 'not reported yet'; don't guess a date."""
        self._post(_payment_event("PAYMENT_CONFIRMED"))
        sub = self._reload()
        self.assertEqual(sub.status, WorkspaceSubscription.Status.ACTIVE)
        self.assertIsNone(sub.current_period_end)
        # Still current: a webhook delay must not read as a lapse.
        self.assertTrue(sub.is_current)

    def test_recovered_payment_clears_the_cancel_tombstone(self):
        self._post(_payment_event("SUBSCRIPTION_DELETED", event_id="evt_cancel"))
        self.assertIsNotNone(self._reload().canceled_at)
        self._post(_payment_event("PAYMENT_CONFIRMED", event_id="evt_paid"))
        sub = self._reload()
        self.assertEqual(sub.status, WorkspaceSubscription.Status.ACTIVE)
        self.assertIsNone(sub.canceled_at)

    # -- payment failed ---------------------------------------------------

    def test_payment_overdue_marks_past_due(self):
        self.sub.status = WorkspaceSubscription.Status.ACTIVE
        self.sub.save(update_fields=["status"])
        self._post(_payment_event("PAYMENT_OVERDUE"))
        sub = self._reload()
        self.assertEqual(sub.status, WorkspaceSubscription.Status.PAST_DUE)
        self.assertFalse(sub.is_active)

    def test_refund_marks_past_due(self):
        self._post(_payment_event("PAYMENT_REFUNDED"))
        self.assertEqual(self._reload().status, WorkspaceSubscription.Status.PAST_DUE)

    def test_chargeback_marks_past_due(self):
        self._post(_payment_event("PAYMENT_CHARGEBACK_REQUESTED"))
        self.assertEqual(self._reload().status, WorkspaceSubscription.Status.PAST_DUE)

    def test_past_due_keeps_the_gateway_ids_so_recovery_works(self):
        self._post(_payment_event("PAYMENT_OVERDUE"))
        sub = self._reload()
        self.assertEqual(sub.gateway_subscription_id, "sub_1")
        self._post(_payment_event("PAYMENT_CONFIRMED", event_id="evt_2"))
        self.assertEqual(self._reload().status, WorkspaceSubscription.Status.ACTIVE)

    # -- cancellation -----------------------------------------------------

    def test_subscription_deleted_cancels(self):
        payload = {
            "id": "evt_cancel",
            "event": "SUBSCRIPTION_DELETED",
            "subscription": {"id": "sub_1", "status": "INACTIVE"},
        }
        self._post(payload)
        sub = self._reload()
        self.assertEqual(sub.status, WorkspaceSubscription.Status.CANCELED)
        self.assertIsNotNone(sub.canceled_at)
        self.assertFalse(sub.is_active)

    # -- unknown and unmatched -------------------------------------------

    def test_unknown_event_is_a_200_and_changes_nothing(self):
        """An error here would make Asaas retry until its queue suspends."""
        response = self._post(_payment_event("PAYMENT_BANK_SLIP_VIEWED"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._reload().status, WorkspaceSubscription.Status.PENDING)
        event = BillingEvent.objects.get(gateway_event_id="evt_1")
        self.assertEqual(event.status, BillingEvent.Status.IGNORED)

    def test_event_for_an_unknown_subscription_is_a_200(self):
        response = self._post(_payment_event("PAYMENT_CONFIRMED", subscription="sub_stranger"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._reload().status, WorkspaceSubscription.Status.PENDING)
        event = BillingEvent.objects.get(gateway_event_id="evt_1")
        self.assertEqual(event.status, BillingEvent.Status.IGNORED)

    def test_one_off_payment_without_a_subscription_is_a_200(self):
        payload = {
            "id": "evt_1",
            "event": "PAYMENT_CONFIRMED",
            "payment": {"id": "pay_1", "customer": "cus_1", "value": 50.0},
        }
        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(self._reload().status, WorkspaceSubscription.Status.PENDING)

    def test_event_does_not_touch_another_workspace(self):
        other = _workspace("Other")
        other_sub = WorkspaceSubscription.objects.create(
            workspace=other,
            gateway_subscription_id="sub_other",
        )
        self._post(_payment_event("PAYMENT_CONFIRMED", subscription="sub_1"))
        other_sub.refresh_from_db()
        self.assertEqual(other_sub.status, WorkspaceSubscription.Status.PENDING)

    # -- courtesy ---------------------------------------------------------

    def test_courtesy_subscription_is_never_dunned(self):
        """Nobody charges a comped workspace, so a failure is stale."""
        self.sub.status = WorkspaceSubscription.Status.COURTESY
        self.sub.save(update_fields=["status"])
        self._post(_payment_event("PAYMENT_OVERDUE"))
        self.assertEqual(self._reload().status, WorkspaceSubscription.Status.COURTESY)

    def test_courtesy_subscription_is_not_overwritten_by_a_payment(self):
        self.sub.status = WorkspaceSubscription.Status.COURTESY
        self.sub.save(update_fields=["status"])
        self._post(_payment_event("PAYMENT_CONFIRMED"))
        self.assertEqual(self._reload().status, WorkspaceSubscription.Status.COURTESY)

    # -- idempotency ------------------------------------------------------

    def test_redelivery_is_a_no_op(self):
        """Asaas retries until it sees a 200; the replay must not re-apply."""
        first = self._post(_payment_event("PAYMENT_CONFIRMED", event_id="evt_same"))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self._reload().status, WorkspaceSubscription.Status.ACTIVE)

        # Simulate state having moved on between the two deliveries.
        self.sub.status = WorkspaceSubscription.Status.PAST_DUE
        self.sub.save(update_fields=["status"])

        second = self._post(_payment_event("PAYMENT_CONFIRMED", event_id="evt_same"))
        self.assertEqual(second.status_code, 200)
        # Untouched: the redelivery did not re-activate.
        self.assertEqual(self._reload().status, WorkspaceSubscription.Status.PAST_DUE)
        self.assertEqual(BillingEvent.objects.filter(gateway_event_id="evt_same").count(), 1)

    def test_distinct_events_are_both_applied(self):
        self._post(_payment_event("PAYMENT_CONFIRMED", event_id="evt_1"))
        self.assertEqual(self._reload().status, WorkspaceSubscription.Status.ACTIVE)
        self._post(_payment_event("PAYMENT_OVERDUE", event_id="evt_2"))
        self.assertEqual(self._reload().status, WorkspaceSubscription.Status.PAST_DUE)
        self.assertEqual(BillingEvent.objects.count(), 2)

    # -- audit trail ------------------------------------------------------

    def test_processed_event_is_logged_against_the_subscription(self):
        self._post(_payment_event("PAYMENT_CONFIRMED"))
        event = BillingEvent.objects.get(gateway_event_id="evt_1")
        self.assertEqual(event.status, BillingEvent.Status.PROCESSED)
        self.assertEqual(event.event_type, "PAYMENT_CONFIRMED")
        self.assertEqual(event.subscription_id, self.sub.pk)
        self.assertIsNotNone(event.processed_at)
        self.assertEqual(event.gateway, "asaas")

    def test_raw_payload_is_kept_for_audit(self):
        self._post(_payment_event("PAYMENT_CONFIRMED"))
        event = BillingEvent.objects.get(gateway_event_id="evt_1")
        self.assertEqual(event.payload["payment"]["id"], "pay_1")


@override_settings(ASAAS_WEBHOOK_TOKEN=TOKEN, RATELIMIT_ENABLE=False)
class WebhookAddonTests(TestCase):
    def setUp(self):
        self.url = reverse("asaas_webhook")
        self.workspace = _workspace()
        WorkspaceSubscription.objects.create(
            workspace=self.workspace,
            base_storage_bytes=10 * GB,
            status=WorkspaceSubscription.Status.ACTIVE,
            gateway_subscription_id="sub_base",
        )
        self.addon = WorkspaceStorageAddon.objects.create(
            workspace=self.workspace,
            quantity=25,
            unit_price_cents=200,
            gateway="asaas",
            gateway_subscription_id="sub_addon",
        )

    def _post(self, payload):
        return self.client.post(
            self.url,
            data=payload,
            content_type="application/json",
            headers={"asaas-access-token": TOKEN},
        )

    def test_payment_confirmed_activates_the_addon_and_grants_disk(self):
        from apps.billing import services

        self.assertEqual(services.total_storage_bytes(self.workspace), 10 * GB)
        response = self._post(_payment_event("PAYMENT_CONFIRMED", subscription="sub_addon"))
        self.assertEqual(response.status_code, 200)
        self.addon.refresh_from_db()
        self.assertEqual(self.addon.status, WorkspaceAddon.Status.ACTIVE)
        self.assertEqual(services.total_storage_bytes(self.workspace), 35 * GB)

    def test_addon_payment_does_not_touch_the_base_subscription(self):
        self._post(_payment_event("PAYMENT_OVERDUE", subscription="sub_addon"))
        base = WorkspaceSubscription.objects.get(workspace=self.workspace)
        self.assertEqual(base.status, WorkspaceSubscription.Status.ACTIVE)

    def test_unpaid_addon_loses_its_disk_but_keeps_its_row(self):
        from apps.billing import services

        self._post(_payment_event("PAYMENT_CONFIRMED", event_id="evt_paid", subscription="sub_addon"))
        self.assertEqual(services.total_storage_bytes(self.workspace), 35 * GB)

        self._post(_payment_event("PAYMENT_OVERDUE", event_id="evt_late", subscription="sub_addon"))
        self.addon.refresh_from_db()
        self.assertEqual(self.addon.status, WorkspaceAddon.Status.PENDING)
        self.assertEqual(services.total_storage_bytes(self.workspace), 10 * GB)
        # The sold price survives for when payment recovers.
        self.assertEqual(self.addon.unit_price_cents, 200)

    def test_subscription_deleted_cancels_the_addon(self):
        payload = {
            "id": "evt_cancel",
            "event": "SUBSCRIPTION_DELETED",
            "subscription": {"id": "sub_addon"},
        }
        self._post(payload)
        self.addon.refresh_from_db()
        self.assertEqual(self.addon.status, WorkspaceAddon.Status.CANCELED)
        self.assertIsNotNone(self.addon.canceled_at)

    def test_addon_event_is_logged_against_the_addon(self):
        self._post(_payment_event("PAYMENT_CONFIRMED", subscription="sub_addon"))
        event = BillingEvent.objects.get(gateway_event_id="evt_1")
        self.assertEqual(event.addon_id, self.addon.pk)
        self.assertIsNone(event.subscription_id)
        self.assertEqual(event.status, BillingEvent.Status.PROCESSED)
