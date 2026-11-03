"""Models for the Unified Social Inbox (F-3.1)."""

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.managers import WorkspaceScopedManager


class InboxMessage(models.Model):
    api_refreshed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class MessageType(models.TextChoices):
        COMMENT = "comment", _("Comment")
        MENTION = "mention", _("Mention")
        DM = "dm", _("Direct Message")
        REVIEW = "review", _("Review")

    class Status(models.TextChoices):
        UNREAD = "unread", _("Unread")
        OPEN = "open", _("Open")
        RESOLVED = "resolved", _("Resolved")
        ARCHIVED = "archived", _("Archived")

    class Sentiment(models.TextChoices):
        POSITIVE = "positive", _("Positive")
        NEUTRAL = "neutral", _("Neutral")
        NEGATIVE = "negative", _("Negative")

    class SentimentSource(models.TextChoices):
        AUTO = "auto", _("Auto")
        MANUAL = "manual", _("Manual")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="inbox_messages",
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="inbox_messages",
    )
    platform_message_id = models.CharField(max_length=255, db_index=True)
    message_type = models.CharField(
        max_length=20,
        choices=MessageType.choices,
        default=MessageType.COMMENT,
        db_index=True,
    )
    sender_name = models.CharField(max_length=255)
    sender_handle = models.CharField(max_length=255, blank=True, default="")
    sender_avatar_url = models.URLField(max_length=500, blank=True, default="")
    body = models.TextField(blank=True, default="")
    sentiment = models.CharField(
        max_length=10,
        choices=Sentiment.choices,
        default=Sentiment.NEUTRAL,
        db_index=True,
    )
    sentiment_source = models.CharField(
        max_length=10,
        choices=SentimentSource.choices,
        default=SentimentSource.AUTO,
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.UNREAD,
        db_index=True,
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_inbox_messages",
    )
    parent_message = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="thread_replies",
    )
    related_post = models.ForeignKey(
        "composer.PlatformPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inbox_messages",
    )
    extra = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "inbox_message"
        ordering = ["-received_at"]
        unique_together = [("social_account", "platform_message_id")]
        indexes = [
            models.Index(
                fields=["workspace", "status", "-received_at"],
                name="inbox_msg_ws_status_recv",
            ),
            models.Index(
                fields=["workspace", "assigned_to", "status"],
                name="inbox_msg_ws_assign_status",
            ),
            models.Index(
                fields=["workspace", "social_account", "-received_at"],
                name="inbox_msg_ws_account_recv",
            ),
        ]

    def __str__(self):
        return f"{self.get_message_type_display()} from {self.sender_name}"

    @property
    def platform(self):
        return self.social_account.platform

    @property
    def attachments(self):
        from providers.meta_messaging import normalize_attachments

        extra = self.extra or {}
        return normalize_attachments(extra.get("attachments", (extra.get("message") or {}).get("attachments", [])))


class InboxReply(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SENT = "sent", _("Sent")
        FAILED = "failed", _("Failed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inbox_message = models.ForeignKey(
        InboxMessage,
        on_delete=models.CASCADE,
        related_name="replies",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="inbox_replies",
    )
    body = models.TextField()
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    platform_reply_id = models.CharField(max_length=255, blank=True, default="")
    send_error = models.TextField(blank=True, default="")
    # ``sent_at`` is null until the reply is actually delivered to the platform;
    # a row now exists in ``draft``/``failed`` states before any send happens.
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inbox_reply"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.get_status_display()} reply by {self.author} ({self.created_at:%Y-%m-%d %H:%M})"


class InternalNote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inbox_message = models.ForeignKey(
        InboxMessage,
        on_delete=models.CASCADE,
        related_name="internal_notes",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="inbox_notes",
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inbox_internal_note"
        ordering = ["created_at"]

    def __str__(self):
        return f"Note by {self.author} on {self.created_at:%Y-%m-%d %H:%M}"


class SavedReply(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="saved_replies",
    )
    title = models.CharField(max_length=255)
    body = models.TextField(
        help_text="Supports variables: {sender_name}, {account_name}, {post_url}",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_saved_replies",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "inbox_saved_reply"
        ordering = ["title"]

    def __str__(self):
        return self.title

    def render(self, context: dict) -> str:
        """Substitute variables in body with context values."""
        text = self.body
        for key, value in context.items():
            text = text.replace(f"{{{key}}}", str(value))
        return text


class InboxSLAConfig(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="inbox_sla_config",
    )
    target_response_minutes = models.PositiveIntegerField(default=120)
    is_active = models.BooleanField(default=False)
    auto_resolve_on_reply = models.BooleanField(
        default=True,
        help_text="Automatically mark messages as resolved when a reply is sent.",
    )

    class Meta:
        db_table = "inbox_sla_config"

    def __str__(self):
        return f"SLA Config for {self.workspace} ({self.target_response_minutes}min)"
