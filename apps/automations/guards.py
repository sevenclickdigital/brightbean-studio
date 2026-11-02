"""The durable idempotency guards for comment automations.

Ported from brightbean-chat ``apps/flows/triggers/guards.py``.
Copyright (c) BrightBean — https://github.com/brightbeanxyz/brightbean-chat
Licensed under AGPL-3.0, ported at commit 7bf959b.

Modifications relative to the Chat original:

* :func:`_sentinel_keys` is new. Chat's once-per-commenter-per-post constraint
  had the same latent hole — an empty ``post_id`` or ``commenter_ref`` makes
  every such row collide on one shared key — and nothing there closed it.
* :func:`hourly_count` is new. The Studio's rules carry a ``max_per_hour``
  ceiling that Chat has no equivalent for.
* :func:`claim_private_reply` replaces Chat's ``mark_private_reply_sent``. Chat
  writes the private-reply timestamp unconditionally because its flow engine has
  already serialized the send; the Studio's worker has not, so both halves of
  the reply are compare-and-set here. See that function's docstring.
* ``_bounded`` is implemented locally instead of delegating to Chat's
  ``messaging_facade.bounded_identifier``, and carries a discriminating prefix
  on the hashed branch — see below.
* Chat's ``claim_default_reply`` / ``DEFAULT_REPLY_INTERVAL`` are not ported:
  the Studio's DM frequency guard lives in ``apps/inbox/reply_policy.py``.

Everything here answers a "may I do this exactly once" question, and every one
of those questions is asked concurrently. Meta redelivers webhooks, and the
worker re-runs handlers that committed without being marked done, so each
question is answered by the *database* — by a unique constraint or by a
conditional ``UPDATE`` — never by a read followed by a write. A ``SELECT`` then
``INSERT`` lets both halves of a race through, reliably rather than rarely, and
the visible result is a second public comment on a customer's post.
"""

import hashlib
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.automations.models import AutomationLog, AutomationRule

__all__ = [
    "PRIVATE_REPLY_WINDOW",
    "claim_private_reply",
    "claim_public_reply",
    "claimed_comment",
    "hourly_count",
    "hourly_window_start",
    "may_claim_comment",
    "may_private_reply",
    "private_reply_deadline",
    "record_comment",
]

#: A comment's private reply has **seven days**, not twenty-four hours.
#:
#: The 24-hour figure is the DM↔DM standard messaging window, and the Studio
#: already enforces that one in ``apps/inbox/reply_policy.py``. The private
#: reply *to a comment* is a different endpoint with its own, longer allowance.
#: Reusing 24h here would make us refuse sends that Meta would have accepted,
#: throwing away six days of reach on every comment older than a day — a
#: failure that looks like nothing at all, because a reply that is never
#: attempted produces no error.
PRIVATE_REPLY_WINDOW = timedelta(days=7)

#: The rolling window ``max_per_hour`` is measured over.
_HOURLY_WINDOW = timedelta(hours=1)

#: ``AutomationLog.comment_key`` / ``post_key`` / ``sender_key`` are
#: ``CharField(max_length=200)``, and they are two thirds of two unique
#: constraints. Platform ids are not ours and are not length-bounded, so they
#: are bounded by *hashing* — see :func:`_bounded`.
_MAX_PLATFORM_ID = 200

#: Marks a value as "this is a digest, not an id". See :func:`_bounded`.
_HASH_PREFIX = "sha256:"


