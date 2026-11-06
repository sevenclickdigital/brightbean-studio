---
name: provider-integration
description: "Use when adding or changing a social platform integration in providers/ (Facebook, Instagram, Threads, LinkedIn, TikTok, YouTube, Pinterest, Bluesky, Mastodon, Google Business, dev.to): publishing payloads, OAuth/token refresh, comments, inbox messages, webhooks, insights/analytics, or when a platform returns an error you need to diagnose."
---

# Provider integrations

Read `docs/architecture.md` §5 (providers), §6 (publishing), §8 (inbox/webhooks) and §10 (analytics) for the subsystem you touch.

## Shape

- One module per platform in `providers/`, subclass of `SocialProvider` (`providers/base.py`), registered in `PROVIDER_REGISTRY` (`providers/__init__.py`).
- HTTP goes through `self._request(...)` from the base — it classifies errors into `providers/exceptions.py` types. Never call `requests` directly.
- Data shapes live in `providers/types.py`. Return those, not raw dicts.
- Meta code is split by concern: `meta_oauth.py`, `meta_messaging.py`, `meta_comments.py`, `meta_insights.py`, `meta_history.py`, `meta_accounts.py`, `meta_feed_extras.py`. Extend the right helper; don't re-implement Graph calls inside `facebook.py`/`instagram.py`.
- Credentials: `resolve_platform_credentials` (`apps/credentials/models.py`) — env wins over the org row. Don't read platform keys from settings directly.

## Diagnosing a platform error

Generic errors (Meta's `{"code": 1, "message": "An unknown error has occurred."}`, 500s) usually point at **our payload**, not at the platform being down.

1. Reproduce with the exact payload (log it, redacting tokens).
2. Vary one thing at a time against the real API: edge (`/photos` vs `/feed`), each optional field, media type.
3. Fix the routing/payload; add retries only for genuinely transient classes.
4. Make sure the user sees a precise failure (account status, `PlatformPost.publish_error`, toast), not "temporarily unavailable" for 30 minutes.

Example from history: a Facebook post with a *feeling* and one photo failed with code 1 because feelings only publish through `/feed`; the fix stages the photo unpublished and attaches it to a `/feed` post.

## Platform-specific branches

Adding a platform touches more than `providers/`: `PlatformCredential` choices and credential settings, and the platform branches in `apps/social_accounts`, `apps/publisher`, `apps/inbox`, `apps/analytics` (metric lists in `apps/analytics/metrics.py`, unsupported-metric notes in `apps/analytics/constants.py`). Grep the platform key across `apps/` before declaring it done. Add the required env vars to `.env.example` and the README "Platform Credentials" section.

## Tests

- Tests live in `tests/providers/test_<platform>.py`.
- Mock the boundary only: replace `provider._request` with a `MagicMock` whose `side_effect` returns `MagicMock(json=MagicMock(return_value={...}))` responses, then assert on the calls (`call(...)` with method, URL, payload). See `tests/providers/test_facebook.py`.
- Cover the payload for each post type you changed and the error classification for the failure you fixed.

## Tokens and limits

- Token refresh is serialized (`apps/social_accounts/token_refresh.py`); use it instead of refreshing inline.
- Respect platform quotas (YouTube quota handling in `providers/google_errors.py`). YouTube data retention has its own rules (`apps/social_accounts/youtube_compliance.py`, `docs/youtube-compliance-rollout.md`).
