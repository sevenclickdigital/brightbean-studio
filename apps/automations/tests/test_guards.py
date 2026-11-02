"""Tests for the automation idempotency guards.

This is the one module in the automations app that carries tests, because a
concurrency bug here is invisible to code review: a check-then-insert and a
unique-constraint insert read almost identically, and only one of them survives
two workers arriving at once.

The concurrency tests are built so they cannot pass by accident. Each uses a
``threading.Barrier`` so that no thread reaches the guard until every thread has
arrived — without it, a thread pool can and usually does run the callables one
after another, and a sequential run passes even against a broken
check-then-insert implementation. They also assert on the *count of winners*
rather than only on the row count, so an implementation that refuses everybody
fails too.

Threads need ``transaction=True`` (a real TransactionTestCase, not the default
wrapping transaction) because each thread uses its own connection and must see
what the others committed. That mode truncates tables on teardown, so every
connection has to be released first — see ``close_thread_connections``.
"""

import threading
from datetime import timedelta

import pytest
from django.db import connection, connections
from django.utils import timezone

from apps.automations.guards import (
    PRIVATE_REPLY_WINDOW,
    _bounded,
    _sentinel_keys,
    claim_private_reply,
    claim_public_reply,
    claimed_comment,
    hourly_count,
    may_claim_comment,
    may_private_reply,
    private_reply_deadline,
    record_comment,
)
from apps.automations.models import AutomationLog, AutomationRule
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def close_thread_connections():
    """Release every connection after the test body, before fixture teardown.

    Threaded tests leave the main thread holding a snapshot taken before the
    workers committed, and the workers' own connections may linger in the
    pool. Django's ``transaction=True`` teardown then TRUNCATEs every table,
    which needs an AccessExclusiveLock and blocks on those connections —
    Postgres reports the result as a deadlock, which surfaces as a teardown
    error on an otherwise passing test. Closing here is ordered strictly
    between the test body and the flush.
    """
    yield
    for conn in connections.all():
        conn.close()


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(name="Automations WS", organization=organization)


@pytest.fixture
def account(db, workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="ig-1",
        account_name="Studio IG",
        oauth_access_token="tok",
    )


@pytest.fixture
def rule(db, workspace, account):
    return AutomationRule.objects.create(
        workspace=workspace,
        social_account=account,
        name="Send the guide",
        is_active=True,
        include_keywords=["guide"],
        public_reply_texts=["On its way!"],
    )


def _make_log(rule, account, workspace, **overrides):
    """An AutomationLog written directly, bypassing the guards under test."""
    fields = {
        "workspace": workspace,
        "social_account": account,
        "rule": rule,
        "comment_key": "direct-1",
        "post_key": "post-1",
        "sender_key": "sender-1",
        "once_per_sender_per_post": False,
        "commented_at": timezone.now(),
    }
    fields.update(overrides)
    return AutomationLog.objects.create(**fields)


def _run_concurrently(fn, count=2):
    """Run ``fn`` in ``count`` threads that are forced to overlap.

    The barrier is what makes this a real race: every thread blocks until the
    last one arrives, so they enter ``fn`` together rather than in sequence.
    Returns the list of results in thread-start order.
    """
    barrier = threading.Barrier(count)
    results: list = [None] * count

    def worker(index):
        try:
            barrier.wait(timeout=10)
            results[index] = fn(index)
        finally:
            # Each thread opens its OWN connection on first query, held in a
            # thread-local the main thread's ``connections.all()`` cannot
            # reach — so it has to be closed here, inside the thread, on every
            # path including a barrier timeout. Django's ``transaction=True``
            # teardown TRUNCATEs every table and needs an AccessExclusiveLock,
            # which a surviving connection blocks; Postgres then reports a
            # deadlock and an otherwise-passing test errors in teardown.
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "worker thread did not finish; the race never resolved"
    # Drop the main thread's pre-race snapshot so the assertions below read
    # what the workers actually committed. Teardown ordering is handled by the
    # close_thread_connections fixture.
    connection.close()
    return results


# --- _bounded ---------------------------------------------------------------


def test_bounded_passes_short_values_through():
    assert _bounded("17895695668004550") == "17895695668004550"


def test_bounded_hashes_long_values_to_a_prefixed_fixed_width_key():
    key = _bounded("x" * 300)
    assert len(key) <= 200
    assert key.startswith("sha256:")


