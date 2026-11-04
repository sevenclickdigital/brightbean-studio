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
        from django.db.models.signals import post_migrate

        from apps.billing import signals  # noqa: F401

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.billing.tasks import COURTESY_EXPIRY_INTERVAL_SECONDS, expire_courtesies
        from apps.common.background import register_recurring_task

        register_recurring_task(
            expire_courtesies,
            repeat=COURTESY_EXPIRY_INTERVAL_SECONDS,
            verbose_name="expire_courtesies",
        )
