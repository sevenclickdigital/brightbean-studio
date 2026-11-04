"""Public webhook route for the billing gateway.

Separate from any future authenticated billing URLs (the contracting
modal in phase 5), because this one is called by Asaas rather than by a
logged-in user and must stay outside every auth-gated include.
"""

from django.urls import path

from .webhooks import asaas_webhook

urlpatterns = [
    path("asaas/", asaas_webhook, name="asaas_webhook"),
]
