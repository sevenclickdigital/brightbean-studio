"""The execution layer: the code that actually speaks in public on a customer's post.

Ported in shape from brightbean-chat ``apps/channels/providers/instagram.py``:
:func:`pick_text` is ``_public_reply_text``, and the order of operations in
:func:`run_for_message` is ``_answer_comment`` / ``_send_public_reply`` /
``_open_thread`` collapsed into one function, because the Studio has no flow
engine to re-dispatch into.
Copyright (c) BrightBean — https://github.com/brightbeanxyz/brightbean-chat
Licensed under AGPL-3.0.

**Everything in this module is ordered, and the order is the behaviour.** Each
step's docstring names the specific failure that moving it would cause. The two
rules that must survive every future edit:

1. Every *refusal* check happens **before** :func:`guards.record_comment`.
   Claiming is what spends the once-per-sender-per-post guard, and that guard is
   the scarce resource — one per person per post, forever. A comment refused
   after the claim has burned somebody's only chance at a reply in exchange for
   a send that never happened.
2. The public reply and the private reply are **independent halves**, each with
   its own claim and its own ``except``. A failed public reply must not cost the
   DM: the DM is what the rule's author actually configured, it has a seven-day
   deadline, and the public comment is cosmetic next to it. The reverse holds
   too — a DM failure must not retract a comment already visible under the post.

**No platform-supplied text reaches a log line anywhere in this module** — not
the comment body, not the sender handle, not the reply text, not a post id.
Logs carry our own ids (message pk, rule pk, log row pk), our own enum values
(outcome, platform) and exception *types*. A log line built from a comment body
is a log-injection sink, and this module's input is attacker-authored by
definition.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Any

from django.utils import timezone

from apps.automations import guards, matching
from apps.automations.models import AutomationLog, AutomationRule
from apps.inbox.models import InboxMessage
from apps.inbox.reply_policy import PROGRAMMATIC

logger = logging.getLogger(__name__)

__all__ = ["PRIVATE_REPLY_PLATFORMS", "pick_text", "render_text", "run_for_message"]

#: Platforms whose API has a *private reply to a comment* endpoint at all.
#:
#: Narrower than "platforms with DMs": a private reply addresses a **comment**
#: (``{"recipient": {"comment_id": ...}}``, see
#: ``providers.meta_messaging.build_send_payload``) rather than a person, which
#: is the only way to DM a commenter who has never messaged the account and so
#: has no PSID/IGSID yet. The same three platforms as
#: ``providers.meta_messaging.META_DM_PLATFORMS``, stated independently here
#: because the two sets answer different questions and must be free to diverge:
#: a platform could gain DM support without gaining a comment-addressed send.
PRIVATE_REPLY_PLATFORMS = frozenset({"facebook", "instagram", "instagram_login"})

#: How long a ``detail`` string may be before it is cut. ``AutomationLog.detail``
#: is a ``TextField`` so there is no column limit, but a provider error echoed in
#: full can be kilobytes of JSON, and this column is rendered in the activity
#: feed.
_MAX_DETAIL = 500


# ---------------------------------------------------------------------------
# Text selection and rendering
# ---------------------------------------------------------------------------


def pick_text(texts: Any, mode: str) -> str:
    """One sendable body out of a rule's list, or ``""`` when there is none.

    ``static`` takes the first, ``random`` takes one at random. Ported from
    brightbean-chat ``apps/channels/providers/instagram.py::_public_reply_text``
    (AGPL-3.0).

    Blank and non-string entries are filtered out *before* the mode is applied,
    not after. A rule's texts live in a JSONField that the form, the admin, a
    Chat import and a hand-written script can all write, so ``[""]``,
    ``["  "]`` and ``[None]`` are real stored values — and a ``random`` pick
    that landed on one would either post an empty comment or be refused by Meta,
    intermittently, for one comment in three. Filtering first makes "has nothing
    to say" a single deterministic answer: the empty string.

    ``ReplyMode.AI`` deliberately falls through to ``""``. The AI fields on
    ``AutomationRule`` are declared but inert (see the model), and an unknown
    mode must mean "say nothing" rather than "say the first thing" — guessing
    would publish static text under a rule whose author asked for generated
    text.
    """
    if not isinstance(texts, list | tuple):
        return ""
    usable = [value.strip() for value in texts if isinstance(value, str) and value.strip()]
    if not usable:
        return ""
    if mode == AutomationRule.ReplyMode.STATIC:
        return usable[0]
    if mode == AutomationRule.ReplyMode.RANDOM:
        # ``secrets`` rather than ``random``: not because a reply needs
        # unpredictability, but because ruff's flake8-bandit rules ban the
        # pseudo-random generator outright, and an exemption here would have to
        # be re-justified by every future reader. Same reasoning as the Chat
        # original.
        return secrets.choice(usable)
    return ""


def render_text(text: str, message: InboxMessage) -> str:
    """Substitute ``{sender_name}`` / ``{account_name}`` / ``{post_url}``.

    **The same mechanism as** :meth:`apps.inbox.models.SavedReply.render` —
    literal ``{key}`` replacement over a context dict — deliberately reusing its
    syntax rather than inventing one. A member who learned the placeholders from
    the saved-reply composer must be able to type the same thing into a rule;
    two substitution dialects in one product is a support burden with no upside.

    It is *replicated* rather than imported because ``SavedReply.render`` is a
    bound method that reads ``self.body``: there is no free function to call,
    and constructing a throwaway ``SavedReply`` to borrow it would be a model
    instantiation per reply for three ``str.replace`` calls. The behaviour is
    the same, including the deliberate no-op on an unknown placeholder — an
    unrecognised ``{foo}`` is left standing verbatim rather than raising, so a
    typo in a rule body publishes slightly odd text instead of silently sending
    nothing at all. ``str.format`` would have done the opposite and raised.

    ``post_url`` is accepted so a body written in the saved-reply composer and
    pasted into a rule does not publish a stray literal, but it resolves to
    ``""`` today: nothing in the Studio persists a published post's public URL
    (``apps.publisher.engine`` returns ``result.url`` and discards it), and the
    only other candidate is the platform-supplied ``permalink_url`` sitting in
    ``extra`` — which is attacker-influenced data that this function's output
    gets *published into*. Substituting empty is the safe failure: the sentence
    reads a little short. Substituting a payload-supplied URL would let a
    crafted comment make the account post a link of the attacker's choosing.
    """
    if not text:
        return ""
    account = message.social_account
    context = {
        "sender_name": message.sender_name or "",
        "account_name": getattr(account, "account_name", "") or "",
        "post_url": "",
    }
    for key, value in context.items():
        text = text.replace(f"{{{key}}}", str(value))
    return text


# ---------------------------------------------------------------------------
# The entry point the worker calls
# ---------------------------------------------------------------------------


def run_for_message(message_id: str) -> None:
    """Match one stored comment against this workspace's rules and act on it.

    Called from :func:`apps.automations.tasks.run_automation_for_message`. It is
    a plain function rather than the task itself so the whole pipeline is
    testable without a worker, and so the task module stays a thin schedule
    declaration.

    Never raises. The caller is a queue handler; django-background-tasks backs a
    raising task off exponentially and deletes it after 25 attempts, so an
    escaping exception turns one unanswerable comment into a task that retries
    for hours and then vanishes. Everything that can be refused is logged and
    recorded instead.
    """
    try:
        _run(message_id)
    except Exception:
        # Only our own id is logged: the message pk is a UUID we generated.
        logger.exception("Automation run failed for inbox message %s", message_id)


def _run(message_id: str) -> None:
    """The ordered pipeline. See the module docstring before reordering anything."""
    # Step 1 — re-read everything from the database, trusting only the id.
    #
    # The id came through a JSON-serialised queue payload, and the row is the
    # durable record either way. Re-reading also closes the
    # enqueued-inside-a-transaction case: ``inbox.tasks._upsert_message`` is
    # ``@transaction.atomic`` and enqueues from inside it, so a rollback leaves
    # a queued task naming a message that never existed. That is not an error
    # condition — it is the correct outcome, and it exits here silently.
    message = InboxMessage.objects.select_related("social_account", "workspace").filter(pk=message_id).first()
    if message is None:
        logger.debug("Automation skipped: inbox message %s is gone", message_id)
        return
    if message.message_type != InboxMessage.MessageType.COMMENT:
        # The services gate already filters this; repeated here because this
        # function is also the manual-replay entry point.
        return

    account = message.social_account
    extra = message.extra or {}

    # Step 2 — never answer ourselves.
    #
    # Replying from the inbox posts a real comment, which Meta hands straight
    # back as a new event. Without this, an automation's own public reply
    # matches the rule that produced it and answers itself — a loop that is
    # visible to the customer's whole audience and that burns the hourly budget
    # in one post. ``_is_own_activity`` is *reused* rather than reimplemented:
    # it knows that an account's identity is ``account_platform_id`` *or*
    # ``webhook_target_id`` (an Instagram account reached through a linked
    # Page authors comments under a different id than the one we subscribed
    # with), and a second copy of that knowledge is a second place to get it
    # wrong.
    from apps.inbox.webhooks import _is_own_activity

    author = extra.get("from")
    if _is_own_activity(account, author if isinstance(author, dict) else {}):
        _log_outcome(
            message,
            rule=None,
            outcome=AutomationLog.Outcome.SKIPPED_OWN,
            detail="The account authored this comment itself.",
        )
        return

    # Step 3 — which rule, if any. No writes happen in ``matching``.
    context = matching.MatchContext.from_inbox_message(message)
    rule = matching.match(context)
    if rule is None:
        # **Deliberately not recorded.** A ``no_match`` row per unmatched
        # comment means one row per comment on every post, matched or not: a
        # single viral post writes tens of thousands of rows that say "we did
        # nothing", the activity feed drowns in them, and ``purge_automation_logs``
        # becomes load-bearing rather than hygiene. The table's job is to hold
        # the guards and the record of what we *said*; "we stayed quiet" is
        # adequately recorded by the inbox row that already exists. DEBUG only.
        logger.debug("Automation: no rule matched inbox message %s", message.pk)
        return

    # Step 4 — the seven-day window, BEFORE the claim.
    #
    # A comment that surfaces eight days late (a backfill, a retry storm, a
    # worker that was down) would otherwise consume the single
    # once-per-sender-per-post claim that person had on that post, in exchange
    # for a private reply Meta is certain to refuse. The claim is the scarce
    # resource, so eligibility is decided before it is taken.
    if not guards.may_claim_comment(message.received_at):
        _log_outcome(
            message,
            rule=rule,
            outcome=AutomationLog.Outcome.SKIPPED_WINDOW,
            detail=f"Comment is older than the {guards.PRIVATE_REPLY_WINDOW.days}-day private-reply window.",
        )
        return

    # Step 5 — the hourly ceiling, also BEFORE the claim, for the same reason.
    #
    # A throttled comment must stay answerable: traffic drains out of the
    # rolling window within the hour, and a human can still reply by hand. Had
    # the claim been taken first, the throttle would have permanently consumed
    # it and the automation would have silently blacklisted that person on that
    # post.
    sent_this_hour = guards.hourly_count(rule)
    if sent_this_hour >= rule.max_per_hour:
        _log_outcome(
            message,
            rule=rule,
            outcome=AutomationLog.Outcome.SKIPPED_LIMIT,
            detail=f"Rule reached its ceiling of {rule.max_per_hour} replies per hour.",
        )
        return

    # Step 6 — now claim. The INSERT is the guard; there is no ``exists()``
    # check anywhere near it (see ``guards.record_comment``).
    row = guards.record_comment(
        social_account=account,
        rule=rule,
        comment_id=message.platform_message_id,
        post_id=context.post_id,
        sender_id=context.sender_id,
        commented_at=message.received_at,
        once_per_sender_per_post=rule.once_per_sender_per_post,
        inbox_message=message,
        workspace=message.workspace,
    )
    if row is None:
        # Two unrelated refusals share this ``None``; ``claimed_comment`` is the
        # only way to tell them apart, and the distinction is worth a query
        # because one of them is routine and the other is the guard working.
        existing = guards.claimed_comment(account, message.platform_message_id)
        if existing is not None:
            # Webhook redelivery, or our own claim coming back. Already handled
            # by whoever holds that row; nothing is sent and nothing is written,
            # because writing a second row here is exactly what the unique
            # constraint just refused.
            logger.debug(
                "Automation: inbox message %s is already claimed by log row %s",
                message.pk,
                existing.pk,
            )
        else:
            # No row for *this* comment, so a different comment from the same
            # person on the same post holds the once-per-sender-per-post lock.
            # The guard doing its job.
            logger.debug(
                "Automation: once-per-sender-per-post guard declined inbox message %s (rule %s)",
                message.pk,
                rule.pk,
            )
        return

    # Steps 7 and 8 — two independent halves. Each one claims separately, sends
    # separately and catches separately; see the module docstring for why the
    # failure of either must not touch the other.
    details: list[str] = []
    public_sent = _send_public_half(message, rule, row, details)

    # Step 8 has two shapes, and which one runs is decided by the flow itself.
    #
    # A flow that is a straight line — trigger, reply, DM — is sent by the v1
    # path below: one claim, one DM, done, with the guards and the audit trail
    # that path already carries. A flow with buttons, cards, conditions or a
    # chain of messages cannot be: it is a conversation that pauses for a tap,
    # so it needs persisted state and a resumable walk
    # (:mod:`apps.automations.engine`).
    #
    # The v1 path is kept rather than replaced because it is the shape most
    # flows have and the one proven in production; routing every flow through
    # the executor would put a state machine behind a send that does not need
    # one.
    private_sent = False
    if _runs_as_graph(rule):
        private_sent = _start_graph_run(message, rule, details)
    else:
        private_sent = _send_private_half(message, rule, row, details)

    _finalise(row, sent=public_sent or private_sent, details=details)


def _runs_as_graph(rule: AutomationRule) -> bool:
    """Whether this rule's flow needs the graph executor rather than the v1 path.

    True when the drawn graph holds anything the two-channel shape cannot
    express: a button, a card, a condition, or more than one message after the
    trigger. Decided from the stored graph rather than from a flag on the rule,
    so the answer cannot fall out of step with what the author actually drew.

    A rule with no flow attached — one written by hand in the rules list — is
    always False. Those have no graph at all.
    """
    flow = getattr(rule, "post_automation", None)
    graph = getattr(flow, "graph", None)
    if not isinstance(graph, dict):
        return False
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return False

    message_nodes = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type in {"condition", "send_card"}:
            return True
        if node_type in {"send_dm", "reply_comment"}:
            message_nodes += 1
            data = node.get("data")
            if isinstance(data, dict) and data.get("buttons"):
                return True
    if message_nodes > 2:
        return True
    # A node wired to more than one successor fans out, and only the graph
    # executor walks every branch. The two-channel path would deliver one
    # reply and one DM in a fixed order regardless of what was drawn, so a
    # deliberate split would come out as something the author did not draw.
    return _fans_out(graph)


def _fans_out(graph: dict) -> bool:
    """Whether any node in this graph has more than one outgoing default edge.

    Labelled edges are excluded: those belong to a condition's two outcomes or
    to a specific reply button, and each of those already forces the graph
    executor on its own.
    """
    edges = graph.get("edges")
    if not isinstance(edges, list):
        return False
    seen: set[str] = set()
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("branch"):
            continue
        source = edge.get("from")
        if not isinstance(source, str) or not source:
            continue
        if source in seen:
            return True
        seen.add(source)
    return False


def _start_graph_run(message: InboxMessage, rule: AutomationRule, details: list[str]) -> bool:
    """Hand this comment to the graph executor. Returns whether the walk began.

    The executor owns delivery from here, including its own failure handling,
    so this function only reports whether a run started — ``_finalise`` records
    that the automation acted, and the run's own progress lives on
    ``FlowRun``.
    """
    account = message.social_account
    if account.platform not in PRIVATE_REPLY_PLATFORMS:
        details.append(f"Flows are not available on {account.platform}; nothing was sent.")
        return False

    flow = rule.post_automation
    graph = flow.graph if isinstance(flow.graph, dict) else {}
    start = _first_step_id(graph)
    if not start:
        details.append("This flow has no step after its trigger.")
        return False

    from apps.automations.engine import start_run

    try:
        run = start_run(message, rule, graph, start)
    except Exception as exc:
        logger.warning(
            "Flow run could not start: message=%s rule=%s error_type=%s",
            message.pk,
            rule.pk,
            type(exc).__name__,
        )
        details.append("The flow could not be started.")
        return False
    if run is None:
        details.append("A flow is already running for this person.")
        return False
    return True


def _first_step_id(graph: dict) -> str:
    """The node a run starts at: whatever the trigger points to.

    The trigger itself is not executed — it is the condition that got us here,
    not a step — so the walk begins at its first outgoing edge. A graph with no
    trigger falls back to the first message node, which is the forgiving reading
    of a flow drawn by an older editor or edited by hand.
    """
    from apps.automations.engine import _next_node_id, _nodes_by_id

    nodes = _nodes_by_id(graph)
    for node_id, node in nodes.items():
        if node.get("type") == "trigger_comment":
            return _next_node_id(graph, node_id)
    for node_id, node in nodes.items():
        if node.get("type") in {"send_dm", "reply_comment", "send_card"}:
            return node_id
    return ""


# ---------------------------------------------------------------------------
# Step 7 — the public reply
# ---------------------------------------------------------------------------


def _send_public_half(message: InboxMessage, rule: AutomationRule, row: AutomationLog, details: list[str]) -> bool:
    """Post the public reply under the comment. Returns whether it went out.

    Routed through :func:`apps.inbox.services.send_reply`, never the provider.
    That gives us, for free and identically to a human reply: an ``InboxReply``
    row as the durable record, the ``platform_reply_id``, a human-readable
    ``send_error`` on refusal, the comment-vs-messaging edge routing (including
    Facebook's two-level threading rule and Instagram's media-vs-replies edge),
    and the SLA side effects. A direct provider call here would be a second,
    divergent copy of all of that, and the first thing to drift would be the
    edge selection — which fails by publishing a *new top-level comment* on the
    customer's post instead of a threaded reply.

    Every exception is swallowed. The DM runs next and is the half that matters.
    """
    if not rule.public_reply_enabled:
        return False
    text = render_text(pick_text(rule.public_reply_texts, rule.public_reply_mode), message)
    if not text:
        details.append(_no_text_detail(rule.public_reply_mode, "Public reply"))
        return False

    # Claimed before the HTTP call, not recorded after it. The failure being
    # prevented is a duplicate comment under a customer's post: public,
    # permanent and visible to their audience. A claim spent on a reply that
    # then failed costs only that reply.
    if not guards.claim_public_reply(row):
        logger.debug("Automation: log row %s already holds its public-reply claim", row.pk)
        return False

    from apps.inbox.services import reply_failure_reason, send_reply

    try:
        send_reply(message=message, body=text, author=None, origin=PROGRAMMATIC)
    except Exception as exc:
        # Type only, never ``str(exc)``: a provider error echoes back the
        # request, which contains the comment text.
        logger.warning(
            "Automation public reply failed: message=%s rule=%s error_type=%s",
            message.pk,
            rule.pk,
            type(exc).__name__,
        )
        details.append(f"Public reply failed: {reply_failure_reason(exc)}")
        return False
    return True


# ---------------------------------------------------------------------------
# Step 8 — the private reply (DM)
# ---------------------------------------------------------------------------


def _send_private_half(message: InboxMessage, rule: AutomationRule, row: AutomationLog, details: list[str]) -> bool:
    """Send the DM as a *private reply to the comment*. Returns whether it went out.

    **This is the one place in the automations app that calls a provider
    directly, and it has to be.** ``inbox.services.send_reply`` routes on
    ``message.message_type``, and a ``comment`` row is routed to
    ``reply_to_comment`` — a public comment, which is precisely the opposite of
    what this half is for. The alternatives were worse:

    * Fabricating a ``message_type="dm"`` ``InboxMessage`` to make the router
      pick the messaging branch would put a message the customer never sent into
      the customer's inbox, with an SLA clock on it.
    * A new parameter on ``inbox.services`` is the right long-term shape, and
      should be done when that module is next opened; it was out of scope here.

    So the dispatch is replicated in the smallest possible form —
    ``_resolve_publish_credentials`` → ``get_provider`` →
    ``reply_to_message`` — the same three calls
    ``inbox.services._dispatch_to_platform`` makes, and the error text still
    comes from ``inbox.services.reply_failure_reason`` so a failed automation
    reads like a failed manual reply.

    ``extra["private_reply_to_comment_id"]`` is what switches the Send API body
    from addressing a person to addressing a comment
    (``providers.meta_messaging.build_send_payload``). It is required, not an
    optimisation: a commenter who has never messaged the account has no
    PSID/IGSID at all, so there is nothing else to address, and the
    comment-addressed call is what opens the thread.

    ``reply_eligibility`` is deliberately **not** consulted. It enforces the
    24-hour DM↔DM messaging window, and a private reply to a comment is a
    different endpoint with its own seven-day allowance — ``guards``' window,
    already checked. Running the DM policy here would refuse sends Meta accepts,
    throwing away six of the seven days on every comment older than a day, and
    it would fail invisibly because a reply never attempted produces no error.
    """
    if not rule.private_reply_enabled:
        return False

    account = message.social_account
    if account.platform not in PRIVATE_REPLY_PLATFORMS:
        # Not a failure and not worth a claim: the rule is simply configured for
        # something this platform's API cannot do. Said in ``detail`` so the
        # author can see why the DM half of their rule never fires.
        details.append(f"Private replies are not available on {account.platform}; DM skipped.")
        return False

    text = render_text(pick_text(rule.private_reply_texts, rule.private_reply_mode), message)
    if not text:
        details.append(_no_text_detail(rule.private_reply_mode, "Private reply"))
        return False

    # Read-only question first — "is this still worth attempting" — then the
    # claim. ``may_private_reply`` also re-checks the deadline against the
    # ``commented_at`` the row actually stored (clamped to "not in the future"),
    # which is the authoritative clock, rather than against ``received_at``.
    if not guards.may_private_reply(row):
        details.append("Private reply window had closed.")
        return False
    if not guards.claim_private_reply(row):
        logger.debug("Automation: log row %s already holds its private-reply claim", row.pk)
        return False

    from apps.inbox.services import reply_failure_reason

    try:
        _dispatch_private_reply(message, text)
    except Exception as exc:
        logger.warning(
            "Automation private reply failed: message=%s rule=%s error_type=%s",
            message.pk,
            rule.pk,
            type(exc).__name__,
        )
        details.append(f"Private reply failed: {reply_failure_reason(exc)}")
        return False
    return True


def _dispatch_private_reply(message: InboxMessage, text: str) -> str:
    """Call the Send API with a comment-addressed body. Returns the platform id.

    Raises on refusal, so the caller never records a DM as delivered when it was
    not. Split out from :func:`_send_private_half` so a test can replace exactly
    the network call and nothing else.

    No ``InboxReply`` row is written for this send, and that is a decision rather
    than an omission. ``InboxReply`` means "a reply to *this inbox message*", and
    the inbox UI renders those under the comment thread — a DM filed there would
    display as though it had been posted publicly under the customer's post,
    which is the single most misleading thing this feature could show. The DM's
    durable record is ``AutomationLog.private_reply_sent_at`` plus ``detail``,
    which is where the activity feed reads it from, and it is the same row the
    claim lives on, so the record and the guard cannot disagree.
    """
    from apps.publisher.engine import _resolve_publish_credentials
    from providers import get_provider

    account = message.social_account
    provider = get_provider(account.platform, _resolve_publish_credentials(account))

    # A copy, never the stored dict: this adds a key that changes the Send API's
    # addressing mode, and mutating ``message.extra`` would persist it onto the
    # inbox row the next time anything saved that message — turning every later
    # manual DM reply on this thread into a private reply, which Meta allows
    # once per comment.
    extra = dict(message.extra or {})
    extra["private_reply_to_comment_id"] = message.platform_message_id

    result = provider.reply_to_message(
        access_token=account.oauth_access_token,
        message_id=message.platform_message_id,
        text=text,
        extra=extra,
        # Never HUMAN_AGENT. That tag asserts a *person* is answering outside
        # the 24-hour window; an automated reply is the opposite claim, and Meta
        # may penalise the Page for making it. ``build_send_payload`` forces it
        # off whenever a comment id is present, so this is belt and braces.
        human_agent=False,
    )
    return getattr(result, "platform_message_id", "") or ""


# ---------------------------------------------------------------------------
# Log bookkeeping
# ---------------------------------------------------------------------------


def _no_text_detail(mode: str, label: str) -> str:
    """Why a configured reply channel had nothing to say."""
    if mode == AutomationRule.ReplyMode.AI:
        return f"{label} is set to AI, which ships in v2; nothing was sent."
    return f"{label} is enabled but has no usable text."


def _finalise(row: AutomationLog, *, sent: bool, details: list[str]) -> None:
    """Write the outcome onto the claim row.

    ``SENT`` when *either* half went out, because the row is one reply's worth
    of budget (``guards.hourly_count`` counts it once however many halves
    succeeded) and because "we spoke to this person about this comment" is true
    if either did. ``FAILED`` only when nothing at all was delivered.

    ``detail`` carries both halves' notes either way: a row that is ``sent``
    because the DM worked still needs to say that the public reply was refused,
    or the rule's author sees a success for an automation that is visibly doing
    half its job.
    """
    row.outcome = AutomationLog.Outcome.SENT if sent else AutomationLog.Outcome.FAILED
    row.detail = " ".join(details)[:_MAX_DETAIL]
    row.save(update_fields=["outcome", "detail"])


def _log_outcome(message: InboxMessage, *, rule: AutomationRule | None, outcome: str, detail: str) -> None:
    """Record a refusal that happened *before* any claim was taken.

    Written with ``once_per_sender_per_post=False`` unconditionally, and that is
    the load-bearing part: a skip row is a record of a reply that never
    happened, so it must stay **out** of the partial unique index. Were it to
    enter, a single ``skipped_window`` row would hold that person's
    once-per-post lock forever and no later comment of theirs on that post could
    ever be answered — the backfill scenario in step 4, but permanent instead of
    one-off.

    ``record_comment`` is not used here for the same reason: it is the claim, and
    these paths are the ones that must not claim. The ``comment_key`` is still
    bounded the same way so the row is findable by the same lookup, but a
    collision with an existing claim row would just mean this skip is not
    recorded — which is why the insert is best-effort and swallows its error.
    """
    from apps.automations.guards import _bounded

    try:
        AutomationLog.objects.create(
            workspace=message.workspace,
            social_account=message.social_account,
            rule=rule,
            inbox_message=message,
            comment_key=_bounded(message.platform_message_id),
            # Left blank on purpose: the sentinel pair exists to make a *claim*
            # safely indexable, and this row must not be indexed at all.
            post_key="",
            sender_key="",
            once_per_sender_per_post=False,
            commented_at=min(message.received_at, timezone.now()),
            outcome=outcome,
            detail=detail[:_MAX_DETAIL],
        )
    except Exception:
        logger.exception("Automation could not record outcome %s for inbox message %s", outcome, message.pk)


def purge_logs(*, older_than: timedelta) -> int:
    """Delete automation logs older than ``older_than``. Returns the row count.

    **Retention is bounded by what the guards still need, not by taste.** Every
    row in this table is also an idempotency guard, and deleting one releases
    it: the comment it claimed becomes answerable again. That is only safe once
    the comment is past its seven-day private-reply window *and* far enough past
    it that no redelivery or backfill can plausibly resurface it — hence a
    retention measured in months rather than days, and hence this function
    filtering on ``created_at`` (when we acted) rather than on ``commented_at``
    (a platform-influenced value).
    """
    cutoff = timezone.now() - older_than
    # Cross-workspace by necessity: a housekeeping worker drains the whole
    # deployment and has no session workspace to scope to. ``for_workspace`` is
    # the scoped door and this is deliberately not going through it.
    deleted, _ = AutomationLog.objects.filter(created_at__lt=cutoff).delete()
    return deleted
