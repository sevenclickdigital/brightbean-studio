"""Request context the chrome in ``base.html`` needs about who you are.

One processor, and it only ever adds keys during an impersonation. The
banner it feeds is not decoration: an operator who forgets they are
inside a customer's account and publishes a post, deletes an asset or
replies to a DM does it *as that customer*, and nothing in the rest of
the interface would look any different. So the flag is computed on every
authenticated request rather than in the one or two views that start and
stop the takeover.

Cost in the common case is one session-dict lookup, which is already in
memory — no query. Only an actual takeover pays for the operator row,
and :func:`apps.accounts.impersonation.real_user` is the single place
that reads it.
"""

from __future__ import annotations

from apps.accounts import impersonation


def impersonation_banner(request):
    """Expose ``impersonation`` while this session wears another account.

    Absent otherwise, so the partial costs one ``{% if %}`` on every
    normal page.
    """
    if not impersonation.is_impersonating(request):
        return {}

    operator = impersonation.real_user(request)
    return {
        "impersonation": {
            "operator": operator,
            "impersonated": request.user,
        }
    }
