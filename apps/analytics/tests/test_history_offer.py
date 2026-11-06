"""The offer to import a channel's own posts, and the import behind it.

The panel above the post table: what it shows, who may act on it, and that
reading it never writes anything.

Asserts on HTTP status, context and database state only. No assertion on any
rendered sentence: the project defaults to pt-BR and copy is not a contract.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.analytics.history_import import ImportResult
from apps.composer.models import PlatformPost
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def setup(db, client):
    user = User.objects.create_user(
        email="owner@example.com",
        password="pw",
        tos_accepted_at=timezone.now(),
    )
    org = Organization.objects.create(name="Org")
    workspace = Workspace.objects.create(organization=org, name="WS")
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="ig-1",
        account_name="Acct",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    client.force_login(user)
    return {"user": user, "org": org, "workspace": workspace, "account": account, "client": client}


def _offer_url(s):
    return reverse(
        "analytics:history_offer",
        kwargs={"workspace_id": s["workspace"].id, "account_id": s["account"].id},
    )


def _import_url(s):
    return reverse(
        "analytics:history_import",
        kwargs={"workspace_id": s["workspace"].id, "account_id": s["account"].id},
    )


@pytest.mark.django_db
def test_the_offer_reports_what_could_be_imported(setup):
    with patch("apps.analytics.views.history_import.importable_count", return_value=7):
        response = setup["client"].get(_offer_url(setup))

    assert response.status_code == 200
    assert response.context["importable"] == 7


@pytest.mark.django_db
def test_the_offer_writes_nothing(setup):
    """Reading is a GET; it must not create rows as a side effect."""
    with patch("apps.analytics.views.history_import.importable_count", return_value=3):
        setup["client"].get(_offer_url(setup))

    assert PlatformPost.objects.count() == 0


@pytest.mark.django_db
def test_an_owner_may_import(setup):
    with patch("apps.analytics.views.history_import.importable_count", return_value=2):
        response = setup["client"].get(_offer_url(setup))

    assert response.context["can_import"] is True


@pytest.mark.django_db
def test_a_member_without_the_channel_permission_may_not_import(setup):
    """Importing spends the channel's API quota, so it takes more than reading."""
    membership = WorkspaceMembership.objects.get(user=setup["user"], workspace=setup["workspace"])
    membership.workspace_role = WorkspaceMembership.WorkspaceRole.VIEWER
    membership.save(update_fields=["workspace_role"])

    with patch("apps.analytics.views.history_import.importable_count", return_value=2):
        response = setup["client"].get(_offer_url(setup))

    assert response.context["can_import"] is False


@pytest.mark.django_db
def test_posting_as_a_viewer_is_refused(setup):
    """The hidden button is not the control — the view re-asks."""
    membership = WorkspaceMembership.objects.get(user=setup["user"], workspace=setup["workspace"])
    membership.workspace_role = WorkspaceMembership.WorkspaceRole.VIEWER
    membership.save(update_fields=["workspace_role"])

    response = setup["client"].post(_import_url(setup))

    assert response.status_code == 403


@pytest.mark.django_db
def test_importing_reports_what_came_in(setup):
    with patch(
        "apps.analytics.views.history_import.import_history",
        return_value=ImportResult(imported=4, skipped=1, available=5),
    ):
        response = setup["client"].post(_import_url(setup))

    assert response.status_code == 200
    assert response.context["result"].imported == 4


@pytest.mark.django_db
def test_the_offer_is_not_reachable_by_GET_for_importing(setup):
    """The import endpoint writes, so it refuses a GET."""
    assert setup["client"].get(_import_url(setup)).status_code == 405


@pytest.mark.django_db
def test_another_workspace_account_is_a_404(setup):
    other_ws = Workspace.objects.create(organization=setup["org"], name="Other")
    theirs = SocialAccount.objects.create(
        workspace=other_ws,
        platform="instagram",
        account_platform_id="ig-9",
        account_name="Theirs",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )

    url = reverse(
        "analytics:history_offer",
        kwargs={"workspace_id": setup["workspace"].id, "account_id": theirs.id},
    )

    assert setup["client"].get(url).status_code == 404


@pytest.mark.django_db
def test_a_non_member_cannot_read_the_offer(setup):
    outsider = User.objects.create_user(
        email="outsider@example.com",
        password="pw",
        tos_accepted_at=timezone.now(),
    )
    setup["client"].force_login(outsider)

    assert setup["client"].get(_offer_url(setup)).status_code == 403
