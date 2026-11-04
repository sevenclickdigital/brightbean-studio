"""Billing screens: contracting, the workspace billing page, staff admin.

URL surface:

- ``/workspace/<workspace_id>/billing/``                  : billing overview
- ``/workspace/<workspace_id>/billing/subscribe/``        : contracting modal
- ``/workspace/<workspace_id>/billing/subscribe/confirm/``: POST, contracts
- ``/workspace/<workspace_id>/billing/storage/quote/``    : HTMX price quote
- ``/workspace/<workspace_id>/billing/storage/``          : POST, buys GB
- ``/billing/staff/``                                     : staff console
- ``/billing/staff/courtesy/<target_workspace_id>/``      : POST, comp toggle
- ``/billing/staff/quota/<org_id>/``                      : POST, org quota

Two things this module is careful about, because both are states the
product is actually in today:

1. **Asaas has no credentials in most environments.** ``AsaasNotConfigured``
   is therefore the *normal* outcome of pressing "Contract", not an
   incident. It is caught separately from ``AsaasError`` and reported as
   "billing isn't set up yet" with the local row left pending — never a
   500, never a stack trace.
2. **Money is cents everywhere.** Nothing here divides by 100; the
   ``brl`` filter in ``templatetags/billing_extras.py`` is the only place
   that formats, so a cents value can't leak into a template as "5000".
"""

from __future__ import annotations

import functools
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.billing import contracting, services
from apps.billing.gateways.asaas import AsaasError, AsaasNotConfigured
from apps.billing.models import WorkspaceAddon
from apps.billing.storage_usage import storage_status
from apps.members.models import WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

# Quantities offered in the add-on picker. A shortlist rather than a free
# number field: the product owner's examples (5, 10, 50) are the sizes
# anyone actually buys, and a fixed set keeps the quote the customer sees
# and the quote we charge on the same rails. "Other" is still allowed —
# the POST validates any integer in range — so this is a convenience, not
# a cap.
STORAGE_ADDON_CHOICES = (5, 10, 25, 50, 100)

# Hard bounds on a storage purchase. Upper bound exists so a fat-fingered
# "5000" is a validation error instead of a R$ 10.000 subscription.
STORAGE_ADDON_MIN_GB = 1
STORAGE_ADDON_MAX_GB = 1000


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _get_workspace(request, workspace_id):
    """Resolve the workspace and enforce membership.

    Mirrors ``apps.composer.views._get_workspace`` rather than the media
    library's middleware-based check: billing pages are reachable by
    direct link (the contracting redirect right after creation), so they
    cannot assume ``request.workspace`` has already been set to this one.
    """
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).exists():
        raise PermissionDenied("You are not a member of this workspace.")
    return workspace


def _can_manage_billing(request, workspace):
    """Whether this member may spend the organization's money.

    Owner or manager of the workspace, or an owner/admin of the org.
    Contributors and clients can see the storage meter (they hit the 90%
    warning too) but must not be able to contract a charge.
    """
    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    if membership and membership.workspace_role in (
        WorkspaceMembership.WorkspaceRole.OWNER,
        WorkspaceMembership.WorkspaceRole.MANAGER,
    ):
        return True
    org_membership = getattr(request, "org_membership", None)
    return bool(
        org_membership
        and org_membership.organization_id == workspace.organization_id
        and org_membership.org_role in ("owner", "admin")
    )


def _require_manage_billing(request, workspace):
    if not _can_manage_billing(request, workspace):
        raise PermissionDenied("You may not manage billing for this workspace.")


def _parse_quantity_gb(raw):
    """Validated GB quantity from form input, or None.

    Returns None rather than raising so each caller decides between a
    message-and-redirect (the POST) and a zeroed quote (the HTMX
    preview).
    """
    try:
        quantity = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if quantity < STORAGE_ADDON_MIN_GB or quantity > STORAGE_ADDON_MAX_GB:
        return None
    return quantity


def _storage_quote(quantity_gb):
    """What ``quantity_gb`` extra GB costs per month, in cents.

    The one place the add-on price is computed, so the figure shown in
    the picker and the figure written onto the add-on row cannot drift.
    """
    unit = services.storage_price_per_gb_cents()
    return {
        "quantity_gb": quantity_gb,
        "unit_price_cents": unit,
        "total_cents": quantity_gb * unit,
    }


