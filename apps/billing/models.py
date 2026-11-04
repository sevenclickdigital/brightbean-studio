"""Models backing per-workspace billing.

The product rule the schema encodes: a workspace is the billable unit.
One plan, every feature included, R$ 50,00/month per workspace. The only
metered resource is disk, and extra disk is sold as a separate add-on row
rather than a column on the subscription — see ``WorkspaceAddon``.

Four roles:

- ``WorkspaceSubscription`` (one per Workspace): the canonical local
  record of what this workspace costs and whether it may be used. Holds
  the gateway (Asaas) customer/subscription ids and the base disk quota.
- ``WorkspaceAddon`` (many per Workspace): anything stacked on top of the
  base subscription. Today only extra storage; tomorrow AI text/image/
  video generation, with no change to the subscription table.
- ``BillingEvent``: append-only log of everything the gateway told us,
  keyed by the gateway's own event id so a redelivered webhook is a
  no-op instead of a double credit.
- ``OrganizationBillingProfile`` (one per Organization): account-level
  billing settings the admin controls, notably how many of the org's
  workspaces are comped.

Two unit conventions, held everywhere in this app: money is always an
integer number of cents, disk is always an integer number of bytes.
Neither is ever a float.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# ---------------------------------------------------------------------------
# WorkspaceSubscription, one per Workspace
# ---------------------------------------------------------------------------


class WorkspaceSubscription(models.Model):
    """One row per Workspace, holding its billing state.

    Status lifecycle:
    - pending   : row exists, nothing contracted yet. Either a brand-new
                  workspace or an existing one from before billing — both
                  land here and must be subscribed before use.
    - active    : the gateway confirmed payment for the current period.
    - past_due  : the gateway failed to charge. The row stays so a
                  recovered payment flips it back to active without
                  losing the external ids.
    - canceled  : the customer (or an admin) ended the subscription.
                  Terminal unless resubscribed.
    - courtesy  : exempt from billing by admin decision. Counts as
                  active everywhere access is checked, and the billing
                  jobs skip it — there is no gateway subscription behind
                  it, so it must never be charged or dunned.

    ``courtesy`` is a status rather than a boolean beside the status
    because the two are mutually exclusive in practice: a comped
    workspace has no payment state to be in. Keeping it in the same
    column means every "may this workspace be used?" check reads one
    field and cannot forget the exemption.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        ACTIVE = "active", _("Active")
        PAST_DUE = "past_due", _("Past due")
        CANCELED = "canceled", _("Canceled")
        COURTESY = "courtesy", _("Courtesy")

    # Statuses that grant access. Courtesy is deliberately in here;
    # past_due is deliberately not — a failed charge should block.
    ACTIVE_STATUSES = frozenset({Status.ACTIVE, Status.COURTESY})

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="billing_subscription",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    # Price of the base subscription in cents, snapshotted on the row
    # rather than read from settings at render time: raising the list
    # price must not silently re-price workspaces already contracted.
    price_cents = models.PositiveIntegerField(default=0)

    # Gateway identifiers. Named for the role, not the vendor — Asaas
    # fills these today (customer_xxx / sub_xxx), and a future gateway
    # swap needs no migration. ``gateway`` records which vendor the ids
    # belong to so a half-migrated table is still unambiguous.
    gateway = models.CharField(max_length=32, blank=True, default="")
    gateway_customer_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    gateway_subscription_id = models.CharField(max_length=255, blank=True, default="", db_index=True)

    # End of the paid-for window. Access decisions use status, not this
    # field — the gateway tells us when a renewal failed and we flip to
    # past_due; expiring locally on a clock we don't control would cut
    # off a paying customer during a webhook delay.
    current_period_end = models.DateTimeField(null=True, blank=True)

    # Base disk allowance in bytes, snapshotted per workspace for the
    # same reason as price: changing the product default must not shrink
    # the quota of a workspace that is already storing files against it.
    base_storage_bytes = models.PositiveBigIntegerField(default=0)

    canceled_at = models.DateTimeField(null=True, blank=True)
    # Why a workspace was comped. Free text and admin-facing only; the
    # billing logic cares about status alone.
    courtesy_note = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_workspace_subscription"

    def __str__(self):
        return f"WorkspaceSubscription({self.workspace_id}, {self.status})"

    @property
    def is_courtesy(self):
        return self.status == self.Status.COURTESY

    @property
    def is_active(self):
        """True when the workspace may be used.

        Courtesy counts as active. Everything that gates a feature on
        "is this workspace paid for?" should go through here instead of
        comparing ``status`` to ``"active"``, so adding another
        access-granting status later is a one-line change.
        """
        return self.status in self.ACTIVE_STATUSES

    @property
    def is_current(self):
        """True when the subscription is active AND the paid period has
        not lapsed.

        Stricter than ``is_active``: this one answers "is the money in
        order right now", which is what a reconcile job or an admin
        report wants. Courtesy rows are always current — nobody is
        charging them, so there is no period to lapse. A NULL
        ``current_period_end`` on a paid row means the gateway hasn't
        reported a period yet; treated as current so a webhook delay
        right after checkout doesn't read as a lapse.
        """
        if self.is_courtesy:
            return True
        if self.status != self.Status.ACTIVE:
            return False
        if self.current_period_end is None:
            return True
        return self.current_period_end >= timezone.now()

    @property
    def extra_storage_bytes(self):
        """Bytes granted by active storage add-ons."""
        from apps.billing.services import extra_storage_bytes

        return extra_storage_bytes(self.workspace_id)

    @property
    def total_storage_bytes(self):
        """Base quota plus every active storage add-on."""
        return self.base_storage_bytes + self.extra_storage_bytes

    @property
    def monthly_total_cents(self):
        """What this workspace bills per month, add-ons included.

        Courtesy workspaces bill nothing regardless of what is attached
        to them, so an admin can comp a workspace that already has
        add-ons without having to strip them first.
        """
        if self.is_courtesy:
            return 0
        from apps.billing.services import addons_monthly_cents

        return self.price_cents + addons_monthly_cents(self.workspace_id)


