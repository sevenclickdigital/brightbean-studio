"""Rebuilding the account chart for the days before a channel was connected.

The ordinary sync writes today's snapshot row and nothing else, so a channel
connected on the 2nd could only ever be charted from the 2nd. These pin the
historical read that fills the days behind it — and, as importantly, what it
refuses to overwrite.

Asserts on database state and return values only.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.analytics.models import AccountInsightsSnapshot
from apps.analytics.tasks import backfill_account_series
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


class FakeProvider:
    def __init__(self, series=None, raises=None):
        self._series = series or {}
        self._raises = raises
        self.ranges = []

    def get_account_metrics_series(self, access_token, date_range):
        self.ranges.append(date_range)
        if self._raises is not None:
            raise self._raises
        return self._series


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


def _run(account, provider, **kwargs):
    return backfill_account_series(account, provider=provider, access_token="tok", **kwargs)


def _day(offset):
    return (timezone.now().date() - timedelta(days=offset)).isoformat()


@pytest.mark.django_db
def test_past_days_become_snapshot_rows(account):
    written = _run(account, FakeProvider({"reach": {_day(3): 5.0, _day(2): 8.0}}))

    assert written == 2
    rows = AccountInsightsSnapshot.objects.filter(social_account=account, metric_key="reach")
    assert {r.value for r in rows} == {5.0, 8.0}


@pytest.mark.django_db
def test_today_is_left_to_the_ordinary_sync(account):
    """Today is still accumulating; a historical read of it is staler."""
    written = _run(account, FakeProvider({"reach": {_day(0): 99.0, _day(1): 4.0}}))

    assert written == 1
    assert not AccountInsightsSnapshot.objects.filter(date=timezone.now().date()).exists()


@pytest.mark.django_db
def test_a_day_already_measured_is_not_overwritten(account):
    """What this product measured at the time beats Meta's recollection."""
    measured_on = date.fromisoformat(_day(2))
    AccountInsightsSnapshot.objects.create(
        social_account=account,
        metric_key="reach",
        date=measured_on,
        value=42.0,
    )

    written = _run(account, FakeProvider({"reach": {_day(2): 7.0, _day(3): 1.0}}))

    assert written == 1
    assert AccountInsightsSnapshot.objects.get(date=measured_on).value == 42.0


@pytest.mark.django_db
def test_several_metrics_are_written_independently(account):
    written = _run(account, FakeProvider({"reach": {_day(1): 3.0}, "views": {_day(1): 9.0}}))

    assert written == 2
    assert AccountInsightsSnapshot.objects.filter(metric_key="views").get().value == 9.0


@pytest.mark.django_db
def test_a_platform_with_no_series_writes_nothing(account):
    """Most platforms have none; a chart that starts at connection is the norm."""
    assert _run(account, FakeProvider({})) == 0
    assert AccountInsightsSnapshot.objects.count() == 0


@pytest.mark.django_db
def test_a_failing_read_does_not_raise(account):
    assert _run(account, FakeProvider(raises=RuntimeError("boom"))) == 0
    assert AccountInsightsSnapshot.objects.count() == 0


@pytest.mark.django_db
def test_an_unparseable_date_is_skipped_not_fatal(account):
    written = _run(account, FakeProvider({"reach": {"not-a-date": 1.0, _day(1): 2.0}}))

    assert written == 1


@pytest.mark.django_db
def test_a_platform_without_analytics_is_skipped_entirely(account):
    """BACKFILL_DAYS_PER_PLATFORM caps these at 0, so there is nothing to ask."""
    account.platform = "bluesky"
    account.save(update_fields=["platform"])
    provider = FakeProvider({"reach": {_day(1): 1.0}})

    assert _run(account, provider) == 0
    assert provider.ranges == []


@pytest.mark.django_db
def test_the_window_honours_the_platform_cap(account):
    """An over-long request is clamped rather than passed through."""
    provider = FakeProvider({})

    _run(account, provider, days=9999)

    start, end = provider.ranges[0]
    assert (end - start).days <= 90


@pytest.mark.django_db
def test_running_twice_writes_nothing_the_second_time(account):
    provider = FakeProvider({"reach": {_day(1): 1.0, _day(2): 2.0}})

    assert _run(account, provider) == 2
    assert _run(account, provider) == 0
