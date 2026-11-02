"""URL patterns for comment automations.

Mounted under ``/workspace/<workspace_id>/automations/`` from ``config.urls``, so
every view takes ``workspace_id`` and the RBAC middleware resolves workspace
context before any of them run.
"""

from django.urls import path

from . import views

app_name = "automations"

urlpatterns = [
    path("", views.rule_list, name="list"),
    path("create/", views.rule_create, name="create"),
    path("<uuid:rule_id>/edit/", views.rule_edit, name="edit"),
    path("<uuid:rule_id>/toggle/", views.rule_toggle, name="toggle"),
    path("<uuid:rule_id>/delete/", views.rule_delete, name="delete"),
    # POST-only, and a read despite that: the dry run writes nothing. It is POST
    # because it submits the (possibly unsaved) form state and because a GET
    # would be prefetchable.
    path("<uuid:rule_id>/test/", views.rule_test, name="test"),
    path("log/", views.log_list, name="log"),
]
