"""The media picker selects several assets at once and inserts them in order.

Picking used to be one asset per click, each a round trip that closed the
modal, with the post's media order left to however the attachments happened to
land. These pin the replacement: one request carries the whole selection, the
click order becomes the carousel order, and the picker runs the library's own
search/filter/sort/paginate rather than the first 50 assets.

Asserts on HTTP status, database state and context only. No assertion on any
rendered sentence: the project defaults to pt-BR and copy is not a contract.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import Post, PostMedia
from apps.media_library.models import MediaAsset, MediaFolder
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


class MediaPickerTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )
        self.org = Organization.objects.create(name="Test Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Test Workspace")
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

    def _asset(self, filename="a.png", media_type=MediaAsset.MediaType.IMAGE, **kwargs):
        asset = MediaAsset(
            organization=self.org,
            workspace=self.workspace,
            filename=filename,
            media_type=media_type,
            file_size=1,
            processing_status=MediaAsset.ProcessingStatus.COMPLETED,
            **kwargs,
        )
        # The card template renders ``asset.file.url`` for an image, which
        # raises on an asset with no file. Only the path is needed — nothing
        # here reads the bytes.
        asset.file = f"media_library/2026/01/{filename}"
        asset.save()
        return asset

    def _post(self):
        return Post.objects.create(workspace=self.workspace, caption="hi")

    def _picker(self, **params):
        return self.client.get(
            reverse("composer:media_picker", kwargs={"workspace_id": self.workspace.id}),
            params,
        )


class AttachBatchTests(MediaPickerTestCase):
    def test_every_selected_asset_is_attached_in_one_request(self):
        post = self._post()
        assets = [self._asset(f"{i}.png") for i in range(4)]

        response = self.client.post(
            reverse(
                "composer:attach_media_batch_post",
                kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
            ),
            {"media_asset_ids": [str(a.id) for a in assets]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.media_attachments.count(), 4)

    def test_attachment_order_follows_the_order_they_were_picked(self):
        """Position, not arrival: the first thumbnail clicked leads the carousel."""
        post = self._post()
        first, second, third = (self._asset(f"{i}.png") for i in range(3))

        # Deliberately not the creation order — this is what the DB would
        # hand back if the view trusted its own ordering.
        picked = [third, first, second]
        self.client.post(
            reverse(
                "composer:attach_media_batch_post",
                kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
            ),
            {"media_asset_ids": [str(a.id) for a in picked]},
        )

        ordered = list(
            PostMedia.objects.filter(post=post).order_by("position").values_list("media_asset_id", flat=True)
        )
        self.assertEqual(ordered, [a.id for a in picked])

    def test_selection_without_a_post_is_held_as_pending_media(self):
        assets = [self._asset(f"{i}.png") for i in range(3)]

        response = self.client.post(
            reverse("composer:attach_media_batch", kwargs={"workspace_id": self.workspace.id}),
            {"media_asset_ids": [str(a.id) for a in assets]},
        )

        self.assertEqual(response.status_code, 200)
        pending = self.client.session[f"pending_media_{self.workspace.id}"]
        self.assertEqual(pending, [str(a.id) for a in assets])

    def test_an_empty_selection_is_refused(self):
        post = self._post()
        response = self.client.post(
            reverse(
                "composer:attach_media_batch_post",
                kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
            ),
            {},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(post.media_attachments.count(), 0)

    def test_another_workspace_asset_is_dropped_from_the_selection(self):
        """The id filter is the workspace's own visibility, not the POST body."""
        post = self._post()
        mine = self._asset("mine.png")
        other_ws = Workspace.objects.create(organization=self.org, name="Other")
        theirs = MediaAsset(
            organization=self.org,
            workspace=other_ws,
            filename="theirs.png",
            media_type=MediaAsset.MediaType.IMAGE,
            file_size=1,
        )
        theirs.file = "media_library/2026/01/theirs.png"
        theirs.save()

        self.client.post(
            reverse(
                "composer:attach_media_batch_post",
                kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
            ),
            {"media_asset_ids": [str(mine.id), str(theirs.id)]},
        )

        attached = list(PostMedia.objects.filter(post=post).values_list("media_asset_id", flat=True))
        self.assertEqual(attached, [mine.id])

    def test_a_malformed_id_does_not_take_down_the_whole_insert(self):
        """A non-UUID reaching the ORM raises ValidationError, i.e. a 500."""
        post = self._post()
        good = self._asset("good.png")

        response = self.client.post(
            reverse(
                "composer:attach_media_batch_post",
                kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
            ),
            {"media_asset_ids": ["not-a-uuid", str(good.id)]},
        )

        self.assertEqual(response.status_code, 200)
        attached = list(PostMedia.objects.filter(post=post).values_list("media_asset_id", flat=True))
        self.assertEqual(attached, [good.id])

    def test_a_selection_of_only_malformed_ids_is_refused(self):
        post = self._post()
        response = self.client.post(
            reverse(
                "composer:attach_media_batch_post",
                kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
            ),
            {"media_asset_ids": ["not-a-uuid", ""]},
        )
        self.assertEqual(response.status_code, 400)

    def test_a_selection_of_only_unknown_ids_is_refused(self):
        post = self._post()
        response = self.client.post(
            reverse(
                "composer:attach_media_batch_post",
                kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
            ),
            {"media_asset_ids": ["0f2b7a44-0000-4000-8000-000000000000"]},
        )
        self.assertEqual(response.status_code, 404)


