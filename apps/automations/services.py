"""The doors into the automation engine: inbound comments, and per-post flows.

Two unrelated things live here because both are "something outside
:mod:`apps.automations` needs the engine to react", and both must be callable
from inside somebody else's transaction without endangering it.

**Inbound comments.** Two places in the Studio create an
:class:`~apps.inbox.models.InboxMessage` — ``apps.inbox.webhooks._create_if_new``
and ``apps.inbox.tasks.InboxSyncEngine._upsert_message`` — and both call
:func:`on_inbox_message_created` the moment they confirm ``created=True``. One
function rather than two call sites' worth of logic so that "what happens when a
comment arrives" has exactly one answer, whichever door it came through.

**Per-post flows.** :func:`arm_for_platform_post` is what turns a flow drawn in
the composer into a running one, and it is called from the publisher the moment
a post is confirmed live. A flow attached to an already-published post calls the
same function directly, on save — see :class:`PostAutomation`.

This module holds **no** matching or sending logic. Everything else — matching,
guards, sending — is the worker's, in :mod:`apps.automations.actions`.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.inbox.models import InboxMessage

logger = logging.getLogger(__name__)

__all__ = ["arm_for_platform_post", "on_inbox_message_created", "on_platform_post_published"]


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


# ---------------------------------------------------------------------------
# Per-post flows
# ---------------------------------------------------------------------------


def arm_for_platform_post(platform_post, *, activate: bool = True) -> bool:
    """Bind this post's flow to its live post id and switch it on. Idempotent.

    This is the step that makes a flow real. Until it runs, the flow's rule is
    scoped to no post and switched off, so it matches nothing — which is the
    correct state for a flow drawn against a post that does not exist yet.

    Returns whether a flow was armed. ``False`` covers every "nothing to do"
    case — no flow on this post, no platform id yet, already armed — and none of
    those is an error.

    **Both entry points land here.** The composer's draft flow is armed by the
    publisher once the post is confirmed; a flow created on an
    already-published post arms immediately, because the id it needs is already
    on the row. Writing that as one function rather than two is what keeps the
    two paths from drifting: there is a single definition of what "armed" means,
    and it is this one.

    **Arming is a compare-and-set on ``armed_post_id``, not a status check.**
    The publisher reaches this from more than one path (the synchronous publish,
    the retry sweep, and the in-flight confirmation sweep all call
    ``_maybe_schedule_first_comment``'s neighbour), so it runs more than once per
    post by design. The guard is the stored id: a flow that already carries one
    is already armed, whatever its status says — and status is the author's
    control (they may have paused it), so keying off status would make a pause
    get silently undone by the next publisher sweep.
    """
    from apps.automations.models import PostAutomation

    flow = PostAutomation.objects.filter(platform_post=platform_post).select_related("rule").first()
    if flow is None:
        return False

    post_id = (getattr(platform_post, "platform_post_id", "") or "").strip()
    if not post_id:
        # Published but with no id recorded — the same state
        # ``_post_first_comment_task`` refuses on. Nothing to scope a rule to,
        # so the flow stays pending and visible rather than silently active and
        # matching every post on the account.
        logger.warning(
            "Automation flow %s cannot arm: platform post %s has no platform_post_id.",
            flow.pk,
            platform_post.pk,
        )
        return False

    if flow.armed_post_id == post_id:
        return False

    rule = flow.rule
    with transaction.atomic():
        # ``specific`` plus exactly this id. The rule must never widen to "every
        # post on the account" — a flow written for one launch post would start
        # answering comments across the whole feed.
        rule.post_scope = "specific"
        rule.post_ids = [post_id]
        if activate:
            # Only switch on a rule that can actually speak. ``is_active`` with
            # nothing to send is the state ``matching.candidates`` filters out,
            # which reads to the author as a working automation answering nobody.
            from apps.automations.flow import rule_can_act

            rule.is_active = rule_can_act(rule)
        rule.save(update_fields=["post_scope", "post_ids", "is_active", "updated_at"])

        flow.armed_post_id = post_id
        flow.armed_at = timezone.now()
        flow.status = PostAutomation.Status.ACTIVE if rule.is_active else PostAutomation.Status.PAUSED
        flow.save(update_fields=["armed_post_id", "armed_at", "status", "updated_at"])

    logger.info("Automation flow %s armed for platform post %s", flow.pk, platform_post.pk)
    return True


def on_platform_post_published(platform_post) -> None:
    """Arm this post's flow, if it has one. Called by the publisher. Never raises.

    The same contract as :func:`on_inbox_message_created`, for the same reason:
    the caller is the publish path, and a post that went out successfully must
    not be recorded as failed because its optional automation could not be
    armed. The flow stays pending and the author can arm it by hand from the
    post, which is the already-published entry point — so the failure mode is a
    flow that needs one click, not a flow that is lost.
    """
    try:
        arm_for_platform_post(platform_post)
    except Exception:
        logger.exception(
            "Could not arm the automation flow for platform post %s",
            getattr(platform_post, "pk", None),
        )
