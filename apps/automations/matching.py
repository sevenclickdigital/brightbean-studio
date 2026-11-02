"""Which automation rule, if any, an inbound comment fires — priority order, first match wins.

Matching runs in priority order (int, lower first) and **the first match wins
per comment**. Two things carry that sentence, and neither is local to this
module: :attr:`apps.automations.models.AutomationRule.Meta.ordering` makes
"lower first" a *total* order rather than a partial one, and :func:`match` stops
at the first matcher that says yes rather than collecting candidates and
choosing afterwards.

**This module performs no writes. None of it may.** A matcher runs once per
candidate in priority order, so a matcher that took a claim would leave that
claim behind for a rule that then went on to *lose* the match — the comment
would be marked handled by a rule that never spoke. Claims, dedupe and rate
limits belong to :mod:`apps.automations.guards`, taken by the action layer
*after* a winner is known. That split is why this module does not import
``guards`` at all.

Ported from BrightBean Chat, ``apps/flows/triggers/matching.py``.
Copyright (c) BrightBean (github.com/brightbeanxyz/brightbean-chat).
Licensed under AGPL-3.0. Source commit: 7bf959b.

Modifications from the Chat original:

* **Reads** :class:`~apps.inbox.models.InboxMessage` **directly, not a**
  ``NormalizedEvent``. Chat routes every platform through a
  ``NormalizedEvent``/``EventPayload`` pair because it fans many chat platforms
  into one flow engine. The Studio has no such layer: the webhook receivers in
  :mod:`apps.inbox.webhooks` already store Meta's payload partially normalised
  on ``InboxMessage.extra``, and the row is the durable record either way. So
  :class:`MatchContext` is built from the row. This is deliberately simpler —
  there is no second representation to keep in sync, and the thing the dry-run
  replays is the same object the live path matched.
* :func:`matches_history`, for the rule-test dry run. Chat has no equivalent in
  this shape; the idea is its ``apps/inbox/rules.py::RuleInput.from_message`` —
  one matcher, two doors.
* The candidate gate is **"this rule has no reply text configured"** rather than
  Chat's ``Exists(published-flow-version)``. Same purpose, different shape of
  unrunnable row — see :func:`candidates`.
"""

import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db.models import Q
from django.utils import timezone

from apps.automations import keywords as keyword_matching
from apps.automations.models import AutomationRule

__all__ = [
    "COMMENT_PLATFORMS",
    "MatchContext",
    "Matcher",
    "candidates",
    "eligible_rules",
    "match",
    "matches_history",
    "register_matcher",
    "registered_matchers",
]

logger = logging.getLogger(__name__)

#: Platforms whose comment webhooks the Studio actually ingests.
#:
#: Derived from the receivers, not from the platform choices: ``facebook_webhook``
#: registers ``platforms=["facebook", "instagram"]`` and
#: ``instagram_login_webhook`` registers ``platforms=["instagram_login"]``
#: (:mod:`apps.inbox.webhooks`), and both dispatch into
#: ``_handle_facebook_change``, which is the only path that produces a
#: ``message_type="comment"`` row from a change event. YouTube reaches
#: ``_create_if_new`` with ``MessageType.COMMENT`` too, but through
#: PubSubHubbub, carrying an Atom entry id and a video title rather than a
#: comment body or a commenter — there is nothing for a keyword rule to match
#: and no comment edge to reply on, so it is not a comment platform *for
#: automation purposes*. Listing it here would make every account-less rule
#: claim YouTube video-publish notifications.
COMMENT_PLATFORMS = frozenset({"facebook", "instagram", "instagram_login"})


