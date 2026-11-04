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
    path("", views.staff_console, name="staff_console"),
    path("courtesy/<uuid:target_workspace_id>/", views.staff_toggle_courtesy, name="toggle_courtesy"),
    path("quota/<uuid:org_id>/", views.staff_set_courtesy_quota, name="set_courtesy_quota"),
]
