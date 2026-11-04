"""Storage-position context for the surfaces that can act on it.

The 90% warning has to appear where someone is about to add a file — the
media library and the composer — and both of those render through views
this app does not own. A context processor is the smallest way in: the
views stay untouched, and the banner partial is included once in each
template.

It runs on every authenticated request, so it has to be cheap. It is:
``storage_status`` reads the cached usage figure (see
``storage_usage.used_storage_bytes``) plus one indexed lookup for the
quota, and it short-circuits entirely when the request has no workspace
or the workspace is nowhere near its limit.
"""

from __future__ import annotations

from apps.billing.storage_usage import storage_status


def workspace_storage_banner(request):
    """Expose ``billing_storage`` when a warning or block should show.

    Returns nothing at all in the common case. ``billing_storage`` being
    absent is what the partial keys off, so a page that includes it costs
    one ``{% if %}`` when there is nothing to say.
    """
    if not hasattr(request, "user") or not request.user.is_authenticated:
        return {}

    workspace = getattr(request, "workspace", None)
    if workspace is None:
        return {}

    status = storage_status(workspace)

    # A workspace with no subscription row reads as full (limit 0) by
    # design — see ``storage_usage.storage_status``. Showing it the
    # "you're out of space" banner would be a lie: it has no contract
    # yet, which the billing page says properly, and every pre-billing
    # workspace would light up red on deploy.
    if not status.has_subscription:
        return {}

    if not (status.is_near_limit or status.is_full):
        return {}

    return {"billing_storage": status}
