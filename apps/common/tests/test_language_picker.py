"""The sidebar's language picker.

Switching language is a round trip through Django's ``set_language``: the
select posts, the view stores the choice on the session and redirects back, and
the next render reads the catalogue. Each of those is asserted here because a
break in any one of them looks identical from the outside — the picker moves
and the page comes back in the old language.
"""

import re

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


class LanguagePickerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )
        self.org = Organization.objects.create(name="Org")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        self.ws = Workspace.objects.create(organization=self.org, name="WS", timezone="Europe/Berlin")
        WorkspaceMembership.objects.create(
            user=self.user, workspace=self.ws, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        self.client.force_login(self.user)

    def _calendar(self):
        return reverse("calendar:calendar", kwargs={"workspace_id": self.ws.id})

    def _picker_form(self, html):
        """The picker's form element, by its action rather than by position."""
        action = reverse("set_language")
        match = re.search(rf'<form action="{re.escape(action)}"[^>]*>(.*?)</form>', html, re.S)
        self.assertIsNotNone(match, "the language picker form is not on the page")
        return match.group(1)

    def test_the_picker_submits_without_an_inline_handler(self):
        """``onchange="this.form.submit()"`` is what this used to be, and CSP
        blocked it: CSP_SCRIPT_SRC carries no ``'unsafe-inline'``, so the browser
        dropped the handler and the select changed while the form never posted.
        An ``on*`` attribute cannot be nonced the way a script block can, so the
        submit has to come from Alpine, which is already bound here."""
        form = self._picker_form(self.client.get(self._calendar()).content.decode())

        select = re.search(r"<select\b[^>]*>", form).group(0)
        self.assertNotIn("onchange", select)
        self.assertIn("@change", select)

    def test_the_picker_offers_every_configured_language(self):
        form = self._picker_form(self.client.get(self._calendar()).content.decode())

        values = re.findall(r'<option value="([^"]+)"', form)
        self.assertEqual(sorted(values), ["en-us", "pt-br"])

    def test_the_picker_marks_the_active_language(self):
        """Otherwise it always reads as the first option, whatever is in force."""
        self.client.post(reverse("set_language"), {"language": "en-us", "next": self._calendar()})

        form = self._picker_form(self.client.get(self._calendar()).content.decode())

        selected = re.search(r'<option value="([^"]+)"[^>]*\bselected\b', form)
        self.assertIsNotNone(selected, "no option is marked selected")
        self.assertEqual(selected.group(1), "en-us")

    def test_switching_language_changes_the_rendered_navigation(self):
        """The whole point, asserted on what the reader sees rather than on the
        session key: a stored language that no catalogue serves would pass a
        session assertion and still render English."""
        resp = self.client.post(reverse("set_language"), {"language": "pt-br", "next": self._calendar()})
        self.assertEqual(resp.status_code, 302)

        html = self.client.get(self._calendar()).content.decode()

        self.assertIn("Publicar", html)
        self.assertIn("Caixa de entrada", html)
        self.assertNotIn('<span class="sidebar-nav-label">Publish</span>', html)

    def test_switching_back_to_english_restores_the_source_strings(self):
        """pt-br is the default, so English is the switch that proves the
        catalogue is consulted per request rather than baked in at startup."""
        self.client.post(reverse("set_language"), {"language": "en-us", "next": self._calendar()})

        html = self.client.get(self._calendar()).content.decode()

        self.assertIn('<span class="sidebar-nav-label">Publish</span>', html)
        self.assertNotIn("Caixa de entrada", html)

    def test_switching_language_returns_to_the_page_it_was_changed_from(self):
        """``next`` is posted so the reader keeps their place; without it
        ``set_language`` sends everyone to the site root."""
        resp = self.client.post(reverse("set_language"), {"language": "pt-br", "next": self._calendar()})

        self.assertEqual(resp.headers["Location"], self._calendar())
