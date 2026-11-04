"""Create (or promote) an account that only administers the product.

    python manage.py create_operator ops@example.com
    python manage.py create_operator ops@example.com --generate-password
    python manage.py create_operator ops@example.com --noinput   # promote only

Three kinds of account, and the difference is what they *own*, never a
flag on the user:

**Operator** — an account with no organization. It has ``is_staff=True``,
so it reaches ``/billing/staff/`` and can impersonate a customer for
support. It is billed nothing because billing is per workspace and it has
no workspace: the exemption is a consequence of owning nothing, not a
rule written anywhere. This command creates exactly that.

**Customer** — an account with an organization and workspaces. Every
workspace costs R$ 50,00/month (``BILLING_WORKSPACE_PRICE_CENTS``),
whether or not the person holding the account also happens to be staff.

**Courtesy** — a specific customer workspace an operator chose not to
charge, granted by hand in the staff console (``grant_courtesy``). A
decision about one workspace, recorded on that workspace, reversible, and
attributed to the operator who made it.

The three are kept apart on purpose. The tempting shortcut — "``is_staff``
means free" — couples support to money: promoting a colleague so they can
help a customer for an afternoon would zero that colleague's own invoice,
and demoting them would start charging a customer who never agreed to a
price change. So the product owner, who is both the operator and a paying
customer, gets two accounts instead of one flag.

What this command deliberately does **not** do: create an organization or
a workspace. ``apps.accounts.signals.create_organization_on_user_create``
provisions both for every new ``User``, which is right for a signup and
wrong here — it would hand the operator a billable workspace and put them
back in the position this separation exists to end. The signal is
disconnected for the duration of the insert.

Idempotent. Run it twice on the same address and the second run promotes
the existing account (and leaves its password alone) instead of failing on
the unique email.
"""

from __future__ import annotations

import getpass

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models.signals import post_save
from django.utils import timezone
from django.utils.crypto import get_random_string

from apps.accounts.models import User
from apps.accounts.signals import create_organization_on_user_create
from apps.members.models import OrgMembership, WorkspaceMembership

# Long enough that a generated password is not worth attacking even
# though it is printed to a terminal once. Django's own
# ``BaseUserManager.make_random_password`` was removed in 5.0; this is the
# replacement it pointed at.
GENERATED_PASSWORD_LENGTH = 24

MAX_PASSWORD_ATTEMPTS = 3


