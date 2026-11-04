"""Inbound webhook from Asaas: what the gateway says happened to the money.

This is the only thing that moves a subscription into ``active``.
Contracting creates the subscription and leaves it ``pending`` — an
agreement to pay is not a payment, and for boleto or Pix the two can be
days apart. Access follows the confirmed payment, which arrives here.

Verification, per Asaas's docs: the token configured beside the webhook
URL in their panel comes back on every delivery in an
``asaas-access-token`` header. There is no HMAC over the body, so the
token is the whole of the authentication and a missing token means we
cannot authenticate at all — hence the refusal below rather than
trusting an anonymous POST. Asaas's own docs warn against reusing the
API key as this token, so ``ASAAS_WEBHOOK_TOKEN`` is a separate secret.

Status mapping, deliberately narrow:

    PAYMENT_CONFIRMED / PAYMENT_RECEIVED        → active
    PAYMENT_OVERDUE / *_REFUNDED / *_CHARGEBACK → past_due
    SUBSCRIPTION_DELETED                        → canceled

Anything else gets a 200 and an ``ignored`` event row. Returning an
error for an event we simply don't handle would make Asaas retry it
until their queue suspends after 15 consecutive failures, taking the
events we *do* care about down with it.
"""

from __future__ import annotations

import datetime
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from apps.billing import services
from apps.billing.gateways.asaas import GATEWAY
from apps.billing.models import BillingEvent, WorkspaceAddon, WorkspaceSubscription

logger = logging.getLogger(__name__)

# A payment settled. Asaas sends CONFIRMED when it considers the money
# guaranteed and RECEIVED when it has actually landed; for card and Pix
# they arrive together, for boleto a day apart. Either is enough to grant
# access — waiting for RECEIVED would lock out a customer whose card
# Asaas has already confirmed.
PAID_EVENTS = frozenset({"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"})

# The money went away or never came. All of these block access, and all
# are recoverable: a later PAYMENT_CONFIRMED flips the row back to active
# without the external ids ever being lost.
UNPAID_EVENTS = frozenset(
    {
        "PAYMENT_OVERDUE",
        "PAYMENT_REFUNDED",
        "PAYMENT_CHARGEBACK_REQUESTED",
        "PAYMENT_REVERSED",
        "PAYMENT_DELETED",
    }
)

CANCELED_EVENTS = frozenset({"SUBSCRIPTION_DELETED"})


@csrf_exempt
@require_POST
@ratelimit(key="ip", rate="120/m", block=True)
def asaas_webhook(request):
    """Apply one Asaas event to the local billing state.

    Answers 200 for every authenticated request it can parse, including
    events it does not act on — see the module docstring on why an error
    here is actively harmful.
    """
    token = getattr(settings, "ASAAS_WEBHOOK_TOKEN", "")
    if not token:
        # Refuse rather than accept unverified input: with no token
        # configured there is nothing to check, and an anonymous caller
        # could otherwise activate any workspace they liked for free.
        logger.error("Asaas webhook called but ASAAS_WEBHOOK_TOKEN is not configured")
        return HttpResponseForbidden("Webhook token not configured.")

    if not _verify(request, token):
        logger.warning("Asaas webhook: access token did not match")
        return HttpResponseForbidden("Invalid token.")

    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return HttpResponse(status=400)
    if not isinstance(payload, dict):
        return HttpResponse(status=400)

    event_type = payload.get("event") or ""
    event_id = payload.get("id") or ""
    if not event_id:
        # Without the gateway's event id there is no idempotency key, and
        # processing an unkeyed event risks double-applying it on retry.
        logger.warning("Asaas webhook: payload carried no event id (%s)", event_type)
        return HttpResponse(status=400)

    event, created = services.record_event(
        event_id,
        gateway=GATEWAY,
        event_type=event_type,
        payload=payload,
    )
    if not created:
        # A redelivery. Asaas retries until it sees a 200, so this is the
        # normal consequence of our first response being slow — not an
        # error, and emphatically not a reason to apply the event twice.
        logger.info("Asaas webhook: ignoring redelivery of %s", event_id)
        return HttpResponse(status=200)

    try:
        _apply(event, event_type, payload)
    except Exception:
        # Record the failure and still answer 200. A retry would replay
        # an event already claimed by ``record_event`` and so be a no-op
        # anyway; the ``failed`` row is what a sweeper (or a human) acts
        # on. Logged with the event id only — the payload holds payment
        # data and goes to the audit column, not to the log stream.
        logger.exception("Asaas webhook: failed to apply %s (%s)", event_id, event_type)
        event.status = BillingEvent.Status.FAILED
        event.error = "Handler raised; see BillingEvent.payload for the event."
        event.save(update_fields=["status", "error"])

    return HttpResponse(status=200)


def _verify(request, token):
    """True when the request carries the configured Asaas token.

    Compared with ``compare_digest`` so a wrong guess cannot be refined
    by timing the response.
    """
    presented = request.headers.get("asaas-access-token", "")
    if not presented:
        return False
    return hmac.compare_digest(presented, token)


