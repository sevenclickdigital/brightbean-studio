import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models

from apps.common.encryption import EncryptedJSONField, EncryptedTextField


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        if password:
            user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255, blank=True, default="")
    avatar = models.ImageField(upload_to="avatars/%Y/%m/", blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    # 2FA fields
    totp_secret = EncryptedTextField(blank=True, null=True)
    totp_recovery_codes = EncryptedJSONField(blank=True, null=True)
    totp_enabled = models.BooleanField(default=False)

    # Workspace persistence
    last_workspace_id = models.UUIDField(blank=True, null=True)

    # Terms of Service acceptance (null = not yet accepted)
    tos_accepted_at = models.DateTimeField(blank=True, null=True)

    # When the sidebar's Trustpilot card was last closed. It stays hidden until
    # the next login moves last_login past this. Kept here rather than in the
    # session because SESSION_SAVE_EVERY_REQUEST lets any request in flight
    # (the notification poll, an autosave) write its stale copy back over it.
    review_banner_dismissed_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "accounts_user"

    def __str__(self):
        return self.email

    @property
    def display_name(self):
        if self.name:
            return self.name
        if self.email:
            return self.email.split("@")[0]
        return "User"


class OAuthConnection(models.Model):
    class Provider(models.TextChoices):
        GOOGLE = "google", "Google"
        GITHUB = "github", "GitHub"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="oauth_connections")
    provider = models.CharField(max_length=20, choices=Provider.choices)
    provider_user_id = models.CharField(max_length=255)
    provider_email = models.EmailField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_oauth_connection"
        unique_together = [("provider", "provider_user_id")]

    def __str__(self):
        return f"{self.user.email} - {self.provider}"


class Session(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="user_sessions")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    device_info = models.CharField(max_length=500, blank=True, default="")
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    last_active_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "accounts_session"

    def __str__(self):
        return f"Session for {self.user.email}"


class ImpersonationSession(models.Model):
    """One row per time an operator took over a customer's account.

    Lives in ``accounts`` rather than ``billing`` because what it records
    is an identity event, not a money event: who was authenticated as
    whom. The staff console happens to be where an operator starts one,
    but the same audit question ("who read this customer's inbox on
    Tuesday?") would be asked if impersonation were reachable from
    anywhere else, and ``accounts`` owns ``User`` and the session.

    Both sides are ``SET_NULL`` with the email snapshotted beside them:
    the whole point of the trail is to survive the accounts it describes.
    A deleted customer must not take the record of who looked inside
    their account with them.

    ``ended_at`` NULL means the takeover is still open — the operator has
    not pressed "stop", or their session expired while inside. That is a
    findable state on purpose; a row that silently closed itself would
    make "was anyone in this account at 14:00?" unanswerable.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    impersonator = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="impersonations_started",
    )
    impersonated = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="impersonations_received",
    )

    # Snapshots, so the trail still names both parties after either
    # account is deleted.
    impersonator_email = models.EmailField(blank=True, default="")
    impersonated_email = models.EmailField(blank=True, default="")

    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    # Where the operator came from. Useful when reconstructing an
    # incident; never used to make an access decision.
    ip_address = models.GenericIPAddressField(blank=True, null=True)

    class Meta:
        db_table = "accounts_impersonation_session"
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["impersonated", "-started_at"]),
            models.Index(fields=["impersonator", "-started_at"]),
        ]

    def __str__(self):
        return f"{self.impersonator_email} as {self.impersonated_email} at {self.started_at:%Y-%m-%d %H:%M}"

    @property
    def is_open(self):
        return self.ended_at is None