class Command(BaseCommand):
    help = "Create or promote a staff-only operator account, with no organization or workspace."

    def add_arguments(self, parser):
        parser.add_argument("email", help="Email address of the operator account.")
        parser.add_argument(
            "--name",
            default="",
            help="Display name for a newly created account. Ignored when promoting.",
        )
        parser.add_argument(
            "--generate-password",
            action="store_true",
            help="Generate a strong password and print it once instead of prompting.",
        )
        parser.add_argument(
            "--superuser",
            action="store_true",
            help="Also grant is_superuser (Django admin's full rights). Staff alone is enough for the billing console.",
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Never prompt. Creating a new account then requires --generate-password.",
        )

    def handle(self, *args, **options):
        email = User.objects.normalize_email(options["email"].strip())
        if not email:
            raise CommandError("An email address is required.")

        existing = User.objects.filter(email__iexact=email).first()
        if existing is not None:
            return self._promote(existing, superuser=options["superuser"])

        password = self._resolve_password(
            email,
            interactive=options["interactive"],
            generate=options["generate_password"],
        )
        return self._create(
            email,
            password=password,
            name=options["name"],
            superuser=options["superuser"],
            generated=options["generate_password"],
        )

    # -- creating -----------------------------------------------------

    def _create(self, email, *, password, name, superuser, generated):
        """Insert the operator with the provisioning signal muted.

        ``post_save`` is disconnected rather than worked around after the
        fact: letting the signal create an org and a workspace and then
        deleting them would briefly give the operator a billable
        workspace, and a crash in between would leave it behind.
        """
        with transaction.atomic():
            post_save.disconnect(create_organization_on_user_create, sender=User)
            try:
                user = User.objects.create_user(
                    email=email,
                    password=password,
                    name=name,
                    is_staff=True,
                    is_superuser=superuser,
                    # Pre-accepted, because otherwise this account cannot
                    # be used at all: TosAcceptanceMiddleware redirects
                    # every URL — the staff console included — to the
                    # acceptance page until this is set, and that page is
                    # the customer-facing terms of service. An operator
                    # account is created by the product owner for their
                    # own staff, not by somebody signing up, so there is
                    # no agreement here to collect. Verified: without
                    # this, GET /billing/staff/ answers 302 to
                    # /accounts/accept-terms/.
                    tos_accepted_at=timezone.now(),
                )
            finally:
                post_save.connect(create_organization_on_user_create, sender=User)

        self.stdout.write(self.style.SUCCESS(f"Operator account created: {user.email}"))
        if generated:
            # Printed once and never stored. The operator is expected to
            # change it or move it into a password manager immediately.
            self.stdout.write(self.style.WARNING(f"Generated password (shown once): {password}"))
        self.stdout.write(
            "No organization and no workspace were created, so this account is billed nothing. "
            "Grant a customer's workspace a courtesy from /billing/staff/ instead of reusing this account."
        )
        return None

    def _resolve_password(self, email, *, interactive, generate):
        if generate:
            return get_random_string(GENERATED_PASSWORD_LENGTH)
        if not interactive:
            raise CommandError(
                "Creating an account without input needs --generate-password, "
                "so the password is never passed on the command line (where it would land in shell history)."
            )
        return self._prompt_password(email)

    def _prompt_password(self, email):
        """Ask twice and validate, the way ``createsuperuser`` does.

        ``getpass`` so the password never echoes, and never a command-line
        argument: an argument would be visible in ``ps`` and recorded in
        shell history.
        """
        for _attempt in range(MAX_PASSWORD_ATTEMPTS):
            password = getpass.getpass("Password: ")
            confirmation = getpass.getpass("Password (again): ")
            if password != confirmation:
                self.stderr.write("The two passwords did not match.")
                continue
            if not password.strip():
                self.stderr.write("Blank passwords are not allowed.")
                continue
            try:
                validate_password(password, user=User(email=email, is_staff=True))
            except ValidationError as error:
                for message in error.messages:
                    self.stderr.write(message)
                continue
            return password
        raise CommandError("Too many failed attempts. No account was created.")

    # -- promoting ----------------------------------------------------

    def _promote(self, user, *, superuser):
        """Make an existing account an operator, idempotently.

        Touches ``is_staff``/``is_superuser`` and nothing else — not the
        password, and above all not the account's memberships. A customer
        promoted here keeps their organization and keeps being charged for
        their workspaces, which is the point: this command never changes
        what anyone owes. If the intent was a clean operator account,
        create one at a different address.
        """
        fields = []
        if not user.is_staff:
            user.is_staff = True
            fields.append("is_staff")
        if superuser and not user.is_superuser:
            user.is_superuser = True
            fields.append("is_superuser")

        if fields:
            fields.append("updated_at")
            user.save(update_fields=fields)
            self.stdout.write(self.style.SUCCESS(f"{user.email} promoted to operator."))
        else:
            self.stdout.write(f"{user.email} is already an operator. Nothing to do.")

        self._warn_if_also_a_customer(user)
        return None

    def _warn_if_also_a_customer(self, user):
        """Say so when the promoted account is also a paying customer.

        This is the exact situation the operator/customer split exists to
        undo, and it is invisible from the command's output otherwise. A
        warning, not a refusal: whether to split the account is the
        owner's call, and this command's job is to make the state legible.
        """
        orgs = list(
            OrgMembership.objects.filter(user=user).select_related("organization").values_list(
                "organization__name", flat=True
            )
        )
        if not orgs:
            return

        workspaces = WorkspaceMembership.objects.filter(user=user).count()
        self.stdout.write(
            self.style.WARNING(
                f"{user.email} is also a customer: member of {', '.join(orgs)} "
                f"and of {workspaces} workspace(s), which are billed normally. "
                "An operator account is one with no organization — create a separate "
                "address with this command if you want the two roles apart."
            )
        )
