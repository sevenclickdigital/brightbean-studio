"""Models for comment automations — keyword-triggered public replies and DMs.

``AutomationRule`` and ``AutomationLog`` are adapted from brightbean-chat
``apps/flows/models.py::Trigger`` and ``::HandledComment``.
Copyright (c) BrightBean — https://github.com/brightbeanxyz/brightbean-chat
Licensed under AGPL-3.0, adapted at commit 7bf959b.

The Studio's version collapses Chat's flow/trigger split into a single rule row:
there is no flow graph here, so a rule carries its own matching config *and* its
own two reply bodies. What it keeps verbatim is the part that was learned the
hard way — the deterministic ordering tiebreak, the CASCADE on a nullable
account FK, and the two durable uniqueness guards on the log table.
"""

import uuid

from django.conf import settings
from django.db import models

from apps.common.managers import WorkspaceScopedManager


class AutomationRule(models.Model):
    """One keyword rule: what to match on a comment, and what to reply with.

    Matching runs in priority order, lowest first, and **the first match wins
    per comment**. That makes ``Meta.ordering`` load-bearing rather than
    cosmetic — see the note on it below.

    ``social_account`` is nullable and null means *all accounts of a compatible
    platform*, not "unassigned". A rule with no account covers every Instagram
    and Facebook account this workspace has connected; it does not become
    orphaned.
    """

    class TriggerType(models.TextChoices):
        COMMENT = "comment", "Comment on post"

    class ReplyMode(models.TextChoices):
        STATIC = "static", "Fixed text"
        RANDOM = "random", "Random variation"
        AI = "ai", "AI (v2)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="automation_rules",
    )
    # CASCADE, not SET_NULL: NULL already means something specific here ("every
    # account of a compatible platform"), so a deleted account quietly widening
    # a rule from one account to ALL of them is the single outcome that must not
    # happen. Same reasoning as brightbean-chat ``Trigger.channel_connection``.
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="automation_rules",
    )
    name = models.CharField(max_length=200)
    trigger_type = models.CharField(
        max_length=20,
        choices=TriggerType.choices,
        default=TriggerType.COMMENT,
    )
    # Rules start disabled: a rule is created half-configured (no reply text
    # yet), and the author decides when it is allowed to speak to the public.
    is_active = models.BooleanField(default=False)
    priority = models.IntegerField(default=0)

    include_keywords = models.JSONField(default=list, blank=True)
    exclude_keywords = models.JSONField(default=list, blank=True)
    #: ``"all"`` (every post on the account) or ``"specific"`` (only ``post_ids``).
    post_scope = models.CharField(max_length=10, default="all")
    post_ids = models.JSONField(default=list, blank=True)
    top_level_only = models.BooleanField(default=False)

    public_reply_enabled = models.BooleanField(default=True)
    public_reply_mode = models.CharField(
        max_length=10,
        choices=ReplyMode.choices,
        default=ReplyMode.STATIC,
    )
    public_reply_texts = models.JSONField(default=list, blank=True)

    private_reply_enabled = models.BooleanField(default=False)
    private_reply_mode = models.CharField(
        max_length=10,
        choices=ReplyMode.choices,
        default=ReplyMode.STATIC,
    )
    private_reply_texts = models.JSONField(default=list, blank=True)

    once_per_sender_per_post = models.BooleanField(default=True)
    max_per_hour = models.PositiveIntegerField(default=30)

    # --- v2 AI fields: declared now, INERT. -------------------------------
    # Carried in the initial migration so the v2 rollout is a code deploy and
    # not a schema migration against a populated table. Nothing in the codebase
    # may read these yet, and ``ReplyMode.AI`` must not be selectable until the
    # provider plumbing lands.
    ai_provider = models.CharField(max_length=20, blank=True, default="")  # "openai" | "ollama"
    ai_model = models.CharField(max_length=100, blank=True, default="")
    ai_prompt = models.TextField(blank=True, default="")
    #: Default true so the first AI release cannot autonomously publish text
    #: nobody reviewed, whatever else the v2 config says.
    ai_draft_only = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_automation_rules",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "automation_rule"
        # LOAD-BEARING, not cosmetic. First match wins per comment, so the
        # tiebreak decides behaviour: with ``priority`` alone, two rules sitting
        # at the same number tie, and a tie means the same comment fires
        # different rules on different days depending on how Postgres chose to
        # plan the scan. ``created_at`` then ``id`` closes it, so "the one that
        # was there first" is also the stable answer. See brightbean-chat
        # ``apps/flows/models.py::Trigger.Meta``.
        ordering = ["priority", "created_at", "id"]
        indexes = [
            # The matcher's candidate query, which runs once per inbound comment
            # webhook.
            models.Index(
                fields=["workspace", "trigger_type", "is_active"],
                name="autom_rule_ws_type_active",
            ),
        ]
        constraints = [
            # Priority is an ordering key, not a score. Negative values would
            # still sort, but they make "lower wins" unreadable in the UI and
            # let a rule jump the whole list by accident.
            models.CheckConstraint(
                condition=models.Q(priority__gte=0),
                name="autom_rule_priority_non_negative",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def covers_all_accounts(self) -> bool:
        """Whether this rule applies to every account of a compatible platform."""
        return self.social_account_id is None


class AutomationLog(models.Model):
    """One comment this workspace has already acted on, plus why.

    This table is both the audit trail and the enforcement point. Two rules live
    in its constraints, and they are **different rules with different keys**:

    **Idempotency**, ``UniqueConstraint(social_account, comment_key)``: Meta
    redelivers webhooks, and the worker's handler must be safe to run twice.
    Without this constraint a redelivery is a second public comment on the
    customer's post and a second DM to the same person about the same comment.
    The insert is taken *before* sending, so losing the race means skipping,
    not duplicating.

    **Once per sender per post**, ``UniqueConstraint(social_account, post_key,
    sender_key)`` conditioned on ``once_per_sender_per_post=True``: one reply
    per person per post, regardless of how many times they comment. It is
    *partial* on the flag, and the flag is copied onto the row rather than read
    from the rule at query time — rows written while the setting was off must
    stay out of the index, and turning the setting on later must not
    retroactively lock history that was never guarded.

    **The ``*_key`` columns hold HASHED platform identifiers, not truncated
    ones.** Hashing is implemented in ``guards.py``; the model only declares the
    bounded column. The reason is this table's failure mode: two distinct
    comment ids sharing a 200-character prefix would collapse into one key, and
    the second comment would be silently discarded as "already handled" — a
    customer's question that never gets answered, with no error anywhere. A
    fixed-width hash has no prefix to collide on. See brightbean-chat
    ``apps/flows/triggers/guards.py::_bounded``.

    ``commented_at`` stores ``min(platform_timestamp, now)``. The clock is ours:
    the reply window is measured from this field, and a comment stamped next
    week would otherwise buy itself extra days of eligibility.
    """

    class Outcome(models.TextChoices):
        SENT = "sent", "Sent"
        SKIPPED_LIMIT = "skipped_limit", "Hourly limit reached"
        SKIPPED_WINDOW = "skipped_window", "Outside reply window"
        SKIPPED_OWN = "skipped_own", "Own comment"
        SKIPPED_DUPLICATE = "skipped_duplicate", "Already handled"
        NO_MATCH = "no_match", "No rule matched"
        FAILED = "failed", "Send failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="automation_logs",
    )
    # Not nullable and not SET_NULL: this FK is half of both unique keys above.
    # A NULL here would make every guard stop firing, because Postgres treats
    # NULLs as distinct — exactly the race this table exists to lose gracefully.
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="automation_logs",
    )
    # SET_NULL: the log outlives the rule. Deleting a rule must not erase the
    # evidence of what it already said in public, nor release its guards.
    rule = models.ForeignKey(
        AutomationRule,
        on_delete=models.SET_NULL,
        null=True,
        related_name="logs",
    )
    inbox_message = models.ForeignKey(
        "inbox.InboxMessage",
        on_delete=models.SET_NULL,
        null=True,
        related_name="automation_logs",
    )

    #: Hashed platform identifiers — see the class docstring. ``comment_key`` is
    #: indexed on its own because the dedupe lookup is the first thing the
    #: webhook handler asks.
    comment_key = models.CharField(max_length=200, db_index=True)
    post_key = models.CharField(max_length=200, blank=True, default="")
    sender_key = models.CharField(max_length=200, blank=True, default="")

    #: The rule's setting at the moment this row was written; it is what the
    #: partial unique index is conditioned on.
    once_per_sender_per_post = models.BooleanField(default=True)
    #: ``min(platform_timestamp, now)`` — see the class docstring.
    commented_at = models.DateTimeField()

    # Two timestamps, not one: the public comment and the DM are different calls
    # to different endpoints with different failure modes, and a re-run that
    # posted the comment but not the DM must be able to finish only the half
    # that is missing.
    public_reply_sent_at = models.DateTimeField(null=True, blank=True)
    private_reply_sent_at = models.DateTimeField(null=True, blank=True)
    #: Blank until the handler decides; a row exists from before the send, so
    #: "no outcome yet" is a real state and not an error.
    outcome = models.CharField(
        max_length=20,
        choices=Outcome.choices,
        blank=True,
        default="",
    )
    detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "automation_log"
        ordering = ["-created_at"]
        constraints = [
            # Idempotency against Meta webhook redelivery — never reply twice to
            # the same comment. See the class docstring.
            models.UniqueConstraint(
                fields=["social_account", "comment_key"],
                name="autom_log_once_per_comment",
            ),
            # One reply per person per post, partial on the per-rule flag as it
            # stood when the row was written. See the class docstring.
            models.UniqueConstraint(
                fields=["social_account", "post_key", "sender_key"],
                condition=models.Q(once_per_sender_per_post=True),
                name="autom_log_once_per_sender_post",
            ),
        ]
        indexes = [
            # The activity feed: this workspace's automation history, newest
            # first.
            models.Index(fields=["workspace", "-created_at"], name="autom_log_ws_created"),
            # Per-rule history, and the hourly-rate count the throttle takes
            # before every send.
            models.Index(fields=["rule", "-created_at"], name="autom_log_rule_created"),
        ]

    def __str__(self) -> str:
        return f"{self.get_outcome_display() or 'pending'} — comment {self.comment_key[:12]}"
