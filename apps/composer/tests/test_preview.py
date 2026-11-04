from pathlib import Path

from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from django.urls import reverse

from apps.composer.models import PlatformPost, Post, Tag
from apps.composer.views import preview
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from .test_unsplash import ComposerTestCase


class ComposerPreviewTests(ComposerTestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.preview_url = reverse("composer:preview", kwargs={"workspace_id": self.workspace.id})
        self.account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="facebook",
            account_platform_id="facebook-page-1",
            account_name="Facebook Page",
        )

    def _preview_post(self, data):
        request = self.factory.post(self.preview_url, data)
        request.user = self.user
        SessionMiddleware(lambda req: None).process_request(request)
        request.session.save()
        return preview(request, self.workspace.id)

    def test_compose_page_posts_live_preview_form_data(self):
        template = Path("templates/composer/compose.html").read_text()

        self.assertIn("hx-post=\"{% url 'composer:preview' workspace_id=workspace.id %}\"", template)
        self.assertNotIn("hx-get=\"{% url 'composer:preview' workspace_id=workspace.id %}\"", template)

    def test_preview_accepts_large_caption_in_post_body(self):
        caption = "Long preview caption. " * 300

        response = self._preview_post(
            {
                "title": "Preview title",
                "caption": caption,
                "selected_accounts": str(self.account.id),
            },
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Facebook Page", content)
        self.assertIn("6600/63206", content)
        self.assertIn("Long preview caption. Long preview caption.", content)

    def test_preview_rejects_get_requests(self):
        response = self.client.get(
            self.preview_url,
            {
                "caption": "This belongs in the request body.",
                "selected_accounts": str(self.account.id),
            },
        )

        self.assertEqual(response.status_code, 405)


class PublishableTagTests(ComposerTestCase):
    """Tags file a post internally; some of them also go out with it."""

    def setUp(self):
        super().setUp()
        self.account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="facebook",
            account_platform_id="facebook-page-tags",
            account_name="Facebook Page",
        )
        Tag.objects.create(workspace=self.workspace, name="SevenClick", kind=Tag.Kind.INTERNAL)
        Tag.objects.create(workspace=self.workspace, name="Black Friday", kind=Tag.Kind.HASHTAG)
        Tag.objects.create(workspace=self.workspace, name="shopeebrasil", kind=Tag.Kind.MENTION)

    def test_suffix_keeps_applied_order_and_drops_internal_tags(self):
        suffix = Tag.publishable_suffix(
            self.workspace.id, ["shopeebrasil", "SevenClick", "Black Friday"]
        )

        self.assertEqual(suffix, "@shopeebrasil #BlackFriday")

    def test_internal_tags_alone_publish_nothing(self):
        self.assertEqual(Tag.publishable_suffix(self.workspace.id, ["SevenClick"]), "")
        self.assertEqual(Tag.publishable_suffix(self.workspace.id, []), "")

    def test_a_name_the_workspace_does_not_define_is_not_published(self):
        # Guessing a kind here would put a word in the post that nobody chose.
        self.assertEqual(Tag.publishable_suffix(self.workspace.id, ["strangers"]), "")

    def test_a_tag_from_another_workspace_does_not_leak(self):
        other = Workspace.objects.create(organization=self.org, name="Other")
        Tag.objects.create(workspace=other, name="theirs", kind=Tag.Kind.HASHTAG)

        self.assertEqual(Tag.publishable_suffix(self.workspace.id, ["theirs"]), "")

    def test_spaces_and_punctuation_are_folded_so_the_tag_survives(self):
        # The networks cut at the first space: "#Black" would lose the rest.
        self.assertEqual(Tag.slug_for_publishing("Black Friday"), "BlackFriday")
        self.assertEqual(Tag.slug_for_publishing("promo-50%"), "promo50")
        # Accents stay: the networks take them, and folding would mangle these.
        self.assertEqual(Tag.slug_for_publishing("São Paulo"), "SãoPaulo")

    def test_effective_caption_appends_the_published_tags(self):
        post = Post.objects.create(
            workspace=self.workspace,
            title="T",
            caption="Promoção de fim de ano",
            tags=["SevenClick", "Black Friday"],
            author=self.user,
        )
        target = PlatformPost.objects.create(post=post, social_account=self.account)

        self.assertEqual(
            target.effective_caption, "Promoção de fim de ano\n\n#BlackFriday"
        )
        # The limit has to be measured against what actually goes out.
        self.assertEqual(target.caption_length, len(target.effective_caption))

    def test_a_caption_that_is_only_tags_carries_no_blank_lines(self):
        post = Post.objects.create(
            workspace=self.workspace,
            title="T",
            caption="",
            tags=["Black Friday"],
            author=self.user,
        )
        target = PlatformPost.objects.create(post=post, social_account=self.account)

        self.assertEqual(target.effective_caption, "#BlackFriday")

    def test_preview_shows_the_tags_the_post_will_publish(self):
        response = self.client.post(
            reverse("composer:preview", kwargs={"workspace_id": self.workspace.id}),
            {
                "caption": "Olha isso",
                "tags": "SevenClick,Black Friday",
                "selected_accounts": str(self.account.id),
            },
        )

        content = response.content.decode()
        self.assertIn("#BlackFriday", content)
        self.assertNotIn("SevenClick", content)
