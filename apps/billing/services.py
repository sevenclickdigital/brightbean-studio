"""Billing domain logic.

Everything here is pure local computation — quota arithmetic, price
lookups, courtesy grants. No gateway calls: the Asaas client arrives in
its own module and will call into these functions, not the other way
round, so the money rules stay testable without a network.

Units, held without exception: money in integer cents, disk in integer
bytes.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from apps.billing.models import (
    BillingEvent,
    OrganizationBillingProfile,
    WorkspaceAddon,
    WorkspaceSubscription,
)

# ---------------------------------------------------------------------------
# Price list
# ---------------------------------------------------------------------------


def workspace_price_cents():
    """List price of one workspace per month, in cents."""
    return settings.BILLING_WORKSPACE_PRICE_CENTS


def base_storage_bytes():
    """Disk included with every workspace, in bytes."""
    return settings.BILLING_WORKSPACE_BASE_STORAGE_BYTES


def storage_price_per_gb_cents():
    """Current list price of one extra GB per month, in cents.

    Read only when contracting a new add-on. The resulting row keeps its
    own ``unit_price_cents``, so changing this value never re-prices an
    add-on already sold.
    """
    return settings.BILLING_STORAGE_PRICE_PER_GB_CENTS


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


def extra_storage_bytes(workspace_id):
    """Bytes granted to a workspace by its active storage add-ons.

    Summed in GB and multiplied once, rather than summing a per-row byte
    expression, to keep the arithmetic identical to
    ``WorkspaceAddon.storage_bytes`` on a single row.
    """
    gb = (
        WorkspaceAddon.objects.filter(
            workspace_id=workspace_id,
            kind=WorkspaceAddon.Kind.EXTRA_STORAGE,
            status=WorkspaceAddon.Status.ACTIVE,
        ).aggregate(total=models.Sum("quantity"))["total"]
        or 0
    )
    return gb * WorkspaceAddon.BYTES_PER_GB


def total_storage_bytes(workspace):
    """A workspace's full disk allowance: base quota plus add-ons.

    Returns 0 for a workspace with no subscription row at all — no
    contract, no quota. That is the honest answer rather than falling
    back to the settings default, which would hand free disk to a
    workspace that never subscribed.
    """
    sub = _subscription_for(workspace)
    if sub is None:
        return 0
    return sub.base_storage_bytes + extra_storage_bytes(_workspace_id(workspace))


def addons_monthly_cents(workspace_id):
    """Monthly cost of a workspace's active add-ons, at sold prices."""
    total = 0
    for addon in WorkspaceAddon.objects.filter(
        workspace_id=workspace_id,
        status=WorkspaceAddon.Status.ACTIVE,
    ):
        total += addon.total_price_cents
    return total


def workspace_monthly_total_cents(workspace):
    """What a workspace bills per month, add-ons included."""
    sub = _subscription_for(workspace)
    if sub is None:
        return 0
    return sub.monthly_total_cents


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def is_workspace_active(workspace):
    """True when a workspace may be used.

    The single question the rest of the app should ask. A workspace with
    no subscription row is not active: every workspace, including the
    ones that predate billing, has to be contracted or comped.
    """
    sub = _subscription_for(workspace)
    return bool(sub and sub.is_active)


def is_subscription_current(workspace):
    """True when a workspace's money is in order right now.

    Stricter than :func:`is_workspace_active` — it also requires the paid
    period not to have lapsed. Meant for reconcile jobs and admin
    reporting, not for gating a page.
    """
    sub = _subscription_for(workspace)
    return bool(sub and sub.is_current)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def ensure_subscription(workspace):
    """Get or create the workspace's subscription row, priced from the
    current list.

    Created ``pending``: the row's existence means "we know what this
    workspace would cost", never "it is paid for". Callers that need the
    workspace usable must subscribe or comp it.
    """
    sub, _created = WorkspaceSubscription.objects.get_or_create(
        workspace=workspace,
        defaults={
            "price_cents": workspace_price_cents(),
            "base_storage_bytes": base_storage_bytes(),
        },
    )
    _cache_subscription(workspace, sub)
    return sub


