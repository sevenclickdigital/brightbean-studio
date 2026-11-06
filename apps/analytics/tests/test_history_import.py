"""Importing the posts a channel already had.

Analytics reads ``PlatformPost``, which only held what this product
published — so a connected account with a year of content showed an empty
post table. These pin the import that closes that gap: what it creates, what
it refuses to create twice, and that it never invents engagement numbers.

Asserts on database state and return values only. No assertion on any
rendered sentence: the project defaults to pt-BR and copy is not a contract.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.analytics.history_import import IMPORT_MARKER, import_history, importable_count
from apps.composer.models import PlatformPost, Post
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.types import ExistingPost


class FakeProvider:
    """Stands in for a real provider's history read.

    A fake rather than a mocked HTTP layer: what these tests are about is what
    the importer does with a list of posts, and the walk that produces that
    list is covered in the provider tests.
    """

    def __init__(self, posts=None, raises=None):
        self._posts = posts or []
        self._raises = raises
        self.calls = []

    def list_existing_posts(self, access_token, *, limit=100):
        self.calls.append(limit)
        if self._raises is not None:
            raise self._raises
        return self._posts[:limit]


def _existing(post_id, caption="hello", days_ago=1):
    return ExistingPost(
        platform_post_id=post_id,
        caption=caption,
        published_at=timezone.now() - timedelta(days=days_ago),
        permalink=f"https://example.test/{post_id}",
        media_url=f"https://cdn.test/{post_id}.jpg",
        thumbnail_url=f"https://cdn.test/{post_id}.jpg",
        media_kind="image",
    )


@pytest.fixture
def account(db):
    org = Organization.objects.create(name="Org")
    workspace = Workspace.objects.create(organization=org, name="WS")
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="ig-1",
        account_name="Acct",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def _run(account, provider):
    return import_history(account, provider=provider, access_token="tok")


@pytest.mark.django_db
def test_an_existing_post_becomes_a_platform_post(account):
    result = _run(account, FakeProvider([_existing("p1", caption="first")]))

    assert result.imported == 1
    row = PlatformPost.objects.get(social_account=account, platform_post_id="p1")
    assert row.status == PlatformPost.Status.PUBLISHED
    assert row.platform_specific_caption == "first"
    assert row.published_at is not None


@pytest.mark.django_db
def test_an_imported_post_is_marked_as_imported(account):
    """The composer has to be able to tell these from posts it sent."""
    _run(account, FakeProvider([_existing("p1")]))

    row = PlatformPost.objects.get(platform_post_id="p1")
    assert row.platform_extra[IMPORT_MARKER] is True
    assert row.platform_extra["permalink"]


@pytest.mark.django_db
def test_running_twice_imports_nothing_the_second_time(account):
    provider = FakeProvider([_existing("p1"), _existing("p2")])

    first = _run(account, provider)
    second = _run(account, provider)

    assert first.imported == 2
    assert second.imported == 0
    assert second.skipped == 2
    assert PlatformPost.objects.filter(social_account=account).count() == 2


@pytest.mark.django_db
def test_a_post_this_product_published_is_not_imported_again(account):
    """Matched by platform_post_id — the same post from the other direction."""
    post = Post.objects.create(workspace=account.workspace, caption="ours")
    PlatformPost.objects.create(
        post=post,
        social_account=account,
        status=PlatformPost.Status.PUBLISHED,
        platform_post_id="p1",
        published_at=timezone.now(),
    )

    result = _run(account, FakeProvider([_existing("p1"), _existing("p2")]))

    assert result.imported == 1
    assert result.skipped == 1
    assert PlatformPost.objects.filter(social_account=account).count() == 2


@pytest.mark.django_db
def test_another_accounts_post_with_the_same_id_is_still_imported(account):
    """Ids are only unique per account — two channels can both have "1"."""
    other = SocialAccount.objects.create(
        workspace=account.workspace,
        platform="facebook",
        account_platform_id="fb-1",
        account_name="Other",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    post = Post.objects.create(workspace=account.workspace, caption="theirs")
    PlatformPost.objects.create(
        post=post,
        social_account=other,
        status=PlatformPost.Status.PUBLISHED,
        platform_post_id="p1",
        published_at=timezone.now(),
    )

    result = _run(account, FakeProvider([_existing("p1")]))

    assert result.imported == 1


@pytest.mark.django_db
def test_the_import_creates_no_metrics(account):
    """Numbers come from the ordinary sync, so there is one way to measure."""
    from apps.analytics.models import PostInsightsSnapshot

    _run(account, FakeProvider([_existing("p1")]))

    assert PostInsightsSnapshot.objects.count() == 0


@pytest.mark.django_db
def test_a_provider_that_cannot_list_history_imports_nothing(account):
    """Most platforms have no history edge; that is normal, not an error."""
    result = _run(account, FakeProvider([]))

    assert result.imported == 0
    assert result.found_nothing
    assert PlatformPost.objects.count() == 0


@pytest.mark.django_db
def test_a_failing_history_read_does_not_raise(account):
    """It runs behind a button: a provider error means "nothing to offer"."""
    result = _run(account, FakeProvider(raises=RuntimeError("token expired")))

    assert result.imported == 0
    assert PlatformPost.objects.count() == 0


@pytest.mark.django_db
def test_the_count_reports_what_an_import_would_bring(account):
    provider = FakeProvider([_existing("p1"), _existing("p2")])

    assert importable_count(account, provider=provider, access_token="tok") == 2

    _run(account, provider)

    assert importable_count(account, provider=provider, access_token="tok") == 0


@pytest.mark.django_db
def test_the_count_creates_nothing(account):
    """It is what the offer shows before anyone commits to the cost."""
    importable_count(account, provider=FakeProvider([_existing("p1")]), access_token="tok")

    assert PlatformPost.objects.count() == 0


@pytest.mark.django_db
def test_imported_posts_belong_to_the_accounts_workspace(account):
    _run(account, FakeProvider([_existing("p1")]))

    assert Post.objects.get(caption="hello").workspace_id == account.workspace_id
