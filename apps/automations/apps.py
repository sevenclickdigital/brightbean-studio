from django.apps import AppConfig


class AutomationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.automations"
    verbose_name = "Automations"

    # No ``ready()`` on purpose. The recurring ``purge_automation_logs`` task
    # registration belongs here, but ``apps.automations.tasks`` does not exist
    # yet — a ``post_migrate`` hook importing it would break ``migrate`` on a
    # fresh database. Add the hook (mirroring ``apps/inbox/apps.py``) in the
    # same change that creates ``tasks.py``.
