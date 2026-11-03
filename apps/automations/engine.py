"""The graph executor: walk a flow's nodes, sending as it goes, parking on taps.

This is what :mod:`apps.automations.actions` is not. That module runs the v1
shape — one public reply, one DM, both fired at once and then done — and it
stays, because that shape has guards and an audit trail built around it and most
flows never leave it.

This module runs the *graph*: it starts at the trigger, follows edges, and sends
one node at a time. The difference that forces a separate module is **time**. A
flow with a button is not one operation with two halves; it is a conversation
that pauses for as long as the person takes to tap, which may be never. So:

* Progress is **persisted** between steps (:class:`~apps.automations.models.FlowRun`),
  because the process that sends step 2 is a different process, minutes or days
  later, woken by a webhook rather than by the comment.
* Execution is **resumable** from a node id, because a postback carries nothing
  else.
* A run **ends** — explicitly, at a node with no outgoing edge — so a parked run
  is distinguishable from a finished one.

**The walk is bounded three ways**, and each bound exists because the graph is
author-drawn and nothing validates it as a DAG:

1. ``visited`` — a node already executed in this run is not executed again, so a
   cycle terminates instead of messaging someone forever.
2. :data:`MAX_STEPS_PER_WAKE` — one wake sends at most this many messages, so a
   long chain cannot become a flood that reads as spam and gets the account
   rate-limited.
3. The run is abandoned on the first send failure, rather than continuing. A
   flow whose step 2 failed should not deliver step 3 as though the person had
   seen step 2.

**Nothing here raises.** Every caller is a webhook handler or a queue worker,
and the same reasoning as ``services.on_inbox_message_created`` applies: losing
the customer's message is strictly worse than failing to automate a reply to it.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.automations.models import AutomationRule, FlowRun

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_STEPS_PER_WAKE",
    "POSTBACK_PREFIX",
    "parse_postback",
    "resume_from_postback",
    "start_run",
]

#: Messages one wake may send before parking. See the module docstring.
#:
#: Five is the number of consecutive messages a person can receive without it
#: reading as a flood — the reference flows in this space send two or three.
MAX_STEPS_PER_WAKE = 5

#: Marks a postback payload as ours. Meta hands the payload back verbatim, and
#: the same webhook receives payloads from any other integration the Page has
#: ever had, plus Meta's own ``GET_STARTED``. A payload that is not ours must be
#: ignored rather than parsed into a node id that happens to collide.
POSTBACK_PREFIX = "bbflow"


def build_postback(run_id: Any, node_id: str) -> str:
    """The opaque payload a postback button carries.

    ``bbflow:<run>:<node>``. The run id makes the payload single-use in
    practice — a tap on a button from a run that has been superseded resolves to
    a row that is no longer waiting, and is declined — and the node id says
    where to continue. Meta caps the payload at 1000 characters, which two UUIDs
    and a prefix are nowhere near.

    **The payload is not a secret and is not trusted.** It arrives from the
    network and names a row; :func:`resume_from_postback` re-reads that row and
    re-checks that it belongs to the account the webhook was delivered for.
    """
    return f"{POSTBACK_PREFIX}:{run_id}:{node_id}"


def parse_postback(payload: Any) -> tuple[str, str] | None:
    """Split one of our payloads into ``(run_id, node_id)``, or ``None``.

    ``None`` for anything that is not ours — another integration's payload,
    Meta's ``GET_STARTED``, a truncated string. The caller treats that as "this
    postback is not a flow step", which is the common case on a Page that has
    other automations attached.
    """
    if not isinstance(payload, str):
        return None
    parts = payload.split(":", 2)
    if len(parts) != 3 or parts[0] != POSTBACK_PREFIX:
        return None
    run_id, node_id = parts[1].strip(), parts[2].strip()
    if not run_id or not node_id:
        return None
    return run_id, node_id


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------


def _nodes_by_id(graph: Any) -> dict[str, dict]:
    """Every well-formed node, keyed by id.

    Tolerant for the reason given in ``flow._nodes``: the graph is a JSONField
    written by a browser, so a malformed entry is a front-end bug and the right
    response is to run what is readable rather than refuse the whole flow.
    """
    if not isinstance(graph, dict):
        return {}
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return {}
    return {
        node["id"]: node
        for node in nodes
        if isinstance(node, dict) and isinstance(node.get("id"), str) and isinstance(node.get("type"), str)
    }


def _next_node_ids(graph: Any, node_id: str, *, branch: str = "") -> list[str]:
    """Every node that follows ``node_id``, in drawn order. Empty at the end.

    **A node may have more than one successor, and all of them run.** That is
    what lets one trigger fan out to a public reply *and* a DM — the shape
    authors reach for first, and the one the two-channel path used to express
    by having two reply channels rather than two edges. Returning a single id
    would make the second connection a line the canvas drew and the engine
    silently ignored.

    ``branch`` selects edges by label, which is how a condition picks between
    its two outputs (``"true"`` / ``"false"``) and how a reply button picks the
    edge belonging to the button that was tapped. An unlabelled edge is the
    default: it matches when no branch was asked for, and it is also the
    fallback when a branch *was* asked for and nothing carries that label — so
    a condition whose author only wired one output still goes somewhere
    instead of silently ending the flow.
    """
    if not isinstance(graph, dict):
        return []
    edges = graph.get("edges")
    if not isinstance(edges, list):
        return []

    labelled: list[str] = []
    unlabelled: list[str] = []
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("from") != node_id:
            continue
        target = edge.get("to")
        if not isinstance(target, str) or not target:
            continue
        label = edge.get("branch") or ""
        if label:
            if branch and label == branch:
                labelled.append(target)
        else:
            unlabelled.append(target)

    if branch:
        # The labelled edges win outright when any exist; the unlabelled ones
        # are the fallback for an output the author never wired.
        return labelled or unlabelled
    # No branch asked for: only the default edges. A labelled edge belongs to a
    # specific button or condition outcome and must not fire on its own.
    return unlabelled


def _next_node_id(graph: Any, node_id: str, *, branch: str = "") -> str:
    """The first successor of ``node_id``, or ``""``.

    Kept for the callers that genuinely want one node — resuming a postback
    picks a single edge, and ``actions._first_step_id`` names where a run
    begins. The walk itself uses :func:`_next_node_ids`.
    """
    nodes = _next_node_ids(graph, node_id, branch=branch)
    return nodes[0] if nodes else ""


# ---------------------------------------------------------------------------
# Condition nodes
# ---------------------------------------------------------------------------


#: Condition fields answered by a profile lookup rather than by the comment.
#:
#: ``follows_us`` is the one this exists for: "send the link only to people who
#: follow us", and the loop that asks someone to follow before continuing. The
#: other three arrive in the same response, so offering them costs nothing
#: beyond the branch that reads them.
PROFILE_CONDITION_FIELDS = frozenset(
    {"follows_us", "we_follow_them", "is_verified", "follower_count"}
)

#: Which Graph field answers each of ours. Named separately so the editor's
#: vocabulary ("follows_us") does not have to match Meta's, and so a renamed
#: Graph field is one line to re-point.
_PROFILE_FIELD_MAP = {
    "follows_us": "is_user_follow_business",
    "we_follow_them": "is_business_follow_user",
    "is_verified": "is_verified_user",
    "follower_count": "follower_count",
}


def _profile_for(context: dict) -> dict:
    """The profile of the person this run is talking to, fetched at most once.

    **Lazy, and cached on the context.** The lookup is a Graph call, and most
    flows never ask a profile question — fetching eagerly would spend a round
    trip per run to answer nothing. Caching matters just as much: a flow with
    three follower checks must not make three calls, and must not be able to
    see a *different* answer at each one.

    ``{}`` is cached too, under a separate flag, so a refusal is not retried
    once per condition. Meta's refusals here are not transient: no consent, a
    blocked business, a platform with no such endpoint.
    """
    if context.get("_profile_loaded"):
        return context.get("_profile") or {}
    context["_profile_loaded"] = True
    context["_profile"] = {}

    run = context.get("run")
    account = getattr(run, "social_account", None)
    recipient = (getattr(run, "recipient_id", "") or "").strip()
    if account is None or not recipient:
        # Before the first tap there is no directly-addressable id, and Meta
        # would refuse anyway: consent comes from the person messaging us.
        return {}

    try:
        from apps.publisher.engine import _resolve_publish_credentials
        from providers import get_provider

        provider = get_provider(account.platform, _resolve_publish_credentials(account))
        profile = provider.fetch_user_profile(account.oauth_access_token, recipient)
    except Exception as exc:
        logger.info(
            "Flow run %s could not read the profile: error_type=%s",
            getattr(run, "pk", None),
            type(exc).__name__,
        )
        return {}

    context["_profile"] = profile if isinstance(profile, dict) else {}
    return context["_profile"]


def _evaluate_profile_condition(field: str, operator: str, value: Any, context: dict) -> bool:
    """Answer a condition about *who* is talking, rather than what they wrote.

    **An unknown answer is always false**, whichever operator was chosen —
    including the negative ones. That is deliberate and worth stating: if Meta
    will not tell us whether someone follows the account, "does not follow"
    must not come out true, because the flow's false branch is where the
    "please follow us" message lives and sending it to an existing follower is
    the one outcome this feature must avoid. A missing answer stops the branch
    instead of guessing at it.
    """
    profile = _profile_for(context)
    graph_field = _PROFILE_FIELD_MAP.get(field, field)
    if graph_field not in profile:
        return False
    actual = profile[graph_field]

    if field == "follower_count":
        try:
            count = int(actual)
            threshold = int(str(value).strip())
        except (TypeError, ValueError):
            return False
        if operator == "greater_than":
            return count > threshold
        if operator == "less_than":
            return count < threshold
        if operator == "equals":
            return count == threshold
        return False

    # The three booleans. ``is_true`` / ``is_false`` rather than a value field,
    # because "follows us: yes" needs no second input and an empty text box
    # next to it would read as something still to fill in.
    truth = bool(actual)
    if operator == "is_false":
        return not truth
    # ``is_true`` and anything unrecognised: the affirmative reading.
    return truth


def evaluate_condition(node: dict, context: dict) -> bool:
    """Whether a condition node takes its ``true`` branch.

    The comparison is deliberately small: one field, one operator, one value.
    A flow author is not writing an expression language, and every operator
    added here is one more thing that can silently mean something other than
    what it looks like.

    ``field`` names something about the person or what they wrote
    (``comment_text``, ``sender_name``); unknown fields read as empty, so a
    typo makes the condition false rather than raising mid-conversation.
    Matching is casefolded because the author typed a word, not a pattern.
    """
    data = node.get("data")
    if not isinstance(data, dict):
        return False

    field = data.get("field") if isinstance(data.get("field"), str) else "comment_text"
    operator = data.get("operator") if isinstance(data.get("operator"), str) else "contains"

    # Profile fields are answered separately: they are booleans and numbers
    # rather than text, and reaching one costs a Graph call that the text
    # fields do not.
    if field in PROFILE_CONDITION_FIELDS:
        return _evaluate_profile_condition(field, operator, data.get("value"), context)

    actual = str(context.get(field, "") or "").casefold()
    expected = data.get("value")
    expected = expected.casefold().strip() if isinstance(expected, str) else ""

    if operator == "contains":
        return bool(expected) and expected in actual
    if operator == "not_contains":
        # True on an empty needle: "does not contain nothing" is vacuously the
        # author's intent to let everything through, and the alternative — a
        # half-filled condition silently blocking every person — is worse.
        return not expected or expected not in actual
    if operator == "equals":
        return actual == expected
    if operator == "is_empty":
        return not actual.strip()
    if operator == "is_not_empty":
        return bool(actual.strip())
    # An unrecognised operator takes the false branch rather than the true one:
    # the false branch is the one that does less.
    return False


# ---------------------------------------------------------------------------
# Sending one node
# ---------------------------------------------------------------------------


def _render(text: Any, context: dict) -> str:
    """Substitute ``{sender_name}`` / ``{account_name}`` in a node's text.

    Same literal-replacement mechanism as ``actions.render_text``, and
    deliberately the same placeholders — a member who learned them in the
    composer must be able to type them into a flow node. Reused via import so
    there is one definition, not two that drift.
    """
    from apps.automations.actions import render_text

    if not isinstance(text, str) or not text:
        return ""
    message = context.get("message")
    if message is None:
        return text
    return render_text(text, message)


def _node_buttons(node: dict, run: FlowRun, context: dict) -> list[dict]:
    """A message node's buttons, with postback payloads bound to this run.

    A ``reply`` button's payload is generated here rather than stored on the
    node, because it has to name *this run* — the same node sends a different
    payload to every person who reaches it. ``build_postback`` points it at the
    button's own node, and the resume picks up from the edge leaving that
    button.
    """
    data = node.get("data")
    if not isinstance(data, dict):
        return []
    raw = data.get("buttons")
    if not isinstance(raw, list):
        return []

    built: list[dict] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        title = _render(entry.get("title"), context)
        if not title.strip():
            continue
        kind = entry.get("type")
        if kind == "url":
            url = entry.get("url")
            if isinstance(url, str) and url.strip():
                built.append({"type": "web_url", "title": title, "url": url.strip()})
        elif kind == "reply":
            # Indexed so two reply buttons on one node resume down different
            # edges — the index is the branch label the edge carries.
            built.append(
                {
                    "type": "postback",
                    "title": title,
                    "payload": build_postback(run.pk, f"{node['id']}#{index}"),
                }
            )
    return built


def _send_node(node: dict, run: FlowRun, context: dict) -> bool:
    """Deliver one message node. Returns whether it went out.

    Routed straight at the provider rather than through ``inbox.services``, for
    the reason spelled out in ``actions._send_private_half``: that module routes
    on ``message_type`` and would turn a DM into a public comment. The dispatch
    is the same three calls it makes — resolve credentials, get provider,
    ``reply_to_message`` — with buttons riding on ``extra``.

    **Addressing changes after the first message.** The opening message of a run
    is a *private reply* to the comment, because the person has never messaged
    the account and has no PSID/IGSID to address. Every later message in the
    same run addresses the person directly, using the id their tap revealed.
    Sending the second message as another private reply would be refused —
    Meta allows one per comment.
    """
    from apps.publisher.engine import _resolve_publish_credentials
    from providers import get_provider

    account = run.social_account
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    text = _render(data.get("text"), context)
    buttons = _node_buttons(node, run, context)
    image_url = data.get("image_url") if isinstance(data.get("image_url"), str) else ""

    if not text and not buttons and not image_url:
        # An empty node is skipped rather than failed: it is a step the author
        # added and has not filled in, and the rest of the flow still works.
        return True

    extra: dict[str, Any] = {}
    if buttons:
        extra["buttons"] = buttons
    if image_url:
        extra["image_url"] = image_url

    if run.recipient_id:
        extra["sender_id"] = run.recipient_id
    elif run.comment_id:
        extra["private_reply_to_comment_id"] = run.comment_id
    else:
        logger.warning("Flow run %s has nobody to address; stopping.", run.pk)
        return False

    provider = get_provider(account.platform, _resolve_publish_credentials(account))
    provider.reply_to_message(
        access_token=account.oauth_access_token,
        message_id=run.comment_id or run.recipient_id,
        text=text,
        extra=extra,
        # Never HUMAN_AGENT — an automated message must not claim a person is
        # answering. Same reasoning as ``actions._dispatch_private_reply``.
        human_agent=False,
    )
    return True


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


def _drain(
    run: FlowRun,
    graph: Any,
    nodes: dict[str, dict],
    pending: list[str],
    visited: list[str],
    context: dict,
    sent: int,
) -> None:
    """Deliver the branches still queued when the run parked on a button.

    A flow that fans out — public reply on one branch, a DM with a button on
    the other — parks the moment the button goes out. Without this, whichever
    branch happened to be queued behind it would never be delivered at all:
    the run is waiting, and the resume picks up from the *button's* successor,
    not from a sibling that never ran.

    The parked state is already saved by the caller before this runs, so a
    crash here costs the sibling branch and not the wait. Sends are attempted
    one at a time against the same per-wake ceiling; nothing here can park
    again, because a second button would have nothing to resume it — the run
    holds one ``awaiting_node_id``, and it is already spoken for.
    """
    while pending and sent < MAX_STEPS_PER_WAKE:
        node_id = pending.pop(0)
        if not node_id or node_id in visited:
            continue
        node = nodes.get(node_id)
        if node is None:
            continue
        visited.append(node_id)
        node_type = node.get("type")

        if node_type == "condition":
            branch = "true" if evaluate_condition(node, context) else "false"
            pending.extend(_next_node_ids(graph, node_id, branch=branch))
            continue

        if node_type in {"send_dm", "reply_comment", "send_card"}:
            try:
                delivered = _send_node(node, run, context)
            except Exception as exc:
                logger.warning(
                    "Flow run %s failed to send sibling node %s error_type=%s",
                    run.pk,
                    node_id,
                    type(exc).__name__,
                )
                continue
            if not delivered:
                continue
            sent += 1
            # A second button cannot park: the run already waits on one, and
            # two parked nodes would make the next tap ambiguous. Its branch
            # simply ends here.
            if any(b.get("type") == "postback" for b in _node_buttons(node, run, context)):
                logger.info(
                    "Flow run %s has a second waiting node %s; its branch ends here.",
                    run.pk,
                    node_id,
                )
                continue
            pending.extend(_next_node_ids(graph, node_id))
            continue

        pending.extend(_next_node_ids(graph, node_id))

    run.visited_node_ids = visited
    run.save(update_fields=["visited_node_ids", "updated_at"])


def _advance(run: FlowRun, graph: Any, start_node_id: str, context: dict) -> None:
    """Walk from ``start_node_id`` until the flow waits, ends, or hits a bound.

    The loop is the whole module. Each turn: look the node up, do what its type
    says, then decide where to go — and the three bounds in the module docstring
    are what guarantee it stops.
    """
    nodes = _nodes_by_id(graph)
    visited = list(run.visited_node_ids or [])
    # A queue, not a cursor: a node may have several successors and all of them
    # run, which is what lets one trigger fan out to a public reply and a DM.
    # Breadth-first so sibling branches interleave in drawn order rather than
    # one branch delivering everything before the other starts — the author
    # drew them side by side and that is the order they read in.
    pending = [start_node_id]
    sent = 0

    while pending:
        node_id = pending.pop(0)
        if not node_id or node_id in visited:
            # Already run in this run. Silently skipped rather than logged: two
            # branches converging on one node is a normal drawing, not a cycle,
            # and it reaches here exactly the same way.
            continue
        node = nodes.get(node_id)
        if node is None:
            # The author deleted this node after the run parked on it. Ending is
            # the honest outcome: there is no step to deliver.
            logger.info("Flow run %s points at missing node %s; skipping.", run.pk, node_id)
            continue

        visited.append(node_id)
        node_type = node.get("type")

        if node_type == "condition":
            branch = "true" if evaluate_condition(node, context) else "false"
            pending.extend(_next_node_ids(graph, node_id, branch=branch))
            continue

        if node_type in {"send_dm", "reply_comment", "send_card"}:
            if sent >= MAX_STEPS_PER_WAKE:
                logger.info("Flow run %s hit the per-wake message ceiling; parking.", run.pk)
                break
            try:
                delivered = _send_node(node, run, context)
            except Exception as exc:
                # Type only, never ``str(exc)``: a provider error echoes the
                # request back, which contains author and platform text.
                logger.warning(
                    "Flow run %s failed to send node %s error_type=%s",
                    run.pk,
                    node_id,
                    type(exc).__name__,
                )
                delivered = False
            if not delivered:
                # This branch stops; the others continue. One failed send is a
                # reason not to deliver what came *after* it, not a reason to
                # abandon a sibling branch that has nothing to do with it.
                continue
            sent += 1

            # A node carrying reply buttons parks the run: the next step is the
            # person's to take. Its own successors wait for the tap, but any
            # sibling branch still queued is delivered first — those are not
            # waiting on anything.
            if any(b.get("type") == "postback" for b in _node_buttons(node, run, context)):
                run.awaiting_node_id = node_id
                run.is_waiting = True
                run.visited_node_ids = visited
                run.save(update_fields=["awaiting_node_id", "is_waiting", "visited_node_ids", "updated_at"])
                _drain(run, graph, nodes, pending, visited, context, sent)
                return

            pending.extend(_next_node_ids(graph, node_id))
            continue

        # Trigger nodes and anything unrecognised: pass straight through. An
        # unknown type is a node a newer editor drew and this deploy cannot run;
        # skipping it keeps the rest of the flow working rather than stranding
        # the person at a step that will never execute.
        pending.extend(_next_node_ids(graph, node_id))

    # Fell out of the loop: nothing left to wait for.
    run.awaiting_node_id = ""
    run.is_waiting = False
    run.visited_node_ids = visited
    run.save(update_fields=["awaiting_node_id", "is_waiting", "visited_node_ids", "updated_at"])


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _context_for(message, run=None) -> dict:
    """What a condition may read and a placeholder may substitute.

    ``run`` is carried so a profile condition can reach the account and the
    person's id — see :func:`_profile_for`. The profile itself is not fetched
    here: most flows never ask, and an eager lookup would be a Graph call per
    run spent on nothing.
    """
    return {
        "message": message,
        "run": run,
        "comment_text": getattr(message, "body", "") or "",
        "sender_name": getattr(message, "sender_name", "") or "",
    }


def start_run(message, rule: AutomationRule, graph: Any, start_node_id: str) -> FlowRun | None:
    """Begin a graph walk for one comment. Returns the run, or ``None``.

    Called by :mod:`apps.automations.actions` once a rule has won the match and
    its guards have been taken — so the once-per-sender-per-post and hourly
    limits are already spent by the time this runs, and this function adds no
    guards of its own.

    **A new run supersedes the person's previous one.** The partial unique
    index allows exactly one waiting run per person per account, so starting a
    second while the first is parked would raise. Rather than refuse the new
    comment, the old run is closed: the person has acted again, and answering
    their *current* comment matters more than keeping them parked on a button
    they did not tap.
    """
    from apps.automations.guards import _bounded

    sender_id = (getattr(message, "sender_handle", "") or "").strip()
    if not sender_id:
        logger.debug("Flow start skipped: inbox message %s has no sender.", getattr(message, "pk", None))
        return None

    sender_key = _bounded(sender_id)
    account = message.social_account

    try:
        with transaction.atomic():
            # Close the previous run first, in the same transaction that opens
            # this one, so the unique index never sees two waiting rows.
            FlowRun.objects.filter(
                social_account=account,
                sender_key=sender_key,
                is_waiting=True,
            ).update(is_waiting=False, awaiting_node_id="", updated_at=timezone.now())

            run = FlowRun.objects.create(
                workspace=message.workspace,
                social_account=account,
                rule=rule,
                sender_key=sender_key,
                comment_id=message.platform_message_id,
                # Unknown until the person replies: a private reply addresses
                # the comment, and the thread it opens is what reveals the id.
                recipient_id="",
                awaiting_node_id=start_node_id,
                is_waiting=True,
            )
    except IntegrityError:
        # Lost the race to a concurrent trigger for the same person. The winner
        # is answering them; a second run would interleave with it.
        logger.info("Flow start declined: a run already exists for this sender on account %s", account.pk)
        return None

    _advance(run, graph, start_node_id, _context_for(message, run))
    return run


def resume_from_postback(account, sender_id: str, payload: str) -> bool:
    """Continue a parked run after the person tapped a button. Returns whether it ran.

    ``False`` covers every "not ours / nothing to do" case — a payload from
    another integration, a run that has finished, a tap on a superseded run's
    button — and none of those is an error.

    **Three things are re-checked, because the payload came from the network.**
    That the payload is ours (:func:`parse_postback`), that the run it names
    belongs to *this* account (a run id is a UUID, and without this check one
    account's webhook could advance another's run), and that the run is still
    waiting at the node the payload claims. The last is what makes a tap
    single-use: tapping the same button twice resumes nothing the second time.
    """
    parsed = parse_postback(payload)
    if parsed is None:
        return False
    run_id, node_ref = parsed

    # ``node#index`` — the index selects which of the node's buttons was tapped,
    # and so which labelled edge to leave by.
    node_id, _, button_index = node_ref.partition("#")

    run = (
        FlowRun.objects.filter(pk=run_id, social_account=account, is_waiting=True)
        .select_related("social_account", "rule")
        .first()
    )
    if run is None:
        logger.debug("Postback ignored: no waiting run %s on account %s", run_id, account.pk)
        return False
    if run.awaiting_node_id != node_id:
        # A tap on an earlier message's button after the flow has moved on.
        logger.debug("Postback ignored: run %s is parked at %s, not %s", run.pk, run.awaiting_node_id, node_id)
        return False

    flow = getattr(run.rule, "post_automation", None)
    if flow is None:
        logger.info("Flow run %s has no flow to resume; closing.", run.pk)
        run.is_waiting = False
        run.awaiting_node_id = ""
        run.save(update_fields=["is_waiting", "awaiting_node_id", "updated_at"])
        return False

    # The tap is what reveals a directly-addressable id for this person. Stored
    # so every later message in the run goes to them rather than as another
    # private reply, which Meta allows only once per comment.
    if sender_id and run.recipient_id != sender_id:
        run.recipient_id = sender_id
        run.save(update_fields=["recipient_id", "updated_at"])

    next_id = _next_node_id(flow.graph, node_id, branch=button_index)
    if not next_id:
        run.is_waiting = False
        run.awaiting_node_id = ""
        run.save(update_fields=["is_waiting", "awaiting_node_id", "updated_at"])
        return False

    # **A tap starts a fresh pass, so the visited set is cleared.**
    #
    # ``visited`` bounds one uninterrupted walk: without it a cycle would
    # message somebody forever in a single wake. A tap is not that — it is a
    # deliberate act, minutes or days later, and it is what makes the "follow
    # us, then tap again" loop possible at all. Keeping the old trail would
    # let that loop run exactly once: the second tap would reach the same
    # check node, find it visited, and stop with nothing sent, which reads as
    # the button having broken.
    #
    # The loop stays bounded because each pass still needs a human tap to
    # begin, and each pass is capped by ``MAX_STEPS_PER_WAKE``.
    run.visited_node_ids = []

    # The parked node is already in ``visited``; the walk continues past it.
    # ``run`` carries the account and the recipient id a profile condition
    # needs. This is the path where such a condition actually works: the tap
    # that got us here is what gave Meta the consent to answer.
    _advance(run, flow.graph, next_id, {"message": None, "run": run, "comment_text": "", "sender_name": ""})
    return True
