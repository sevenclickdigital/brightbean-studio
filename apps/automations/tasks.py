"""Background tasks for comment automations.

Deliberately thin. Both tasks are schedule declarations that delegate
immediately: the work lives in :mod:`apps.automations.actions`, which is a
plain module a test can call without a worker. Logic that only runs inside a
``@background`` wrapper is logic nobody exercises until production.
"""

import logging
from datetime import timedelta

from background_task import background

logger = logging.getLogger(__name__)

#: How long an :class:`~apps.automations.models.AutomationLog` row is kept.
#:
#: Months, not days, and bounded from below by the guards rather than by taste.
#: Every row in that table is also an idempotency claim, so deleting one
#: *releases* it and makes its comment answerable again. Ninety days clears the
#: seven-day private-reply window by a wide margin, so no row we drop can still
#: be guarding something a redelivery or a backfill could resurface — while
#: still keeping a quarter of history for the activity feed and for answering
#: "what did this rule say to my customers".
AUTOMATION_LOG_RETENTION_DAYS = 90

#: Daily is often enough for a table that only grows by one row per handled
#: comment, and it keeps the purge off the hour boundaries the hourly throttle
#: counts over.
PURGE_INTERVAL_SECONDS = 24 * 60 * 60


@background(schedule=0)
def run_automation_for_message(message_id: str) -> None:
    """Match one newly-arrived comment and act on it.

    Enqueued by :func:`apps.automations.services.on_inbox_message_created`.

    Queued rather than run inline because the handler makes up to two Graph API
    round trips, and its caller is a webhook Meta expects to answer in well
    under a couple of seconds. The handler is also required to be safe to run
    more than once: django-background-tasks re-runs a task that committed
    without being marked done, which is exactly the redelivery case
    :mod:`apps.automations.guards` is built to lose gracefully.
    """
    from apps.automations.actions import run_for_message

    run_for_message(message_id)


@background(schedule=0)
def purge_automation_logs() -> None:
    """Drop automation logs past :data:`AUTOMATION_LOG_RETENTION_DAYS`.

    Exceptions are caught rather than propagated, matching
    ``apps.common.tasks.purge_email_counters``: django-background-tasks backs a
    raising task off exponentially and deletes it after 25 attempts, so letting
    a transient database error escape would cost the *schedule* — and a purge
    that silently stopped running is how a table grows without bound.
    """
    from apps.automations.actions import purge_logs

    try:
        deleted = purge_logs(older_than=timedelta(days=AUTOMATION_LOG_RETENTION_DAYS))
    except Exception:
        logger.exception("Automation log purge failed")
        return

    if deleted:
        logger.info("Purged %d automation log row(s) older than %d days", deleted, AUTOMATION_LOG_RETENTION_DAYS)