def _apply(event, event_type, payload):
    """Route one event to the status change it implies."""
    if event_type in CANCELED_EVENTS:
        target = WorkspaceSubscription.Status.CANCELED
    elif event_type in PAID_EVENTS:
        target = WorkspaceSubscription.Status.ACTIVE
    elif event_type in UNPAID_EVENTS:
        target = WorkspaceSubscription.Status.PAST_DUE
    else:
        _finish(event, BillingEvent.Status.IGNORED)
        return

    subscription_id = _subscription_id(payload)
    if not subscription_id:
        # A one-off charge, or a subscription event shaped differently
        # than we expect. Kept as an audit row rather than dropped.
        _finish(event, BillingEvent.Status.IGNORED)
        return

    sub = WorkspaceSubscription.objects.filter(
        gateway_subscription_id=subscription_id,
    ).first()
    if sub is not None:
        _apply_to_subscription(sub, target, payload)
        event.subscription = sub
        _finish(event, BillingEvent.Status.PROCESSED)
        return

    addon = WorkspaceAddon.objects.filter(gateway_subscription_id=subscription_id).first()
    if addon is not None:
        _apply_to_addon(addon, target, payload)
        event.addon = addon
        _finish(event, BillingEvent.Status.PROCESSED)
        return

    # An event for a subscription we have no row for — created straight
    # in the Asaas dashboard, or since deleted here. The payload is kept
    # precisely because this is the case someone will need to explain.
    logger.warning("Asaas webhook: no local row for subscription %s", subscription_id)
    _finish(event, BillingEvent.Status.IGNORED)


def _apply_to_subscription(sub, target, payload):
    """Move a workspace subscription to ``target``.

    A courtesy row is never touched: there is no gateway subscription
    behind a comped workspace, so an event claiming otherwise is stale
    and applying it would start dunning a workspace an admin comped.
    """
    if sub.is_courtesy:
        return

    fields = ["status", "updated_at"]
    sub.status = target

    if target == WorkspaceSubscription.Status.ACTIVE:
        period_end = _due_date(payload)
        if period_end is not None:
            sub.current_period_end = period_end
            fields.append("current_period_end")
        # Clear the tombstone so a recovered customer doesn't read as
        # canceled-but-active.
        if sub.canceled_at is not None:
            sub.canceled_at = None
            fields.append("canceled_at")
    elif target == WorkspaceSubscription.Status.CANCELED:
        sub.canceled_at = timezone.now()
        fields.append("canceled_at")

    sub.save(update_fields=fields)


def _apply_to_addon(addon, target, payload):
    """Move an add-on to the status the event implies.

    ``WorkspaceAddon`` has no ``past_due``: an add-on is a grant of disk,
    and the meaningful question is only whether it is active. An unpaid
    add-on therefore goes back to ``pending``, which withdraws the disk
    (``extra_storage_bytes`` counts active rows only) while keeping the
    row and its sold price for when payment recovers.
    """
    fields = ["status", "updated_at"]
    if target == WorkspaceSubscription.Status.ACTIVE:
        addon.status = WorkspaceAddon.Status.ACTIVE
        period_end = _due_date(payload)
        if period_end is not None:
            addon.current_period_end = period_end
            fields.append("current_period_end")
    elif target == WorkspaceSubscription.Status.CANCELED:
        addon.status = WorkspaceAddon.Status.CANCELED
        addon.canceled_at = timezone.now()
        fields.append("canceled_at")
    else:
        addon.status = WorkspaceAddon.Status.PENDING

    addon.save(update_fields=fields)


def _subscription_id(payload):
    """The Asaas subscription id an event refers to, or "".

    Payment events nest it under ``payment.subscription``; subscription
    events carry the object itself under ``subscription.id``. Both shapes
    are handled here so the callers stay about status, not JSON.
    """
    payment = payload.get("payment")
    if isinstance(payment, dict):
        subscription = payment.get("subscription")
        if isinstance(subscription, str) and subscription:
            return subscription
        if isinstance(subscription, dict):
            return subscription.get("id") or ""

    subscription = payload.get("subscription")
    if isinstance(subscription, dict):
        return subscription.get("id") or ""
    if isinstance(subscription, str):
        return subscription
    return ""


def _due_date(payload):
    """The paid period's end, from the payment's ``dueDate``.

    Asaas dates are plain ``YYYY-MM-DD``; stored as an aware datetime in
    the project timezone. Returns None rather than guessing when the
    field is absent or malformed — ``current_period_end`` being NULL is
    already handled as "the gateway hasn't told us yet", which is a
    truthful state, whereas a wrong date would read as a lapse.
    """
    payment = payload.get("payment")
    if not isinstance(payment, dict):
        return None
    raw = payment.get("dueDate") or ""
    if not isinstance(raw, str) or not raw:
        return None
    parsed = parse_date(raw)
    if parsed is None:
        return None
    return timezone.make_aware(
        datetime.datetime.combine(parsed, datetime.time.max),
        timezone.get_current_timezone(),
    )


def _finish(event, status):
    """Close out the event row with its outcome."""
    event.status = status
    event.processed_at = timezone.now()
    event.save(update_fields=["status", "processed_at", "subscription", "addon"])