def _billing_context(request, workspace):
    """Everything both the overview and the contracting modal need."""
    subscription = services.ensure_subscription(workspace)
    status = storage_status(workspace)
    addons = list(
        WorkspaceAddon.objects.filter(
            workspace=workspace,
            kind=WorkspaceAddon.Kind.EXTRA_STORAGE,
        ).exclude(status=WorkspaceAddon.Status.CANCELED)
    )
    return {
        "workspace": workspace,
        "subscription": subscription,
        "storage": status,
        "addons": addons,
        "monthly_total_cents": services.workspace_monthly_total_cents(workspace),
        "storage_price_per_gb_cents": services.storage_price_per_gb_cents(),
        "storage_addon_choices": STORAGE_ADDON_CHOICES,
        # Priced for the amount pre-selected in the picker, so the first
        # paint already shows a figure instead of an empty slot that only
        # fills after the customer touches the input.
        "quote": _storage_quote(STORAGE_ADDON_CHOICES[0]),
        "min_gb": STORAGE_ADDON_MIN_GB,
        "max_gb": STORAGE_ADDON_MAX_GB,
        "can_manage_billing": _can_manage_billing(request, workspace),
        # Lets the page say "billing isn't configured yet" up front rather
        # than only after the customer presses Contract and fails.
        "gateway_configured": bool(getattr(settings, "ASAAS_API_KEY", "")),
    }


# ---------------------------------------------------------------------------
# Workspace-facing screens
# ---------------------------------------------------------------------------


@login_required
@require_GET
def workspace_billing(request, workspace_id):
    """Subscription status, monthly cost, disk meter, add-on picker."""
    workspace = _get_workspace(request, workspace_id)
    context = _billing_context(request, workspace)
    context["settings_active"] = "billing"
    return render(request, "billing/workspace_billing.html", context)


@login_required
@require_GET
def subscribe_modal(request, workspace_id):
    """The contracting modal shown when a workspace has no subscription.

    A page rather than markup injected into every layout: the only way in
    is the redirect right after creating a workspace (and the CTA on the
    billing page), and a page can be linked, reloaded and bookmarked
    after a failed attempt.
    """
    workspace = _get_workspace(request, workspace_id)
    context = _billing_context(request, workspace)
    if context["subscription"].is_active:
        return redirect("billing:workspace_billing", workspace_id=workspace.id)
    return render(request, "billing/subscribe_modal.html", context)


@login_required
@require_POST
def subscribe_confirm(request, workspace_id):
    """Contract the workspace's monthly subscription.

    The three outcomes are deliberately different to the user:

    - success          : contracted, awaiting the gateway's confirmation.
    - not configured   : an expected state in every environment without
                         Asaas credentials. Said plainly, as an ``info``
                         message, because nothing is broken and there is
                         nothing for the customer to retry.
    - gateway failure  : an error the customer may retry; logged with the
                         traceback so support has it, never shown one.
    """
    workspace = _get_workspace(request, workspace_id)
    _require_manage_billing(request, workspace)

    try:
        contracting.subscribe_workspace(workspace, email=request.user.email)
    except AsaasNotConfigured:
        # Expected without credentials. The local row stays pending, so
        # pressing Contract again once Asaas is configured just works.
        logger.info("Workspace %s contracting attempted with Asaas unconfigured", workspace.pk)
        messages.info(
            request,
            _(
                "Billing is not set up on this installation yet, so no charge was created. "
                "Your workspace is registered and we will contact you to finish the subscription."
            ),
        )
        return redirect("billing:workspace_billing", workspace_id=workspace.id)
    except AsaasError:
        logger.exception("Asaas rejected contracting for workspace %s", workspace.pk)
        messages.error(
            request,
            _("We could not reach the payment provider. Nothing was charged — please try again in a moment."),
        )
        return redirect("billing:subscribe_modal", workspace_id=workspace.id)

    messages.success(
        request,
        _("Subscription contracted. It activates as soon as the first payment is confirmed."),
    )
    return redirect("billing:workspace_billing", workspace_id=workspace.id)


