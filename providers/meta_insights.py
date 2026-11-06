"""Helpers shared by Meta-backed providers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .exceptions import APIError

logger = logging.getLogger(__name__)

META_PERMISSION_ERROR_CODES = {10, 190, 200}
META_PERMISSION_ERROR_MARKERS = (
    "access token",
    "authorization",
    "authorized",
    "insufficient",
    "oauth",
    "permission",
    "permissions",
    "scope",
)


def parse_insights_response(data: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    periods: dict[str, str] = {}
    for entry in data.get("data", []):
        name = entry.get("name", "")
        if not name:
            continue
        period = entry.get("period", "")
        if periods.get(name) == "lifetime" and period != "lifetime":
            continue
        if "total_value" in entry:
            value = entry.get("total_value", {}).get("value", 0)
        else:
            value = entry.get("values", [{}])[0].get("value", 0)
        if name not in values or period == "lifetime":
            values[name] = value
            periods[name] = period
            continue
    return values


def fetch_insights_series(
    request: Callable[..., Any],
    *,
    platform: str,
    endpoint: str,
    access_token: str,
    metrics: list[str],
    since: int,
    until: int,
) -> dict[str, dict[str, float]]:
    """Daily values for each metric over ``since``..``until``, as UNIX stamps.

    One request per metric, like :func:`fetch_insights_safe` and for the same
    reason: Meta rejects the whole call when any one metric in it is invalid
    for the account, so asking separately is what keeps one unsupported
    metric from emptying the backfill.

    A metric the platform refuses contributes nothing and is logged. The
    caller gets whatever the platform was willing to serve, which is the only
    useful answer for a backfill — there is no partial failure worth aborting
    on when the alternative is no history at all.
    """
    series: dict[str, dict[str, float]] = {}
    for metric in metrics:
        try:
            resp = request(
                "GET",
                endpoint,
                access_token=access_token,
                params={
                    "metric": metric,
                    "period": "day",
                    "since": since,
                    "until": until,
                },
            )
        except APIError as exc:
            if is_meta_permission_error(exc):
                raise
            logger.warning("Skipping %s historical metric %s: %s", platform, metric, exc)
            continue
        series.update(parse_insights_series(resp.json()))
    return series


def parse_insights_series(data: dict[str, Any]) -> dict[str, dict[str, float]]:
    """``{metric: {"YYYY-MM-DD": value}}`` — the daily series, not one number.

    The companion to :func:`parse_insights_response`, which answers "what is
    this metric now" by reading ``values[0]``. A ``period=day`` request over a
    date range returns one entry per day in ``values``, and that whole list is
    what a backfill needs: without it an account connected today can only ever
    chart from today, however much history the platform is willing to hand
    over.

    Added alongside rather than folded into the existing parser. That one is
    on the hot path for every publish and every sync in all three Meta
    providers, and widening its return type to serve this one caller would be
    a change to code that is working.

    ``end_time`` is the *end* of the day the value covers, which Meta stamps
    at 07:00 UTC of the following day. The date is taken from it as-is: the
    series is what the platform reported for that stamp, and shifting it here
    would make this parser disagree with the platform's own dashboard.

    Entries without a usable date are skipped — a metric that came back as a
    lifetime total has nothing to place on a timeline.
    """
    series: dict[str, dict[str, float]] = {}
    for entry in data.get("data", []):
        name = entry.get("name", "")
        if not name or not isinstance(entry.get("values"), list):
            continue
        points: dict[str, float] = {}
        for point in entry["values"]:
            if not isinstance(point, dict):
                continue
            end_time = point.get("end_time")
            if not isinstance(end_time, str) or len(end_time) < 10:
                continue
            value = point.get("value")
            # A breakdown comes back as a dict of segments; the series is
            # about the metric as a whole, so only scalars are placed.
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            points[end_time[:10]] = float(value)
        if points:
            series[name] = points
    return series


def is_meta_permission_error(exc: APIError) -> bool:
    """Return true for Meta auth/scope errors that should trigger reconnect handling."""
    error = exc.raw_response.get("error") if isinstance(exc.raw_response, dict) else None
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    message = " ".join(
        str(value)
        for value in (
            error.get("message"),
            error.get("type"),
            error.get("error_user_msg"),
            exc,
        )
        if value
    ).lower()

    if code in META_PERMISSION_ERROR_CODES:
        return True
    if exc.status_code == 403:
        return True
    return any(marker in message for marker in META_PERMISSION_ERROR_MARKERS)


def fetch_insights_safe(
    request: Callable[..., Any],
    *,
    platform: str,
    endpoint: str,
    access_token: str,
    metrics: list[str],
    base_params: dict[str, Any] | None = None,
    metric_params: dict[str, dict[str, Any]] | None = None,
    endpoint_type: str = "insights",
    require_success: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Isolate invalid metrics, optionally requiring a successful insights response."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    first_error: APIError | None = None
    successful_response = False
    for metric in metrics:
        params = {**(base_params or {}), **(metric_params or {}).get(metric, {}), "metric": metric}
        try:
            resp = request("GET", endpoint, access_token=access_token, params=params)
        except APIError as exc:
            if is_meta_permission_error(exc):
                raise
            first_error = first_error or exc
            errors[metric] = str(exc)
            logger.warning(
                "Skipping unsupported %s %s metric %s at %s: %s",
                platform,
                endpoint_type,
                metric,
                endpoint,
                exc,
            )
            continue
        successful_response = True
        values.update(parse_insights_response(resp.json()))
    if require_success and not successful_response and first_error is not None:
        raise first_error
    return values, errors
