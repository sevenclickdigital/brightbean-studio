"""Per-workspace disk usage, the 90% warning, and the 100% block.

This is the billing side of the disk story: ``services.total_storage_bytes``
says what a workspace *bought*, and this module says what it is *using* and
whether the next upload fits.

How this sits beside the older per-organization quota
-----------------------------------------------------
``apps.media_library.quotas`` already caps an Organization's total bytes.
That cap predates per-workspace billing and is NOT replaced here, because
the two answer different questions:

- The org cap is an infrastructure guard. It exists so one account — or one
  leaked Agent API key with ``upload_media`` — cannot fill the bucket. It
  covers every asset the org owns, including shared media.
- The workspace cap is the product's contract. It is what the customer
  bought, what the 90% warning is measured against, and what an extra-GB
  add-on raises.

An upload must satisfy BOTH. They are checked in that order — org first,
then workspace — so the cheaper, already-indexed org aggregate rejects the
pathological cases before we touch billing rows.

Who pays for shared media (``MediaAsset.workspace`` is NULL)
------------------------------------------------------------
Shared media counts against the ORGANIZATION only; it is charged to no
workspace. The deciding argument is that the remedy has to be actionable:
at 90% we tell the user to delete old files or buy GB, and a workspace
member can do neither for bytes living in the org-wide library they may
not even be able to list. Splitting shared bytes across workspaces would
also make every workspace's usage move when an unrelated admin uploads a
shared file, and make a workspace's quota unreachable by its own cleanup.
So: shared bytes are the org's problem, workspace bytes are the
workspace's. There is no shared media in the database today, so this
codifies a rule rather than changing behaviour.

Performance
-----------
``used_storage_bytes`` is a ``SUM`` over a workspace's assets. It runs on
every upload, so the result is cached for
``BILLING_STORAGE_USAGE_CACHE_SECONDS`` and invalidated on asset
create/delete by the signals in ``apps.billing.signals``. The cache is
only ever a *lower-bound-correctness* risk if invalidation is missed, so
the TTL is short and the signals cover both directions.
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _

from apps.billing import services

# Fraction of the quota at which the UI should start warning. Expressed as
# an integer percent so it reads the same in settings, logs and templates.
WARNING_PERCENT = 90


class WorkspaceStorageExceededError(Exception):
    """Raised when an upload would push a workspace over its disk quota.

    Carries the numbers rather than a formatted string so each surface can
    render them its own way; ``message`` holds the translated,
    user-facing sentence for the ones that just need something to show.
    """

    def __init__(self, *, workspace_id, used, limit, attempted):
        self.workspace_id = workspace_id
        self.used = int(used)
        self.limit = int(limit)
        self.attempted = int(attempted)
        super().__init__(
            f"Workspace storage exceeded: workspace={workspace_id} "
            f"used={self.used} limit={self.limit} attempted={self.attempted}"
        )

    @property
    def message(self):
        """Translated sentence telling the user what to do about it.

        Names both remedies the product owner specified — free up space or
        contract more — because "quota exceeded" alone leaves the user with
        no next step. Sizes are rendered in GB; bytes are the storage unit,
        not a unit anyone reads.
        """
        return _(
            "This workspace has used all %(limit)s GB of its storage. "
            "Delete files you no longer need, or contract extra storage to keep uploading."
        ) % {"limit": _gb_label(self.limit)}


def _gb_label(num_bytes):
    """Bytes as a short decimal-GB string, matching how GB are sold.

    ``WorkspaceAddon.BYTES_PER_GB`` is 10^9 — the customer buys what the
    price list calls a GB — so the display has to divide by the same
    number the invoice does, or 10 GB of quota reads as 9.3.
    """
    gb = int(num_bytes) / 1_000_000_000
    return f"{gb:.0f}" if gb >= 10 or gb == int(gb) else f"{gb:.1f}"


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


def _usage_cache_key(workspace_id):
    return f"billing:ws_storage_used:{workspace_id}"


def _cache_seconds():
    return int(getattr(settings, "BILLING_STORAGE_USAGE_CACHE_SECONDS", 300))


def used_storage_bytes(workspace, *, use_cache=True):
    """Bytes currently stored by a workspace's own assets.

    Shared org-level assets (``workspace`` NULL) are deliberately excluded
    — see the module docstring. Cached briefly because this runs on the
    upload path; pass ``use_cache=False`` for a reconcile job or a test
    that wants the live number.
    """
    workspace_id = services._workspace_id(workspace)
    key = _usage_cache_key(workspace_id)

    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return int(cached)

    from apps.media_library.models import MediaAsset

    total = MediaAsset.objects.filter(workspace_id=workspace_id).aggregate(total=Sum("file_size"))["total"]
    total = int(total or 0)

    if use_cache:
        seconds = _cache_seconds()
        if seconds > 0:
            cache.set(key, total, seconds)
    return total


def invalidate_usage(workspace_id):
    """Drop a workspace's cached usage figure.

    Called from the asset create/delete signals. Deleting rather than
    recomputing: the next reader pays for the ``SUM``, and a burst of
    uploads then costs one query instead of one per asset.
    """
    if workspace_id is None:
        return
    cache.delete(_usage_cache_key(workspace_id))


# ---------------------------------------------------------------------------
# Status — what the UI asks
# ---------------------------------------------------------------------------


class WorkspaceStorageStatus:
    """A workspace's disk position: quota, usage, and the two thresholds.

    The single object phase 5 reads to render the meter and the 90%
    warning. Plain attributes, no queries after construction, so a
    template can touch every field for free.
    """

    __slots__ = ("workspace_id", "used_bytes", "limit_bytes", "has_subscription")

    def __init__(self, *, workspace_id, used_bytes, limit_bytes, has_subscription):
        self.workspace_id = workspace_id
        self.used_bytes = int(used_bytes)
        self.limit_bytes = int(limit_bytes)
        self.has_subscription = bool(has_subscription)

    @property
    def remaining_bytes(self):
        return max(self.limit_bytes - self.used_bytes, 0)

    @property
    def percent_used(self):
        """Usage as a float percent, uncapped.

        A workspace with no quota at all reads as 100% when it holds
        anything and 0% when it is empty — dividing by zero has no answer,
        and "full" is the honest one for a workspace that may not upload.
        """
        if self.limit_bytes <= 0:
            return 100.0 if self.used_bytes > 0 else 0.0
        return (self.used_bytes / self.limit_bytes) * 100

    @property
    def is_near_limit(self):
        """True at or above the warning threshold but not yet full.

        The 90% signal the product owner asked for. False once full,
        because at that point the UI should show the block, not a warning
        — the two messages say different things and must not both appear.
        """
        return not self.is_full and self.percent_used >= WARNING_PERCENT

    @property
    def is_full(self):
        """True when no further bytes may be accepted."""
        return self.used_bytes >= self.limit_bytes

    def fits(self, incoming_bytes):
        """Whether ``incoming_bytes`` more would still be within quota."""
        return self.used_bytes + max(int(incoming_bytes), 0) <= self.limit_bytes

    @property
    def warning_percent(self):
        """The threshold itself, so the UI need not import this module."""
        return WARNING_PERCENT


def storage_status(workspace, *, use_cache=True):
    """Resolve a workspace's full disk position in one call.

    The question phase 5 asks: "is this workspace near its limit, and with
    what numbers?" Always returns a status — a workspace with no
    subscription gets ``limit_bytes=0``, which reads as full and blocks
    uploads, matching :func:`apps.billing.services.total_storage_bytes`.
    """
    return WorkspaceStorageStatus(
        workspace_id=services._workspace_id(workspace),
        used_bytes=used_storage_bytes(workspace, use_cache=use_cache),
        limit_bytes=services.total_storage_bytes(workspace),
        has_subscription=services._subscription_for(workspace) is not None,
    )


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


def enforce_workspace_storage(workspace, incoming_bytes):
    """Raise :class:`WorkspaceStorageExceededError` if the upload won't fit.

    No-op when ``workspace`` is None — that is shared org-level media,
    which this quota does not govern (the org cap still applies) — and
    when ``BILLING_STORAGE_QUOTA_ENABLED`` is False, which is how a
    deployment mid-migration runs with billing rows present but disk
    enforcement off.

    A workspace with no subscription row is NOT blocked here. Blocking it
    would take every pre-billing workspace offline the moment this ships,
    which is a migration decision and not this function's to make; access
    gating is ``services.is_workspace_active``'s job. Such a workspace
    reads as full in :func:`storage_status`, so the UI still shows it needs
    a contract.
    """
    if workspace is None:
        return
    if not getattr(settings, "BILLING_STORAGE_QUOTA_ENABLED", True):
        return
    if services._subscription_for(workspace) is None:
        return

    status = storage_status(workspace)
    if status.fits(incoming_bytes):
        return

    # Re-read without the cache before refusing. A stale figure may only
    # ever be too high here (assets deleted since it was cached, and the
    # invalidation missed), and paying one extra SUM to avoid wrongly
    # blocking a paying customer's upload is the right trade.
    fresh = storage_status(workspace, use_cache=False)
    if fresh.fits(incoming_bytes):
        return

    raise WorkspaceStorageExceededError(
        workspace_id=fresh.workspace_id,
        used=fresh.used_bytes,
        limit=fresh.limit_bytes,
        attempted=int(incoming_bytes),
    )
