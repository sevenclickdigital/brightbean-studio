"""Operator impersonation: taking over a customer's account for support.

The most dangerous feature in the product, so every rule it depends on
lives here rather than being spread across a view, a template and a
decorator:

- **Only a real operator starts one.** The ``is_staff`` check reads the
  *real* user, resolved via :func:`real_user`, never ``request.user`` —
  which during a takeover is the customer.
- **Staff never impersonate staff.** Lateral takeover between operators
  is privilege escalation with no audit value: the trail would say
  "operator A acted as operator B", and every action B's account then
  took would be deniable. Refused outright.
- **The takeover carries no operator privilege.** While impersonating,
  :func:`is_operator` is False, so the staff console and anything else
  gated on it are closed. An operator who wants to comp a workspace must
  stop impersonating first. Otherwise the audit trail would show a
  customer's account performing operator actions.
- **Every takeover is recorded**, start and end, in
  ``ImpersonationSession``.

How the session is held
-----------------------
Two keys, written on top of Django's normal auth session:

``IMPERSONATOR_SESSION_KEY``
    The real operator's primary key. Its presence *is* the "I am
    impersonating" flag — there is no separate boolean that could
    disagree with it.
``RECORD_SESSION_KEY``
    The ``ImpersonationSession`` row's id, so stopping can close the
    exact row that was opened instead of guessing at the latest one.

``django.contrib.auth.login`` cycles the session key on every call and
flushes the session, so the keys are written *after* logging in as the
customer and re-written after logging back in as the operator. That
cycling is also what makes the swap safe: a fixated session cookie does
not survive the switch.

Why the operator's id and not a full second session: the id is the
minimum that can restore the operator, and it is re-fetched from the
database on every request, so an operator who is deactivated or demoted
mid-takeover loses the privilege immediately instead of carrying a
snapshot of it around.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model, login
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.accounts.models import ImpersonationSession

# The real operator's pk. Presence of this key means "this session is
# impersonating"; nothing else records that fact.
IMPERSONATOR_SESSION_KEY = "impersonator_user_id"

# The open ImpersonationSession row, so stop() closes the row start()
# opened rather than the most recent one for the pair.
RECORD_SESSION_KEY = "impersonation_record_id"

# Django's own backend path. login() needs a backend when the user did
# not arrive through authenticate(); ModelBackend is first in
# AUTHENTICATION_BACKENDS and is the one that would have answered for a
# password login.
_BACKEND = "django.contrib.auth.backends.ModelBackend"


def is_impersonating(request):
    """True when this session is an operator wearing a customer's account."""
    session = getattr(request, "session", None)
    return bool(session and session.get(IMPERSONATOR_SESSION_KEY))


def real_user(request):
    """The human behind the request, ignoring any impersonation.

    Returns ``request.user`` normally. During a takeover it returns the
    operator, re-read from the database — so a demoted or deactivated
    operator stops being one on their very next request rather than at
    the end of a 14-day session.

    Returns None when there is no authenticated user at all, and falls
    back to ``request.user`` if the stored operator id no longer resolves
    (deleted account), which leaves the session as a plain customer
    session with no privilege attached.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    if not is_impersonating(request):
        return user

    operator = (
        get_user_model().objects.filter(pk=request.session[IMPERSONATOR_SESSION_KEY], is_active=True).first()
    )
    return operator or user


def is_operator(request):
    """True when this request may use operator-only surfaces.

    Deliberately False while impersonating, even though the real user is
    staff: see the module docstring. This is the single predicate the
    staff console asks.
    """
    if is_impersonating(request):
        return False
    user = getattr(request, "user", None)
    return bool(user and user.is_authenticated and user.is_active and user.is_staff)


def may_impersonate(operator, target):
    """Whether ``operator`` is allowed to take over ``target``.

    The two refusals that matter, in one place so a second entry point
    cannot implement only one of them:

    - the operator must be an active staff account;
    - the target must not be staff, and must not be the operator.
    """
    if operator is None or not operator.is_authenticated:
        return False
    if not (operator.is_active and operator.is_staff):
        return False
    if target is None or not target.is_active:
        return False
    if target.pk == operator.pk:
        return False
    # No lateral takeover between operators.
    return not target.is_staff


def start(request, target):
    """Log the session in as ``target``, remembering the operator.

    Raises ``PermissionDenied`` when :func:`may_impersonate` refuses, so
    a caller that forgets to check cannot accidentally allow a takeover.
    Returns the ``ImpersonationSession`` row it opened.
    """
    operator = real_user(request)
    if not may_impersonate(operator, target):
        raise PermissionDenied("You may not impersonate this account.")

    record = ImpersonationSession.objects.create(
        impersonator=operator,
        impersonated=target,
        impersonator_email=operator.email,
        impersonated_email=target.email,
        ip_address=_client_ip(request),
    )

    # login() flushes and cycles the session, so the two keys have to be
    # written after it — not before, or they would be thrown away.
    login(request, target, backend=_BACKEND)
    request.session[IMPERSONATOR_SESSION_KEY] = str(operator.pk)
    request.session[RECORD_SESSION_KEY] = str(record.pk)
    return record


def stop(request):
    """Hand the session back to the operator and close the audit row.

    No re-authentication: the operator never stopped being themselves,
    the session only wore another name. Returns the operator, or None if
    this session was not impersonating (making a double-submitted "stop"
    harmless rather than a crash).
    """
    if not is_impersonating(request):
        return None

    operator_id = request.session.get(IMPERSONATOR_SESSION_KEY)
    record_id = request.session.get(RECORD_SESSION_KEY)

    # Close the trail before the session swap. If the login below fails
    # or the response is lost, an audit row saying the takeover ended is
    # the safe side of the error: the alternative is a row that stays
    # open forever and makes every later report ambiguous.
    if record_id:
        ImpersonationSession.objects.filter(pk=record_id, ended_at__isnull=True).update(ended_at=timezone.now())

    operator = get_user_model().objects.filter(pk=operator_id, is_active=True).first()
    if operator is None:
        # The operator's account is gone or disabled while they were
        # inside. Restoring it is not an option, and leaving the session
        # as the customer would be worse, so end the session entirely.
        from django.contrib.auth import logout

        logout(request)
        return None

    login(request, operator, backend=_BACKEND)
    # login() cycled the session, which already dropped both keys. Popped
    # anyway so this stays correct if that ever changes.
    request.session.pop(IMPERSONATOR_SESSION_KEY, None)
    request.session.pop(RECORD_SESSION_KEY, None)
    return operator


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None