def _bounded(value: str) -> str:
    """Bound key material to the column width by hashing, never by truncating.

    A short value passes through verbatim; anything over
    :data:`_MAX_PLATFORM_ID` is replaced by a fixed-width SHA-256 digest.

    **Why not truncation.** This value lands in
    ``UniqueConstraint(social_account, comment_key)``. Two distinct comment ids
    that share a 200-character prefix would truncate to the *same* key, and the
    second comment would be rejected by the constraint and silently classified
    as "already handled" — a customer's question that nobody ever answers, with
    no error raised anywhere, no failed send to retry, and nothing in the logs
    that looks wrong. A digest has no prefix to collide on.

    **Why the prefix.** Mixing two namespaces in one column (raw ids under the
    limit, digests over it) creates a theoretical path for a short raw id to
    equal some long id's digest, which would make two unrelated comments share
    a key. ``"sha256:"`` closes it: a Meta object id is a decimal string or an
    opaque base64-ish token, and never begins with that literal, so the raw and
    hashed namespaces cannot overlap. The prefix is counted in the budget, so
    the result stays inside the column either way.

    ``None`` and non-strings answer ``""``. An absent identifier is a real
    case — see :func:`_sentinel_keys` for what happens next — and it must not
    be allowed to become the string ``"None"``, which would then be a key that
    every id-less comment shares.
    """
    if not isinstance(value, str) or not value:
        return ""
    if len(value) <= _MAX_PLATFORM_ID:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    # 7 + 64 = 71 characters, comfortably inside the 200-character column, and
    # fixed-width regardless of how long the input was.
    return f"{_HASH_PREFIX}{digest}"


def _sentinel_keys(comment_key: str, post_key: str, sender_key: str) -> tuple[str, str]:
    """Make ``(post_key, sender_key)`` safe to put in a partial unique index.

    Returns the pair to store, substituting a per-comment sentinel for either
    one when it is missing.

    **The trap this closes.** ``UniqueConstraint(social_account, post_key,
    sender_key)`` guards "one reply per person per post". Postgres treats NULLs
    as distinct, so a nullable column would opt out of the index — but these
    columns are ``blank=True, default=""``, not nullable, and ``""`` is a
    perfectly ordinary value that compares equal to itself. The platform does
    not always tell us both halves: an Instagram caption mention arrives with a
    ``media_id`` and no post id, and some payloads carry no author reference at
    all. Every one of those rows would be written as ``("", "")`` and would
    therefore collide with *every other such row on the account*. The second
    comment from anybody, on any post, on any rule, would hit the constraint and
    be discarded as a duplicate. One missing field in a webhook payload would
    silently switch the automation off for that whole account.

    **The fix, and why it is shaped this way.** When either half is missing we
    derive the stored value from ``comment_key``, which the idempotency
    constraint already guarantees is unique per account. The row still enters
    the index — so the insert keeps behaving identically, and we do not need a
    second code path — but it can now only ever collide with *itself*. That is
    the deliberate trade: for a comment whose post or author we do not know, the
    once-per-sender-per-post lock becomes **inert** rather than **wrong**. An
    inert lock means at worst one extra reply to a person we could not identify
    as a repeat; a wrong lock means dropping real comments in silence. The first
    is recoverable and visible, the second is neither.

    Both keys are replaced when either is missing. Keeping the known half would
    put a real post id into the index paired with a sentinel sender, which reads
    as if we had identified an author we did not; the sentinel pair keeps the
    index entry honestly self-referential. ``_nopost:`` and ``_nosender:``
    prefixes start with an underscore, which no platform id does, so a sentinel
    can never be mistaken for — or collide with — a genuine identifier.
    """
    if post_key and sender_key:
        return post_key, sender_key
    # Truncate the comment key so prefix + key stays inside the 200-char
    # column. _bounded already capped comment_key at 200, and the sentinel
    # prefixes cost 10 characters; hashing instead would be pointless here,
    # since this value's only job is to differ from other rows' sentinels.
    stem = comment_key[: _MAX_PLATFORM_ID - 10]
    return f"_nopost:{stem}", f"_nosend:{stem}"


