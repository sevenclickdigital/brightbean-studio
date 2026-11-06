"""Importing the posts a channel already had, so analytics can see them.

Analytics reads ``PlatformPost``, which only ever held what this product
published. A freshly connected account therefore shows an empty post table
beside a profile full of content — and so does an account whose owner posts
from the platform's own app.

This closes that gap by reading the account's own timeline
(``SocialProvider.list_existing_posts``) and creating the rows analytics
expects. Imported posts are ordinary ``PlatformPost`` rows, measured by the
same sync path as everything else; they carry a marker in ``platform_extra``
so the composer can tell them apart from posts it actually sent.

Nothing here fetches metrics. The existing analytics sync picks the new rows
up on its next pass, which keeps one definition of how a post is measured.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Marker written into ``PlatformPost.platform_extra``. Its presence is what
#: "this post came from the channel, not from us" means — there is no column
#: for it, and inferring it from a missing draft would be guesswork.
IMPORT_MARKER = "imported_from_platform"

#: How many posts one import reads. Generous enough to cover a year of
#: ordinary posting, bounded so a decade-old account cannot turn a single
#: click into thousands of API calls.
DEFAULT_IMPORT_LIMIT = 200


@dataclass(frozen=True)
class ImportResult:
    """What one import did. ``skipped`` is posts already known, not failures."""

    imported: int = 0
    skipped: int = 0
    available: int = 0

    @property
    def found_nothing(self) -> bool:
        return self.available == 0


def _is_imported(platform_post) -> bool:
    return bool((platform_post.platform_extra or {}).get(IMPORT_MARKER))


def importable_count(account, *, provider=None, access_token: str | None = None) -> int:
    """How many posts an import would bring in, without importing them.

    What the "import history" offer shows before anyone commits to it: a
    number the user can judge, rather than a button whose cost is unknown.
    """
    posts = _read_history(account, provider=provider, access_token=access_token)
    if not posts:
        return 0
    known = _known_platform_post_ids(account, [p.platform_post_id for p in posts])
    return len([p for p in posts if p.platform_post_id not in known])


def _read_history(account, *, provider=None, access_token: str | None = None, limit=DEFAULT_IMPORT_LIMIT):
    from apps.analytics.tasks import _analytics_provider_and_token

    if provider is None or access_token is None:
        try:
            provider, access_token = _analytics_provider_and_token(account)
        except Exception as exc:
            logger.warning("history import: no provider for %s: %s", account, exc)
            return []
    try:
        return provider.list_existing_posts(access_token, limit=limit)
    except Exception as exc:
        # Deliberately broad: this runs behind a user-facing button, and every
        # provider failure here means the same thing to the caller — there is
        # no history to offer. The specific error is logged, not raised.
        logger.warning("history import: %s refused the history read: %s", account.platform, exc)
        return []


def _known_platform_post_ids(account, candidate_ids: list[str]) -> set[str]:
    """Ids already on a ``PlatformPost`` for this account.

    Scoped to the candidates rather than loading the account's whole history:
    an account this product has published to for a year would otherwise pull
    every row to answer a question about fifty.
    """
    from apps.composer.models import PlatformPost

    return set(
        PlatformPost.objects.filter(
            social_account=account,
            platform_post_id__in=candidate_ids,
        ).values_list("platform_post_id", flat=True)
    )


@transaction.atomic
def import_history(account, *, provider=None, access_token: str | None = None, limit=DEFAULT_IMPORT_LIMIT):
    """Create ``PlatformPost`` rows for this account's existing posts.

    Idempotent by ``platform_post_id``: a post already known — whether this
    product published it or a previous import created it — is skipped, so
    running this twice imports nothing the second time.
    """
    from apps.composer.models import PlatformPost, Post

    existing = _read_history(account, provider=provider, access_token=access_token, limit=limit)
    if not existing:
        return ImportResult()

    known = _known_platform_post_ids(account, [p.platform_post_id for p in existing])
    to_create = [p for p in existing if p.platform_post_id not in known]

    workspace = account.workspace
    now = timezone.now()
    created = 0

    for entry in to_create:
        # Each imported post gets its own Post: the parent is what carries the
        # caption and the workspace, and sharing one across unrelated posts
        # would make the composer show them as variants of each other.
        # No status passed: ``Post.status`` is a property derived from its
        # ``PlatformPost`` children, so creating the child below as PUBLISHED
        # is what makes the parent read as published.
        post = Post.objects.create(
            workspace=workspace,
            caption=entry.caption,
            published_at=entry.published_at or now,
        )
        PlatformPost.objects.create(
            post=post,
            social_account=account,
            status=PlatformPost.Status.PUBLISHED,
            platform_post_id=entry.platform_post_id,
            platform_specific_caption=entry.caption,
            published_at=entry.published_at or now,
            platform_extra={
                IMPORT_MARKER: True,
                "imported_at": now.isoformat(),
                "permalink": entry.permalink,
                "media_url": entry.media_url,
                "thumbnail_url": entry.thumbnail_url,
                "media_kind": entry.media_kind,
            },
        )
        created += 1

    logger.info(
        "history import: %s — %s imported, %s already known",
        account,
        created,
        len(known),
    )
    return ImportResult(imported=created, skipped=len(known), available=len(existing))
