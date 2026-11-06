from unittest.mock import MagicMock

import pytest

from providers.exceptions import APIError
from providers.meta_insights import (
    fetch_insights_safe,
    fetch_insights_series,
    parse_insights_response,
    parse_insights_series,
)


def _resp(data):
    return MagicMock(json=MagicMock(return_value=data))


def test_fetch_insights_safe_skips_invalid_metric_errors():
    request = MagicMock(
        side_effect=[
            APIError(
                "Meta API error 400: invalid metric",
                platform="Facebook",
                status_code=400,
                raw_response={"error": {"code": 100, "message": "Tried accessing nonexisting field insights"}},
            ),
            _resp({"data": [{"name": "page_media_view", "values": [{"value": 10}]}]}),
        ]
    )

    values, errors = fetch_insights_safe(
        request,
        platform="Facebook",
        endpoint="https://graph.facebook.com/v25.0/page-1/insights",
        access_token="page-token",
        metrics=["bad_metric", "page_media_view"],
    )

    assert values == {"page_media_view": 10}
    assert set(errors) == {"bad_metric"}


def test_fetch_insights_safe_reraises_permission_errors():
    permission_error = APIError(
        "Meta API error 400: missing permission",
        platform="Facebook",
        status_code=400,
        raw_response={
            "error": {
                "code": 200,
                "type": "OAuthException",
                "message": "Permissions error: read_insights is required.",
            }
        },
    )
    request = MagicMock(side_effect=permission_error)

    with pytest.raises(APIError) as excinfo:
        fetch_insights_safe(
            request,
            platform="Facebook",
            endpoint="https://graph.facebook.com/v25.0/page-1/insights",
            access_token="page-token",
            metrics=["page_media_view"],
        )

    assert excinfo.value is permission_error


@pytest.mark.parametrize("require_success", [False, True])
def test_all_failed_insights_propagate_only_when_success_is_required(require_success):
    error = APIError("Upstream unavailable", status_code=503)
    request = MagicMock(side_effect=error)
    kwargs = {
        "platform": "Instagram",
        "endpoint": "https://graph.facebook.com/ig-1/insights",
        "access_token": "user",
        "metrics": ["reach", "views"],
        "require_success": require_success,
    }
    if require_success:
        with pytest.raises(APIError) as excinfo:
            fetch_insights_safe(request, **kwargs)
        assert excinfo.value is error
    else:
        values, errors = fetch_insights_safe(request, **kwargs)
        assert values == {}
        assert set(errors) == {"reach", "views"}


@pytest.mark.parametrize("data", [[], [{"name": "reach", "values": [{"value": 0}]}]])
def test_required_success_keeps_invalid_metric_fallback_and_empty_responses(data):
    request = MagicMock(
        side_effect=[
            APIError("Invalid metric", status_code=400, raw_response={"error": {"code": 100}}),
            _resp({"data": data}),
        ]
    )
    values, errors = fetch_insights_safe(
        request,
        platform="Instagram",
        endpoint="https://graph.facebook.com/ig-1/insights",
        access_token="user",
        metrics=["unsupported", "reach"],
        require_success=True,
    )
    assert values == ({"reach": 0} if data else {})
    assert set(errors) == {"unsupported"}


def test_parse_insights_response_prefers_lifetime_when_metric_name_repeats():
    values = parse_insights_response(
        {
            "data": [
                {
                    "name": "post_media_view",
                    "period": "lifetime",
                    "values": [{"value": 32741}],
                },
                {
                    "name": "post_total_media_view_unique",
                    "period": "lifetime",
                    "values": [{"value": 21105}],
                },
                {
                    "name": "post_total_media_view_unique",
                    "period": "day",
                    "values": [
                        {"value": 0, "end_time": "2026-06-21T07:00:00+0000"},
                        {"value": 0, "end_time": "2026-06-22T07:00:00+0000"},
                    ],
                },
            ]
        }
    )

    assert values == {
        "post_media_view": 32741,
        "post_total_media_view_unique": 21105,
    }


# ---------------------------------------------------------------------------
# The daily series — the other half of parse_insights_response
# ---------------------------------------------------------------------------


