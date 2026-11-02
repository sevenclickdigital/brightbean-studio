"""Social platform provider registry.

Maps PlatformCredential.Platform enum values to provider classes.
Use get_provider() to instantiate a provider with app credentials.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

from .bluesky import BlueskyProvider
from .devto import DevtoProvider
from .facebook import FacebookProvider
from .google_business import GoogleBusinessProvider
from .instagram import InstagramProvider
from .instagram_login import InstagramLoginProvider
from .linkedin import LINKEDIN_RESERVED_CHARS
from .linkedin_company import LinkedInCompanyProvider
from .linkedin_personal import LinkedInPersonalProvider
from .mastodon import MastodonProvider
from .pinterest import PinterestProvider
from .threads import ThreadsProvider
from .tiktok import TikTokProvider
from .types import PostType
from .youtube import YouTubeProvider

if TYPE_CHECKING:
    from .base import SocialProvider

PROVIDER_REGISTRY: dict[str, type[SocialProvider]] = {
    "facebook": FacebookProvider,
    "instagram": InstagramProvider,
    "instagram_login": InstagramLoginProvider,
    "linkedin_personal": LinkedInPersonalProvider,
    "linkedin_company": LinkedInCompanyProvider,
    "tiktok": TikTokProvider,
    "youtube": YouTubeProvider,
    "pinterest": PinterestProvider,
    "threads": ThreadsProvider,
    "bluesky": BlueskyProvider,
    "google_business": GoogleBusinessProvider,
    "mastodon": MastodonProvider,
    "devto": DevtoProvider,
}

# Characters a platform escapes in the caption it publishes. Each one costs two
# characters on the wire, so a limit has to be counted against the escaped text
# rather than the text the user typed.
CAPTION_ESCAPED_CHARS: dict[str, str] = {
    # LinkedIn's little-text commentary; the backslash escapes itself too.
    "linkedin_personal": "\\" + LINKEDIN_RESERVED_CHARS,
    "linkedin_company": "\\" + LINKEDIN_RESERVED_CHARS,
}


def caption_wire_length(platform: str, text: str) -> int:
    """Length of ``text`` as ``platform`` counts it, after any escaping.

    The composer's counter and the published payload have to agree: a LinkedIn
    caption of 2,990 characters holding 20 parentheses arrives as 3,010 and is
    rejected, even though the user was shown a green counter.
    ``providers.linkedin.escape_commentary`` is the transform this mirrors;
    ``tests/providers/test_caption_wire_length.py`` holds the two together.
    """
    escaped = CAPTION_ESCAPED_CHARS.get(platform)
    if not escaped:
        return len(text)
    return len(text) + sum(text.count(ch) for ch in escaped)


VIDEO_POST_TYPES = frozenset({PostType.VIDEO, PostType.SHORT})


@functools.cache
def is_video_only(platform: str) -> bool:
    """Whether ``platform`` can publish nothing but video.

    The publisher drops every non-video attachment for these platforms (the
    same ``VIDEO_POST_TYPES`` test in ``PublishEngine._dispatch_to_provider``),
    so a post without a video reaches them with nothing to upload and can only
    fail. Cached because ``supported_post_types`` is fixed per provider class,
    and this is asked for every account an API response or scheduling check
    touches.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        return False
    return set(provider_cls().supported_post_types) <= VIDEO_POST_TYPES


@functools.cache
def requires_media(platform: str) -> bool:
    """Whether ``platform`` can't publish text on its own.

    True wherever the provider supports no ``TEXT`` post: Instagram and
    Pinterest refuse a post with nothing attached, and the video-only
    platforms are a stricter case of the same. Derived from the provider, like
    ``is_video_only``, so a new platform is covered without a list to update.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        return False
    return PostType.TEXT not in provider_cls().supported_post_types


def offers_account_picker(platform: str) -> bool:
    """Whether connecting ``platform`` must let the author pick a target.

    True where one grant reaches several connectable things — Facebook Pages,
    LinkedIn company pages, Google Business locations — so the OAuth callback
    has to offer a choice instead of connecting a single profile.

    Derived from the provider class, like ``requires_media``, so a new platform
    is covered without a list to update: the platform list the callback used to
    carry had to agree with the providers implementing ``get_user_pages``, and
    when it did not, the platform silently connected the API's first result.

    Reads the registered class rather than the instance the callback holds,
    which is also the test doubles: ``hasattr`` on a Mock answers yes to every
    attribute, routing platforms that have no picker into one.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        return False
    return provider_cls.offers_account_picker


def get_provider(platform: str, credentials: dict | None = None) -> SocialProvider:
    """Instantiate and return a provider for the given platform.

    Args:
        platform: A PlatformCredential.Platform value (e.g. "facebook").
        credentials: Platform app credentials (client_id, client_secret, etc.)
                     from PlatformCredential or settings.PLATFORM_CREDENTIALS_FROM_ENV.
                     If None, falls back to env credentials from
                     ``settings.PLATFORM_CREDENTIALS_FROM_ENV``.

    Raises:
        ValueError: If no provider is registered for the given platform.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        raise ValueError(f"No provider registered for platform: {platform}")
    if credentials is None:
        from django.conf import settings

        env_creds = getattr(settings, "PLATFORM_CREDENTIALS_FROM_ENV", {})
        credentials = env_creds.get(platform, {})
    return provider_cls(credentials=credentials)
