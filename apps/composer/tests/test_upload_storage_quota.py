"""Every composer path that mints a MediaAsset must respect the workspace
disk block.

The composer has no single upload funnel — four views create assets
independently — and before this phase only the Unsplash import consulted a
quota at all. These tests pin the hole shut: for each path, a full
workspace refuses the upload and writes no row, and the same path succeeds
once there is headroom.

Asserts on HTTP status and database state only. No assertion on any
rendered sentence: the project defaults to pt-BR and copy is not a
contract.
"""

from __future__ import annotations

import shutil
import tempfile

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing import services
from apps.billing.models import WorkspaceAddon, WorkspaceSubscription
from apps.composer.models import Idea, IdeaGroup
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

GB = WorkspaceAddon.BYTES_PER_GB
TEMP_MEDIA_ROOT = tempfile.mkdtemp(prefix="bb-test-quota-media-")

# A 1x1 PNG — real magic bytes, so the paths that sniff the file agree it
# is an image.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def tearDownModule():
    shutil.rmtree(TEMP_MEDIA_ROOT, ignore_errors=True)


def _upload(name="x.png"):
    return SimpleUploadedFile(name, PNG_BYTES, content_type="image/png")


@override_settings(MEDIA_ROOT=TEMP_MEDIA_ROOT)
class UploadQuotaTestCase(TestCase):
    def setUp(self):
        cache.clear()
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

    def _subscribe(self, base_bytes):
        sub = services.ensure_subscription(self.workspace)
        sub.status = WorkspaceSubscription.Status.ACTIVE
        sub.base_storage_bytes = base_bytes
        sub.save(update_fields=["status", "base_storage_bytes", "updated_at"])
        return sub

    def _fill(self, size):
        """Park ``size`` bytes of existing usage on the workspace."""
        asset = MediaAsset(
            organization=self.org,
            workspace=self.workspace,
            filename="existing.jpg",
            media_type=MediaAsset.MediaType.IMAGE,
            file_size=size,
        )
        asset.file = "media_library/2026/01/existing.jpg"
        asset.save()
        return asset

    def _fill_to_capacity(self):
        """A workspace whose quota is exactly consumed."""
        self._subscribe(1000)
        self._fill(1000)

    def _give_headroom(self):
        self._subscribe(10 * GB)

    def _asset_count(self):
        return MediaAsset.objects.filter(workspace=self.workspace).count()


class ThumbnailUploadQuotaTests(UploadQuotaTestCase):
    """``thumbnail_upload`` — the YouTube thumbnail path."""

    def _post(self):
        url = reverse("composer:thumbnail_upload", kwargs={"workspace_id": self.workspace.id})
        return self.client.post(url, {"file": _upload()})

    def test_blocked_when_full(self):
        self._fill_to_capacity()
        before = self._asset_count()
        response = self._post()
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self._asset_count(), before)

    def test_allowed_with_headroom(self):
        self._give_headroom()
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._asset_count(), 1)

    def test_error_body_carries_the_numbers_for_the_ui(self):
        self._fill_to_capacity()
        response = self._post()
        body = response.json()
        self.assertEqual(body["used_bytes"], 1000)
        self.assertEqual(body["limit_bytes"], 1000)
        self.assertTrue(body["error"].strip())


class ComposerUploadMediaQuotaTests(UploadQuotaTestCase):
    """``upload_media`` — the composer's direct attach path."""

    def _post(self):
        url = reverse("composer:upload_media", kwargs={"workspace_id": self.workspace.id})
        return self.client.post(url, {"file": _upload()})

    def test_blocked_when_full(self):
        self._fill_to_capacity()
        before = self._asset_count()
        response = self._post()
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self._asset_count(), before)

    def test_allowed_with_headroom(self):
        self._give_headroom()
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._asset_count(), 1)

    def test_active_addon_unblocks_the_same_upload(self):
        """The product promise, end to end through the view."""
        self._fill_to_capacity()
        self.assertEqual(self._post().status_code, 413)

        addon = services.contract_storage_addon(self.workspace, 1)
        addon.status = WorkspaceAddon.Status.ACTIVE
        addon.save(update_fields=["status", "updated_at"])

        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(self._asset_count(), 2)  # the filler plus the new one


class IdeaUploadMediaQuotaTests(UploadQuotaTestCase):
    """``idea_upload_media`` — the Idea modal's pre-upload endpoint."""

    def _post(self):
        url = reverse("composer:idea_upload_media", kwargs={"workspace_id": self.workspace.id})
        return self.client.post(url, {"file": _upload()})

    def test_blocked_when_full(self):
        self._fill_to_capacity()
        before = self._asset_count()
        response = self._post()
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self._asset_count(), before)

    def test_allowed_with_headroom(self):
        self._give_headroom()
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._asset_count(), 1)


class IdeaCreateUploadQuotaTests(UploadQuotaTestCase):
    """``idea_create`` — multipart upload inside the Idea create flow."""

    def _post(self):
        url = reverse("composer:idea_create", kwargs={"workspace_id": self.workspace.id})
        return self.client.post(url, {"title": "An idea", "media": _upload()})

    def test_blocked_when_full_and_no_idea_is_created(self):
        self._fill_to_capacity()
        before = self._asset_count()
        response = self._post()
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self._asset_count(), before)
        self.assertEqual(Idea.objects.filter(workspace=self.workspace).count(), 0)

    def test_allowed_with_headroom(self):
        self._give_headroom()
        response = self._post()
        self.assertIn(response.status_code, (200, 204, 302))
        self.assertEqual(self._asset_count(), 1)


class IdeaEditUploadQuotaTests(UploadQuotaTestCase):
    """``idea_edit`` — multipart upload inside the Idea edit flow."""

    def setUp(self):
        super().setUp()
        self.group = IdeaGroup.objects.create(workspace=self.workspace, name="Backlog", position=0)
        self.idea = Idea.objects.create(
            workspace=self.workspace,
            group=self.group,
            title="Existing idea",
            author=self.user,
        )

    def _post(self):
        url = reverse("composer:idea_edit", kwargs={"workspace_id": self.workspace.id, "idea_id": self.idea.id})
        return self.client.post(url, {"title": "Existing idea", "media": _upload()})

    def test_blocked_when_full(self):
        self._fill_to_capacity()
        before = self._asset_count()
        response = self._post()
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self._asset_count(), before)

    def test_allowed_with_headroom(self):
        self._give_headroom()
        response = self._post()
        self.assertIn(response.status_code, (200, 204, 302))
        self.assertEqual(self._asset_count(), 1)


class UncontractedWorkspaceUploadTests(UploadQuotaTestCase):
    """A workspace with no subscription is not blocked by the disk quota.

    Gating an uncontracted workspace is ``is_workspace_active``'s job; the
    disk block must not take every pre-billing workspace offline.
    """

    def test_upload_succeeds_without_a_subscription(self):
        url = reverse("composer:upload_media", kwargs={"workspace_id": self.workspace.id})
        response = self.client.post(url, {"file": _upload()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._asset_count(), 1)
