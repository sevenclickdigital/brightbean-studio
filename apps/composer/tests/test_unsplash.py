"""Tests for the composer's Unsplash search/import endpoints and the TikTok
cover timestamp field.

Unsplash HTTP traffic is mocked at apps.composer.views.httpx so no network is
involved.
"""

import json
import shutil
import tempfile
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post, PostMedia
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

TEMP_MEDIA_ROOT = tempfile.mkdtemp(prefix="bb-test-media-")


def tearDownModule():
    shutil.rmtree(TEMP_MEDIA_ROOT, ignore_errors=True)


def _response(status_code=200, json_data=None, content=b"", headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_data or {})
    resp.content = content
    resp.headers = headers or {}
    return resp


def _unsplash_photo(photo_id="abc123"):
    return {
        "id": photo_id,
        "thumb": f"https://images.unsplash.com/{photo_id}?w=400",
        "full": f"https://images.unsplash.com/{photo_id}?w=1080",
        "width": 4000,
        "height": 3000,
        "color": "#c0ffee",
        "alt": "A cup of coffee",
        "photographer": "Brigitte Tohm",
        "photographer_url": "https://unsplash.com/@brigittetohm",
        "photo_url": f"https://unsplash.com/photos/{photo_id}",
        "download_location": f"https://api.unsplash.com/photos/{photo_id}/download",
    }


class ComposerTestCase(TestCase):
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


