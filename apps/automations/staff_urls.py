"""Staff-only flow template URLs — the operator's library of ready-made flows.

A separate module and namespace from ``urls.py`` for the same reason
``apps.billing.staff_urls`` is: these paths carry no ``workspace_id``,
because the template library belongs to the product rather than to any
one workspace, and mounting them inside the workspace-scoped include
would make every ``reverse()`` demand a workspace that does not exist
here.

It also keeps ``apps.members.middleware`` out of the way. That middleware
keys off the *name* ``workspace_id`` and refuses any URL carrying one the
requesting user is not a member of — an operator editing the shared
library is a member of nothing relevant, so a workspace-scoped path would
refuse the only thing this area exists to do.

Access is ``is_staff``, enforced by ``_staff_required`` on each view and
not here, so the rule travels with the view rather than with the mount
point.
"""

from django.urls import path

from . import template_views

app_name = "automations_staff"

urlpatterns = [
    path("", template_views.template_list, name="template_list"),
    # POST-only, and deliberately not a "new template" form: it creates an
    # empty template and redirects into the canvas. See the view.
    path("new/", template_views.template_create, name="template_create"),
    path("<uuid:template_id>/", template_views.template_edit, name="template_edit"),
    # The canvas posts here by fetch; the card's form posts to ``details``.
    # Two endpoints because they are two different writes with two different
    # response shapes — one returns JSON to the editor, the other redirects.
    path("<uuid:template_id>/save/", template_views.template_save, name="template_save"),
    path("<uuid:template_id>/details/", template_views.template_details, name="template_details"),
    path("<uuid:template_id>/duplicate/", template_views.template_duplicate, name="template_duplicate"),
    path("<uuid:template_id>/delete/", template_views.template_delete, name="template_delete"),
]