def test_parse_insights_series_returns_a_value_per_day():
    series = parse_insights_series(
        {
            "data": [
                {
                    "name": "reach",
                    "period": "day",
                    "values": [
                        {"value": 5, "end_time": "2026-10-02T07:00:00+0000"},
                        {"value": 8, "end_time": "2026-10-03T07:00:00+0000"},
                    ],
                }
            ]
        }
    )

    assert series == {"reach": {"2026-10-02": 5.0, "2026-10-03": 8.0}}


def test_parse_insights_series_ignores_a_lifetime_total():
    """total_value has no timeline to place it on."""
    assert parse_insights_series({"data": [{"name": "views", "total_value": {"value": 99}}]}) == {}


def test_parse_insights_series_skips_points_with_no_date():
    series = parse_insights_series(
        {"data": [{"name": "reach", "values": [{"value": 1}, {"value": 2, "end_time": "2026-10-02T07:00:00+0000"}]}]}
    )

    assert series == {"reach": {"2026-10-02": 2.0}}


def test_parse_insights_series_skips_breakdown_dicts():
    """A breakdown is per-segment; the series is about the metric as a whole."""
    series = parse_insights_series(
        {
            "data": [
                {
                    "name": "reach",
                    "values": [
                        {"value": {"follower": 3, "non_follower": 4}, "end_time": "2026-10-02T07:00:00+0000"},
                        {"value": 7, "end_time": "2026-10-03T07:00:00+0000"},
                    ],
                }
            ]
        }
    )

    assert series == {"reach": {"2026-10-03": 7.0}}


def test_parse_insights_series_drops_a_metric_with_no_usable_points():
    assert parse_insights_series({"data": [{"name": "reach", "values": [{"value": 1}]}]}) == {}


def test_fetch_insights_series_asks_for_each_metric_separately():
    """One invalid metric must not empty the whole backfill."""
    calls = []

    def request(method, url, access_token=None, params=None):
        calls.append(params["metric"])
        return _resp({"data": [{"name": params["metric"], "values": [{"value": 1, "end_time": "2026-10-02T07:00:00+0000"}]}]})

    series = fetch_insights_series(
        request,
        platform="Facebook",
        endpoint="https://graph.facebook.com/v25.0/page-1/insights",
        access_token="t",
        metrics=["reach", "views"],
        since=1,
        until=2,
    )

    assert calls == ["reach", "views"]
    assert set(series) == {"reach", "views"}


def test_fetch_insights_series_skips_a_metric_the_platform_refuses():
    def request(method, url, access_token=None, params=None):
        if params["metric"] == "bad":
            raise APIError("Meta API error 400: nonexisting field", platform="Facebook", status_code=400)
        return _resp({"data": [{"name": "reach", "values": [{"value": 4, "end_time": "2026-10-02T07:00:00+0000"}]}]})

    series = fetch_insights_series(
        request,
        platform="Facebook",
        endpoint="https://graph.facebook.com/v25.0/page-1/insights",
        access_token="t",
        metrics=["bad", "reach"],
        since=1,
        until=2,
    )

    assert set(series) == {"reach"}


def test_fetch_insights_series_reraises_a_permission_error():
    """A missing scope is the caller's to handle — it means reconnect."""
    error = APIError(
        "Meta API error 400: missing permission",
        platform="Facebook",
        status_code=400,
        raw_response={"error": {"code": 200, "message": "requires instagram_manage_insights permission"}},
    )

    def request(method, url, access_token=None, params=None):
        raise error

    with pytest.raises(APIError):
        fetch_insights_series(
            request,
            platform="Instagram",
            endpoint="https://graph.facebook.com/v25.0/ig-1/insights",
            access_token="t",
            metrics=["reach"],
            since=1,
            until=2,
        )


def test_fetch_insights_series_asks_for_a_daily_period():
    """Without period=day Meta returns one aggregate, not a series."""
    seen = {}

    def request(method, url, access_token=None, params=None):
        seen.update(params)
        return _resp({"data": []})

    fetch_insights_series(
        request,
        platform="Facebook",
        endpoint="https://x/insights",
        access_token="t",
        metrics=["reach"],
        since=111,
        until=222,
    )

    assert seen["period"] == "day"
    assert seen["since"] == 111
    assert seen["until"] == 222