# This asserts on rendered copy, which is translated while the project
# defaults to pt-br. Pin English so it tests the markup, not the catalogue.
@override_settings(LANGUAGE_CODE="en-us")
class UnsplashButtonVisibilityTests(ComposerTestCase):
    """The composer only renders the Unsplash tile when an API key is set."""

    def _compose_html(self):
        url = reverse("composer:compose", kwargs={"workspace_id": self.workspace.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    @override_settings(UNSPLASH_ACCESS_KEY="test-key")
    def test_button_rendered_with_api_key(self):
        html = self._compose_html()
        self.assertIn('title="Search Unsplash"', html)
        self.assertIn('x-show="showUnsplashModal"', html)  # modal markup present

    @override_settings(UNSPLASH_ACCESS_KEY="")
    def test_button_hidden_without_api_key(self):
        html = self._compose_html()
        self.assertNotIn('title="Search Unsplash"', html)
        # Modal markup gone too; only the JS state/methods remain in the script.
        self.assertNotIn('x-show="showUnsplashModal"', html)


@override_settings(UNSPLASH_ACCESS_KEY="test-key")
class UnsplashSearchTests(ComposerTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("composer:unsplash_search", kwargs={"workspace_id": self.workspace.id})

    @override_settings(UNSPLASH_ACCESS_KEY="")
    def test_returns_503_without_api_key(self):
        response = self.client.get(self.url, {"q": "coffee"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("UNSPLASH_ACCESS_KEY", response.json()["error"])

    def test_missing_query_returns_400(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400)

    @patch("apps.composer.views.httpx.get")
    def test_results_trimmed_to_ui_fields(self, mock_get):
        mock_get.return_value = _response(
            json_data={
                "total": 1,
                "results": [
                    {
                        "id": "abc123",
                        "width": 4000,
                        "height": 3000,
                        "color": "#c0ffee",
                        "alt_description": "A cup of coffee",
                        "description": None,
                        "urls": {
                            "small": "https://images.unsplash.com/abc?w=400",
                            "regular": "https://images.unsplash.com/abc?w=1080",
                            "raw": "x",
                            "full": "y",
                            "thumb": "z",
                        },
                        "user": {
                            "name": "Brigitte Tohm",
                            "username": "brigittetohm",
                            "links": {"html": "https://unsplash.com/@brigittetohm"},
                        },
                        "links": {
                            "html": "https://unsplash.com/photos/abc123",
                            "download_location": "https://api.unsplash.com/photos/abc123/download",
                        },
                    }
                ],
            }
        )
        response = self.client.get(self.url, {"q": "coffee"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 1)
        photo = data["results"][0]
        self.assertEqual(
            set(photo.keys()),
            {
                "id",
                "thumb",
                "full",
                "width",
                "height",
                "color",
                "alt",
                "photographer",
                "photographer_url",
                "photo_url",
                "download_location",
            },
        )
        self.assertEqual(photo["alt"], "A cup of coffee")
        self.assertEqual(photo["photographer"], "Brigitte Tohm")
        # The key must be sent as a Client-ID header, not a query param.
        headers = mock_get.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Client-ID test-key")

    @patch("apps.composer.views.httpx.get")
    def test_rate_limit_maps_to_429(self, mock_get):
        mock_get.return_value = _response(status_code=429)
        response = self.client.get(self.url, {"q": "coffee"})
        self.assertEqual(response.status_code, 429)

    @patch("apps.composer.views.httpx.get")
    def test_non_object_body_maps_to_502(self, mock_get):
        # A 200 whose JSON top level is a list/null/scalar must not 500 on
        # data.get(). (Build the mock directly so the _response helper's
        # `json_data or {}` fallback doesn't coerce the falsy bodies away.)
        for body in ([], None, "oops"):
            resp = MagicMock()
            resp.status_code = 200
            resp.json = MagicMock(return_value=body)
            mock_get.return_value = resp
            response = self.client.get(self.url, {"q": "coffee"})
            self.assertEqual(response.status_code, 502, body)


@override_settings(UNSPLASH_ACCESS_KEY="test-key", MEDIA_ROOT=TEMP_MEDIA_ROOT)
class UnsplashImportTests(ComposerTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("composer:unsplash_import", kwargs={"workspace_id": self.workspace.id})

    def _post(self, photos, url=None):
        return self.client.post(
            url or self.url,
            data=json.dumps({"photos": photos}),
            content_type="application/json",
        )

    def _patch_client(self, *, get_side_effect, stream_side_effect=()):
        """Patch the pooled httpx.Client so the import never hits the network.

        client.get(...) handles the per-photo download registration;
        client.stream(...) yields the (streamed, size-capped) image bytes.
        """
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.get = MagicMock(side_effect=list(get_side_effect))
        client.stream = MagicMock(side_effect=list(stream_side_effect))
        return patch("apps.composer.views.httpx.Client", return_value=client), client

    def _stream_cm(self, *, status_code=200, content=b"jpeg-bytes", content_type="image/jpeg", content_length=None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = {"content-type": content_type}
        if content_length is not None:
            resp.headers["content-length"] = content_length
        resp.iter_bytes = MagicMock(return_value=[content])
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=resp)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    def _ok_photo_mocks(self, photo_id="abc123"):
        return {
            "get_side_effect": [_response(json_data={"url": f"https://images.unsplash.com/{photo_id}?dl=1"})],
            "stream_side_effect": [self._stream_cm()],
        }

    @override_settings(UNSPLASH_ACCESS_KEY="")
    def test_returns_503_without_api_key(self):
        response = self._post([_unsplash_photo()])
        self.assertEqual(response.status_code, 503)

    def test_pending_path_creates_asset_and_session_entry(self):
        patcher, _client = self._patch_client(**self._ok_photo_mocks())
        with patcher:
            response = self._post([_unsplash_photo()])
        self.assertEqual(response.status_code, 200)

        asset = MediaAsset.objects.get(workspace=self.workspace)
        self.assertEqual(asset.source, "unsplash")
        self.assertEqual(asset.organization, self.org)
        self.assertEqual(asset.source_url, "https://unsplash.com/photos/abc123")
        self.assertEqual(asset.attribution, "Photo by Brigitte Tohm on Unsplash")
        self.assertEqual(asset.media_type, MediaAsset.MediaType.IMAGE)

        session_key = f"pending_media_{self.workspace.id}"
        self.assertEqual(self.client.session.get(session_key), [str(asset.id)])

        data = response.json()
        self.assertIn("html", data)
        self.assertEqual(data["assets"], [{"id": str(asset.id), "url": asset.file.url}])
        self.assertEqual(data["failed"], 0)

    def test_post_path_attaches_at_next_position(self):
        patcher, _client = self._patch_client(**self._ok_photo_mocks())
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="hi")
        url = reverse(
            "composer:unsplash_import_post",
            kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
        )
        with patcher:
            response = self._post([_unsplash_photo()], url=url)
        self.assertEqual(response.status_code, 200)

        attachment = PostMedia.objects.get(post=post)
        self.assertEqual(attachment.position, 1)
        self.assertEqual(attachment.media_asset.source, "unsplash")

    def test_rejects_non_unsplash_download_location(self):
        photo = _unsplash_photo()
        photo["download_location"] = "https://evil.example.com/steal"
        patcher, client = self._patch_client(get_side_effect=[])
        with patcher:
            response = self._post([photo])
        self.assertEqual(response.status_code, 502)
        self.assertEqual(MediaAsset.objects.count(), 0)
        client.get.assert_not_called()

    def test_null_url_fields_fail_gracefully(self):
        # A crafted/stale payload with null (non-string) URL fields must be
        # counted as failed, not raise AttributeError -> 500.
        photo = _unsplash_photo()
        photo["download_location"] = None
        photo["full"] = None
        patcher, client = self._patch_client(get_side_effect=[])
        with patcher:
            response = self._post([photo])
        self.assertEqual(response.status_code, 502)
        self.assertEqual(MediaAsset.objects.count(), 0)
        client.get.assert_not_called()

    def test_null_attribution_fields_do_not_crash_save(self):
        # download_location is valid, but null photo_url/alt/photographer must
        # not pass None into the non-null model fields (IntegrityError -> 500).
        photo = _unsplash_photo()
        photo["photo_url"] = None
        photo["photographer"] = None
        photo["alt"] = None
        patcher, _client = self._patch_client(**self._ok_photo_mocks())
        with patcher:
            response = self._post([photo])
        self.assertEqual(response.status_code, 200)
        asset = MediaAsset.objects.get(workspace=self.workspace)
        self.assertEqual(asset.source_url, "")
        self.assertEqual(asset.alt_text, "")
        self.assertEqual(asset.attribution, "Photo by Unknown on Unsplash")

    def test_rejects_non_unsplash_image_url(self):
        # Registration succeeds but points the byte download somewhere else.
        photo = _unsplash_photo()
        photo["full"] = "https://evil.example.com/img.jpg"
        patcher, client = self._patch_client(
            get_side_effect=[_response(json_data={"url": "https://evil.example.com/img.jpg"})],
        )
        with patcher:
            response = self._post([photo])
        self.assertEqual(response.status_code, 502)
        self.assertEqual(MediaAsset.objects.count(), 0)
        # The allowlist must reject before any image bytes are streamed.
        client.stream.assert_not_called()

    def test_oversized_image_rejected_by_declared_length(self):
        patcher, _client = self._patch_client(
            get_side_effect=[_response(json_data={"url": "https://images.unsplash.com/abc?dl=1"})],
            stream_side_effect=[self._stream_cm(content_length=str(20 * 1024 * 1024))],
        )
        with patcher:
            response = self._post([_unsplash_photo()])
        self.assertEqual(response.status_code, 502)
        self.assertEqual(MediaAsset.objects.count(), 0)

    def test_quota_exceeded_returns_413(self):
        from apps.media_library.quotas import StorageQuotaExceededError

        patcher, _client = self._patch_client(**self._ok_photo_mocks())
        with (
            patcher,
            patch(
                "apps.media_library.quotas.enforce_storage_quota",
                side_effect=StorageQuotaExceededError(used=10, limit=10, attempted=10),
            ),
        ):
            response = self._post([_unsplash_photo()])
        self.assertEqual(response.status_code, 413)
        self.assertEqual(MediaAsset.objects.count(), 0)

    def test_caps_photos_per_import(self):
        response = self._post([_unsplash_photo(f"p{i}") for i in range(11)])
        self.assertEqual(response.status_code, 400)


class TiktokCoverTimestampTests(ComposerTestCase):
    """save_post persists tiktok_video_cover_timestamp_ms_<acc> into platform_extra."""

    def setUp(self):
        super().setUp()
        self.account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="tiktok",
            account_platform_id="tiktok-1",
            account_name="tiktok-1",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.save_url = reverse("composer:save_post", kwargs={"workspace_id": self.workspace.id})

    def _save(self, cover_value=None):
        payload = {
            "action": "save_draft",
            "caption": "body",
            "selected_accounts": str(self.account.id),
            f"tiktok_privacy_level_{self.account.id}": "PUBLIC_TO_EVERYONE",
        }
        if cover_value is not None:
            payload[f"tiktok_video_cover_timestamp_ms_{self.account.id}"] = cover_value
        response = self.client.post(self.save_url, data=payload)
        self.assertIn(response.status_code, (200, 204, 302))
        return PlatformPost.objects.get(social_account=self.account)

    def test_cover_timestamp_persisted_as_int(self):
        pp = self._save("12500")
        self.assertEqual(pp.platform_extra["video_cover_timestamp_ms"], 12500)

    def test_blank_cover_timestamp_omitted(self):
        pp = self._save("")
        self.assertNotIn("video_cover_timestamp_ms", pp.platform_extra)

    def test_invalid_cover_timestamp_omitted(self):
        pp = self._save("-5")
        self.assertNotIn("video_cover_timestamp_ms", pp.platform_extra)

    def test_unicode_digit_cover_timestamp_does_not_500(self):
        # "²" passes str.isdigit() but int() rejects it; parsing must not crash.
        pp = self._save("²")
        self.assertNotIn("video_cover_timestamp_ms", pp.platform_extra)

    def test_zero_cover_timestamp_persisted(self):
        pp = self._save("0")
        self.assertEqual(pp.platform_extra["video_cover_timestamp_ms"], 0)
