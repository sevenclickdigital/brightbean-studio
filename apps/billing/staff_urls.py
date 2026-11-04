"""Staff-only billing console URLs.

Separate module, and a separate namespace, from ``urls.py`` for the same
reason the Intelligence app splits its org-scoped and global patterns:
these paths carry no ``workspace_id``, so including them in the
workspace-scoped mount would make every reverse() need a workspace it
does not have.

Access is ``is_staff`` — enforced in the views, not here, so the rule
travels with the view rather than the mount point.

The courtesy route's kwarg is ``target_workspace_id``, not
``workspace_id``, and that matters: ``apps.members.middleware`` keys off
the name ``workspace_id`` and raises ``PermissionDenied`` for any URL
carrying one the requesting user is not a member of. An operator comping
a customer's workspace is by definition not a member of it, so naming the
kwarg ``workspace_id`` here would make the console refuse the only thing
it exists to do.
"""

from django.urls import path

from . import views

app_name = "billing_staff"

urlpatterns = [
    # The customers screen keeps the bare path and the ``staff_console``
    # name it had as the one console: it is what the account-menu link
    # reverses, what the no-workspace operator is redirected to, and what
    # the existing tests use.
    path("", views.staff_console, name="staff_console"),
    path("plans/", views.staff_plans, name="plans"),
    # The price-list form posts here rather than back to ``plans/``: the
    # screen is a GET-only read of the whole plan plus a form, and keeping
    # the write on its own path is what lets ``plans/`` stay
    # ``require_GET`` — so a crawler or a prefetch cannot reach the write
    # at all.
    path("plans/prices/", views.staff_update_prices, name="update_prices"),
    path("subscriptions/", views.staff_subscriptions, name="subscriptions"),
    path("impersonations/", views.staff_impersonation_log, name="impersonation_log"),
    path("courtesy/<uuid:target_workspace_id>/", views.staff_toggle_courtesy, name="toggle_courtesy"),
    path("quota/<uuid:org_id>/", views.staff_set_courtesy_quota, name="set_courtesy_quota"),
    # The per-account actions behind the customers table's modal. All
    # POST-only; see the section comment in views.py for why each is its
    # own endpoint rather than one view switching on a hidden field.
    #
    # The user-scoped ones say ``target_user_id`` for symmetry with
    # ``target_workspace_id`` above — not because the middleware keys off
    # it (it does not), but so that every id in this namespace reads as
    # "somebody else's", which is the whole premise of the area.
    path("accounts/<uuid:org_id>/courtesy/", views.staff_grant_courtesy, name="grant_courtesy"),
    path("accounts/<uuid:org_id>/courtesy/end/", views.staff_revoke_courtesy, name="revoke_courtesy"),
    path("accounts/<uuid:org_id>/edit/", views.staff_edit_account, name="edit_account"),
    path("users/<uuid:target_user_id>/reset-password/", views.staff_send_password_reset, name="send_password_reset"),
    path("users/<uuid:target_user_id>/active/", views.staff_set_user_active, name="set_user_active"),
]