# ---------------------------------------------------------------------------
# WorkspaceAddon, stackable extras
# ---------------------------------------------------------------------------


class WorkspaceAddon(models.Model):
    """An extra contracted on top of a workspace's base subscription.

    One row per contracted add-on, never a column on
    ``WorkspaceSubscription`` — the product roadmap adds AI text/image/
    video generation next, and each of those should be a new ``Kind``
    value plus a row, not a schema change to the subscription every
    agency's billing depends on.

    ``quantity`` is interpreted per kind: for ``extra_storage`` it is a
    number of GB. ``unit_price_cents`` is the price that applied at the
    moment of contracting and is never rewritten — the list price in
    settings may change, but an existing customer's bill must not move
    retroactively. Recomputing a historical charge from today's settings
    is exactly the bug this column exists to prevent.
    """

    class Kind(models.TextChoices):
        EXTRA_STORAGE = "extra_storage", _("Extra storage")

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        ACTIVE = "active", _("Active")
        CANCELED = "canceled", _("Canceled")

    # Bytes per GB as billed. Decimal GB (10^9) rather than binary GiB
    # (2^30) because the customer is buying what the price list calls
    # "1 GB" and we must not quietly hand out 7% more than we sell.
    BYTES_PER_GB = 1_000_000_000

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="billing_addons",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    quantity = models.PositiveIntegerField(default=0)
    unit_price_cents = models.PositiveIntegerField(default=0)

    # Add-ons may be billed as their own gateway subscription (Asaas has
    # no concept of a line item on an existing subscription), so they
    # carry their own external id.
    gateway = models.CharField(max_length=32, blank=True, default="")
    gateway_subscription_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_workspace_addon"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workspace", "kind", "status"]),
        ]

    def __str__(self):
        return f"WorkspaceAddon({self.workspace_id}, {self.kind}, {self.quantity})"

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    @property
    def total_price_cents(self):
        """Monthly cost of this add-on at the price it was sold for."""
        return self.quantity * self.unit_price_cents

    @property
    def storage_bytes(self):
        """Bytes this add-on grants, or 0 for non-storage kinds."""
        if self.kind != self.Kind.EXTRA_STORAGE:
            return 0
        return self.quantity * self.BYTES_PER_GB


