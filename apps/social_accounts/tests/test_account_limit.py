"""A workspace may connect only so many accounts of one platform.

The cap guards new connections only. Counting every account instead would make
"Reconnect" impossible at exactly the moment it matters — and would strand every
workspace that was already past the cap before it existed.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.social_accounts.models import SocialAccount
from apps.social_accounts.views import AccountLimitReached, _create_or_update_account


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Limit WS", organization=organization)


@pytest.fixture
def other_workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Other Limit WS", organization=organization)


@pytest.fixture
def manager_setup(db, user, organization, workspace):
    from apps.members.models import OrgMembership, WorkspaceMembership

    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="manager")
    return user


@pytest.fixture
def authenticated_client(client, user, manager_setup):
    client.force_login(user)
    return client


@pytest.fixture(autouse=True)
def _no_webhook_calls():
    """The subscribe task is a network round trip and irrelevant to the cap."""
    with patch("apps.social_accounts.views.subscribe_account_webhooks_task"):
        yield


def _profile(platform_id, name="A Page"):
    return SimpleNamespace(
        platform_id=platform_id,
        name=name,
        handle="apage",
        avatar_url="",
        follower_count=0,
    )


def _existing(workspace, platform, platform_id):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=platform_id,
        account_name=f"{platform} {platform_id}",
        oauth_access_token="stored-token",
    )


def _connect(workspace, platform, platform_id):
    return _create_or_update_account(
        workspace_id=workspace.id,
        platform=platform,
        profile=_profile(platform_id),
        access_token="fresh-token",
    )


@pytest.mark.django_db
class TestNewConnections:
    def test_connecting_up_to_the_cap_succeeds(self, workspace):
        for index in range(3):
            _connect(workspace, "facebook", f"page-{index}")

        assert SocialAccount.objects.filter(workspace=workspace, platform="facebook").count() == 3

    def test_the_connection_past_the_cap_is_refused(self, workspace):
        for index in range(3):
            _existing(workspace, "facebook", f"page-{index}")

        with pytest.raises(AccountLimitReached):
            _connect(workspace, "facebook", "page-4")

        assert SocialAccount.objects.filter(workspace=workspace, platform="facebook").count() == 3

    @override_settings(MAX_ACCOUNTS_PER_PLATFORM=1)
    def test_the_cap_comes_from_settings(self, workspace):
        _existing(workspace, "facebook", "page-0")

        with pytest.raises(AccountLimitReached):
            _connect(workspace, "facebook", "page-1")

    @override_settings(MAX_ACCOUNTS_PER_PLATFORM=5)
    def test_a_raised_cap_admits_more(self, workspace):
        for index in range(4):
            _existing(workspace, "facebook", f"page-{index}")

        assert _connect(workspace, "facebook", "page-4").pk is not None


@pytest.mark.django_db
class TestReconnection:
    def test_reconnecting_at_the_cap_is_allowed(self, workspace):
        """Otherwise the Reconnect button is dead for every workspace at the cap."""
        for index in range(3):
            _existing(workspace, "facebook", f"page-{index}")

        account = _connect(workspace, "facebook", "page-1")

        assert account.oauth_access_token == "fresh-token"
        assert SocialAccount.objects.filter(workspace=workspace, platform="facebook").count() == 3

    def test_reconnecting_above_the_cap_is_allowed(self, workspace):
        """The cap is not retroactive: workspaces already over it keep working."""
        for index in range(5):
            _existing(workspace, "facebook", f"page-{index}")

        account = _connect(workspace, "facebook", "page-3")

        assert account.oauth_access_token == "fresh-token"
        assert SocialAccount.objects.filter(workspace=workspace, platform="facebook").count() == 5

    def test_a_workspace_over_the_cap_still_cannot_add_a_new_one(self, workspace):
        for index in range(5):
            _existing(workspace, "facebook", f"page-{index}")

        with pytest.raises(AccountLimitReached):
            _connect(workspace, "facebook", "page-new")

        assert SocialAccount.objects.filter(workspace=workspace, platform="facebook").count() == 5


@pytest.mark.django_db
class TestScope:
    def test_the_cap_is_per_platform(self, workspace):
        """A full Facebook shelf must leave Instagram's three untouched."""
        for index in range(3):
            _existing(workspace, "facebook", f"page-{index}")

        for index in range(3):
            _connect(workspace, "instagram", f"ig-{index}")

        assert SocialAccount.objects.filter(workspace=workspace, platform="instagram").count() == 3

        with pytest.raises(AccountLimitReached):
            _connect(workspace, "instagram", "ig-4")

    def test_the_cap_is_per_workspace(self, workspace, other_workspace):
        for index in range(3):
            _existing(workspace, "facebook", f"page-{index}")

        account = _connect(other_workspace, "facebook", "page-0")

        assert account.workspace_id == other_workspace.id

    def test_one_workspaces_accounts_do_not_fill_anothers_quota(self, workspace, other_workspace):
        for index in range(3):
            _existing(other_workspace, "facebook", f"page-{index}")

        for index in range(3):
            _connect(workspace, "facebook", f"page-{index}")

        assert SocialAccount.objects.filter(workspace=workspace, platform="facebook").count() == 3


