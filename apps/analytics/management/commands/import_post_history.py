"""Import a channel's existing posts from the command line.

The same work the analytics page's Import button does, reachable without a
browser — which is what makes it safe to try on one production account and
look at the result before trusting the button.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.analytics.history_import import DEFAULT_IMPORT_LIMIT, import_history, importable_count
from apps.social_accounts.models import SocialAccount


class Command(BaseCommand):
    help = "Import posts a channel already had, so analytics can see them."

    def add_arguments(self, parser):
        parser.add_argument("account_id", help="UUID of the SocialAccount to import.")
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_IMPORT_LIMIT,
            help=f"Most posts to read (default: {DEFAULT_IMPORT_LIMIT}).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be imported and write nothing.",
        )

    def handle(self, *args, **opts):
        try:
            account = SocialAccount.objects.get(id=opts["account_id"])
        # ValidationError, not just DoesNotExist: Django raises that one for
        # a malformed UUID before it ever reaches the database.
        except (SocialAccount.DoesNotExist, ValidationError) as exc:
            raise CommandError(f"No SocialAccount with id={opts['account_id']!r}") from exc

        self.stdout.write(f"{account.account_name} ({account.platform})")

        if opts["dry_run"]:
            count = importable_count(account)
            self.stdout.write(self.style.SUCCESS(f"Would import {count} post(s). Nothing written."))
            return

        result = import_history(account, limit=opts["limit"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {result.imported}, already known {result.skipped}, "
                f"found {result.available} on the channel."
            )
        )
        if result.imported:
            self.stdout.write("Metrics arrive on the next analytics sync.")

