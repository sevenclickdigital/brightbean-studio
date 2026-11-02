from django.contrib import admin

from .models import AutomationLog, AutomationRule


@admin.register(AutomationRule)
class AutomationRuleAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "workspace",
        "social_account",
        "trigger_type",
        "is_active",
        "priority",
        "updated_at",
    ]
    list_filter = ["is_active", "trigger_type", "public_reply_mode", "private_reply_mode"]
    search_fields = ["name"]
    raw_id_fields = ["workspace", "social_account", "created_by"]


@admin.register(AutomationLog)
class AutomationLogAdmin(admin.ModelAdmin):
    list_display = [
        "comment_key",
        "outcome",
        "rule",
        "social_account",
        "commented_at",
        "created_at",
    ]
    list_filter = ["outcome", "once_per_sender_per_post"]
    search_fields = ["comment_key", "post_key", "sender_key", "detail"]
    raw_id_fields = ["workspace", "social_account", "rule", "inbox_message"]
