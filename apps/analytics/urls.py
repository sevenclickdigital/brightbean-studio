"""URLs for the analytics page."""

from django.urls import path

from . import views

app_name = "analytics"

urlpatterns = [
    path("<uuid:account_id>/status/", views.analytics_status, name="status"),
    # Importing the channel's own posts. The offer is a separate GET because
    # counting costs a platform call the analytics page must not wait on.
    path("<uuid:account_id>/history/", views.history_offer, name="history_offer"),
    path("<uuid:account_id>/history/import/", views.history_import_view, name="history_import"),
    path("", views.analytics_index, name="index"),
    path("post/<uuid:post_id>/", views.post_detail, name="post_detail"),
    path("<uuid:account_id>/", views.analytics_account, name="account"),
]
