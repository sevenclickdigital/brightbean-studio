"""Error handlers for 400/403/404/500.

Django's own handlers render the matching template, which is enough for
everything except the 500: that one is rendered by ``server_error`` with an
empty context (deliberately — the request may be the thing that broke), so a
reference code has to be produced here.

The code is minted per failure and logged next to the traceback, which is
what makes it useful: someone reads it off the screen and it leads straight
to the one log entry. It carries no information on its own — no path, no
account, no stack.
"""

from __future__ import annotations

import logging
import uuid

from django.http import HttpResponseServerError
from django.template import TemplateDoesNotExist, loader
from django.views.defaults import server_error as django_server_error

logger = logging.getLogger(__name__)


def server_error(request, template_name="500.html"):
    """Render 500.html with a reference code tying the page to the log entry."""
    reference = uuid.uuid4().hex[:8].upper()

    # Logged with the code so support can find this exact failure. The
    # traceback itself is emitted separately by django.request.
    logger.error(
        "Unhandled server error [ref=%s] path=%s method=%s",
        reference,
        getattr(request, "path", "?"),
        getattr(request, "method", "?"),
    )

    try:
        template = loader.get_template(template_name)
    except TemplateDoesNotExist:
        # Falls back to Django's bare response rather than raising: a second
        # failure here would replace the error page with a worse one.
        return django_server_error(request, template_name)

    return HttpResponseServerError(template.render({"request_id": reference}))