@dataclass(frozen=True)
class MatchContext:
    """Everything a matcher may read — deliberately not the raw Meta payload.

    A matcher that reached into ``message.extra`` for itself would be a platform
    branch sitting inside :mod:`apps.automations`, which is the thing this class
    exists to prevent: Facebook puts the post id in a flat ``post_id`` and
    Instagram nests it under ``media.id``, and that difference is already
    reconciled once, in :mod:`apps.inbox.webhooks`. What is on this dataclass is
    normalised, bounded, and the same shape whichever receiver delivered it.

    Frozen because a matcher receives it and must not be able to change what the
    *next* matcher sees.
    """

    message: Any
    social_account: Any
    text: str
    post_id: str
    parent_id: str
    sender_id: str
    now: datetime

    @classmethod
    def from_inbox_message(cls, message: Any, *, now: datetime | None = None) -> "MatchContext":
        """Build the context for a stored inbox row.

        ``social_account`` is read off the row rather than passed in, so there is
        no way to match a comment against the wrong account's rules by supplying
        a mismatched pair.
        """
        extra = message.extra or {}
        return cls(
            message=message,
            social_account=message.social_account,
            # Normalised here, once. ``matches_any`` normalises again internally
            # and that is idempotent; doing it at construction means every
            # matcher and every caller reads the same bounded, casefolded text
            # rather than each deciding for itself.
            text=keyword_matching.normalise(message.body),
            post_id=_related_post_id(extra),
            parent_id=_extra_value(extra, "parent_id"),
            # The webhook writes the platform user id into ``sender_handle``
            # ("Platform user ID as fallback handle", ``webhooks._create_if_new``).
            # It is not the @handle and must not be shown as one; it is here
            # because the guards key per-sender dedupe off this identity.
            sender_id=(message.sender_handle or "").strip(),
            now=now or timezone.now(),
        )

    @property
    def is_top_level_comment(self) -> bool:
        """A comment with no parent. **Absent means top level, not unknown.**

        That reading is only safe because :mod:`apps.inbox.webhooks` already
        *sanitises* ``parent_id`` before storing it: both
        ``_facebook_comment_extra`` and ``_instagram_comment_extra`` drop the key
        when it names the post (or the comment itself) instead of a parent
        comment, because Facebook reports the post id there for every top-level
        comment. So by the time a row reaches here, a present ``parent_id`` is a
        real parent comment and an absent one is a genuine top-level comment —
        there is no third "the platform told us something useless" state left to
        distinguish.
        """
        return not self.parent_id

    @property
    def platform(self) -> str:
        return self.social_account.platform


Matcher = Callable[[AutomationRule, MatchContext], bool]

_MATCHERS: dict[str, Matcher] = {}


def register_matcher(trigger_type: str, matcher: Matcher, *, replace: bool = False) -> Matcher:
    """Teach the matcher how one trigger type decides. Duplicates raise.

    The registry is kept even though v1 ships exactly one trigger type, and that
    is not speculative generality — it is what keeps the v2 types (DM, mention,
    AI) out of *this* file: they arrive by calling this from their own owning
    module, not by someone editing the dispatch here and risking the comment
    path on the way past. It also gives the absence a shape: a type whose
    platform signal has not landed yet can register a matcher that always
    declines, so it shows up in :func:`registered_matchers` as
    present-but-inert, which is a thing you can see and test, rather than as a
    missing key, which is a thing you discover in production.
    """
    if trigger_type not in AutomationRule.TriggerType.values:
        raise ValueError(f"{trigger_type!r} is not an automation trigger type.")
    if trigger_type in _MATCHERS and not replace:
        raise ValueError(f"{trigger_type!r} already has a matcher; pass replace=True if that is deliberate.")
    _MATCHERS[trigger_type] = matcher
    return matcher


def registered_matchers() -> tuple[str, ...]:
    """Trigger types that have a matcher, sorted."""
    return tuple(sorted(_MATCHERS))


def candidates(context: MatchContext) -> Any:
    """Every rule that could fire for this comment, in match order. One query.

    The ``can_act`` half of the filter is **not an optimisation**, and this is
    the Studio's version of the note on Chat's ``Exists(published)``: a rule with
    nothing to say would otherwise *win* the match and then swallow the comment,
    because first-match-wins means the rule that should have matched next never
    runs. The customer gets silence, and nothing anywhere reports an error. A row
    that cannot act must not be a candidate at all.

    "Cannot act" here means neither reply channel is both enabled and stocked
    with text. Expressed in SQL as far as SQL can take it — ``enabled AND texts
    <> '[]'`` per channel — and finished in Python by
    :func:`_rule_can_act`, called from :func:`eligible_rules`. The split is not
    laziness about JSON operators; it is that the two checks are not the same
    check. ``<> '[]'`` can only see whether the *array* is empty, and the rows
    this gate exists to catch include ``[""]``, ``["   "]``, ``[None]`` and
    anything else a hand-edited or imported rule can put in a JSONField — all of
    which are non-empty arrays holding no sendable text. Doing only the SQL half
    would let exactly the failure described above through. Doing only the Python
    half would load every disabled-reply rule in the workspace to throw it away.
    So: SQL discards the cheap bulk, Python decides.
    """
    has_public = Q(public_reply_enabled=True) & ~Q(public_reply_texts=[])
    has_private = Q(private_reply_enabled=True) & ~Q(private_reply_texts=[])
    return (
        AutomationRule.objects.for_workspace(context.social_account.workspace_id)
        .filter(trigger_type=AutomationRule.TriggerType.COMMENT, is_active=True)
        # NULL means "every account of a compatible platform", not "unassigned"
        # — see ``AutomationRule``. The platform half of that cannot be
        # expressed here; ``eligible_rules`` adds it.
        .filter(Q(social_account=context.social_account) | Q(social_account__isnull=True))
        .filter(has_public | has_private)
    )