def grant_courtesy(workspace, note="", granted_by=None):
    """Mark a workspace exempt from billing.

    Does not consult the organization's courtesy quota: the quota is an
    admin-facing budget, and an admin overriding their own budget for one
    workspace is a decision, not an error. :func:`courtesy_quota_exceeded`
    is there for the UI to surface it.
    """
    sub = ensure_subscription(workspace)
    sub.status = WorkspaceSubscription.Status.COURTESY
    sub.courtesy_note = note
    sub.canceled_at = None
    sub.save(update_fields=["status", "courtesy_note", "canceled_at", "updated_at"])
    if granted_by is not None:
        profile = ensure_billing_profile(workspace.organization)
        if profile.courtesy_granted_at is None:
            profile.courtesy_granted_at = timezone.now()
            profile.courtesy_granted_by = granted_by
            profile.save(update_fields=["courtesy_granted_at", "courtesy_granted_by", "updated_at"])
    return sub


def revoke_courtesy(workspace):
    """Take a workspace's exemption away, returning it to ``pending``.

    Back to pending rather than active: removing a comp means the
    workspace now needs a real contract, and only the gateway can say it
    has one.
    """
    sub = ensure_subscription(workspace)
    if sub.status == WorkspaceSubscription.Status.COURTESY:
        sub.status = WorkspaceSubscription.Status.PENDING
        sub.courtesy_note = ""
        sub.save(update_fields=["status", "courtesy_note", "updated_at"])
    return sub


def contract_storage_addon(workspace, quantity_gb, unit_price_cents=None):
    """Contract ``quantity_gb`` extra GB for a workspace.

    The price is snapshotted onto the row at contracting time, defaulting
    to the current list price. Pass ``unit_price_cents`` explicitly to
    honour a quote that was shown to the customer before the list moved.
    """
    return WorkspaceAddon.objects.create(
        workspace=workspace,
        kind=WorkspaceAddon.Kind.EXTRA_STORAGE,
        quantity=quantity_gb,
        unit_price_cents=(storage_price_per_gb_cents() if unit_price_cents is None else unit_price_cents),
    )


# ---------------------------------------------------------------------------
# Organization-level courtesy
# ---------------------------------------------------------------------------


def ensure_billing_profile(organization):
    profile, _created = OrganizationBillingProfile.objects.get_or_create(organization=organization)
    return profile


def grant_account_courtesy(organization, *, quota, expires_on=None, note="", granted_by=None):
    """Open (or extend) an account-wide exemption, optionally with a deadline.

    One call rather than the operator setting a ceiling and then toggling
    each workspace: the product owner's decision is "this account is free
    — this many workspaces, until this day, for this reason", and splitting
    it across two writes is how a quota ends up raised with nothing
    actually exempted.

    So this both records the budget *and* spends it, comping the account's
    cheapest-to-decide set — its oldest workspaces, up to ``quota``. Oldest
    first because an agency's first workspace is the one the courtesy was
    negotiated over; a later one they create is the one they would expect
    to pay for.

    Workspaces already comped are left alone, which keeps a re-grant (a
    renewal, say) idempotent instead of shuffling which workspace is free.
    """
    profile = ensure_billing_profile(organization)
    profile.courtesy_workspace_quota = quota
    profile.courtesy_expires_on = expires_on
    profile.courtesy_note = note
    profile.courtesy_granted_at = timezone.now()
    profile.courtesy_granted_by = granted_by
    profile.save(
        update_fields=[
            "courtesy_workspace_quota",
            "courtesy_expires_on",
            "courtesy_note",
            "courtesy_granted_at",
            "courtesy_granted_by",
            "updated_at",
        ]
    )

    workspaces = list(organization.workspaces.order_by("created_at", "id"))
    comped = 0
    for workspace in workspaces:
        if comped >= quota:
            break
        sub = ensure_subscription(workspace)
        if sub.status != WorkspaceSubscription.Status.COURTESY:
            grant_courtesy(workspace, note=note)
        comped += 1
    return profile


def revoke_account_courtesy(organization):
    """Close an account's exemption and bill every workspace again.

    Used both by the operator pressing "charge this account again" and by
    the expiry job. Zeroes the budget and clears the deadline so the
    account does not sit at "0 workspaces free until the 30th", which
    reads as a comp that is still live.

    Changes billing state only: no workspace, membership, asset or login
    is touched. A lapsed courtesy is an invoice event, never a lockout —
    see :func:`expire_due_courtesies`.
    """
    profile = ensure_billing_profile(organization)
    profile.courtesy_workspace_quota = 0
    profile.courtesy_expires_on = None
    profile.save(update_fields=["courtesy_workspace_quota", "courtesy_expires_on", "updated_at"])

    for workspace in organization.workspaces.all():
        revoke_courtesy(workspace)
    return profile


