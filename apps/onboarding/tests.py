"""Tests for the connection-link OAuth flow — PKCE round-trip for TikTok.

The connection-link flow is a second, public OAuth entry point (separate from
social_accounts.connect_platform). It must apply PKCE for providers that need
it (TikTok), otherwise the authorize URL lacks code_challenge and TikTok
rejects it with errCode=10007.
"""

from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.onboarding.models import ConnectionLink
from apps.onboarding.views import CONNECTION_LINK_OAUTH_SESSION_KEY, _sign_connection_link_state
from providers.types import AccountProfile, OAuthTokens


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Test WS", organization=organization)


@pytest.fixture
def connection_link(db, workspace):
    return ConnectionLink.objects.create(
        workspace=workspace,
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("business_login", [False, True])
def test_instagram_connection_link_uses_dm_scope_flag(client, connection_link, settings, flag, business_login):
    settings.PLATFORM_INSTAGRAM_FACEBOOK_DM_SCOPES_ENABLED = flag
    credentials = {"app_id": "ID", "app_secret": "SECRET"}
    if business_login:
        credentials["config_id"] = "config-1"
    settings.PLATFORM_CREDENTIALS_FROM_ENV = {"instagram": credentials}
    url = reverse("onboarding:connection_oauth_start", kwargs={"token": connection_link.token})
    response = client.post(url, {"platform": "instagram"})
    assert response.status_code == 302
    query = parse_qs(urlparse(response.url).query)
    assert query["client_id"] == ["ID"]
    assert query["response_type"] == ["code"]
    if business_login:
        assert query["config_id"] == ["config-1"]
        assert "scope" not in query
    else:
        scopes = query["scope"][0].split(",")
        assert ("instagram_manage_messages" in scopes) is flag
        assert {"pages_manage_metadata", "instagram_content_publish", "instagram_manage_comments"} <= set(scopes)


@pytest.mark.django_db
class TestConnectionLinkPkce:
    def test_oauth_start_generates_and_forwards_verifier(self, client, workspace, connection_link):
        from apps.credentials.models import PlatformCredential

        PlatformCredential.objects.create(
            organization=workspace.organization,
            platform="tiktok",
            credentials={"client_key": "k", "client_secret": "s"},
            is_configured=True,
        )

        mock_provider = MagicMock()
        mock_provider.uses_pkce = True
        mock_provider.get_auth_url.return_value = "https://www.tiktok.com/v2/auth/authorize/?ok=1"

        url = reverse("onboarding:connection_oauth_start", kwargs={"token": connection_link.token})
        with patch("apps.onboarding.views._get_provider_for_platform", return_value=mock_provider):
            response = client.post(url, {"platform": "tiktok"})

        assert response.status_code == 302
        verifier = client.session[CONNECTION_LINK_OAUTH_SESSION_KEY]["code_verifier"]
        assert verifier  # non-empty
        _, kwargs = mock_provider.get_auth_url.call_args
        assert kwargs["code_verifier"] == verifier

    def test_oauth_callback_replays_verifier(self, client, workspace, connection_link):
        nonce = "nonce-xyz"
        verifier = "stored-connection-verifier"
        state = _sign_connection_link_state(workspace.id, "tiktok", connection_link.token, nonce)
        session = client.session
        session[CONNECTION_LINK_OAUTH_SESSION_KEY] = {
            "nonce": nonce,
            "workspace_id": str(workspace.id),
            "platform": "tiktok",
            "token": connection_link.token,
            "code_verifier": verifier,
        }
        session.save()

        mock_provider = MagicMock()
        mock_provider.exchange_code.return_value = OAuthTokens(access_token="tok", refresh_token="r", expires_in=3600)
        mock_provider.get_profile.return_value = AccountProfile(platform_id="open-1", name="TT")

        url = reverse("onboarding:oauth_callback", kwargs={"platform": "social1"})
        with patch("apps.onboarding.views._get_provider_for_platform", return_value=mock_provider):
            response = client.get(url, {"code": "auth-code", "state": state})

        assert response.status_code == 302
        mock_provider.exchange_code.assert_called_once()
        _, kwargs = mock_provider.exchange_code.call_args
        assert kwargs["code_verifier"] == verifier


@pytest.mark.django_db
class TestConnectionLinkPageTokens:
    """A Page must be driven by its own token, and a Page we had to skip must
    not leave the client on a success page for an account never connected."""

    def _callback(self, client, workspace, connection_link, pages):
        nonce = "nonce-pages"
        state = _sign_connection_link_state(workspace.id, "facebook", connection_link.token, nonce)
        session = client.session
        session[CONNECTION_LINK_OAUTH_SESSION_KEY] = {
            "nonce": nonce,
            "workspace_id": str(workspace.id),
            "platform": "facebook",
            "token": connection_link.token,
        }
        session.save()

        provider = MagicMock()
        provider.exchange_code.return_value = OAuthTokens(access_token="USER-TOKEN", refresh_token="", expires_in=None)
        provider.get_profile.return_value = AccountProfile(platform_id="me-1", name="Owner")
        provider.get_user_pages.return_value = pages

        url = reverse("onboarding:oauth_callback", kwargs={"platform": "facebook"})
        with (
            patch("apps.onboarding.views._get_provider_for_platform", return_value=provider),
            patch("apps.social_accounts.views.subscribe_account_webhooks_task"),
        ):
            response = client.get(url, {"code": "auth-code", "state": state})
        return response

    def test_a_page_is_connected_with_its_own_token(self, client, workspace, connection_link):
        from apps.social_accounts.models import SocialAccount

        response = self._callback(
            client,
            workspace,
            connection_link,
            [{"id": "page-1", "name": "Page One", "access_token": "PAGE-TOKEN"}],
        )

        assert response.status_code == 302
        account = SocialAccount.objects.get(account_platform_id="page-1")
        assert account.oauth_access_token == "PAGE-TOKEN"
        assert "connection_link_error" not in client.session

    def test_a_page_without_its_own_token_is_reported_not_silently_dropped(self, client, workspace, connection_link):
        from apps.social_accounts.models import SocialAccount

        response = self._callback(
            client,
            workspace,
            connection_link,
            [
                {"id": "page-1", "name": "Page One", "access_token": "PAGE-TOKEN"},
                {"id": "page-2", "name": "Page Two"},
            ],
        )

        assert response.status_code == 302
        assert SocialAccount.objects.filter(account_platform_id="page-1").exists()
        # Never connected with the user token as a stand-in.
        assert not SocialAccount.objects.filter(account_platform_id="page-2").exists()
        error = client.session["connection_link_error"]
        assert "Page Two" in error
        assert "Page One" not in error

    # Five channels is what makes "separate credentials per channel" worth
    # asserting, and the per-platform cap would otherwise refuse the last two —
    # turning this into a test of the cap rather than of the credentials.
    @override_settings(MAX_ACCOUNTS_PER_PLATFORM=5)
    @pytest.mark.parametrize("promoted,expiry_seconds", [(True, 5184000), (False, 3600)])
    def test_instagram_link_preserves_separate_credentials_for_all_channels(
        self, client, workspace, connection_link, promoted, expiry_seconds
    ):
        from apps.social_accounts.models import SocialAccount

        started = timezone.now()
        nonce = "nonce-instagram"
        state = _sign_connection_link_state(workspace.id, "instagram", connection_link.token, nonce)
        session = client.session
        session[CONNECTION_LINK_OAUTH_SESSION_KEY] = {
            "nonce": nonce,
            "workspace_id": str(workspace.id),
            "platform": "instagram",
            "token": connection_link.token,
        }
        session.save()
        provider = MagicMock()
        provider.get_user_pages.return_value = [
            {"id": f"ig-{i}", "name": f"Channel {i}", "access_token": f"page-{i}"} for i in range(5)
        ]
        user_tokens = OAuthTokens(access_token="user", expires_in=expiry_seconds)
        with (
            patch("apps.onboarding.views._get_provider_for_platform", return_value=provider),
            patch("apps.onboarding.views.promote_meta_user_token", return_value=(user_tokens, promoted)),
            patch("apps.social_accounts.views.subscribe_account_webhooks_task"),
        ):
            response = client.get(
                reverse("onboarding:oauth_callback", kwargs={"platform": "instagram"}),
                {
                    "code": "code",
                    "state": state,
                },
            )
        assert response.status_code == 302
        assert SocialAccount.objects.filter(workspace=workspace).count() == 5
        for i in range(5):
            account = SocialAccount.objects.get(workspace=workspace, account_platform_id=f"ig-{i}")
            assert account.oauth_access_token == f"page-{i}"
            assert account.oauth_facebook_user_access_token == "user"
            assert started + timezone.timedelta(seconds=expiry_seconds) <= account.facebook_user_token_expires_at
            assert account.facebook_user_token_expires_at <= timezone.now() + timezone.timedelta(seconds=expiry_seconds)
            assert (account.token_expires_at is None) == promoted
            assert account.connection_status == "connected"