def record_comment(
    *,
    social_account,
    rule,
    comment_id: str,
    post_id: str,
    sender_id: str,
    commented_at: datetime,
    once_per_sender_per_post: bool = True,
    inbox_message=None,
    workspace=None,
    now: datetime | None = None,
) -> AutomationLog | None:
    """Claim this comment, or ``None`` when something already holds the guard.

    **The IntegrityError is the guard.** One insert is checked against both
    constraints on :class:`~apps.automations.models.AutomationLog`: the
    redelivery guard on ``(social_account, comment_key)``, and — when the rule
    asks for it — once-per-sender-per-post on ``(social_account, post_key,
    sender_key)``.

    There is deliberately **no** ``exists()`` call here. A check-then-insert
    passes on both sides of a race: two workers each read "no row", each insert,
    and the customer's post gets two identical replies. Webhook redelivery makes
    that race the normal case rather than an unlucky one. Letting the unique
    index arbitrate means the loser finds out by failing to insert, which is the
    only moment at which the answer is actually knowable.

    The insert is taken *before* sending, so losing means skipping rather than
    duplicating — the asymmetry the whole table is built around.

    Returns ``None`` for two different refusals, which the caller often needs to
    tell apart; :func:`claimed_comment` is how.
    """
    moment = now or timezone.now()
    comment_key = _bounded(comment_id)
    post_key, sender_key = _sentinel_keys(comment_key, _bounded(post_id), _bounded(sender_id))

    try:
        # Its own atomic block, not the caller's. A database error marks the
        # surrounding transaction as needing rollback, so catching IntegrityError
        # without an inner block leaves the caller holding a poisoned
        # transaction in which every later query raises TransactionManagementError
        # — the guard would "work" and then break everything after it. The
        # savepoint this opens is what makes the failure recoverable in place.
        with transaction.atomic():
            row = AutomationLog(
                workspace_id=(workspace.pk if workspace is not None else social_account.workspace_id),
                social_account=social_account,
                rule=rule,
                inbox_message=inbox_message,
                comment_key=comment_key,
                post_key=post_key,
                sender_key=sender_key,
                once_per_sender_per_post=once_per_sender_per_post,
                # The clock is ours, not the platform's. Every window in this
                # module is measured from this column, so a comment stamped next
                # week would otherwise buy itself extra days of eligibility — and
                # the timestamp comes from a payload we do not control.
                commented_at=min(commented_at, moment),
            )
            row.save()
            return row
    except IntegrityError:
        # Either this comment is already claimed, or this sender already has the
        # once-per-post lock on this post. Both mean "do not send".
        return None


def claimed_comment(social_account, comment_id: str) -> AutomationLog | None:
    """The row already claiming **this** comment on this account, if any.

    The distinction :func:`record_comment` cannot make on its own. It answers
    ``None`` to two unrelated refusals: *this* comment is already handled (the
    platform handing one of our own claims back — benign, and the matching row
    tells us what we did last time), or a *different* comment from the same
    person on the same post holds the once-per-sender-per-post lock (the guard
    doing its job, and there is no row for this comment at all). The caller's
    outcome, logging, and whether it should try to finish a half-done send all
    differ between the two.

    Bounded through the same :func:`_bounded` the insert used. Hashing on one
    side only would make this query search for a value that merely resembles
    what was written, and it would answer ``None`` for every long id — turning
    the benign case into the guarded one exactly where the hashing matters most.
    """
    return (
        AutomationLog.objects.for_workspace(social_account.workspace_id)
        .filter(social_account=social_account, comment_key=_bounded(comment_id))
        .first()
    )


def claim_public_reply(row: AutomationLog, *, now: datetime | None = None) -> bool:
    """May the public reply go out — and if so, take the guard.

    A compare-and-set, not a read followed by a write. The ``UPDATE`` takes a
    row lock for its own duration and the ``public_reply_sent_at IS NULL``
    predicate is evaluated under it, so of any number of concurrent or repeated
    runs **exactly one** sees ``updated == 1``. Every loser posts nothing. The
    caller is a queue handler that must be safe to run more than once, because
    recovery re-runs handlers that committed without being marked done.

    Taken *before* the HTTP call rather than recorded after it. The failure this
    prevents is a duplicate comment under a customer's post — public, permanent,
    and visible to their audience. A claim spent on a reply that then failed
    costs only that reply, and the private reply is a separate column and a
    separate claim, so it is unaffected.
    """
    moment = now or timezone.now()
    updated = AutomationLog.objects.filter(pk=row.pk, public_reply_sent_at__isnull=True).update(
        public_reply_sent_at=moment
    )
    if updated:
        # Keep the in-memory row consistent with the committed one, so a caller
        # that goes on to read the field does not see stale None.
        row.public_reply_sent_at = moment
    return bool(updated)