@pytest.mark.django_db
class TestBatchSelection:
    """Picking several Meta Pages at once can cross the cap mid-selection."""

    def _post_pages(self, client, workspace, page_ids, selected=None):
        session = client.session
        session["oauth_page_select"] = {
            "workspace_id": str(workspace.id),
            "platform": "facebook",
            "user_tokens": {"access_token": "user-token", "refresh_token": None},
            "pages": [
                {"id": page_id, "name": f"Page {page_id}", "access_token": f"token-{page_id}"}
                for page_id in page_ids
            ],
        }
        session.save()
        return client.post(
            reverse("social_accounts:select_account"),
            {"selected_pages": selected if selected is not None else page_ids},
        )

    def test_the_batch_connects_up_to_the_cap_and_drops_the_rest(self, authenticated_client, workspace):
        response = self._post_pages(authenticated_client, workspace, ["p1", "p2", "p3", "p4", "p5"])

        assert response.status_code == 302
        connected = set(
            SocialAccount.objects.filter(workspace=workspace, platform="facebook").values_list(
                "account_platform_id", flat=True
            )
        )
        assert len(connected) == 3
        assert connected == {"p1", "p2", "p3"}

    def test_a_refused_page_does_not_abort_the_ones_that_fit(self, authenticated_client, workspace):
        """The failure arrives part-way through, so the earlier rows are already
        written — they must survive the refusal rather than be rolled back."""
        _existing(workspace, "facebook", "already")

        response = self._post_pages(authenticated_client, workspace, ["p1", "p2", "p3"])

        assert response.status_code == 302
        assert SocialAccount.objects.filter(workspace=workspace, platform="facebook").count() == 3
        assert SocialAccount.objects.filter(workspace=workspace, account_platform_id="p1").exists()
        assert SocialAccount.objects.filter(workspace=workspace, account_platform_id="p2").exists()
        assert not SocialAccount.objects.filter(workspace=workspace, account_platform_id="p3").exists()

    def test_a_batch_at_the_cap_still_refreshes_the_accounts_it_already_has(
        self, authenticated_client, workspace
    ):
        """Re-ticking connected rows is the picker's own reconnect path."""
        for page_id in ("p1", "p2", "p3"):
            _existing(workspace, "facebook", page_id)

        response = self._post_pages(authenticated_client, workspace, ["p1", "p2", "p3"])

        assert response.status_code == 302
        refreshed = SocialAccount.objects.filter(workspace=workspace, platform="facebook")
        assert refreshed.count() == 3
        assert all(account.oauth_access_token == f"token-{account.account_platform_id}" for account in refreshed)