def test_bounded_does_not_collide_on_a_shared_200_char_prefix():
    """The anti-collision test — the whole reason hashing replaced truncation.

    Truncating at 200 would make these one key, and the second comment would be
    silently dropped as "already handled".
    """
    shared = "a" * 200
    first = _bounded(shared + "first-comment-id")
    second = _bounded(shared + "second-comment-id")

    assert first != second
    assert first.startswith("sha256:")
    assert second.startswith("sha256:")


@pytest.mark.parametrize("value", [None, 12345, b"bytes", ""])
def test_bounded_returns_empty_for_missing_or_non_string(value):
    assert _bounded(value) == ""


# --- _sentinel_keys ---------------------------------------------------------


def test_sentinel_keys_passes_complete_pairs_through():
    assert _sentinel_keys("c-1", "post-1", "sender-1") == ("post-1", "sender-1")


@pytest.mark.parametrize(
    ("post_key", "sender_key"),
    [("", "sender-1"), ("post-1", ""), ("", "")],
)
def test_sentinel_keys_derives_a_unique_pair_when_either_half_is_missing(post_key, sender_key):
    first = _sentinel_keys("comment-a", post_key, sender_key)
    second = _sentinel_keys("comment-b", post_key, sender_key)

    assert first != second, "two different comments must not share a sentinel"
    assert all(len(part) <= 200 for part in first + second)


# --- record_comment ---------------------------------------------------------


@pytest.mark.django_db
def test_record_comment_claims_once_and_refuses_the_redelivery(rule, account, workspace):
    now = timezone.now()

    first = record_comment(
        social_account=account,
        rule=rule,
        comment_id="comment-1",
        post_id="post-1",
        sender_id="sender-1",
        commented_at=now,
        now=now,
    )
    second = record_comment(
        social_account=account,
        rule=rule,
        comment_id="comment-1",
        post_id="post-1",
        sender_id="sender-1",
        commented_at=now,
        now=now,
    )

    assert first is not None
    assert second is None, "a webhook redelivery must not produce a second claim"
    assert AutomationLog.objects.filter(social_account=account).count() == 1


@pytest.mark.django_db
def test_record_comment_derives_workspace_from_the_account(rule, account, workspace):
    row = record_comment(
        social_account=account,
        rule=rule,
        comment_id="comment-ws",
        post_id="post-1",
        sender_id="sender-1",
        commented_at=timezone.now(),
    )

    assert row is not None
    assert row.workspace_id == workspace.pk


@pytest.mark.django_db
def test_record_comment_clamps_a_future_timestamp_to_now(rule, account, workspace):
    """The clock is ours: a comment dated next week must not buy extra window."""
    now = timezone.now()

    row = record_comment(
        social_account=account,
        rule=rule,
        comment_id="comment-future",
        post_id="post-1",
        sender_id="sender-1",
        commented_at=now + timedelta(days=5),
        now=now,
    )

    assert row is not None
    assert row.commented_at == now
    assert private_reply_deadline(row) == now + PRIVATE_REPLY_WINDOW


@pytest.mark.django_db
def test_record_comment_keeps_a_past_timestamp(rule, account, workspace):
    now = timezone.now()
    earlier = now - timedelta(hours=3)

    row = record_comment(
        social_account=account,
        rule=rule,
        comment_id="comment-past",
        post_id="post-1",
        sender_id="sender-1",
        commented_at=earlier,
        now=now,
    )

    assert row is not None
    assert row.commented_at == earlier


@pytest.mark.django_db
def test_once_per_sender_per_post_blocks_a_second_comment_from_the_same_sender(rule, account, workspace):
    now = timezone.now()

    first = record_comment(
        social_account=account,
        rule=rule,
        comment_id="comment-1",
        post_id="post-1",
        sender_id="sender-1",
        commented_at=now,
        once_per_sender_per_post=True,
        now=now,
    )
    second = record_comment(
        social_account=account,
        rule=rule,
        comment_id="comment-2",  # a DIFFERENT comment
        post_id="post-1",
        sender_id="sender-1",
        commented_at=now,
        once_per_sender_per_post=True,
        now=now,
    )

    assert first is not None
    assert second is None, "one reply per person per post"