# A proxy over the same table, so the storage case reads clearly at call
# sites ("WorkspaceStorageAddon") while the roadmap's future add-on kinds
# keep sharing one physical table and one set of billing jobs.
class WorkspaceStorageAddonManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(kind=WorkspaceAddon.Kind.EXTRA_STORAGE)

    def create(self, **kwargs):
        kwargs.setdefault("kind", WorkspaceAddon.Kind.EXTRA_STORAGE)
        return super().create(**kwargs)


class WorkspaceStorageAddon(WorkspaceAddon):
    """Extra GB of disk contracted for a workspace.

    A proxy, not a table: storage is the only add-on today but the
    billing jobs, the gateway sync, and the admin are all written
    against ``WorkspaceAddon``, and adding AI add-ons later must not
    mean a second code path. ``quantity`` is GB; ``unit_price_cents`` is
    the per-GB price frozen at contracting time.
    """

    objects = WorkspaceStorageAddonManager()

    class Meta:
        proxy = True
        verbose_name = _("workspace storage add-on")
        verbose_name_plural = _("workspace storage add-ons")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set in __init__ as well as save() so ``storage_bytes`` and the
        # other kind-dependent properties are already correct on an
        # unsaved instance — a caller computing a quote before writing
        # the row would otherwise read 0 bytes.
        self.kind = WorkspaceAddon.Kind.EXTRA_STORAGE

    def save(self, *args, **kwargs):
        # The proxy only ever addresses storage rows; forcing the kind
        # here keeps a caller that forgot it from writing a row the
        # proxy's own manager would then filter out of existence.
        self.kind = WorkspaceAddon.Kind.EXTRA_STORAGE
        return super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# BillingEvent, gateway webhook log
# ---------------------------------------------------------------------------


class BillingEvent(models.Model):
    """One row per event the payment gateway sent us.

    Exists for two reasons that happen to need the same table:

    - Idempotency. Gateways redeliver: Asaas retries a webhook until it
      gets a 200, and a slow response means the same event arrives
      twice. ``gateway_event_id`` is unique, so the handler can insert
      first and treat an IntegrityError as "already processed" — the
      check and the claim are then one atomic operation, which a
      ``SELECT ... if not exists`` cannot be.
    - Audit. When a customer disputes a charge, the raw payload we acted
      on is the only honest record of why we did what we did.

    ``processed_at`` stays NULL until the handler finishes, so a crash
    mid-processing leaves a claimed-but-unprocessed row that a sweeper
    can find rather than an event silently lost.
    """

    class Status(models.TextChoices):
        RECEIVED = "received", _("Received")
        PROCESSED = "processed", _("Processed")
        IGNORED = "ignored", _("Ignored")
        FAILED = "failed", _("Failed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    gateway = models.CharField(max_length=32, blank=True, default="")
    # The gateway's own event identifier (Asaas sends ``evt_...``). The
    # unique constraint is the whole point of this column.
    gateway_event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=64, blank=True, default="", db_index=True)

    # Nullable: an event may arrive for a subscription we never recorded
    # (a customer created directly in the gateway dashboard, or a row
    # since deleted). Dropping the event instead would lose the audit
    # trail precisely when it is most needed.
    subscription = models.ForeignKey(
        "billing.WorkspaceSubscription",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_events",
    )
    addon = models.ForeignKey(
        "billing.WorkspaceAddon",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_events",
    )

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.RECEIVED,
        db_index=True,
    )
    payload = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "billing_event"
        ordering = ["-created_at"]

    def __str__(self):
        return f"BillingEvent({self.gateway_event_id}, {self.status})"


# ---------------------------------------------------------------------------
# OrganizationBillingProfile, account-level billing settings
# ---------------------------------------------------------------------------


