"""The optional fields a Facebook Page post can carry: place and feeling.

Both ride on ``PublishContent.extra`` rather than the publish signature, the
same way ``page_id`` and ``post_type`` do, so adding them touched no provider
that does not support them.

What is deliberately absent: tagging people. ``POST /{page-id}/feed`` still
documents a ``tags`` parameter, but the endpoint that discovered taggable
profiles (``/{user-id}/taggable_friends``) was removed in the post-Cambridge
Analytica lockdown, and ``tags`` cannot be sent without a ``place`` anyway.
There is no way to offer a person picker, so the composer does not pretend to.
"""

from __future__ import annotations

# A feeling is two Open Graph ids, not one: an *action* (what the Page is
# doing) and an *object* (what it is doing it to). "Feeling happy" is the
# FEELS action pointing at the HAPPY object.
FEELS_ACTION_ID = "383634835006146"

# The feeling objects we offer, as (object id, label).
#
# These have to be hardcoded. Meta documents og_object_id but publishes no
# endpoint that lists the available objects, so there is nothing to read them
# from at runtime. Meta's own list runs to several hundred entries; this is
# the handful a business Page plausibly uses, because every entry here is a
# maintenance liability if Meta ever renumbers them.
#
# Sourced from Meta's feelings reference. An id the API rejects costs the
# feeling, never the post: the publish retries once without the decoration
# (``strip_feed_extras``), so a stale entry here degrades rather than fails.
FEELINGS: tuple[tuple[str, str], ...] = (
    ("528297480516636", "happy"),
    ("525497104142297", "blessed"),
    ("123103951186111", "loved"),
    ("308167675961412", "excited"),
    ("118365354995398", "grateful"),
    ("443609379021593", "thankful"),
    ("136050896551329", "accomplished"),
    ("480134215368252", "amazed"),
    ("539354326075059", "inspired"),
    ("379355252154431", "proud"),
    ("304503342999403", "confident"),
    ("112626832239338", "peaceful"),
    ("175500129263221", "content"),
    ("477906418922117", "joyful"),
)

FEELING_IDS = frozenset(object_id for object_id, _label in FEELINGS)


def feeling_label(object_id: str) -> str:
    """The label for ``object_id``, or "" when it isn't one we offer."""
    for known_id, label in FEELINGS:
        if known_id == object_id:
            return label
    return ""


PLACE_SEARCH_FIELDS = "id,name,location,link"
PLACE_SEARCH_LIMIT = 10


def parse_place_results(data: dict) -> list[dict]:
    """Flatten ``/pages/search`` results into what the picker renders.

    ``where`` is the address as a human reads it, assembled from whichever
    location parts Meta returned — many Pages carry only a city.
    """
    results = []
    for row in (data or {}).get("data", []) or []:
        page_id = str(row.get("id") or "")
        name = row.get("name") or ""
        if not page_id or not name:
            continue
        location = row.get("location") or {}
        parts = [location.get(key) for key in ("street", "city", "state", "country")]
        results.append(
            {
                "id": page_id,
                "name": name,
                "where": ", ".join(part for part in parts if part),
            }
        )
    return results


# The keys ``apply_feed_extras`` can add. Used to strip the decoration back off
# a payload when the API refuses it, so the retry carries the post without it.
FEED_EXTRA_PAYLOAD_KEYS = ("place", "og_action_type_id", "og_object_id")


def apply_feed_extras(payload: dict, extra: dict) -> dict:
    """Add ``place`` and the feeling fields to a Page feed payload.

    Mutates and returns ``payload``. Anything absent or unrecognised is
    skipped: these decorate a post, and a stale feeling id must not be the
    reason a publish fails.

    Only valid on ``POST /{page-id}/feed``. ``/photos`` and ``/videos`` answer a
    bare ``OAuthException code 1`` (HTTP 500) when these fields are present —
    see ``has_feed_extras``, which callers use to route a decorated photo post
    through ``/feed`` instead.
    """
    place_id = str(extra.get("place_id") or "").strip()
    if place_id:
        payload["place"] = place_id

    object_id = str(extra.get("feeling_object_id") or "").strip()
    if object_id in FEELING_IDS:
        payload["og_action_type_id"] = FEELS_ACTION_ID
        payload["og_object_id"] = object_id
    return payload


def has_feed_extras(extra: dict) -> bool:
    """Whether ``extra`` carries a decoration ``apply_feed_extras`` would add.

    Mirrors the two conditions in ``apply_feed_extras`` exactly — an
    unrecognised feeling id is not a decoration, because that function drops it.
    """
    place_id = str((extra or {}).get("place_id") or "").strip()
    object_id = str((extra or {}).get("feeling_object_id") or "").strip()
    return bool(place_id) or object_id in FEELING_IDS


def strip_feed_extras(payload: dict) -> dict:
    """Remove the decoration keys from ``payload``. Mutates and returns it.

    The other half of the promise in ``apply_feed_extras``: when the platform
    refuses a decorated payload, the publish retries without the decoration
    rather than failing the post.
    """
    for key in FEED_EXTRA_PAYLOAD_KEYS:
        payload.pop(key, None)
    return payload
