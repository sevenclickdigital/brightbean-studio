"""Public legal pages.

Meta's App Review requires a Privacy Policy URL and a data-deletion URL that
both resolve anonymously. These views therefore take no login decorator and
read nothing from the database — App Review fetches them with no session.
"""

from django.conf import settings
from django.shortcuts import render

# Shown in the page header. Static rather than auto-updated: the date should
# change when the text changes, not on every deploy.
LAST_UPDATED = "2 de novembro de 2026"

APP_NAME = "Seven Automator"


def _context():
    return {
        "app_name": APP_NAME,
        "last_updated": LAST_UPDATED,
        # DEFAULT_FROM_EMAIL is often a noreply address, so prefer an explicit
        # contact when one is configured.
        "contact_email": getattr(settings, "LEGAL_CONTACT_EMAIL", "")
        or "contato@sevenclick.com.br",
    }


def privacy_policy(request):
    """Public Privacy Policy, linked from the Meta app configuration."""
    return render(request, "legal/privacy_policy.html", _context())


def data_deletion(request):
    """Public data-deletion instructions, required by Meta App Review."""
    return render(request, "legal/data_deletion.html", _context())
