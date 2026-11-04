from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import ImpersonationSession, OAuthConnection, Session, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("email", "name", "is_active", "is_staff", "created_at")
    list_filter = ("is_active", "is_staff", "totp_enabled")
    search_fields = ("email", "name")
    ordering = ("-created_at",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("name", "avatar")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser")}),
        ("2FA", {"fields": ("totp_enabled",)}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),)


@admin.register(OAuthConnection)
class OAuthConnectionAdmin(admin.ModelAdmin):
    list_display = ("user", "provider", "provider_email", "created_at")
    list_filter = ("provider",)


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ("user", "device_info", "ip_address", "last_active_at", "expires_at")
    list_filter = ("created_at",)


@admin.register(ImpersonationSession)
class ImpersonationSessionAdmin(admin.ModelAdmin):
    """Read-only view of who took over whose account, and when.

    An audit trail that can be edited is not one, so every field is
    read-only and there is no add form: the only writer is
    ``apps.accounts.impersonation``. Deletion is left to Django's normal
    permissions — a retention policy is a deliberate act, unlike quietly
    correcting a row to say someone was never in an account.
    """

    list_display = ("started_at", "impersonator_email", "impersonated_email", "ended_at", "ip_address")
    list_filter = ("started_at", "ended_at")
    search_fields = ("impersonator_email", "impersonated_email")
    date_hierarchy = "started_at"
    readonly_fields = (
        "impersonator",
        "impersonated",
        "impersonator_email",
        "impersonated_email",
        "started_at",
        "ended_at",
        "ip_address",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
