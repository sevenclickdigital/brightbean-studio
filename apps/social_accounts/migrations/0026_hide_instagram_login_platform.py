from django.db import migrations


def hide_instagram_login(apps, schema_editor):
    """Take Instagram (Direct) off the connect page.

    The connector targets the Instagram API with Instagram Login, which Meta
    still serves and has set no retirement date for. What went away is the App
    Dashboard setup for it: apps created after the Unified Login rollout get
    "API setup with Unified login" and "API setup with Facebook login" only, so
    there is nowhere left to read the Instagram App ID that
    ``PLATFORM_INSTAGRAM_APP_ID`` needs. Without it the card can only ever
    render as "Not configured".

    Hidden rather than deleted: ``providers/instagram_login.py`` works, and it
    is the starting point for a Unified Login provider (which drops the Page
    requirement the same way this one does). Flip ``is_visible`` back on in the
    admin if the credentials ever become obtainable.
    """
    PlatformVisibility = apps.get_model("social_accounts", "PlatformVisibility")
    PlatformVisibility.objects.update_or_create(
        platform="instagram_login",
        defaults={"is_visible": False},
    )


def show_instagram_login(apps, schema_editor):
    PlatformVisibility = apps.get_model("social_accounts", "PlatformVisibility")
    PlatformVisibility.objects.filter(platform="instagram_login").update(is_visible=True)


class Migration(migrations.Migration):
    dependencies = [
        ("social_accounts", "0025_youtuberevocation_and_more"),
    ]

    operations = [
        migrations.RunPython(hide_instagram_login, show_instagram_login),
    ]
