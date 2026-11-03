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
# Sourced from Meta's feelings reference. ``apply_feed_extras`` drops an id
# the API rejects rather than failing the publish, so a stale entry costs the
# feeling, never the post.
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


def apply_feed_extras(payload: dict, extra: dict) -> dict:
    """Add ``place`` and the feeling fields to a Page feed payload.

    Mutates and returns ``payload``. Anything absent or unrecognised is
    skipped: these decorate a post, and a stale feeling id must not be the
    reason a publish fails.
    """
    place_id = str(extra.get("place_id") or "").strip()
    if place_id:
        payload["place"] = place_id

    object_id = str(extra.get("feeling_object_id") or "").strip()
    if object_id in FEELING_IDS:
        payload["og_action_type_id"] = FEELS_ACTION_ID
        payload["og_object_id"] = object_id
    return payload
