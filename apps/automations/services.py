"""The single door between the inbox and the automation engine.

Two places in the Studio create an :class:`~apps.inbox.models.InboxMessage` —
``apps.inbox.webhooks._create_if_new`` and
``apps.inbox.tasks.InboxSyncEngine._upsert_message`` — and both call
:func:`on_inbox_message_created` the moment they confirm ``created=True``. One
function rather than two call sites' worth of logic so that "what happens when a
comment arrives" has exactly one answer, whichever door it came through.

This module holds **no** decision logic beyond the cheapest possible gate.
Everything else — matching, guards, sending — is the worker's, in
:mod:`apps.automations.actions`.
"""

from __future__ import annotations

import logging

from apps.inbox.models import InboxMessage

logger = logging.getLogger(__name__)

__all__ = ["on_inbox_message_created"]


def on_inbox_message_created(message) -> None:
    """Hand a newly created inbox message to the automation engine.

    Called from the two places a message is born — the webhook handler and
    the polling engine — right after each confirms ``created=True``.

    **This function never raises, and that is its most important property.** One
    of its two callers is the body of a live webhook handler, and the other is
    inside ``@transaction.atomic``. An exception escaping here does not mean
    "the automation failed"; it means the handler unwinds and **the customer's
    message is never recorded in the inbox at all**. Losing a customer's comment
    is strictly worse than failing to automate a reply to it, so every failure
    is logged and swallowed. ``except Exception`` is therefore deliberate and
    must stay broad: a narrower clause is a list of the failures somebody
    thought of.

    **It performs no I/O.** The only work is one cheap in-memory check and one
    enqueue. That is what makes it safe to call from inside the polling engine's
    transaction: it adds no network round trip to a webhook that Meta times out,
    and it holds no locks. If that transaction then rolls back, the queued task
    will look the message up by id, find nothing, and return — step 1 of
    :func:`apps.automations.actions.run_for_message` exists for exactly that.

    The gate is intentionally only ``message_type == COMMENT``. No rule query
    happens here: "does this workspace have a matching rule" needs the rules
    table and the keyword lists, which is the worker's job, and doing it inline
    would put a query on the webhook's hot path for every DM, mention and review
    the inbox ingests.
    """
    try:
        if getattr(message, "message_type", None) != InboxMessage.MessageType.COMMENT:
            return
        from apps.automations.tasks import run_automation_for_message

        # ``str(message.id)``, never the instance: ``@background`` serialises its
        # arguments to JSON, and a model instance is not JSON. Passing the pk
        # also means the worker re-reads the row rather than acting on a copy
        # that was captured before the transaction committed.
        run_automation_for_message(str(message.id))
    except Exception:
        # Only our own identifiers reach the log: the message pk is a UUID we
        # generated. No comment body, no sender handle (log injection).
        logger.exception("Could not enqueue automations for inbox message %s", getattr(message, "id", None))
