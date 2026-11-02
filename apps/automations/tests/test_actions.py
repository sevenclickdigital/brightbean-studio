"""Tests for the automation execution layer.

**Nothing here touches the network.** Both send paths are mocked at the
narrowest useful seam: the public reply at ``actions.send_reply`` (so
``inbox.services``' own routing is exercised in its own suite, not re-asserted
here) and the DM at ``actions._dispatch_private_reply`` (the single function
that holds the provider call, split out of ``_send_private_half`` precisely so a
test can replace the call and nothing else).

The tests that matter most are the ones about *ordering*, because an ordering
bug here is invisible to code review and expensive in production:

* ``test_a_late_comment_does_not_spend_its_claim`` is the step-4-before-step-6
  rule. An implementation that claimed first and checked the window afterwards
  passes every other test in this file.
* ``test_running_twice_posts_one_public_reply`` is end-to-end idempotency
  against webhook redelivery.
* The two independence tests assert that each half's failure leaves the other
  half's outcome untouched — the thing the module docstring exists to protect.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.automations import actions, services
from apps.automations.models import AutomationLog, AutomationRule
from apps.inbox.models import InboxMessage
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

OWN_ID = "ig-owner-1"
COMMENTER_ID = "1122334455"


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(name="Automations WS", organization=organization)


@pytest.fixture
def account(db, workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id=OWN_ID,
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
        include_keywords=[{"text": "guide", "mode": "contains"}],
        public_reply_enabled=True,
        public_reply_texts=["On its way, {sender_name}!"],
        private_reply_enabled=False,
    )


def make_comment(account, *, body="send me the guide", author_id=COMMENTER_ID, received_at=None, platform_id="c-1"):
    """A stored comment shaped the way the Instagram webhook stores one."""
    return InboxMessage.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform_message_id=platform_id,
        message_type=InboxMessage.MessageType.COMMENT,
        sender_name="Dana",
        sender_handle=author_id,
        body=body,
        extra={
            "id": platform_id,
            "from": {"id": author_id, "username": "dana"},
            "media": {"id": "media-1"},
            "post_id": "media-1",
            "stored_post_id": "media-1",
            "source": "webhook",
        },
        received_at=received_at or timezone.now(),
    )


# ---------------------------------------------------------------------------
# pick_text
# ---------------------------------------------------------------------------


def test_pick_text_static_takes_the_first():
    assert actions.pick_text(["first", "second"], AutomationRule.ReplyMode.STATIC) == "first"


def test_pick_text_random_picks_from_the_list():
    texts = ["a", "b", "c"]
    for _ in range(20):
        assert actions.pick_text(texts, AutomationRule.ReplyMode.RANDOM) in texts


def test_pick_text_filters_blanks_before_picking():
    # The filter has to run *before* the mode, or a random pick lands on "" for
    # one comment in three and posts an empty reply.
    assert actions.pick_text(["", "   ", "real"], AutomationRule.ReplyMode.RANDOM) == "real"
    assert actions.pick_text(["", "  ", None], AutomationRule.ReplyMode.STATIC) == ""
    assert actions.pick_text([], AutomationRule.ReplyMode.STATIC) == ""
    assert actions.pick_text("not a list", AutomationRule.ReplyMode.STATIC) == ""


def test_pick_text_treats_ai_as_no_text():
    assert actions.pick_text(["anything"], AutomationRule.ReplyMode.AI) == ""


def test_a_rule_whose_texts_are_all_blank_sends_nothing(db, account, rule):
    rule.public_reply_texts = ["", "   "]
    rule.save(update_fields=["public_reply_texts"])
    message = make_comment(account)

    with patch("apps.inbox.services.send_reply") as send:
        actions.run_for_message(str(message.id))

    assert send.call_count == 0
    # A rule with nothing sendable is not even a match candidate
    # (``matching.candidates``), so no claim row is written either.
    assert not AutomationLog.objects.filter(inbox_message=message).exists()


def test_render_text_substitutes_the_saved_reply_variables(db, account):
    message = make_comment(account)
    rendered = actions.render_text("Hi {sender_name} from {account_name}{post_url}", message)
    assert rendered == "Hi Dana from Studio IG"
    # An unknown placeholder is left standing rather than raising, matching
    # ``SavedReply.render``.
    assert actions.render_text("{nope}", message) == "{nope}"


# ---------------------------------------------------------------------------
# Step 2 — own comments
# ---------------------------------------------------------------------------


def test_own_comment_is_skipped(db, account, rule):
    message = make_comment(account, author_id=OWN_ID, body="send me the guide")

    with patch("apps.inbox.services.send_reply") as send:
        actions.run_for_message(str(message.id))

    assert send.call_count == 0
    log = AutomationLog.objects.get(inbox_message=message)
    assert log.outcome == AutomationLog.Outcome.SKIPPED_OWN
    # A skip row must stay out of the partial unique index, or it would hold
    # this sender's once-per-post lock forever.
    assert log.once_per_sender_per_post is False
    assert log.public_reply_sent_at is None


def test_own_comment_matched_against_webhook_target_id(db, account, rule):
    account.webhook_target_id = "page-77"
    account.save(update_fields=["webhook_target_id"])
    message = make_comment(account, author_id="page-77")

    with patch("apps.inbox.services.send_reply") as send:
        actions.run_for_message(str(message.id))

    assert send.call_count == 0
    assert AutomationLog.objects.get(inbox_message=message).outcome == AutomationLog.Outcome.SKIPPED_OWN


# ---------------------------------------------------------------------------
# Step 3 — matching
# ---------------------------------------------------------------------------


def test_no_rule_matches_sends_nothing_and_writes_no_row(db, account, rule):
    message = make_comment(account, body="just saying hello")

    with patch("apps.inbox.services.send_reply") as send:
        actions.run_for_message(str(message.id))

    assert send.call_count == 0
    # Deliberately no ``no_match`` row: one row per unmatched comment would mean
    # tens of thousands of rows for one viral post.
    assert not AutomationLog.objects.filter(inbox_message=message).exists()


def test_a_missing_message_is_not_an_error(db):
    import uuid

    # The rolled-back-transaction case: the task names a message that never
    # committed. It must return quietly.
    actions.run_for_message(str(uuid.uuid4()))
    assert AutomationLog.objects.count() == 0


# ---------------------------------------------------------------------------
# Step 7 — the public reply
# ---------------------------------------------------------------------------


def test_a_matching_rule_posts_one_public_reply(db, account, rule):
    message = make_comment(account)

    with patch("apps.inbox.services.send_reply") as send:
        actions.run_for_message(str(message.id))

    assert send.call_count == 1
    assert send.call_args.kwargs["message"] == message
    assert send.call_args.kwargs["body"] == "On its way, Dana!"
    assert send.call_args.kwargs["author"] is None

    log = AutomationLog.objects.get(inbox_message=message)
    assert log.outcome == AutomationLog.Outcome.SENT
    assert log.public_reply_sent_at is not None
    assert log.private_reply_sent_at is None
    assert log.rule_id == rule.id


def test_running_twice_posts_one_public_reply(db, account, rule):
    """End-to-end idempotency: Meta redelivers, and the handler is re-run."""
    message = make_comment(account)

    with patch("apps.inbox.services.send_reply") as send:
        actions.run_for_message(str(message.id))
        actions.run_for_message(str(message.id))

    assert send.call_count == 1
    assert AutomationLog.objects.filter(inbox_message=message).count() == 1


# ---------------------------------------------------------------------------
# Step 4 — the seven-day window, before the claim
# ---------------------------------------------------------------------------


def test_a_late_comment_does_not_spend_its_claim(db, account, rule):
    """An eight-day-old comment is refused *before* ``record_comment``.

    The assertion that catches a reordering is the last one: a claim row has
    ``once_per_sender_per_post=True`` and a blank-but-indexed key pair, and its
    existence would permanently lock this sender out of this post in exchange
    for a reply Meta was always going to refuse.
    """
    message = make_comment(account, received_at=timezone.now() - timedelta(days=8))

    with patch("apps.inbox.services.send_reply") as send:
        actions.run_for_message(str(message.id))

    assert send.call_count == 0
    log = AutomationLog.objects.get(inbox_message=message)
    assert log.outcome == AutomationLog.Outcome.SKIPPED_WINDOW
    assert log.public_reply_sent_at is None
    assert log.private_reply_sent_at is None
    # No claim was spent: the row is not in the once-per-sender-per-post index.
    assert log.once_per_sender_per_post is False
    assert not AutomationLog.objects.filter(once_per_sender_per_post=True).exists()


def test_a_later_comment_from_the_same_sender_is_still_answerable(db, account, rule):
    """The point of refusing before the claim: the lock was never taken."""
    late = make_comment(account, received_at=timezone.now() - timedelta(days=8), platform_id="c-late")
    fresh = make_comment(account, platform_id="c-fresh")

    with patch("apps.inbox.services.send_reply") as send:
        actions.run_for_message(str(late.id))
        actions.run_for_message(str(fresh.id))

    assert send.call_count == 1
    assert AutomationLog.objects.get(inbox_message=fresh).outcome == AutomationLog.Outcome.SENT


# ---------------------------------------------------------------------------
# Step 5 — the hourly ceiling, before the claim
# ---------------------------------------------------------------------------


def test_the_hourly_ceiling_skips_without_claiming(db, account, rule):
    message = make_comment(account)

    with (
        patch("apps.automations.guards.hourly_count", return_value=rule.max_per_hour),
        patch("apps.inbox.services.send_reply") as send,
    ):
        actions.run_for_message(str(message.id))

    assert send.call_count == 0
    log = AutomationLog.objects.get(inbox_message=message)
    assert log.outcome == AutomationLog.Outcome.SKIPPED_LIMIT
    assert log.once_per_sender_per_post is False
    assert log.public_reply_sent_at is None


# ---------------------------------------------------------------------------
# Step 6 — once per sender per post
# ---------------------------------------------------------------------------


def test_once_per_sender_per_post_declines_a_second_comment(db, account, rule):
    first = make_comment(account, platform_id="c-1")
    second = make_comment(account, platform_id="c-2")

    with patch("apps.inbox.services.send_reply") as send:
        actions.run_for_message(str(first.id))
        actions.run_for_message(str(second.id))

    assert send.call_count == 1
    assert not AutomationLog.objects.filter(inbox_message=second).exists()


# ---------------------------------------------------------------------------
# Steps 7 and 8 are independent
# ---------------------------------------------------------------------------


@pytest.fixture
def both_halves_rule(rule):
    rule.private_reply_enabled = True
    rule.private_reply_texts = ["Here is the guide, {sender_name}."]
    rule.save(update_fields=["private_reply_enabled", "private_reply_texts"])
    return rule


def test_both_halves_go_out_for_a_fully_configured_rule(db, account, both_halves_rule):
    message = make_comment(account)

    with (
        patch("apps.inbox.services.send_reply") as send,
        patch.object(actions, "_dispatch_private_reply", return_value="mid-1") as dm,
    ):
        actions.run_for_message(str(message.id))

    assert send.call_count == 1
    assert dm.call_count == 1
    assert dm.call_args.args[1] == "Here is the guide, Dana."
    log = AutomationLog.objects.get(inbox_message=message)
    assert log.outcome == AutomationLog.Outcome.SENT
    assert log.public_reply_sent_at is not None
    assert log.private_reply_sent_at is not None


def test_a_failed_public_reply_does_not_block_the_dm(db, account, both_halves_rule):
    message = make_comment(account)

    with (
        patch("apps.inbox.services.send_reply", side_effect=RuntimeError("platform refused")) as send,
        patch.object(actions, "_dispatch_private_reply", return_value="mid-1") as dm,
    ):
        actions.run_for_message(str(message.id))

    assert send.call_count == 1
    assert dm.call_count == 1
    log = AutomationLog.objects.get(inbox_message=message)
    # The DM is what the rule's author configured, so the row is a success.
    assert log.outcome == AutomationLog.Outcome.SENT
    assert log.private_reply_sent_at is not None
    assert "Public reply failed" in log.detail


def test_a_failed_dm_does_not_undo_the_public_reply(db, account, both_halves_rule):
    message = make_comment(account)

    with (
        patch("apps.inbox.services.send_reply") as send,
        patch.object(actions, "_dispatch_private_reply", side_effect=RuntimeError("send api down")) as dm,
    ):
        actions.run_for_message(str(message.id))

    assert send.call_count == 1
    assert dm.call_count == 1
    log = AutomationLog.objects.get(inbox_message=message)
    assert log.outcome == AutomationLog.Outcome.SENT
    # The public comment is published and permanent; its claim must stand.
    assert log.public_reply_sent_at is not None
    assert "Private reply failed" in log.detail


def test_both_halves_failing_is_recorded_as_failed(db, account, both_halves_rule):
    message = make_comment(account)

    with (
        patch("apps.inbox.services.send_reply", side_effect=RuntimeError("no")),
        patch.object(actions, "_dispatch_private_reply", side_effect=RuntimeError("no")),
    ):
        actions.run_for_message(str(message.id))

    assert AutomationLog.objects.get(inbox_message=message).outcome == AutomationLog.Outcome.FAILED


def test_the_dm_is_addressed_to_the_comment_not_the_person(db, account, both_halves_rule):
    """The private reply must carry ``private_reply_to_comment_id``.

    Without it the Send API addresses a PSID/IGSID the commenter may not have,
    and the dispatch either fails or DMs the wrong person. The stored ``extra``
    must also come back unchanged — the key is added to a copy, or every later
    manual DM on this thread would become a private reply.
    """
    message = make_comment(account)
    captured = {}

    def fake_reply_to_message(**kwargs):
        captured.update(kwargs)

        class _Result:
            platform_message_id = "mid-1"

        return _Result()

    class _Provider:
        reply_to_message = staticmethod(fake_reply_to_message)

    with (
        patch("apps.inbox.services.send_reply"),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
        patch("providers.get_provider", return_value=_Provider()),
    ):
        actions.run_for_message(str(message.id))

    assert captured["extra"]["private_reply_to_comment_id"] == "c-1"
    assert captured["human_agent"] is False
    message.refresh_from_db()
    assert "private_reply_to_comment_id" not in message.extra


def test_a_non_meta_platform_skips_the_dm_but_still_replies_publicly(db, workspace, both_halves_rule):
    linkedin = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin",
        account_platform_id="li-1",
        account_name="Studio LI",
        oauth_access_token="tok",
    )
    # The rule must cover this account; an account-scoped rule would not match.
    both_halves_rule.social_account = None
    both_halves_rule.save(update_fields=["social_account"])
    # ``matching.COMMENT_PLATFORMS`` gates account-less rules by platform, so a
    # LinkedIn comment reaches the action layer only through an explicit rule.
    both_halves_rule.social_account = linkedin
    both_halves_rule.save(update_fields=["social_account"])
    message = make_comment(linkedin, platform_id="li-c-1")

    with (
        patch("apps.inbox.services.send_reply") as send,
        patch.object(actions, "_dispatch_private_reply") as dm,
    ):
        actions.run_for_message(str(message.id))

    assert send.call_count == 1
    assert dm.call_count == 0
    log = AutomationLog.objects.get(inbox_message=message)
    assert log.outcome == AutomationLog.Outcome.SENT
    assert log.private_reply_sent_at is None
    assert "not available on linkedin" in log.detail


def test_an_ai_mode_reply_is_inert_and_says_so(db, account, rule):
    rule.public_reply_mode = AutomationRule.ReplyMode.AI
    rule.save(update_fields=["public_reply_mode"])
    message = make_comment(account)

    with patch("apps.inbox.services.send_reply") as send:
        actions.run_for_message(str(message.id))

    assert send.call_count == 0
    log = AutomationLog.objects.get(inbox_message=message)
    assert log.outcome == AutomationLog.Outcome.FAILED
    assert "v2" in log.detail


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def test_on_inbox_message_created_enqueues_a_comment(db, account):
    message = make_comment(account)

    with patch("apps.automations.tasks.run_automation_for_message") as task:
        services.on_inbox_message_created(message)

    assert task.call_count == 1
    # The pk as a string, never the instance: ``@background`` serialises to JSON.
    assert task.call_args.args == (str(message.id),)


def test_on_inbox_message_created_ignores_a_dm(db, account):
    dm = InboxMessage.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform_message_id="m-1",
        message_type=InboxMessage.MessageType.DM,
        sender_name="Dana",
        body="hi",
        received_at=timezone.now(),
    )

    with patch("apps.automations.tasks.run_automation_for_message") as task:
        services.on_inbox_message_created(dm)

    assert task.call_count == 0


def test_on_inbox_message_created_never_raises(db, account):
    """The property the inbox depends on.

    Its callers are a live webhook handler and the body of an atomic block; an
    exception escaping here loses the customer's message entirely.
    """
    message = make_comment(account)

    with patch("apps.automations.tasks.run_automation_for_message", side_effect=RuntimeError("queue is down")):
        services.on_inbox_message_created(message)  # must not raise


def test_run_for_message_never_raises(db, account, rule):
    """The worker's contract: a raising task is retried 25 times and deleted."""
    message = make_comment(account)

    with patch("apps.automations.matching.match", side_effect=RuntimeError("boom")):
        actions.run_for_message(str(message.id))  # must not raise


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_purge_logs_drops_only_rows_past_retention(db, account, rule):
    old = make_comment(account, platform_id="c-old")
    fresh = make_comment(account, platform_id="c-fresh")
    stale_row = AutomationLog.objects.create(
        workspace=account.workspace,
        social_account=account,
        rule=rule,
        inbox_message=old,
        comment_key="c-old",
        once_per_sender_per_post=False,
        commented_at=timezone.now(),
    )
    kept_row = AutomationLog.objects.create(
        workspace=account.workspace,
        social_account=account,
        rule=rule,
        inbox_message=fresh,
        comment_key="c-fresh",
        once_per_sender_per_post=False,
        commented_at=timezone.now(),
    )
    # ``created_at`` is auto_now_add, so it has to be moved with an UPDATE.
    AutomationLog.objects.filter(pk=stale_row.pk).update(created_at=timezone.now() - timedelta(days=100))

    assert actions.purge_logs(older_than=timedelta(days=90)) == 1
    assert list(AutomationLog.objects.values_list("pk", flat=True)) == [kept_row.pk]