def eligible_rules(context: MatchContext) -> Iterator[AutomationRule]:
    """Candidates this account can actually fire, in match order.

    :func:`candidates` answers the database's half of the question; this adds the
    two parts it cannot express.

    The first is the platform gate. ``social_account=NULL`` means "all accounts
    of a *compatible* platform", and compatible is a set
    (:data:`COMMENT_PLATFORMS`), not a column — there is nothing for the query to
    join against. Without this gate an account-less rule would also claim the
    comment-typed rows the YouTube receiver writes.

    The second is the rest of the "has something to say" gate, for the reason
    given in :func:`candidates`.

    Both halves live in one generator because :func:`match` and
    :func:`matches_history` need exactly this list, and a second copy of the
    platform gate would be a second place to fix the day the comment platforms
    change.
    """
    platform = context.platform
    for rule in candidates(context):
        if not _rule_can_act(rule):
            continue
        if rule.social_account_id is not None:
            # Already constrained to this account by the query.
            yield rule
            continue
        if platform in COMMENT_PLATFORMS:
            yield rule


def match(context: MatchContext) -> AutomationRule | None:
    """The first rule that fires for this comment, or ``None``.

    Stops at the first match rather than collecting and choosing afterwards —
    "first match wins" is the behaviour, and a function that gathered every
    match would make the tiebreak implicit in whatever the caller then did with
    the list.
    """
    for rule in eligible_rules(context):
        matcher = _MATCHERS.get(rule.trigger_type)
        if matcher is None:
            continue
        try:
            fired = matcher(rule, context)
        except Exception:
            # A malformed rule must not take the whole comment down: the next
            # rule in priority order may well be the one that should fire, and a
            # raise here would lose a real customer question to somebody's typo
            # in a JSON field. Nothing platform-supplied reaches the log line —
            # no comment text, no handle, no post id (log injection); the rule pk
            # and the trigger type are both ours.
            logger.exception("Automation matcher for %s failed on rule %s", rule.trigger_type, rule.pk)
            continue
        if fired:
            return rule
    return None


def matches_history(rule: AutomationRule, messages: Iterable[Any]) -> list:
    """Which of these stored messages this one rule would match. For the dry run.

    Deliberately **one rule, not the competition**: the rule-test view asks "what
    would *this* rule pick up", and folding priority in would answer a different
    question — a message another rule outranks is still a message this rule
    matched, and hiding it would read as a broken keyword list.

    It routes through the very same :func:`_match_comment` the live path uses, by
    way of the same :class:`MatchContext` construction. That is the whole point
    of the function existing here rather than in the view: a dry run that
    reimplemented the predicate would agree with production right up until one
    of the two was changed.

    The platform and "can act" gates are intentionally *not* applied. They
    decide whether a rule may be a candidate at all, which is a question about
    configuration rather than about this message; a preview that silently
    returned nothing because the author has not written the reply text yet —
    which is the state every rule starts in, since rules are created inactive and
    half-configured — would look like the keywords were wrong.
    """
    matcher = _MATCHERS.get(rule.trigger_type)
    if matcher is None:
        return []
    matched = []
    for message in messages:
        context = MatchContext.from_inbox_message(message)
        try:
            fired = matcher(rule, context)
        except Exception:
            logger.exception("Automation matcher for %s failed on rule %s", rule.trigger_type, rule.pk)
            continue
        if fired:
            matched.append(message)
    return matched


# ---------------------------------------------------------------------------
# The matchers that ship with v1
# ---------------------------------------------------------------------------


def _match_comment(rule: AutomationRule, context: MatchContext) -> bool:
    """Post scope, the top-level rule, then the two keyword lists.

    The order is load-bearing in one place: **exclude is checked before
    include**, so exclude wins. A comment matching both lists does not fire.
    That is the only arrangement that makes exclude usable at all — the point of
    "price" plus "not free" is to carve an exception out of a broad include, and
    an include-first reading would make the exception unreachable.

    No side effects, per the module docstring. In particular the
    once-per-sender-per-post guard is not here: it is a write, and this function
    runs for rules that go on to lose.
    """
    if rule.post_scope == "specific":
        # An empty ``post_ids`` therefore matches nothing, which is correct: the
        # author narrowed the rule to a list and then listed nothing. The
        # ``not context.post_id`` half matters independently — a comment we could
        # not attribute to a post must not satisfy a rule that names posts.
        post_ids = rule.post_ids or ()
        if not context.post_id or context.post_id not in post_ids:
            return False
    if rule.top_level_only and not context.is_top_level_comment:
        return False

    exclude = _keyword_entries(rule.exclude_keywords)
    if exclude and keyword_matching.matches_any(context.text, exclude):
        return False
    include = _keyword_entries(rule.include_keywords)
    if include:
        return keyword_matching.matches_any(context.text, include)
    # No include list means "every comment on this scope". A catch-all rule is a
    # real and useful configuration (reply to everything on one post), so this
    # is an allow and not a refusal.
    return True


