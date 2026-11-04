"""The two endpoints that start and stop an operator's takeover.

Thin on purpose: every rule about who may impersonate whom lives in
``apps.accounts.impersonation``, so there is one place to read and one
place to change. These views only resolve the target, apply the verdict,
and say what happened.

Both are POST. A GET would mean a link, an image tag or a prefetch could
hand a logged-in operator someone else's account, and a takeover is not
something a browser should be able to perform by navigating.
"""

from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts import impersonation
from apps.accounts.models import User

logger = logging.getLogger(__name__)


@login_required
@require_POST
def start_impersonation(request, user_id):
    """Take over ``user_id``'s account, if the rules allow it.

    403 rather than a redirect for a refusal: a non-operator reaching
    this URL is not a navigation mistake, and ``PermissionDenied`` is how
    every other authorisation failure in this codebase ends.
    """
    operator = impersonation.real_user(request)
    if not impersonation.is_operator(request):
        # Covers both a plain customer who found the URL and an operator
        # who is already impersonating someone — the second one would
        # otherwise chain takeovers and lose the original operator from
        # the session.
        raise PermissionDenied("Operator access required.")

    target = get_object_or_404(User, pk=user_id)
    if not impersonation.may_impersonate(operator, target):
        raise PermissionDenied("You may not impersonate this account.")

    impersonation.start(request, target)
    logger.warning(
        "Impersonation started: %s is now acting as %s",
        operator.email,
        target.email,
    )
    messages.warning(
        request,
        _("You are now using %(email)s's account. Everything you do happens as them.")
        % {"email": target.email},
    )
    return redirect("/")


@login_required
@require_POST
def stop_impersonation(request):
    """Hand the session back to the operator.

    No permission check: the only thing this can do is *drop* privilege
    back to the real user named in the session, and that user is the one
    who asked. Refusing a stop would be the dangerous direction — it
    would leave an operator stuck inside a customer's account.
    """
    if not impersonation.is_impersonating(request):
        return redirect("/")

    impersonated_email = request.user.email
    operator = impersonation.stop(request)
    if operator is None:
        # The operator's own account went away mid-takeover;
        # ``stop`` logged the session out rather than leaving it as the
        # customer.
        messages.error(request, _("Your operator session has ended. Please sign in again."))
        return redirect("account_login")

    logger.warning("Impersonation ended: %s stopped acting as %s", operator.email, impersonated_email)
    messages.success(request, _("You are back in your own account."))
    return redirect("billing_staff:staff_console")