@login_required
@require_GET
def storage_quote(request, workspace_id):
    """HTMX fragment: the price of the quantity currently selected.

    Server-side so the number the customer agrees to is the number the
    server computed — an Alpine-only calculation would let a tampered
    page display one price and buy at another.
    """
    workspace = _get_workspace(request, workspace_id)
    quantity = _parse_quantity_gb(request.GET.get("quantity_gb"))
    context = {
        "workspace": workspace,
        "quote": _storage_quote(quantity) if quantity else None,
        "invalid": quantity is None and bool(request.GET.get("quantity_gb")),
        "min_gb": STORAGE_ADDON_MIN_GB,
        "max_gb": STORAGE_ADDON_MAX_GB,
    }
    return render(request, "billing/_storage_quote.html", context)


@login_required
@require_POST
def contract_storage(request, workspace_id):
    """Buy extra GB for this workspace.

    The add-on row is written before the gateway is called so a failed or
    unconfigured gateway leaves a ``pending`` record of what the customer
    asked for — which grants no disk (only ``active`` add-ons count) and
    can be retried or cleaned up, instead of vanishing.
    """
    workspace = _get_workspace(request, workspace_id)
    _require_manage_billing(request, workspace)

    quantity = _parse_quantity_gb(request.POST.get("quantity_gb"))
    if quantity is None:
        messages.error(
            request,
            _("Choose how much extra storage to contract, between %(min)s GB and %(max)s GB.")
            % {"min": STORAGE_ADDON_MIN_GB, "max": STORAGE_ADDON_MAX_GB},
        )
        return redirect("billing:workspace_billing", workspace_id=workspace.id)

    addon = services.contract_storage_addon(workspace, quantity)

    try:
        contracting.subscribe_storage_addon(addon)
    except AsaasNotConfigured:
        logger.info("Storage add-on %s created with Asaas unconfigured", addon.pk)
        messages.info(
            request,
            _(
                "Billing is not set up on this installation yet, so no charge was created. "
                "Your request for %(gb)s GB was recorded and we will contact you to finish it."
            )
            % {"gb": quantity},
        )
        return redirect("billing:workspace_billing", workspace_id=workspace.id)
    except AsaasError:
        logger.exception("Asaas rejected storage add-on %s", addon.pk)
        messages.error(
            request,
            _("We could not reach the payment provider. Nothing was charged — please try again in a moment."),
        )
        return redirect("billing:workspace_billing", workspace_id=workspace.id)

    messages.success(
        request,
        _("%(gb)s GB contracted. The space is released as soon as the payment is confirmed.") % {"gb": quantity},
    )
    return redirect("billing:workspace_billing", workspace_id=workspace.id)


# ---------------------------------------------------------------------------
# Staff console
# ---------------------------------------------------------------------------


def _is_billing_staff(user):
    """Gate for the cross-organization billing console.

    ``is_staff``, not ``require_org_permission``: every org-scoped
    decorator in this codebase resolves an ``OrgMembership`` for the
    ``org_id`` in the URL, and this console deliberately has no org in
    its URL — it lists *every* organization so the product owner can comp
    one. There is no membership that could grant that, and inventing an
    org permission for it would mean any customer who grants themselves
    that permission could read every other customer's billing.

    ``is_staff`` is the account flag Django already reserves for
    operators, is what ``/admin/`` uses, and is not something a customer
    can set on themselves through the product.
    """
    return bool(user.is_active and user.is_staff)


def _staff_required(view_func):
    """``login_required`` plus the staff check, 403 for logged-in non-staff.

    Deliberately not ``user_passes_test``: that redirects to the login
    page on failure, so an already-authenticated member who found the URL
    would bounce between login and here forever. Raising
    ``PermissionDenied`` matches how every other permission failure in
    this codebase ends, while the outer ``login_required`` still sends an
    anonymous visitor to log in.
    """

    @functools.wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not _is_billing_staff(request.user):
            raise PermissionDenied("Staff access required.")
        return view_func(request, *args, **kwargs)

    return login_required(_wrapped)