@pytest.mark.django_db
def test_once_per_sender_per_post_disabled_allows_the_second_comment(rule, account, workspace):
    now = timezone.now()

    first = record_comment(
        social_account=account,
        rule=rule,
        comment_id="comment-1",
        post_id="post-1",
        sender_id="sender-1",
        commented_at=now,
        once_per_sender_per_post=False,
        now=now,
    )
    second = record_comment(
        social_account=account,
        rule=rule,
        comment_id="comment-2",
        post_id="post-1",
        sender_id="sender-1",
        commented_at=now,
        once_per_sender_per_post=False,
        now=now,
    )

    assert first is not None
    assert second is not None, "with the flag off the partial index must not apply"
    assert AutomationLog.objects.filter(social_account=account).count() == 2


@pytest.mark.django_db
def test_missing_post_and_sender_do_not_collide(rule, account, workspace):
    """The sentinel test.

    Without ``_sentinel_keys`` both rows would be written as ``("", "")`` and
    the second would be rejected by the partial unique index — switching the
    automation off for every comment whose post or author Meta did not report.
    """
    now = timezone.now()

    first = record_comment(
        social_account=account,
        rule=rule,
        comment_id="comment-1",
        post_id="",
        sender_id="",
        commented_at=now,
        once_per_sender_per_post=True,
        now=now,
    )
    second = record_comment(
        social_account=account,
        rule=rule,
        comment_id="comment-2",
        post_id="",
        sender_id="",
        commented_at=now,
        once_per_sender_per_post=True,
        now=now,
    )

    assert first is not None
    assert second is not None, "a missing post id must not become a shared lock"
    assert AutomationLog.objects.filter(social_account=account).count() == 2
    # The sentinel still enters the index, so the row collides with itself only.
    assert first.post_key != second.post_key
    assert first.sender_key != second.sender_key


@pytest.mark.django_db
def test_record_comment_leaves_the_caller_transaction_usable(rule, account, workspace):
    """The IntegrityError must be absorbed by the inner atomic block.

    If ``record_comment`` caught the error without its own savepoint, the
    surrounding transaction would be poisoned and this follow-up query would
    raise TransactionManagementError instead of answering.
    """
    now = timezone.now()
    record_comment(
        social_account=account,
        rule=rule,
        comment_id="comment-1",
        post_id="post-1",
        sender_id="sender-1",
        commented_at=now,
        now=now,
    )

    assert (
        record_comment(
            social_account=account,
            rule=rule,
            comment_id="comment-1",
            post_id="post-1",
            sender_id="sender-1",
            commented_at=now,
            now=now,
        )
        is None
    )
    # The real assertion: the connection still works after the caught error.
    assert AutomationLog.objects.filter(social_account=account).count() == 1


@pytest.mark.django_db(transaction=True)
def test_record_comment_is_safe_under_real_concurrency(rule, account, workspace, close_thread_connections):
    """Two threads, one barrier, one row.

    This is the test a check-then-insert implementation fails: both threads
    would read "no row" and both would insert.
    """
    now = timezone.now()

    def claim(_index):
        return record_comment(
            social_account=account,
            rule=rule,
            comment_id="comment-race",
            post_id="post-1",
            sender_id="sender-1",
            commented_at=now,
            now=now,
        )

    results = _run_concurrently(claim, count=2)

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"exactly one claim must win, got {len(winners)}"
    assert AutomationLog.objects.filter(social_account=account, comment_key="comment-race").count() == 1


# --- claimed_comment --------------------------------------------------------


@pytest.mark.django_db
def test_claimed_comment_finds_the_row_record_comment_wrote(rule, account, workspace):
    row = record_comment(
        social_account=account,
        rule=rule,
        comment_id="comment-1",
        post_id="post-1",
        sender_id="sender-1",
        commented_at=timezone.now(),
    )

    assert claimed_comment(account, "comment-1") == row


@pytest.mark.django_db
def test_claimed_comment_matches_long_ids_through_the_same_hashing(rule, account, workspace):
    """Both sides must hash, or the lookup searches for a value never written."""
    long_id = "z" * 300
    row = record_comment(
        social_account=account,
        rule=rule,
        comment_id=long_id,
        post_id="post-1",
        sender_id="sender-1",
        commented_at=timezone.now(),
    )

    assert row is not None
    assert claimed_comment(account, long_id) == row


