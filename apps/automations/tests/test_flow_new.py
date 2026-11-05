"""The new-rule chooser: channel, then what the rule does, then which posts.

"New rule" used to open a form. It now opens an empty canvas behind a chooser,
because a flow is drawn on a canvas and the canvas cannot be drawn until it
knows which account it listens to. These pin what the chooser offers, what it
refuses, and what the canvas is seeded with.

Asserts on HTTP status, context and the compiled graph only. No assertion on
any rendered sentence: the project defaults to pt-BR and copy is not a
contract.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.automations import channels, flow
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


class ChooserTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        OrgMembership.objects.create(
            user=self.user,
            organization=self.org,
            org_role=OrgMembership.OrgRole.OWNER,
        )
        WorkspaceMembership.objects.create(
            user=self.user,
            workspace=self.workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
        )
        self.client.force_login(self.user)

    def _account(self, platform="facebook", name="Page"):
        return SocialAccount.objects.create(
            workspace=self.workspace,
            platform=platform,
            account_platform_id=f"{platform}-1",
            account_name=name,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

    def _published_post(self, account, caption="hello"):
        post = Post.objects.create(workspace=self.workspace, caption=caption)
        return PlatformPost.objects.create(
            post=post,
            social_account=account,
            status=PlatformPost.Status.PUBLISHED,
            platform_post_id="pp-1",
            published_at=timezone.now(),
        )

    def _new_url(self):
        return reverse("automations:flow_new", kwargs={"workspace_id": self.workspace.id})

    def _posts_url(self, account, **params):
        url = reverse(
            "automations:flow_choose_posts",
            kwargs={"workspace_id": self.workspace.id, "account_id": account.id},
        )
        if params:
            from urllib.parse import urlencode

            url = f"{url}?{urlencode(params)}"
        return url


class ChannelStepTests(ChooserTestCase):
    def test_a_supported_account_is_offered(self):
        self._account("facebook")
        response = self.client.get(self._new_url())

        self.assertEqual(response.status_code, 200)
        entries = response.context["channels"]
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["can_automate"])

    def test_an_unsupported_account_is_listed_but_refused(self):
        """Listed, not hidden: a missing channel reads as a bug."""
        self._account("tiktok")
        response = self.client.get(self._new_url())

        entries = response.context["channels"]
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["can_automate"])
        self.assertTrue(entries[0]["reason"])

    def test_google_business_says_what_is_planned_for_it(self):
        """Its automation is review replies, which is unbuilt, not unsupported."""
        self._account("google_business")
        response = self.client.get(self._new_url())

        entry = response.context["channels"][0]
        self.assertFalse(entry["can_automate"])
        self.assertNotEqual(entry["reason"], str(channels.UNSUPPORTED_REASON))

    def test_supported_accounts_sort_ahead_of_the_rest(self):
        self._account("tiktok", "TikTok")
        self._account("facebook", "FB")

        entries = self.client.get(self._new_url()).context["channels"]

        self.assertTrue(entries[0]["can_automate"])
        self.assertFalse(entries[-1]["can_automate"])

    def test_a_disconnected_account_is_not_offered(self):
        account = self._account("facebook")
        account.connection_status = SocialAccount.ConnectionStatus.DISCONNECTED
        account.save(update_fields=["connection_status"])

        self.assertEqual(self.client.get(self._new_url()).context["channels"], [])


class PostStepTests(ChooserTestCase):
    def test_published_posts_on_the_account_are_listed(self):
        account = self._account("facebook")
        self._published_post(account)

        response = self.client.get(self._posts_url(account))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["posts"]), 1)

    def test_a_scheduled_post_is_offered(self):
        """The case the feature was built for: write the reply while planning.

        The flow saves unarmed and ``services.arm_for_platform_post`` switches
        it on when the publisher confirms the post.
        """
        account = self._account("facebook")
        post = Post.objects.create(workspace=self.workspace, caption="queued")
        PlatformPost.objects.create(
            post=post,
            social_account=account,
            status=PlatformPost.Status.SCHEDULED,
            scheduled_at=timezone.now(),
        )

        entries = self.client.get(self._posts_url(account)).context["posts"]

        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["is_live"])

    def test_a_draft_is_offered(self):
        account = self._account("facebook")
        post = Post.objects.create(workspace=self.workspace, caption="draft")
        PlatformPost.objects.create(
            post=post,
            social_account=account,
            status=PlatformPost.Status.DRAFT,
        )

        self.assertEqual(len(self.client.get(self._posts_url(account)).context["posts"]), 1)

    def test_a_failed_post_is_not_offered(self):
        """It is not on its way anywhere without the author going back to it."""
        account = self._account("facebook")
        post = Post.objects.create(workspace=self.workspace, caption="broken")
        PlatformPost.objects.create(
            post=post,
            social_account=account,
            status=PlatformPost.Status.FAILED,
        )

        self.assertEqual(self.client.get(self._posts_url(account)).context["posts"], [])

    def test_a_live_post_is_marked_as_live(self):
        account = self._account("facebook")
        self._published_post(account)

        entries = self.client.get(self._posts_url(account)).context["posts"]

        self.assertTrue(entries[0]["is_live"])

    def test_published_posts_sort_ahead_of_queued_ones(self):
        account = self._account("facebook")
        draft = Post.objects.create(workspace=self.workspace, caption="draft")
        PlatformPost.objects.create(
            post=draft,
            social_account=account,
            status=PlatformPost.Status.DRAFT,
        )
        self._published_post(account, caption="live")

        entries = self.client.get(self._posts_url(account)).context["posts"]

        self.assertTrue(entries[0]["is_live"])
        self.assertFalse(entries[-1]["is_live"])

    def test_an_unsupported_channel_is_refused_even_by_direct_id(self):
        """The disabled button is not the control — the view re-asks."""
        account = self._account("tiktok")

        response = self.client.get(self._posts_url(account))

        self.assertEqual(response.status_code, 400)

    def test_another_workspace_account_is_a_404(self):
        other_ws = Workspace.objects.create(organization=self.org, name="Other")
        theirs = SocialAccount.objects.create(
            workspace=other_ws,
            platform="facebook",
            account_platform_id="fb-9",
            account_name="Theirs",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

        self.assertEqual(self.client.get(self._posts_url(theirs)).status_code, 404)

    def test_another_accounts_post_is_not_listed(self):
        mine = self._account("facebook", "Mine")
        theirs = self._account("instagram", "Theirs")
        self._published_post(theirs)

        self.assertEqual(self.client.get(self._posts_url(mine)).context["posts"], [])


class EditRoutingTests(ChooserTestCase):
    """Where editing a rule lands, now that the canvas is the editor."""

    def _rule(self, **kwargs):
        from apps.automations.models import AutomationRule

        return AutomationRule.objects.create(
            workspace=self.workspace,
            name="Rule",
            created_by=self.user,
            **kwargs,
        )

    def _edit_url(self, rule):
        return reverse(
            "automations:edit",
            kwargs={"workspace_id": self.workspace.id, "rule_id": rule.id},
        )

    def test_a_rule_with_a_canvas_opens_in_the_builder(self):
        from apps.automations.models import PostAutomation

        account = self._account("facebook")
        platform_post = self._published_post(account)
        rule = self._rule(social_account=account)
        PostAutomation.objects.create(
            workspace=self.workspace,
            rule=rule,
            platform_post=platform_post,
            graph={},
        )

        response = self.client.get(self._edit_url(rule))

        self.assertEqual(response.status_code, 302)
        self.assertIn(str(platform_post.id), response["Location"])

    def test_a_rule_without_a_canvas_keeps_the_form(self):
        """Its graph can't be rebuilt from the rule — see PostAutomation."""
        rule = self._rule(social_account=self._account("facebook"), post_scope="all")

        response = self.client.get(self._edit_url(rule))

        self.assertEqual(response.status_code, 200)
        self.assertIn("automations/form.html", [t.name for t in response.templates])

    def test_creating_without_a_channel_goes_to_the_chooser(self):
        response = self.client.get(
            reverse("automations:create", kwargs={"workspace_id": self.workspace.id})
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/automations/new/", response["Location"])

    def test_the_account_wide_path_still_reaches_the_form(self):
        account = self._account("facebook")

        response = self.client.get(
            reverse("automations:create", kwargs={"workspace_id": self.workspace.id}),
            {"account": str(account.id), "kind": "both"},
        )

        self.assertEqual(response.status_code, 200)


class SeededCanvasTests(ChooserTestCase):
    """What the chooser's second step decides: which nodes are already drawn."""

    def test_dm_seeds_a_trigger_and_a_dm(self):
        graph = flow.default_graph("dm")
        self.assertEqual([n["type"] for n in graph["nodes"]], ["trigger_comment", "send_dm"])

    def test_comment_seeds_a_public_reply_instead(self):
        graph = flow.default_graph("comment")
        self.assertEqual([n["type"] for n in graph["nodes"]], ["trigger_comment", "reply_comment"])

    def test_both_seeds_the_two_replies_off_one_trigger(self):
        graph = flow.default_graph("both")
        self.assertEqual(
            [n["type"] for n in graph["nodes"]],
            ["trigger_comment", "reply_comment", "send_dm"],
        )
        self.assertEqual(len(graph["edges"]), 2)

    def test_the_default_is_unchanged_for_callers_passing_nothing(self):
        """default_graph() is called from the builder for every new canvas."""
        self.assertEqual(flow.default_graph(), flow.default_graph("dm"))

    def test_the_builder_seeds_the_canvas_the_chooser_asked_for(self):
        account = self._account("facebook")
        platform_post = self._published_post(account)

        url = reverse(
            "automations:flow_builder",
            kwargs={"workspace_id": self.workspace.id, "platform_post_id": platform_post.id},
        )
        response = self.client.get(f"{url}?kind=both")

        types = [n["type"] for n in response.context["graph"]["nodes"]]
        self.assertEqual(types, ["trigger_comment", "reply_comment", "send_dm"])

    def test_an_unknown_kind_falls_back_rather_than_erroring(self):
        account = self._account("facebook")
        platform_post = self._published_post(account)

        url = reverse(
            "automations:flow_builder",
            kwargs={"workspace_id": self.workspace.id, "platform_post_id": platform_post.id},
        )
        response = self.client.get(f"{url}?kind=nonsense")

        self.assertEqual(response.status_code, 200)
        types = [n["type"] for n in response.context["graph"]["nodes"]]
        self.assertEqual(types, ["trigger_comment", "send_dm"])
