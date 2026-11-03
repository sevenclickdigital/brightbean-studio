"""REST and MCP refuse to schedule a post that is certain to fail at publish.

An agent scheduling an image to TikTok or YouTube, or any pin to Pinterest
(the API cannot set the board a pin needs), used to get a 201 and a post that
failed four retries later. Both surfaces schedule through the same composer
services, so both must now refuse up front, and ``requires_video`` on the
account lets an agent check before it tries.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.composer.models import Post
from apps.mcp.protocol import INVALID_PARAMS
from apps.media_library.models import MediaAsset
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount

MCP_URL = "/api/v1/mcp/"


@pytest.fixture(autouse=True)
def _english(settings):
    """The blocker messages are translated and the project defaults to pt-br.

    These tests assert on the rule, not on the catalogue, so pin English.
    """
    settings.LANGUAGE_CODE = "en-us"


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="agent-owner@example.com",
        password="testpass123",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(db):
    from apps.organizations.models import Organization

    return Organization.objects.create(name="Publishability Org")


@pytest.fixture
def workspace(db, organization, user):
    from apps.workspaces.models import Workspace

    workspace = Workspace.objects.create(name="Publishability WS", organization=organization)
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return workspace


def _account(workspace, platform):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-1",
        account_name=platform,
        connection_status="connected",
    )


@pytest.fixture
def tiktok(workspace):
    return _account(workspace, "tiktok")


@pytest.fixture
def pinterest(workspace):
    return _account(workspace, "pinterest")


@pytest.fixture
def linkedin(workspace):
    return _account(workspace, "linkedin_personal")


@pytest.fixture
def client(user, workspace, tiktok, pinterest, linkedin):
    key = services.issue_api_key(
        workspace=workspace,
        social_accounts=[tiktok, pinterest, linkedin],
        issued_by=user,
        name="publishability",
        permissions=list(PERMISSION_KEYS),
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


def _asset(workspace, media_type):
    return MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        file=f"test/{media_type}",
        filename=media_type,
        media_type=media_type,
        mime_type="video/mp4" if media_type == MediaAsset.MediaType.VIDEO else "image/jpeg",
    )


def _when():
    return (timezone.now() + timedelta(hours=2)).isoformat()


def _create(client, account, *, action="schedule", media=()):
    body = {
        "social_account_id": str(account.id),
        "caption": "hi",
        "action": action,
        "media_asset_ids": [str(asset.id) for asset in media],
    }
    if action == "schedule":
        body["scheduled_at"] = _when()
    return client.post("/api/v1/posts/", data=json.dumps(body), content_type="application/json")


def _mcp(client, tool, arguments):
    rpc = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": tool, "arguments": arguments}, "id": 1}
    return client.post(MCP_URL, data=json.dumps(rpc), content_type="application/json").json()


@pytest.mark.django_db
class TestRest:
    def test_scheduling_an_image_to_tiktok_is_refused(self, client, workspace, tiktok):
        response = _create(client, tiktok, media=[_asset(workspace, MediaAsset.MediaType.IMAGE)])

        assert response.status_code == 422, response.content
        assert "TikTok can only publish videos" in response.json()["detail"]
        assert not Post.objects.exists()

    def test_scheduling_a_video_to_tiktok_is_accepted(self, client, workspace, tiktok):
        response = _create(client, tiktok, media=[_asset(workspace, MediaAsset.MediaType.VIDEO)])

        assert response.status_code == 201, response.content

    def test_scheduling_a_draft_without_a_video_is_refused(self, client, tiktok):
        draft = _create(client, tiktok, action="draft")
        assert draft.status_code == 201, draft.content

        response = client.post(
            f"/api/v1/posts/{draft.json()['id']}/schedule",
            data=json.dumps({"scheduled_at": _when()}),
            content_type="application/json",
        )

        assert response.status_code == 422, response.content
        assert "TikTok can only publish videos" in response.json()["detail"]

    def test_scheduling_a_pinterest_pin_is_refused(self, client, workspace, pinterest):
        response = _create(client, pinterest, media=[_asset(workspace, MediaAsset.MediaType.IMAGE)])

        assert response.status_code == 422, response.content
        assert "Pinterest needs a board" in response.json()["detail"]

    def test_a_pinterest_draft_is_still_accepted(self, client, workspace, pinterest):
        response = _create(client, pinterest, action="draft", media=[_asset(workspace, MediaAsset.MediaType.IMAGE)])

        assert response.status_code == 201, response.content

    def test_other_platforms_are_unaffected(self, client, linkedin):
        assert _create(client, linkedin).status_code == 201

    def test_accounts_say_which_platforms_need_video(self, client, tiktok, linkedin):
        accounts = {a["id"]: a for a in client.get("/api/v1/accounts/").json()["accounts"]}

        assert accounts[str(tiktok.id)]["requires_video"] is True
        assert accounts[str(tiktok.id)]["requires_media"] is True
        assert accounts[str(linkedin.id)]["requires_video"] is False
        assert accounts[str(linkedin.id)]["requires_media"] is False

    def _patch_media(self, client, post_id, assets):
        return client.patch(
            f"/api/v1/posts/{post_id}",
            data=json.dumps({"media_asset_ids": [str(asset.id) for asset in assets]}),
            content_type="application/json",
        )

    def test_swapping_a_scheduled_video_for_an_image_is_refused(self, client, workspace, tiktok):
        """Scheduling checked the post; PATCH must not undo that afterwards."""
        video = _asset(workspace, MediaAsset.MediaType.VIDEO)
        post_id = _create(client, tiktok, media=[video]).json()["id"]

        response = self._patch_media(client, post_id, [_asset(workspace, MediaAsset.MediaType.IMAGE)])

        assert response.status_code == 422, response.content
        assert "scheduled to TikTok, which needs a video" in response.json()["detail"]
        assert list(Post.objects.get(id=post_id).media_attachments.values_list("media_asset", flat=True)) == [video.id]

    def test_swapping_one_video_for_another_is_allowed(self, client, workspace, tiktok):
        post_id = _create(client, tiktok, media=[_asset(workspace, MediaAsset.MediaType.VIDEO)]).json()["id"]

        response = self._patch_media(client, post_id, [_asset(workspace, MediaAsset.MediaType.VIDEO)])

        assert response.status_code == 200, response.content

    def test_scheduling_instagram_with_no_media_is_refused(self, workspace, user):
        instagram = _account(workspace, "instagram")
        key = services.issue_api_key(
            workspace=workspace,
            social_accounts=[instagram],
            issued_by=user,
            name="ig",
            permissions=list(PERMISSION_KEYS),
        )
        ig_client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")

        response = _create(ig_client, instagram)

        assert response.status_code == 422, response.content
        assert "Instagram needs an image or a video" in response.json()["detail"]
        assert ig_client.get("/api/v1/accounts/").json()["accounts"][0]["requires_media"] is True

    def test_a_draft_may_drop_its_video(self, client, workspace, tiktok):
        draft = _create(client, tiktok, action="draft", media=[_asset(workspace, MediaAsset.MediaType.VIDEO)])

        response = self._patch_media(client, draft.json()["id"], [_asset(workspace, MediaAsset.MediaType.IMAGE)])

        assert response.status_code == 200, response.content


@pytest.mark.django_db
class TestMcp:
    def test_schedule_post_refuses_tiktok_without_a_video(self, client, tiktok):
        body = _mcp(
            client,
            "schedule_post",
            {"social_account_id": str(tiktok.id), "caption": "hi", "scheduled_at": _when()},
        )

        assert body["error"]["code"] == INVALID_PARAMS
        assert "TikTok can only publish videos" in body["error"]["message"]
        assert not Post.objects.exists()

    def test_schedule_draft_refuses_tiktok_without_a_video(self, client, tiktok):
        draft = _mcp(client, "create_draft", {"social_account_id": str(tiktok.id), "caption": "hi"})
        post_id = json.loads(draft["result"]["content"][0]["text"])["id"]

        body = _mcp(client, "schedule_draft", {"post_id": post_id, "scheduled_at": _when()})

        assert body["error"]["code"] == INVALID_PARAMS
        assert "TikTok can only publish videos" in body["error"]["message"]

    def test_list_accounts_reports_requires_video(self, client, tiktok):
        body = _mcp(client, "list_accounts", {})
        accounts = json.loads(body["result"]["content"][0]["text"])["accounts"]

        assert {a["id"]: a["requires_video"] for a in accounts}[str(tiktok.id)] is True
