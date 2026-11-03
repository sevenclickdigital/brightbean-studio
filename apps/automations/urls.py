"""URL patterns for comment automations.

Mounted under ``/workspace/<workspace_id>/automations/`` from ``config.urls``, so
every view takes ``workspace_id`` and the RBAC middleware resolves workspace
context before any of them run.
"""

from django.urls import path

from . import flow_views, views

app_name = "automations"

urlpatterns = [
    # The per-post flow builder, keyed by ``PlatformPost`` rather than by rule:
    # the canvas is reached from a post (in the composer or after publishing),
    # and the flow it edits may not exist yet when the URL is first opened.
    path(
        "post/<uuid:platform_post_id>/flow/",
        flow_views.flow_builder,
        name="flow_builder",
    ),
    path(
        "post/<uuid:platform_post_id>/flow/save/",
        flow_views.flow_save,
        name="flow_save",
    ),
    path(
        "post/<uuid:platform_post_id>/flow/toggle/",
        flow_views.flow_toggle,
        name="flow_toggle",
    ),
    path(
        "post/<uuid:platform_post_id>/flow/delete/",
        flow_views.flow_delete,
        name="flow_delete",
    ),
    # Workspace-scoped rather than per-post: the library belongs to the
    # workspace, and the picker asks the same question whichever flow is open.
    path("media/", flow_views.flow_media, name="flow_media"),
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