@pytest.mark.django_db
def test_claimed_comment_distinguishes_the_two_refusals(rule, account, workspace):
    """``record_comment`` answers None twice; ``claimed_comment`` says which."""
    now = timezone.now()
    record_comment(
        social_account=account,
        rule=rule,
        comment_id="comment-1",
        post_id="post-1",
        sender_id="sender-1",
        commented_at=now,
        once_per_sender_per_post=True,
        now=now,
    )

    # Refusal 1: this exact comment is already claimed -> a row exists.
    assert (
        record_comment(
            social_account=account,
            rule=rule,
            comment_id="comment-1",
            post_id="post-1",
            sender_id="sender-1",
            commented_at=now,
            now=now,
        )
        is None
    )
    assert claimed_comment(account, "comment-1") is not None

    # Refusal 2: a DIFFERENT comment blocked by once-per-sender-per-post ->
    # no row for it at all.
    assert (
        record_comment(
            social_account=account,
            rule=rule,
            comment_id="comment-2",
            post_id="post-1",
            sender_id="sender-1",
            commented_at=now,
            once_per_sender_per_post=True,
            now=now,
        )
        is None
    )
    assert claimed_comment(account, "comment-2") is None


@pytest.mark.django_db
def test_claimed_comment_is_scoped_to_the_account(rule, account, workspace):
    record_comment(
        social_account=account,
        rule=rule,
        comment_id="comment-1",
        post_id="post-1",
        sender_id="sender-1",
        commented_at=timezone.now(),
    )
    other = SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="fb-1",
        account_name="Other",
        oauth_access_token="tok",
    )

    assert claimed_comment(other, "comment-1") is None


# --- claim_public_reply / claim_private_reply -------------------------------


@pytest.mark.django_db
def test_claim_public_reply_succeeds_once(rule, account, workspace):
    row = _make_log(rule, account, workspace)

    assert claim_public_reply(row) is True
    assert claim_public_reply(row) is False, "a re-run must not post a second comment"

    row.refresh_from_db()
    assert row.public_reply_sent_at is not None


@pytest.mark.django_db
def test_claim_public_reply_updates_the_in_memory_row(rule, account, workspace):
    row = _make_log(rule, account, workspace)
    now = timezone.now()

    assert claim_public_reply(row, now=now) is True
    assert row.public_reply_sent_at == now


@pytest.mark.django_db
def test_claim_private_reply_succeeds_once(rule, account, workspace):
    row = _make_log(rule, account, workspace)

    assert claim_private_reply(row) is True
    assert claim_private_reply(row) is False, "a re-run must not send a second DM"

    row.refresh_from_db()
    assert row.private_reply_sent_at is not None


@pytest.mark.django_db
def test_the_two_claims_are_independent(rule, account, workspace):
    """A run that posted the comment but not the DM must finish only the DM."""
    row = _make_log(rule, account, workspace)

    assert claim_public_reply(row) is True
    assert claim_private_reply(row) is True


@pytest.mark.django_db(transaction=True)
def test_claim_public_reply_is_safe_under_real_concurrency(rule, account, workspace, close_thread_connections):
    row = _make_log(rule, account, workspace)

    results = _run_concurrently(lambda _i: claim_public_reply(AutomationLog.objects.get(pk=row.pk)), count=2)

    assert sum(1 for r in results if r) == 1, f"exactly one winner expected, got {results}"


@pytest.mark.django_db(transaction=True)
def test_claim_private_reply_is_safe_under_real_concurrency(rule, account, workspace, close_thread_connections):
    row = _make_log(rule, account, workspace)

    results = _run_concurrently(lambda _i: claim_private_reply(AutomationLog.objects.get(pk=row.pk)), count=2)

    assert sum(1 for r in results if r) == 1, f"exactly one winner expected, got {results}"


# --- windows ----------------------------------------------------------------


@pytest.mark.django_db
def test_private_reply_window_is_seven_days_not_twenty_four_hours():
    """Guards against a regression to the DM↔DM figure, which would discard
    six days of reach on every comment older than a day."""
    dm_window = timedelta(hours=24)
    assert PRIVATE_REPLY_WINDOW.days == 7
    assert dm_window < PRIVATE_REPLY_WINDOW