def claim_private_reply(row: AutomationLog, *, now: datetime | None = None) -> bool:
    """May the private reply (DM) go out — and if so, take the guard.

    Identical compare-and-set to :func:`claim_public_reply`, on
    ``private_reply_sent_at``.

    **A deliberate divergence from Chat.** brightbean-chat has
    ``mark_private_reply_sent``, an unconditional ``save(update_fields=...)``
    *after* the send, because there the private reply is the first message of a
    flow run and the flow engine has already serialized it — the row cannot be
    reached twice concurrently, so a plain write is sufficient. The Studio has
    no such serializer: the worker may run the same comment handler twice, and
    the public and private halves are independent calls either of which may have
    already succeeded. An unconditional write would report success to a second
    run that then sends a second DM. So both halves are claimed the same way,
    for the same reason, and the private DM — which is a direct message to a
    real person, and the thing Meta rate-limits hardest — gets the stronger
    guarantee rather than the weaker one.
    """
    moment = now or timezone.now()
    updated = AutomationLog.objects.filter(pk=row.pk, private_reply_sent_at__isnull=True).update(
        private_reply_sent_at=moment
    )
    if updated:
        row.private_reply_sent_at = moment
    return bool(updated)


def private_reply_deadline(row: AutomationLog) -> datetime:
    """When the platform stops accepting a private reply to this comment."""
    return row.commented_at + PRIVATE_REPLY_WINDOW


def may_private_reply(row: AutomationLog, *, now: datetime | None = None) -> bool:
    """Whether a private reply is still allowed and has not already been sent.

    A read-only question, and therefore *not* a claim: it answers "is this worth
    attempting", which a caller may ask while deciding or rendering. The claim
    itself is :func:`claim_private_reply`, and a ``True`` here does not reserve
    anything.
    """
    if row.private_reply_sent_at is not None:
        return False
    return (now or timezone.now()) <= private_reply_deadline(row)


def may_claim_comment(commented_at: datetime, *, now: datetime | None = None) -> bool:
    """Is this comment still inside the seven-day private-reply window?

    Checked *before* :func:`record_comment`, never after. Claiming is what
    *spends* the once-per-sender-per-post guard: a comment that surfaces eight
    days late — a backfill, a webhook retry storm, a queue that was down — would
    otherwise consume the single claim that person had on that post, in exchange
    for a reply the platform is certain to refuse. The claim is the scarce
    resource, so eligibility is decided before it is taken.

    The clock is ours on both sides: the incoming timestamp is clamped to "not
    in the future" here as well as on write, so a forged date can move a comment
    *out* of the window but never further into it.
    """
    moment = now or timezone.now()
    return min(commented_at, moment) + PRIVATE_REPLY_WINDOW >= moment


def hourly_window_start(now: datetime | None = None) -> datetime:
    """Start of the rolling hour :func:`hourly_count` measures over."""
    return (now or timezone.now()) - _HOURLY_WINDOW


def hourly_count(rule: AutomationRule, *, now: datetime | None = None) -> int:
    """How many replies this rule has actually sent in the last rolling hour.

    Feeds ``AutomationRule.max_per_hour``. Rolling rather than clock-aligned, so
    there is no minute at which a rule's whole budget becomes available again
    and thirty replies go out at once.

    **Only rows that really sent something are counted** — ones with a
    ``public_reply_sent_at`` or a ``private_reply_sent_at``. Rows exist in this
    table from *before* the send, and a ``skipped_limit`` row is a record of a
    reply that never happened. Counting those would make the ceiling
    self-perpetuating: once it is hit, every subsequent comment writes a
    skip row, each skip row keeps the count at the limit, and the rule stays
    throttled long after the real traffic subsided — a throttle that, having
    fired once, never unfires. Counting sends only means the window drains on
    its own.

    This is an advisory pre-check, not a guard. Unlike everything else in this
    module it is a plain read and two concurrent workers can both pass it at the
    ceiling; a ceiling is a cost control, and overshooting it by one costs one
    extra reply. The guarantees that must hold exactly are the unique
    constraints and the two claims, and none of them depend on this number.
    """
    # One OR'd predicate rather than two queries: a row that sent *both* halves
    # is one reply's worth of budget, not two, so it must be counted once.
    return AutomationLog.objects.filter(
        Q(public_reply_sent_at__isnull=False) | Q(private_reply_sent_at__isnull=False),
        rule=rule,
        created_at__gte=hourly_window_start(now),
    ).count()
