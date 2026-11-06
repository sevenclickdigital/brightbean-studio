"""Reading an account's own timeline — the posts this app did not publish.

Shared by the three Meta providers because the walk is the same shape for all
of them: ask an edge for a page of posts, map each entry onto
:class:`~providers.types.ExistingPost`, follow the cursor until ``limit`` is
reached. Only the edge and the field names differ — Instagram's ``/media``
against Facebook's ``/feed`` — so those are parameters rather than three
copies of the same loop.

Nothing here fetches metrics. An imported post gets its numbers from the same
``get_post_metrics`` path every other post uses; making this a second source
of truth for engagement is exactly the drift worth avoiding.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Callable

from .types import ExistingPost

logger = logging.getLogger(__name__)

#: Posts per API call. Meta's edges accept up to 100 and the response is small
#: — a caption and a handful of ids — so a larger page costs the same round
#: trip and spends less of the hourly call budget.
HISTORY_PAGE_SIZE = 50

#: Stop walking after this many pages whatever ``limit`` says. An account with
#: a decade of posts would otherwise keep a backfill running against someone
#: else's quota; the cap turns "forever" into a bounded, explainable import.
MAX_HISTORY_PAGES = 20

#: Instagram's ``media_type`` to the word the rest of the codebase uses.
_IG_MEDIA_KINDS = {
    "IMAGE": "image",
    "VIDEO": "video",
    "CAROUSEL_ALBUM": "carousel",
}


def _parse_timestamp(value: Any) -> datetime | None:
    """Graph's ISO-8601 with a ``+0000`` offset, or ``None``.

    Returns ``None`` rather than raising on anything unexpected: a post whose
    date cannot be read is still worth importing, and the caller orders by
    what it has.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("+0000", "+00:00"))
    except ValueError:
        logger.debug("Unparseable Graph timestamp: %r", value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def walk_history(
    request: Callable[..., Any],
    *,
    url: str,
    access_token: str,
    fields: str,
    to_post: Callable[[dict], ExistingPost | None],
    limit: int,
) -> list[ExistingPost]:
    """Page through an edge, mapping entries until ``limit`` or the end.

    ``to_post`` returns ``None`` for an entry to skip — a Facebook feed story
    with no id, say — so the mapper decides what counts as a post rather than
    this loop guessing.
    """
    posts: list[ExistingPost] = []
    next_url: str | None = url
    params: dict[str, Any] | None = {"fields": fields, "limit": HISTORY_PAGE_SIZE}

    for _ in range(MAX_HISTORY_PAGES):
        if next_url is None or len(posts) >= limit:
            break
        resp = request("GET", next_url, access_token=access_token, params=params)
        body = resp.json()

        for entry in body.get("data", []):
            post = to_post(entry)
            if post is not None:
                posts.append(post)
            if len(posts) >= limit:
                break

        # The cursor URL already carries fields, limit and the token, so
        # passing params again would duplicate them.
        next_url = (body.get("paging") or {}).get("next")
        params = None

    return posts[:limit]


# ---------------------------------------------------------------------------
# Instagram — the /media edge
# ---------------------------------------------------------------------------

INSTAGRAM_HISTORY_FIELDS = "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp"


def instagram_post(entry: dict) -> ExistingPost | None:
    post_id = str(entry.get("id") or "").strip()
    if not post_id:
        return None
    return ExistingPost(
        platform_post_id=post_id,
        caption=(entry.get("caption") or "").strip(),
        published_at=_parse_timestamp(entry.get("timestamp")),
        permalink=entry.get("permalink") or "",
        media_url=entry.get("media_url") or "",
        # Instagram gives a separate thumbnail for video only; for an image the
        # media URL *is* the picture, so fall back to it rather than leaving
        # the caller with a blank frame.
        thumbnail_url=entry.get("thumbnail_url") or entry.get("media_url") or "",
        media_kind=_IG_MEDIA_KINDS.get(entry.get("media_type", ""), ""),
        extra=entry,
    )


def list_instagram_history(request, *, api_base: str, owner: str, access_token: str, limit: int) -> list[ExistingPost]:
    """``owner`` is the IG user id, or ``me`` on the Instagram-login path."""
    return walk_history(
        request,
        url=f"{api_base}/{owner}/media",
        access_token=access_token,
        fields=INSTAGRAM_HISTORY_FIELDS,
        to_post=instagram_post,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Facebook — the /feed edge
# ---------------------------------------------------------------------------

FACEBOOK_HISTORY_FIELDS = "id,message,created_time,permalink_url,full_picture,status_type"


def facebook_post(entry: dict) -> ExistingPost | None:
    post_id = str(entry.get("id") or "").strip()
    if not post_id:
        return None
    # status_type is Facebook's word for what happened ("added_photos",
    # "shared_story"); only the first part is about the medium.
    status = str(entry.get("status_type") or "").lower()
    if "video" in status:
        kind = "video"
    elif "photo" in status:
        kind = "image"
    else:
        kind = ""
    picture = entry.get("full_picture") or ""
    return ExistingPost(
        platform_post_id=post_id,
        caption=(entry.get("message") or "").strip(),
        published_at=_parse_timestamp(entry.get("created_time")),
        permalink=entry.get("permalink_url") or "",
        media_url=picture,
        thumbnail_url=picture,
        media_kind=kind,
        extra=entry,
    )


def list_facebook_history(request, *, base_url: str, page_id: str, access_token: str, limit: int) -> list[ExistingPost]:
    return walk_history(
        request,
        url=f"{base_url}/{page_id}/feed",
        access_token=access_token,
        fields=FACEBOOK_HISTORY_FIELDS,
        to_post=facebook_post,
        limit=limit,
    )
