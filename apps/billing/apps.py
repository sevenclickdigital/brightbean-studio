from django.apps import AppConfig


class BillingConfig(AppConfig):
    name = "apps.billing"
    label = "billing"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Wire the handlers that drop a workspace's cached disk usage when
        # an asset is created, resized or deleted. Without them the 100%
        # block could be walked past for the length of the cache TTL, and
        # a user who deleted files to get under the limit would still be
        # refused.
        from apps.billing import signals  # noqa: F401
