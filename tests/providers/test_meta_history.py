"""Reading a Meta account's own timeline.

The walk behind ``list_existing_posts``: how it pages, when it stops, and how
each platform's entry shape maps onto ``ExistingPost``.
"""

from __future__ import annotations

from providers import meta_history
from providers.meta_history import MAX_HISTORY_PAGES, facebook_post, instagram_post


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def recorder(pages):
    """A ``request`` stand-in that returns ``pages`` in order, recording calls."""
    calls = []

    def request(method, url, access_token=None, params=None):
        calls.append({"url": url, "params": params})
        return FakeResponse(pages[len(calls) - 1] if len(calls) <= len(pages) else {"data": []})

    return request, calls


def _entry(i):
    return {"id": f"p{i}", "caption": f"post {i}", "timestamp": "2026-01-02T03:04:05+0000"}


def test_a_single_page_is_returned_whole():
    request, calls = recorder([{"data": [_entry(1), _entry(2)]}])

    posts = meta_history.list_instagram_history(
        request, api_base="https://x/v25.0", owner="me", access_token="t", limit=50
    )

    assert [p.platform_post_id for p in posts] == ["p1", "p2"]
    assert len(calls) == 1


def test_the_cursor_is_followed_to_the_next_page():
    request, calls = recorder(
        [
            {"data": [_entry(1)], "paging": {"next": "https://x/next"}},
            {"data": [_entry(2)]},
        ]
    )

    posts = meta_history.list_instagram_history(
        request, api_base="https://x/v25.0", owner="me", access_token="t", limit=50
    )

    assert [p.platform_post_id for p in posts] == ["p1", "p2"]
    assert calls[1]["url"] == "https://x/next"


def test_the_cursor_url_is_not_given_params_again():
    """It already carries fields, limit and the token."""
    request, calls = recorder(
        [
            {"data": [_entry(1)], "paging": {"next": "https://x/next"}},
            {"data": [_entry(2)]},
        ]
    )

    meta_history.list_instagram_history(
        request, api_base="https://x/v25.0", owner="me", access_token="t", limit=50
    )

    assert calls[0]["params"] is not None
    assert calls[1]["params"] is None


def test_walking_stops_at_the_limit():
    request, calls = recorder([{"data": [_entry(i) for i in range(10)], "paging": {"next": "https://x/next"}}])

    posts = meta_history.list_instagram_history(
        request, api_base="https://x/v25.0", owner="me", access_token="t", limit=3
    )

    assert len(posts) == 3
    assert len(calls) == 1


def test_walking_stops_at_the_page_cap_however_high_the_limit():
    """An account with a decade of posts must not spend someone's whole quota."""
    endless = [{"data": [_entry(1)], "paging": {"next": "https://x/next"}} for _ in range(MAX_HISTORY_PAGES + 10)]
    request, calls = recorder(endless)

    meta_history.list_instagram_history(
        request, api_base="https://x/v25.0", owner="me", access_token="t", limit=10_000
    )

    assert len(calls) == MAX_HISTORY_PAGES


def test_an_entry_with_no_id_is_skipped():
    request, _ = recorder([{"data": [{"caption": "orphan"}, _entry(1)]}])

    posts = meta_history.list_instagram_history(
        request, api_base="https://x/v25.0", owner="me", access_token="t", limit=50
    )

    assert [p.platform_post_id for p in posts] == ["p1"]


class TestInstagramEntries:
    def test_media_type_maps_onto_a_kind(self):
        assert instagram_post({"id": "1", "media_type": "CAROUSEL_ALBUM"}).media_kind == "carousel"
        assert instagram_post({"id": "1", "media_type": "VIDEO"}).media_kind == "video"
        assert instagram_post({"id": "1", "media_type": "IMAGE"}).media_kind == "image"

    def test_an_unknown_media_type_leaves_the_kind_empty(self):
        assert instagram_post({"id": "1", "media_type": "HOLOGRAM"}).media_kind == ""

    def test_an_image_falls_back_to_its_media_url_for_a_thumbnail(self):
        """Instagram only sends thumbnail_url for video."""
        post = instagram_post({"id": "1", "media_type": "IMAGE", "media_url": "https://cdn/x.jpg"})

        assert post.thumbnail_url == "https://cdn/x.jpg"

    def test_the_timestamp_is_parsed_as_aware(self):
        post = instagram_post({"id": "1", "timestamp": "2026-01-02T03:04:05+0000"})

        assert post.published_at is not None
        assert post.published_at.tzinfo is not None

    def test_an_unparseable_timestamp_leaves_the_date_unset(self):
        """A post whose date cannot be read is still worth importing."""
        post = instagram_post({"id": "1", "timestamp": "last tuesday"})

        assert post.published_at is None
        assert post.platform_post_id == "1"


class TestFacebookEntries:
    def test_the_message_becomes_the_caption(self):
        assert facebook_post({"id": "1", "message": "hi"}).caption == "hi"

    def test_status_type_hints_the_kind(self):
        assert facebook_post({"id": "1", "status_type": "added_video"}).media_kind == "video"
        assert facebook_post({"id": "1", "status_type": "added_photos"}).media_kind == "image"
        assert facebook_post({"id": "1", "status_type": "shared_story"}).media_kind == ""

    def test_the_feed_edge_is_addressed_by_page(self):
        request, calls = recorder([{"data": []}])

        meta_history.list_facebook_history(
            request, base_url="https://g/v25.0", page_id="42", access_token="t", limit=10
        )

        assert calls[0]["url"] == "https://g/v25.0/42/feed"