def due_courtesy_profiles(*, today=None):
    """Profiles whose deadline has passed and are still exempt.

    ``courtesy_expires_on < today`` (strict), matching
    ``OrganizationBillingProfile.courtesy_is_expired`` — a comp granted
    "until the 30th" lasts all of the 30th.

    Filtered on ``quota > 0`` rather than on the workspace statuses so a
    second run the same day finds nothing: :func:`revoke_account_courtesy`
    zeroes the quota, which is what makes the sweep idempotent.
    """
    if today is None:
        today = timezone.localdate()
    return OrganizationBillingProfile.objects.filter(
        courtesy_expires_on__isnull=False,
        courtesy_expires_on__lt=today,
        courtesy_workspace_quota__gt=0,
    ).select_related("organization")


def expire_due_courtesies(*, today=None):
    """Put every lapsed account back on the invoice. Returns how many.

    What expiring means, exactly, and the whole reason this is a separate
    function rather than a line in the job: it flips billing state and
    nothing else. No workspace is archived, no asset is deleted, no
    member loses their login. A customer whose free period ended owes
    money — they have not done anything wrong, and waking up to a dead
    account would be the one failure mode worse than an unpaid invoice.

    Each account is committed on its own so one bad row cannot roll back
    the rest of the sweep.
    """
    expired = 0
    for profile in due_courtesy_profiles(today=today):
        with transaction.atomic():
            revoke_account_courtesy(profile.organization)
        expired += 1
    return expired


def courtesy_quota_exceeded(organization):
    """True when an org has more comped workspaces than its quota allows.

    Reported rather than enforced — see :func:`grant_courtesy`. Lets the
    admin UI show "4 of 3 courtesy workspaces in use" instead of
    silently refusing a grant an admin deliberately made.
    """
    profile = OrganizationBillingProfile.objects.filter(organization=organization).first()
    quota = profile.courtesy_workspace_quota if profile else 0
    used = WorkspaceSubscription.objects.filter(
        workspace__organization=organization,
        status=WorkspaceSubscription.Status.COURTESY,
    ).count()
    return used > quota


# ---------------------------------------------------------------------------
# Webhook idempotency
# ---------------------------------------------------------------------------


def record_event(gateway_event_id, *, gateway="", event_type="", payload=None):
    """Claim a gateway event for processing.

    Returns ``(event, created)``. ``created=False`` means this event id
    was already recorded and the caller must not act on it again — the
    insert IS the claim, which is why the unique constraint does the
    work instead of a prior existence check that two concurrent
    deliveries would both pass.

    Wrapped in its own atomic block so the IntegrityError that
    ``get_or_create`` absorbs doesn't poison an outer transaction.
    """
    with transaction.atomic():
        event, created = BillingEvent.objects.get_or_create(
            gateway_event_id=gateway_event_id,
            defaults={
                "gateway": gateway,
                "event_type": event_type,
                "payload": payload or {},
            },
        )
    return event, created


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_workspace_id(workspace):
    return isinstance(workspace, str | uuid.UUID)


def _cache_subscription(workspace, sub):
    """Point a Workspace instance's cached reverse relation at ``sub``.

    Django caches the reverse OneToOne on first access, so a caller
    holding the Workspace it just passed to ``grant_courtesy`` would
    otherwise keep reading the pre-grant status (or a cached
    DoesNotExist) for the rest of the request.
    """
    if not _is_workspace_id(workspace):
        workspace.billing_subscription = sub


def _workspace_id(workspace):
    return workspace if _is_workspace_id(workspace) else workspace.pk


def _subscription_for(workspace):
    """The workspace's subscription, or None.

    Tolerates being handed either a Workspace or a bare id. Accessing
    the reverse side of a OneToOne raises rather than returning None
    when the row is absent, so the missing-subscription case — the
    normal state of every workspace that predates billing — is caught
    here instead of at each call site.
    """
    if _is_workspace_id(workspace):
        return WorkspaceSubscription.objects.filter(workspace_id=workspace).first()
    try:
        return workspace.billing_subscription
    except WorkspaceSubscription.DoesNotExist:
        return None