class PickerListingTests(MediaPickerTestCase):
    def test_the_picker_paginates_instead_of_stopping_at_fifty(self):
        """The old picker sliced [:50]; anything past it could not be reached."""
        for i in range(60):
            self._asset(f"{i:03}.png")

        first = self._picker()
        self.assertEqual(first.status_code, 200)
        page = first.context["page"]
        self.assertTrue(page.has_next())

        second = self._picker(page=2)
        self.assertEqual(second.context["page"].number, 2)
        seen = page.paginator.count
        self.assertEqual(seen, 60)

    def test_search_narrows_the_listing(self):
        # Searched by a whole word of the name: Postgres tokenizes
        # "christmas tree.png" as "christmas" + "tree.png", so a bare
        # "christmas" matches here but would not match "christmas.png",
        # which indexes as one token.
        self._asset("christmas tree.png")
        self._asset("summer sale.png")

        response = self._picker(q="christmas")

        names = [a.filename for a in response.context["page"].object_list]
        self.assertEqual(names, ["christmas tree.png"])

    def test_the_type_filter_narrows_the_listing(self):
        self._asset("a.png", media_type=MediaAsset.MediaType.IMAGE)
        self._asset("b.mp4", media_type=MediaAsset.MediaType.VIDEO)

        response = self._picker(type=MediaAsset.MediaType.VIDEO)

        names = [a.filename for a in response.context["page"].object_list]
        self.assertEqual(names, ["b.mp4"])

    def test_the_folder_filter_narrows_the_listing(self):
        folder = MediaFolder.objects.create(organization=self.org, workspace=self.workspace, name="Natal")
        self._asset("in-folder.png", folder=folder)
        self._asset("loose.png")

        response = self._picker(folder=str(folder.id))

        names = [a.filename for a in response.context["page"].object_list]
        self.assertEqual(names, ["in-folder.png"])

    def test_favorites_narrow_the_listing(self):
        self._asset("starred.png", is_starred=True)
        self._asset("plain.png")

        response = self._picker(starred="1")

        names = [a.filename for a in response.context["page"].object_list]
        self.assertEqual(names, ["starred.png"])

    def test_sorting_by_name_reorders_the_listing(self):
        self._asset("zebra.png")
        self._asset("apple.png")

        response = self._picker(sort="name")

        names = [a.filename for a in response.context["page"].object_list]
        self.assertEqual(names, ["apple.png", "zebra.png"])

    def test_a_grid_targeted_request_returns_the_grid_alone(self):
        """Searching swaps the grid; it must not swap the modal's own chrome."""
        self._asset("a.png")

        response = self.client.get(
            reverse("composer:media_picker", kwargs={"workspace_id": self.workspace.id}),
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET="picker-asset-grid",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([t.name for t in response.templates][0], "media_library/_asset_grid.html")

    def test_opening_the_picker_returns_the_whole_modal_body(self):
        self._asset("a.png")

        response = self._picker()

        self.assertEqual([t.name for t in response.templates][0], "composer/partials/media_picker.html")
        self.assertTrue(response.context["picker_mode"])
        self.assertIn("folders", response.context)

    def test_another_workspace_media_is_not_listed(self):
        self._asset("mine.png")
        other_ws = Workspace.objects.create(organization=self.org, name="Other")
        theirs = MediaAsset(
            organization=self.org,
            workspace=other_ws,
            filename="theirs.png",
            media_type=MediaAsset.MediaType.IMAGE,
            file_size=1,
        )
        theirs.file = "media_library/2026/01/theirs.png"
        theirs.save()

        response = self._picker()

        names = [a.filename for a in response.context["page"].object_list]
        self.assertEqual(names, ["mine.png"])