class OrganizationBillingProfile(models.Model):
    """Account-level billing settings for one Organization.

    Chosen over adding the fields straight onto ``Organization`` because
    this is the account's billing *relationship*, not part of its
    identity: it grows with every gateway concern (tax id, payment
    method, dunning state, invoice address) and none of that belongs on
    a model that every request already loads. A separate table also
    keeps billing columns out of the org's query plan and lets this app
    be reasoned about — and migrated — on its own.

    ``courtesy_workspace_quota`` is the account-level comp the product
    owner asked for: "this organization gets N workspaces free". It is a
    quota rather than a flag so an agency can be given, say, three comped
    workspaces while paying for the rest. Which specific workspaces
    consume the quota is deliberately not recorded here — the quota is
    spent by counting workspaces whose subscription sits in
    ``courtesy``, so an admin moves the comp between workspaces by
    flipping statuses and never has to keep two tables in agreement.

    ``courtesy_expires_on`` is the deadline on that comp. It sits on the
    account rather than on each ``WorkspaceSubscription`` for the same
    reason the quota does: the operator grants "this account is free
    until the 30th", one decision, and the expiry job then has one date
    to read instead of having to agree with N per-workspace copies of
    it. NULL means open-ended, which is what every row granted before
    this field existed means — see the field's own comment.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="billing_profile",
    )

    # How many of this org's workspaces may be comped. 0 = none.
    courtesy_workspace_quota = models.PositiveIntegerField(default=0)
    courtesy_note = models.CharField(max_length=255, blank=True, default="")

    # The day the exemption stops. NULL means "no end date" — which is
    # every row that predates this field, so an existing open-ended
    # courtesy is never silently given a deadline by the migration.
    #
    # A DateField, not a DateTimeField: the operator grants "free until
    # the 30th", never "until 14:35". Expiry is therefore evaluated as
    # "today is past that day", which is also why the expiry job can run
    # once a day instead of on the minute.
    courtesy_expires_on = models.DateField(null=True, blank=True)
    courtesy_granted_at = models.DateTimeField(null=True, blank=True)
    courtesy_granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_billing_courtesies",
    )

    # One gateway customer per account even though billing is per
    # workspace: the payer is the organization, and Asaas keys payment
    # methods to the customer. Each workspace subscription then points
    # at this same customer id.
    gateway = models.CharField(max_length=32, blank=True, default="")
    gateway_customer_id = models.CharField(max_length=255, blank=True, default="", db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_organization_profile"

    def __str__(self):
        return f"OrganizationBillingProfile({self.organization_id})"

    @property
    def courtesy_workspaces_used(self):
        """How many of this org's workspaces are currently comped."""
        return WorkspaceSubscription.objects.filter(
            workspace__organization_id=self.organization_id,
            status=WorkspaceSubscription.Status.COURTESY,
        ).count()

    @property
    def courtesy_workspaces_remaining(self):
        """Unused comp slots, floored at 0.

        Can be floored rather than negative when an admin lowers the
        quota below what is already granted; the existing comps stand
        (we don't start charging retroactively) and the account simply
        has no slots left.
        """
        return max(0, self.courtesy_workspace_quota - self.courtesy_workspaces_used)

    @property
    def has_courtesy_available(self):
        return self.courtesy_workspaces_remaining > 0

    @property
    def courtesy_has_deadline(self):
        return self.courtesy_expires_on is not None

    @property
    def courtesy_is_expired(self):
        """True once the granted day has passed.

        Strictly ``<``, so a comp that expires "on the 30th" is still
        honoured all through the 30th and only lapses on the 31st. An
        operator typing today's date means "free for today", not "free
        for none of today".
        """
        if self.courtesy_expires_on is None:
            return False
        return self.courtesy_expires_on < timezone.localdate()

    @property
    def courtesy_days_remaining(self):
        """Whole days left on the deadline, or None when open-ended.

        Negative once lapsed, so a screen can tell "expires tomorrow"
        from "expired last week" without recomputing the subtraction.
        """
        if self.courtesy_expires_on is None:
            return None
        return (self.courtesy_expires_on - timezone.localdate()).days