def test_may_claim_comment_accepts_a_one_day_old_comment():
    now = timezone.now()
    assert may_claim_comment(now - timedelta(days=1), now=now) is True


def test_may_claim_comment_refuses_an_eight_day_old_comment():
    """Claiming spends the once-per-sender-per-post guard, so a comment the
    platform will refuse must never be allowed to spend it."""
    now = timezone.now()
    assert may_claim_comment(now - timedelta(days=8), now=now) is False


def test_may_claim_comment_clamps_a_forged_future_date():
    """A forged date may move a comment out of the window, never further in."""
    now = timezone.now()
    assert may_claim_comment(now + timedelta(days=30), now=now) is True


@pytest.mark.django_db
def test_may_private_reply_inside_the_window(rule, account, workspace):
    now = timezone.now()
    row = _make_log(rule, account, workspace, commented_at=now - timedelta(days=2))

    assert may_private_reply(row, now=now) is True


@pytest.mark.django_db
def test_may_private_reply_outside_the_window(rule, account, workspace):
    now = timezone.now()
    row = _make_log(rule, account, workspace, commented_at=now - timedelta(days=8))

    assert may_private_reply(row, now=now) is False


@pytest.mark.django_db
def test_may_private_reply_is_false_once_already_sent(rule, account, workspace):
    now = timezone.now()
    row = _make_log(
        rule,
        account,
        workspace,
        commented_at=now - timedelta(hours=1),
        private_reply_sent_at=now,
    )

    assert may_private_reply(row, now=now) is False


# --- hourly_count -----------------------------------------------------------


@pytest.mark.django_db
def test_hourly_count_ignores_rows_that_never_sent_anything(rule, account, workspace):
    """A ``skipped_limit`` row must not consume quota.

    Otherwise the ceiling is self-perpetuating: each skip row keeps the count at
    the limit, and a throttle that fired once would never unfire.
    """
    now = timezone.now()
    _make_log(
        rule,
        account,
        workspace,
        comment_key="skipped-1",
        outcome=AutomationLog.Outcome.SKIPPED_LIMIT,
    )
    _make_log(rule, account, workspace, comment_key="no-outcome-1")

    assert hourly_count(rule, now=now) == 0


@pytest.mark.django_db
def test_hourly_count_counts_public_and_private_sends(rule, account, workspace):
    now = timezone.now()
    _make_log(rule, account, workspace, comment_key="public-1", public_reply_sent_at=now)
    _make_log(rule, account, workspace, comment_key="private-1", private_reply_sent_at=now)

    assert hourly_count(rule, now=now) == 2


@pytest.mark.django_db
def test_hourly_count_counts_a_row_that_sent_both_halves_once(rule, account, workspace):
    """One comment is one reply's worth of budget, not two."""
    now = timezone.now()
    _make_log(
        rule,
        account,
        workspace,
        comment_key="both-1",
        public_reply_sent_at=now,
        private_reply_sent_at=now,
    )

    assert hourly_count(rule, now=now) == 1


@pytest.mark.django_db
def test_hourly_count_is_scoped_to_the_rule(rule, account, workspace):
    now = timezone.now()
    other_rule = AutomationRule.objects.create(
        workspace=workspace,
        social_account=account,
        name="Other rule",
        public_reply_texts=["hi"],
    )
    _make_log(rule, account, workspace, comment_key="mine-1", public_reply_sent_at=now)
    _make_log(other_rule, account, workspace, comment_key="theirs-1", public_reply_sent_at=now)

    assert hourly_count(rule, now=now) == 1
    assert hourly_count(other_rule, now=now) == 1


@pytest.mark.django_db
def test_hourly_count_window_is_rolling(rule, account, workspace):
    """Rolling, not clock-aligned: no minute at which the budget resets wholesale."""
    now = timezone.now()
    fresh = _make_log(rule, account, workspace, comment_key="fresh-1", public_reply_sent_at=now)
    stale = _make_log(rule, account, workspace, comment_key="stale-1", public_reply_sent_at=now)

    # created_at is auto_now_add, so it has to be pushed back after the fact.
    AutomationLog.objects.filter(pk=stale.pk).update(created_at=now - timedelta(hours=2))
    AutomationLog.objects.filter(pk=fresh.pk).update(created_at=now - timedelta(minutes=30))

    assert hourly_count(rule, now=now) == 1