register_matcher(AutomationRule.TriggerType.COMMENT, _match_comment)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _keyword_entries(values: Any) -> tuple[dict[str, Any], ...]:
    """Coerce a rule's keyword list into the ``{"text", "mode"}`` entries ``matches_any`` reads.

    Tolerant of both shapes on purpose. The Studio's rule form writes full dicts,
    so dicts pass straight through untouched. Bare strings are also accepted and
    read as ``contains`` — which is both the Chat original's behaviour (its
    comment trigger stored naked strings, hence its ``_plain_keywords``) and the
    forgiving reading for a rule that was edited by hand in the admin, imported
    from a Chat export, or written by a script. ``contains`` is the right default
    for a string with no stated mode: it is the broadest non-exact mode, so the
    failure direction is "fires when you expected it to" rather than a keyword
    that silently never matches.

    Anything else in the list — a number, a nested list, ``None`` — is dropped
    rather than raised on. ``matches_any`` already skips non-dicts, so this is
    belt and braces, but it keeps "one bad entry" from being indistinguishable
    from "the whole list is bad".
    """
    if not isinstance(values, list | tuple):
        return ()
    entries: list[dict[str, Any]] = []
    for value in values:
        if isinstance(value, dict):
            entries.append(value)
        elif isinstance(value, str):
            entries.append({"text": value, "mode": "contains"})
    return tuple(entries)


def _rule_can_act(rule: AutomationRule) -> bool:
    """Whether this rule has at least one enabled reply channel holding real text.

    The Python half of the gate :func:`candidates` documents. ``_has_text``
    rather than a truth test on the list, because the rows that get past SQL are
    exactly the non-empty lists with nothing sendable in them.
    """
    return (rule.public_reply_enabled and _has_text(rule.public_reply_texts)) or (
        rule.private_reply_enabled and _has_text(rule.private_reply_texts)
    )


def _has_text(values: Any) -> bool:
    """At least one non-blank string in this JSON list.

    Whitespace is not text: a reply body of ``"   "`` would post an empty comment
    or be rejected by Meta, and either way the rule cannot do the thing it won
    the match to do.
    """
    if not isinstance(values, list | tuple):
        return False
    return any(isinstance(value, str) and value.strip() for value in values)


def _extra_value(extra: dict | None, key: str) -> str:
    """One key of ``InboxMessage.extra``, as a trimmed string, or empty.

    Platform payloads put integers where ids belong often enough that reading
    ``extra`` without coercing is a latent bug: a ``post_id`` arriving as an int
    would compare unequal to every string in ``rule.post_ids`` and the rule would
    simply never fire, with nothing to see.
    """
    value = (extra or {}).get(key)
    if value is None:
        return ""
    return str(value).strip()


def _related_post_id(extra: dict | None) -> str:
    """Which post this comment was left on, read **exactly** the way the inbox reads it.

    This defers to :func:`apps.inbox.tasks._related_post_key`, which is the one
    definition of that question in the codebase: ``stored_post_id`` first, raw
    ``post_id`` as the fallback for providers that do not strip the page prefix.
    Reusing it rather than re-deriving it is the whole point — two divergent
    readings of "which post is this" is precisely how a matcher and a guard end
    up disagreeing, and the disagreement would show up as a rule scoped to a
    specific post that fires for a comment the guard then files under a different
    post.

    Imported inside the function, not at module scope, for two reasons.
    ``apps.inbox.tasks`` is the polling engine: it pulls in ``background_task``,
    the notification engine and every provider, which is a great deal of import
    weight for one dict lookup on the webhook's hot path. And the action layer
    will run automations off inbound inbox messages, so inbox importing
    automations is the expected direction — a module-scope import back the other
    way would make that a cycle later rather than now. The same deferred import
    of the same function, for the same reason, is already how
    ``apps.inbox.webhooks._create_if_new`` calls it.
    """
    from apps.inbox.tasks import _related_post_key

    return _related_post_key(extra).strip()
