"""Contracting: turning a local subscription row into a live Asaas one.

Sits between ``services.py`` (pure local arithmetic, no network) and
``gateways/asaas.py`` (pure HTTP, no database). The dependency runs one
way only — ``services`` never imports this module — so the money rules
stay testable without a gateway.

The split of roles across the two systems is the thing to keep in mind:

    the PAYER is the Organization  → OrganizationBillingProfile
                                     .gateway_customer_id  (cus_…)
    the SUBSCRIPTION is per Workspace → WorkspaceSubscription
                                     .gateway_subscription_id  (sub_…)

Asaas keys payment methods to the customer, so an organization with five
workspaces is one customer and five subscriptions. Entering card details
once then covers every workspace the org adds later.

Everything here is idempotent, because the modal in the UI is one click
away from being double-submitted and the recovery path after a timeout
is a retry. Idempotency is held in three layers:

1. The local row is checked first. A workspace that already has a
   ``gateway_subscription_id`` is returned untouched — no HTTP at all.
2. Before creating, Asaas is asked whether an object already exists for
   our ``externalReference``. This is what covers the nasty case: the
   POST succeeded, the response never arrived, and we have no id.
3. ``externalReference`` is the local primary key (org id for the
   customer, workspace id for the subscription), so step 2 can only ever
   match the object we would have created.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.billing import services
from apps.billing.gateways import asaas as asaas_gateway
from apps.billing.gateways.asaas import GATEWAY, AsaasClient, AsaasError
from apps.billing.models import WorkspaceSubscription

logger = logging.getLogger(__name__)


def ensure_customer(organization, *, client=None, email=""):
    """Make sure the organization exists as a customer in Asaas.

    Returns the ``OrganizationBillingProfile`` carrying the id. Safe to
    call repeatedly: the stored id short-circuits, and a stored id that
    predates a gateway switch is replaced rather than reused.
    """
    client = client or AsaasClient()
    profile = services.ensure_billing_profile(organization)

    if profile.gateway_customer_id and profile.gateway == GATEWAY:
        return profile

    # Asaas may already hold a customer for this org from a call whose
    # response we lost. Adopting it beats creating a duplicate, which
    # would strand the org's saved card on the orphan.
    existing = client.find_customer_by_reference(organization.pk)
    if existing is None:
        existing = client.create_customer(
            name=organization.name,
            external_reference=organization.pk,
            email=email or getattr(organization, "billing_email", "") or "",
        )

    customer_id = existing.get("id") or ""
    if not customer_id:
        raise AsaasError("Asaas returned a customer without an id.")

    profile.gateway = GATEWAY
    profile.gateway_customer_id = customer_id
    profile.save(update_fields=["gateway", "gateway_customer_id", "updated_at"])
    return profile


def subscribe_workspace(workspace, *, client=None, email=""):
    """Contract the monthly subscription for one workspace.

    The call the contracting modal makes. Ensures the org's customer,
    creates the recurring subscription, and records both ids on the
    workspace's ``WorkspaceSubscription``.

    The row stays ``pending``: a created subscription is an agreement to
    pay, not a payment. Only ``PAYMENT_CONFIRMED`` on the webhook flips
    it to ``active``, so an unpaid boleto never grants access.

    Returns the ``WorkspaceSubscription``. Raises ``AsaasError`` on any
    gateway failure, leaving the local row untouched for a clean retry.
    """
    client = client or AsaasClient()
    sub = services.ensure_subscription(workspace)

    # Layer 1: already contracted. Returned as-is rather than re-POSTed,
    # which is what makes a double-clicked modal harmless.
    if sub.gateway_subscription_id:
        return sub

    # A comped workspace has no gateway subscription behind it by
    # design; charging one would be a billing incident.
    if sub.is_courtesy:
        return sub

    profile = ensure_customer(workspace.organization, client=client, email=email)

    # Layer 2: recover an id we may have failed to persist.
    remote = client.find_subscription_by_reference(workspace.pk)
    if remote is None:
        remote = client.create_subscription(
            customer_id=profile.gateway_customer_id,
            value_cents=sub.price_cents,
            external_reference=workspace.pk,
            # Translated: it reaches the customer, on the Asaas invoice
            # and in the charge notification emails Asaas sends.
            description=str(_("Brightbean Studio — workspace %(name)s") % {"name": workspace.name}),
        )

    subscription_id = remote.get("id") or ""
    if not subscription_id:
        raise AsaasError("Asaas returned a subscription without an id.")

    with transaction.atomic():
        sub.gateway = GATEWAY
        sub.gateway_customer_id = profile.gateway_customer_id
        sub.gateway_subscription_id = subscription_id
        sub.canceled_at = None
        sub.save(
            update_fields=[
                "gateway",
                "gateway_customer_id",
                "gateway_subscription_id",
                "canceled_at",
                "updated_at",
            ]
        )
    logger.info("Contracted Asaas subscription for workspace %s", workspace.pk)
    return sub


def subscribe_storage_addon(addon, *, client=None):
    """Contract an extra-storage add-on as its own Asaas subscription.

    Asaas has no concept of a line item on an existing subscription, so
    an add-on is a second recurring charge against the same customer —
    which is why ``WorkspaceAddon`` carries its own
    ``gateway_subscription_id``.

    The add-on stays ``pending`` and therefore grants no disk until its
    first payment is confirmed.
    """
    client = client or AsaasClient()
    if addon.gateway_subscription_id:
        return addon

    workspace = addon.workspace
    profile = ensure_customer(workspace.organization, client=client)

    # Namespaced so the add-on's reference can never collide with the
    # workspace subscription's, which is the bare workspace id.
    reference = f"addon:{addon.pk}"
    remote = client.find_subscription_by_reference(reference)
    if remote is None:
        remote = client.create_subscription(
            customer_id=profile.gateway_customer_id,
            value_cents=addon.total_price_cents,
            external_reference=reference,
            description=str(
                _("Brightbean Studio — %(gb)s GB extra storage for %(name)s")
                % {"gb": addon.quantity, "name": workspace.name}
            ),
        )

    subscription_id = remote.get("id") or ""
    if not subscription_id:
        raise AsaasError("Asaas returned a subscription without an id.")

    addon.gateway = GATEWAY
    addon.gateway_subscription_id = subscription_id
    addon.save(update_fields=["gateway", "gateway_subscription_id", "updated_at"])
    return addon


def cancel_workspace_subscription(workspace, *, client=None):
    """Cancel a workspace's subscription at the gateway and locally.

    The local row is marked ``canceled`` even when it had no gateway
    subscription (a pending workspace that was never paid for), so the
    customer's intent is recorded either way.

    A gateway 404 is treated as success: the subscription being absent
    upstream is the state we were asking for, and raising would leave
    the local row stuck active against a subscription that is already
    gone.
    """
    client = client or AsaasClient()
    sub = services.ensure_subscription(workspace)

    if sub.gateway_subscription_id:
        try:
            client.cancel_subscription(sub.gateway_subscription_id)
        except asaas_gateway.AsaasAPIError as exc:
            if exc.status_code != 404:
                raise

    sub.status = WorkspaceSubscription.Status.CANCELED
    sub.canceled_at = sub.canceled_at or timezone.now()
    sub.save(update_fields=["status", "canceled_at", "updated_at"])
    return sub
