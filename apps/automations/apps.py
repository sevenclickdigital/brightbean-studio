from django.apps import AppConfig


class AutomationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.automations"
    verbose_name = "Automations"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        # Imported inside the handler, not at module scope: ``ready()`` runs
        # before the app registry is fully populated, and ``tasks`` reaches
        # ``actions`` which imports inbox models. ``post_migrate`` also
        # guarantees the background-task tables exist by the time this runs.
        from apps.automations.tasks import PURGE_INTERVAL_SECONDS, purge_automation_logs
        from apps.common.background import register_recurring_task

        register_recurring_task(
            purge_automation_logs,
            repeat=PURGE_INTERVAL_SECONDS,
            verbose_name="purge_automation_logs",
        )