@_staff_required
@require_GET
def staff_console(request):
    """Every organization, its comp budget, and each workspace's state.

    One page rather than a drill-down because the question the product
    owner asks it is comparative: "who is paying, who is comped, who is
    about to run out of disk". Usage is read per workspace from the
    billing cache, which is what every other surface reads.
    """
    organizations = (
        Organization.objects.all()
        .prefetch_related(
            Prefetch(
                "workspaces",
                queryset=Workspace.objects.select_related("billing_subscription").order_by("name"),
            )
        )
        .order_by("name")
    )

    rows = []
    for organization in organizations:
        profile = services.ensure_billing_profile(organization)
        workspace_rows = []
        for workspace in organization.workspaces.all():
            workspace_rows.append(
                {
                    "workspace": workspace,
                    "subscription": services.ensure_subscription(workspace),
                    "storage": storage_status(workspace),
                    "monthly_total_cents": services.workspace_monthly_total_cents(workspace),
                }
            )
        rows.append(
            {
                "organization": organization,
                "profile": profile,
                "workspaces": workspace_rows,
                "monthly_total_cents": sum(w["monthly_total_cents"] for w in workspace_rows),
                "courtesy_exceeded": services.courtesy_quota_exceeded(organization),
            }
        )

    return render(
        request,
        "billing/staff_console.html",
        {
            "rows": rows,
            "workspace_price_cents": services.workspace_price_cents(),
            "storage_price_per_gb_cents": services.storage_price_per_gb_cents(),
        },
    )


@_staff_required
@require_POST
def staff_toggle_courtesy(request, target_workspace_id):
    """Grant or revoke a workspace's billing exemption.

    The action is explicit in the POST (``grant``/``revoke``) rather than
    inferred from current status, so a double-submitted form is
    idempotent instead of flipping the comp back off.

    The kwarg is ``target_workspace_id`` because
    ``apps.members.middleware`` rejects any request whose URL carries a
    ``workspace_id`` the user is not a member of — and an operator comping
    a customer's workspace never is. See the note in ``staff_urls``.
    """
    workspace = get_object_or_404(Workspace.objects.select_related("organization"), id=target_workspace_id)
    action = request.POST.get("action")

    if action == "grant":
        services.grant_courtesy(
            workspace,
            note=request.POST.get("note", "").strip()[:255],
            granted_by=request.user,
        )
        messages.success(
            request,
            _('Workspace "%(name)s" is now a courtesy and will not be charged.') % {"name": workspace.name},
        )
        if services.courtesy_quota_exceeded(workspace.organization):
            messages.warning(
                request,
                _('Organization "%(org)s" now has more courtesy workspaces than its quota allows.')
                % {"org": workspace.organization.name},
            )
    elif action == "revoke":
        services.revoke_courtesy(workspace)
        messages.success(
            request,
            _('Courtesy removed from "%(name)s". It needs a subscription to stay active.') % {"name": workspace.name},
        )
    else:
        messages.error(request, _("Unknown action."))

    return redirect("billing_staff:staff_console")


@_staff_required
@require_POST
def staff_set_courtesy_quota(request, org_id):
    """Set how many of an organization's workspaces may be comped."""
    organization = get_object_or_404(Organization, id=org_id)

    try:
        quota = int(request.POST.get("courtesy_workspace_quota", "").strip())
    except (TypeError, ValueError):
        quota = -1
    if quota < 0:
        messages.error(request, _("The courtesy quota must be zero or a positive whole number."))
        return redirect("billing_staff:staff_console")

    profile = services.ensure_billing_profile(organization)
    profile.courtesy_workspace_quota = quota
    profile.courtesy_note = request.POST.get("courtesy_note", "").strip()[:255]
    profile.save(update_fields=["courtesy_workspace_quota", "courtesy_note", "updated_at"])

    messages.success(
        request,
        _('Courtesy quota for "%(org)s" set to %(quota)s workspace(s).') % {"org": organization.name, "quota": quota},
    )
    if services.courtesy_quota_exceeded(organization):
        messages.warning(
            request,
            _("Existing courtesies above the new quota were kept — nobody starts being charged retroactively."),
        )
    return redirect("billing_staff:staff_console")
