# Technical Architecture

How the codebase is put together today. It replaces the original v1 plan (`development_specs/architecture.md`, now only in git history). When this file and the code disagree, the code is right. Please fix this file.

---

## 1. Stack

| Layer | Technology |
|-------|-----------|
| Backend | Django 5.1 (`requirements.txt`), Python 3.12 (`Dockerfile`) |
| Frontend | Django templates, HTMX (`django-htmx`), Alpine.js, vendored in `static/js/` |
| CSS | Tailwind CSS 4 via `django-tailwind` (`theme/`) |
| Database | PostgreSQL 16. Also holds the job queue. SQLite works for local development only. |
| Background jobs | `django-background-tasks` rows, run by the custom worker `apps/common/worker.py`. No Celery. |
| Cache | LocMemCache by default. Redis is used only if `REDIS_URL` is set (`config/settings/base.py`). |
| Media storage | Local filesystem (default) or S3-compatible storage via `django-storages` |
| Media processing | Pillow (images), FFmpeg (video, installed in the image) |
| Email | SMTP behind a budget wrapper (`apps/common/mail.py`). Resend is used as an SMTP provider and sends bounce webhooks. |
| Agent API | `django-ninja` (REST + OpenAPI), a hand-written MCP transport, `django-oauth-toolkit` |
| Payments | Asaas (workspace billing). Stripe is only reached through the optional Intelligence service. |
| Monitoring | Sentry (`sentry-sdk`), turned on when `SENTRY_DSN` is set |
| Web server | Gunicorn (`--workers 1 --threads 4`) behind Dokploy's Traefik |

---

## 2. Repository Layout

```
config/settings/{base,development,production,test}.py
config/urls.py            # all URL mounts, public media routing, error handlers
apps/                     # Django apps (below)
providers/                # one module per social platform + shared Meta helpers
theme/                    # django-tailwind app; static_src/src/styles.css
templates/                # project templates (base.html, components, ninja/)
static/                   # favicon, images, vendored JS (htmx, alpine, cropper)
locale/pt_BR/             # translations (.po; .mo compiled at image build)
tests/                    # cross-app tests (providers, etc.); apps also have tests/
docker-compose.yml + docker-compose.override.yml   # development
docker-compose.dokploy.yml                         # production
```

| App | Responsibility |
|-----|----------------|
| `accounts` | Custom `User`, allauth adapters, signup modes, ToS gate, auth rate limit, operator impersonation |
| `organizations`, `workspaces` | Tenancy. An Organization owns Workspaces. |
| `members` | Org and workspace memberships, roles, invitations, `RBACMiddleware` |
| `settings_manager` | `OrgSetting` / `WorkspaceSetting` key-value overrides |
| `credentials` | `PlatformCredential` (per-org app credentials) and credential resolution |
| `social_accounts` | OAuth connect flows, `SocialAccount`, health checks, webhook subscription, YouTube compliance |
| `composer` | `Post` / `PlatformPost`, ideas, templates, tags, CSV import, feeds |
| `calendar` | Calendar views, posting slots, queues, recurrence rules, custom events |
| `publisher` | Publishing engine (`engine.py`), `PublishLog`, rate-limit state |
| `media_library` | Assets, folders, versions, direct-to-S3 uploads, thumbnails, quotas |
| `approvals` | Approval workflow, post comments, reminders |
| `client_portal` | Magic-link portal for client reviewers |
| `onboarding` | Client connection links, onboarding checklist |
| `inbox` | Unified inbox (comments, mentions, DMs), polling sync, inbound webhooks |
| `automations` | Comment-triggered auto-replies and flows (DM, buttons) |
| `analytics` | Account and post metrics, history import |
| `notifications` | In-app, email and webhook notifications, digests |
| `billing` | Per-workspace subscription via Asaas, storage add-ons, staff console |
| `intelligence` | Optional client for the hosted BrightBean Intelligence SaaS |
| `api_keys` | `bb_studio_` keys, audit log, hourly usage |
| `api` | Agent REST API (`/api/v1/`) |
| `mcp` | MCP tools and Streamable-HTTP transport (`/api/v1/mcp`) |
| `oauth_server` | OAuth 2.1 discovery + DCR for MCP clients |
| `common` | Shared bits: encrypted fields, scoped managers, worker, mail budget, Resend webhook, legal pages |

---

## 3. Tenancy and Access Control

- **Hierarchy:** `Organization` → `Workspace` → content (posts, accounts, media, inbox). A workspace is also the billable unit (§11).
- **Roles** (`apps/members/models.py`): org roles `owner|admin|member`. Workspace roles `owner|manager|editor|contributor|client|viewer`.
- **`RBACMiddleware`** (`apps/members/middleware.py`) attaches `request.org`, `request.org_membership`, `request.workspace` and `request.workspace_membership`. v1 assumes one org per user. It skips `/static/` and `/media/`.
- **Data isolation is explicit, not automatic.** `apps/common/managers.py` provides `OrgScopedManager.for_org()` and `WorkspaceScopedManager.for_workspace()`. Views must call these themselves (or filter by workspace). Nothing filters querysets implicitly, and there is no PostgreSQL row-level security.
- Workspace-scoped URLs follow the pattern `/workspace/<uuid:workspace_id>/...` (`config/urls.py`).
- **Staff-only surfaces** (gated on `is_staff`, outside any workspace prefix): `/billing/staff/` (billing console, courtesy grants, impersonation log) and `/automations/staff/templates/` (flow template library). The impersonation rules are in `apps/accounts/impersonation.py`.

---

## 4. Frontend

Server-rendered HTML. The only build step is Tailwind. In development it runs as `npm run start` in `theme/static_src` (or the `tailwind` service in `docker-compose.override.yml`). In the image, `npm run build` runs in the `Dockerfile`.

- HTMX handles partial swaps, inline actions and polling. `django_htmx.middleware.HtmxMiddleware` is installed.
- Alpine.js handles client-side UI: dropdowns, modals, drag and drop, character counters. CSP allows `'unsafe-eval'` because the standard Alpine build needs it (`base.py`).
- HTMX and Alpine are vendored (`static/js/htmx.min.js`, `static/js/alpine.min.js`), as is Cropper.js under `static/js/vendor/`. Static files are served by WhiteNoise (`CompressedManifestStaticFilesStorage` in production).
- Context processors in `base.py` add sidebar data, the onboarding checklist, the unread-notification count, the Intelligence flag, a storage-quota banner and an impersonation banner to every page.

---

## 5. Social Providers

`providers/base.py` defines the abstract `SocialProvider`. Required members: `platform_name`, `auth_type`, `max_caption_length`, `supported_post_types`, `supported_media_types`, `required_scopes`, `get_profile`, `publish_post`. Optional members have defaults: OAuth (`get_auth_url`, `exchange_code`, `refresh_token`), `check_publish_status`, comments, metrics (single and batch), account metric series, demographics, `get_messages`, `reply_to_message`, `reply_to_comment`, webhook (un)subscription, `revoke_token` and others. Shared HTTP handling (`_request`, `_poll_until`, error classification) lives in the base. Exceptions are defined in `providers/exceptions.py` and data types in `providers/types.py`.

`PROVIDER_REGISTRY` (`providers/__init__.py`) maps platform keys to classes: `facebook`, `instagram` (via Facebook Login), `instagram_login` (Instagram Login, no Page), `linkedin_personal`, `linkedin_company`, `tiktok`, `youtube`, `pinterest`, `threads`, `bluesky`, `google_business`, `mastodon`, `devto`. Meta-specific helpers are split into `meta_oauth.py`, `meta_messaging.py`, `meta_comments.py`, `meta_insights.py`, `meta_history.py`, `meta_accounts.py` and `meta_feed_extras.py`. `google_errors.py` handles YouTube quota and reset time.

**Credential resolution** (`apps/credentials/models.py: resolve_platform_credentials`): if the env vars (`PLATFORM_CREDENTIALS_FROM_ENV` in `base.py`) fully configure a platform, they win. Otherwise the org's admin-entered `PlatformCredential` row is used. Bluesky (app passwords), Mastodon (per-instance app registration in `MastodonAppRegistration`) and dev.to (per-account API key) need no app-level credentials.

**Adding a platform:** add a module in `providers/`, register it in `PROVIDER_REGISTRY`, add the platform to the `PlatformCredential` choices and credential settings, then check the platform-specific branches in `social_accounts`, `publisher`, `inbox` and `analytics`. In practice these systems do branch on platform keys.

---

## 6. Publishing

`apps/publisher/engine.py` (`PublishEngine`) runs inside the worker's dedicated publishing lane (§7):

1. `run_publish_cycle` (every 15 s) claims `PlatformPost` rows with `status=scheduled` and `scheduled_at <= now`, and moves them to `publishing`.
2. It dispatches them in parallel. Concurrency is capped by `PUBLISHER_MAX_CONCURRENT_POSTS` and `PUBLISHER_MAX_CONCURRENT_PLATFORM_PUBLISHES`. Each thread holds its own Postgres connection, so these caps are a connection budget (see `.env.example`).
3. Failures retry with exponential backoff. The first comment is posted `PUBLISHER_FIRST_COMMENT_DELAY` seconds (default 120) after go-live.
4. `confirm_pending_publishes` (every 60 s) asks the platform about posts it accepted but is still processing (for example, TikTok transcoding). It also gives up on stale rows (`PUBLISHER_STALE_PUBLISHING_TIMEOUT`, `PUBLISHER_PUBLISH_CONFIRM_TIMEOUT`, `PUBLISHER_UNCONFIRMED_TIMEOUT`). Those end as visible failures and are never retried silently.

Status belongs to `PlatformPost`. `Post.status` is an aggregate. Media goes out in one of two ways. For platforms that fetch server-side (Instagram, Threads, Facebook, Pinterest, Google Business, dev.to), the engine sends the absolute URL `APP_URL + asset.file.url`. Otherwise it downloads the file to a temp path and uploads it (`apps/media_library/storage.download_to_path`). There is no pre-publish transcoding step. Files are published as stored.

---

## 7. Background Worker

`python manage.py run_studio_worker` (`apps/common/management/commands/run_studio_worker.py`) runs `StudioWorker` (`apps/common/worker.py`):

- It keeps `django-background-tasks` for storage, row locks and task lifecycle (`bg_runner`), but replaces that library's thread pool with **two lanes of one thread each**. The `publishing` lane runs only `run_publish_cycle` and `confirm_pending_publishes`. The `general` lane runs everything else. A long analytics job therefore can't delay publishing.
- At startup it releases locks left behind by a previous process with the same PID name (PID 1 in a container). This stops a killed task from staying parked until `MAX_RUN_TIME`.
- SIGTERM stops new claims and lets in-flight tasks finish. Locks are never released early. `--duration` makes the worker recycle (general work drains first). Dokploy runs the worker without it.
- `apps/publisher/management/commands/run_publisher.py` is an older standalone publish loop. Production does not use it.

**Recurring tasks** are registered idempotently on `post_migrate`, mostly through `apps/common/background.register_recurring_task`. Registration is skipped if a row with the same `verbose_name` already exists. **Changing an interval constant does not change a task that is already registered.** You have to delete its row first.

| Task (`verbose_name`) | Interval | Registered in |
|------|-----------|---------------|
| `run_publish_cycle` | 15 s | `apps/publisher/apps.py` |
| `confirm_pending_publishes` | 60 s | `apps/publisher/apps.py` |
| `run_inbox_sync_cycle` | 5 min. YouTube accounts also have a per-account floor, `INBOX_YOUTUBE_MIN_POLL_SECONDS` (30 min). | `apps/inbox/apps.py` |
| `sync_all_account_analytics` | hourly | `apps/analytics/apps.py` |
| `schedule_all_health_checks` | 6 h. Validates tokens, refreshes expiring ones, retries webhook subscriptions. | `apps/social_accounts/apps.py` |
| `retry_youtube_revocations` / `enforce_youtube_retention` | hourly / daily | `apps/social_accounts/apps.py` |
| `run_approval_reminders_cycle` | hourly | `apps/approvals/apps.py` |
| `retry_failed_deliveries`, `send_batched_email_digests` | 60 s | `apps/notifications/apps.py` |
| `sweep_pending_uploads` / `run_orphaned_media_sweep` | hourly / daily | `apps/media_library/apps.py` |
| `sweep_idempotency` / `sweep_api_usage` | hourly / daily | `apps/api/apps.py` |
| `purge_automation_logs` | daily (90-day retention) | `apps/automations/apps.py` |
| `expire_courtesies` | daily | `apps/billing/apps.py` |
| `purge_email_counters` | daily | `apps/common/apps.py` |
| `clear_expired_sessions` | daily | `apps/accounts/apps.py` |
| `intelligence_reconcile` | 6 h (only relevant when Intelligence is enabled) | `apps/intelligence/apps.py` |

One-off `@background` jobs include media processing (`apps/media_library/tasks.py`), automation runs (`apps/automations/tasks.run_automation_for_message`), first comments and analytics backfills.

`apps/calendar/tasks.generate_recurring_posts` exists and is tested, but no recurring task registers it.

---

## 8. Inbox and Inbound Webhooks

`InboxMessage` (`apps/inbox/models.py`) has types `comment|mention|dm|review`. No provider produces `review` yet. Messages arrive two ways:

- **Polling:** `InboxSyncEngine` (`apps/inbox/tasks.py`) calls `provider.get_messages()` every 5 minutes. YouTube is rate-limited per account, and a weekly deep sweep (`INBOX_DEEP_SWEEP_SECONDS`) catches replies on old threads. Polling always runs. Webhooks only make delivery faster.
- **Webhooks** (`apps/inbox/webhook_urls.py`, CSRF-exempt, rate-limited):

| Endpoint | Source | Verification |
|----------|--------|--------------|
| `/webhooks/facebook/` | Facebook Pages + Instagram (Facebook Login): feed, mentions, comments, messages, postbacks | GET handshake with `FACEBOOK_WEBHOOK_VERIFY_TOKEN`. POST checks `X-Hub-Signature-256` HMAC against every configured app secret, and events are applied only to accounts whose org's secret signed the body. |
| `/webhooks/instagram_login/` | Instagram Login accounts | Same scheme, with `INSTAGRAM_LOGIN_WEBHOOK_VERIFY_TOKEN` |
| `/webhooks/youtube/` | YouTube PubSubHubbub (Atom XML) | `X-Hub-Signature` HMAC with `YOUTUBE_WEBHOOK_SECRET`. XML is parsed with `safe_xml_fromstring`. |

Handlers process the payload **synchronously** in the request: they find the `SocialAccount`, dedupe on `(social_account, platform_message_id)`, link the message to its `PlatformPost`, run keyword sentiment (`apps/inbox/sentiment.py`), send notifications, and call `apps.automations.services.on_inbox_message_created`. That call only enqueues work and never raises. Meta postbacks (button taps) resume automation flows (§9).

Webhook subscription goes through `provider.subscribe_webhooks`, which is implemented for Facebook, Instagram and Instagram Login. It runs on connect, during health checks, and from `manage.py subscribe_webhooks`. No code in the repo subscribes YouTube channels to the PubSubHubbub hub, so YouTube comments arrive through polling.

DM sending eligibility (Meta's 24-hour window and the optional Human Agent tag, `PLATFORM_*_HUMAN_AGENT_ENABLED`) is decided in one place, `apps/inbox/reply_policy.py`. The web inbox, REST and MCP all use it.

Other inbound webhooks: `/webhooks/resend/` (Svix-signed bounces/suppressions → `EmailSuppression`, `apps/common/webhooks.py`) and `/webhooks/asaas/` (§11).

---

## 9. Automations (Comment Replies and Flows)

`apps/automations/` answers comments on Facebook, Instagram and Instagram Login posts (`matching.COMMENT_PLATFORMS`). Google Business review replies are listed as planned only (`channels.py`).

- **Authoring:** a `PostAutomation` stores the canvas graph (`{"nodes", "edges"}`) for one `PlatformPost`. It can be created while the post is still a draft, and it is armed when the publisher confirms the post (`services.arm_for_platform_post`). `flow.py` compiles the graph down into an `AutomationRule`: keywords, post scope, public reply and DM. It never decompiles a rule back into a graph. Staff publish reusable `FlowTemplate` graphs.
- **Matching:** `matching.py` runs rules in priority order, and the first match wins. It performs no writes. `keywords.py` supports `exact`, `contains`, `any_word` and accent-insensitive `_loose` variants (for pt-BR).
- **Execution:** `actions.run_for_message` checks refusals first, then claims idempotency guards (`guards.py`: one reply per sender per post, `max_per_hour`). The guards rely on database constraints and conditional updates. The public reply and the private DM are independent halves. Flows with more steps run in `engine.py`. A `FlowRun` stores progress keyed by `(social_account, sender_key)`, and a button postback resumes the run from a node id. The walk is bounded by a visited set and `MAX_STEPS_PER_WAKE`, and it stops at the first send failure.
- **Audit:** `AutomationLog` rows double as idempotency claims and are purged after 90 days.

---

## 10. Analytics

`apps/analytics/tasks.py` collects account snapshots and post metrics hourly. Each post's sync interval depends on its age (`post_sync_interval`). Failures back off, and a revoked grant queues a health check. YouTube reports are written in the background and read from cache (`youtube_reports.py`). `history_import.py` and `manage.py import_post_history` / `backfill_analytics` import a channel's past posts and metrics.

---

## 11. Billing (Asaas)

`apps/billing/`. **The workspace is the billable unit:** one plan with every feature, priced at `BILLING_WORKSPACE_PRICE_CENTS` per month (default R$ 50,00). Disk is the only metered resource. The base quota is `BILLING_WORKSPACE_BASE_STORAGE_BYTES` (10 GB), and extra GB are sold as `WorkspaceStorageAddon`. Money is always stored as integer cents and disk as integer bytes.

- Models: `WorkspaceSubscription` (`pending|active|past_due|canceled|courtesy`. Only `active` and `courtesy` grant access), `WorkspaceAddon`, `BillingEvent` (append-only, keyed by gateway event id), `OrganizationBillingProfile` (comped workspaces) and `ProductPrice` / `ProductPriceChange`. Each subscription keeps a snapshot of the price it was sold at.
- Gateway: `apps/billing/gateways/asaas.py` is the only module that knows Asaas's wire format. It is configured by `ASAAS_API_KEY`, `ASAAS_API_BASE` (defaults to the **sandbox**), `ASAAS_WEBHOOK_TOKEN` and `ASAAS_TIMEOUT_SECONDS`. If the key or token is missing, the integration does nothing.
- Webhook: `/webhooks/asaas/` authenticates with the `asaas-access-token` header. Asaas does not sign the body.
- Storage enforcement: `apps/billing/storage_usage.py` (`BILLING_STORAGE_QUOTA_ENABLED`) is separate from the older per-org cap in `apps/media_library/quotas.py` (`STORAGE_QUOTA_*`).
- UI: `/workspace/<id>/billing/` for customers and `/billing/staff/` for operators.

---

## 12. Intelligence (optional)

`apps/intelligence/` connects Studio to the hosted BrightBean Intelligence SaaS. It is turned on only when all five of `INTELLIGENCE_INTERNAL_URL`, `INTELLIGENCE_PUBLIC_URL`, `STUDIO_DEPLOYMENT_ID`, `STUDIO_SHARED_SECRET` and `STUDIO_BASE_URL` are set (`INTELLIGENCE_ENABLED`). Its URLs (`/orgs/<id>/intelligence/`, `/intelligence/`) are mounted only in that case. The app is always installed so migrations stay consistent. `services/client.py` has two clients. One makes HMAC-signed calls to `/internal/v1/` (provisioning, billing, eligibility), with nonces and idempotency keys. The other calls the public `/v1/` tools with a per-org `bb_` bearer key. Subscription checkout uses Stripe, run by Intelligence. The boot sequence refuses non-https URLs except loopback.

---

## 13. Agent API, MCP and OAuth Server

- **REST** (`apps/api/api.py`, django-ninja) at `/api/v1/`. Routers: `me`, `accounts`, `posts`, `media`, `analytics`, `inbox`. OpenAPI docs are at `/api/v1/docs`. Every route requires authentication. The README ("API & MCP for Agents") documents the endpoints.
- **API keys** (`apps/api_keys/`). Format: `bb_studio_<random32>_<lookup8>`. Only the lookup prefix and an HMAC-SHA256 of the secret part (peppered from `SECRET_KEY`) are stored. Each key is scoped to one workspace, an allowlist of social accounts, and a list of permissions. Its effective permissions are the intersection of that list with the issuer's current workspace permissions. `ApiKeyAuditLog` records each call and `ApiKeyUsageHourly` keeps hourly counts. `IdempotencyRecord` (`apps/api/models.py`) backs idempotent writes. Rate limits live in `apps/api/limits.py`. Client IPs are taken from `X-Forwarded-For` only for addresses listed in `BB_TRUSTED_PROXIES`.
- **MCP** (`apps/mcp/`) at `/api/v1/mcp`. A hand-written Streamable-HTTP transport (POST only, no SSE) exposes about 21 tools from `handlers.py`: accounts, drafts/scheduling, media upload, analytics, and inbox reply drafts and sending. The `mcp` package is listed in `requirements.txt` but is not imported.
- **OAuth 2.1** (`apps/oauth_server/`, django-oauth-toolkit) lets native MCP clients log in without a pre-shared key. It provides Dynamic Client Registration at `/oauth/register`, authorization code + PKCE with S256 only (`validator.py`), https-only redirect URIs and the single scope `mcp`. Discovery documents are served at `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource[/api/v1/mcp]`. `MCP_PUBLIC_BASE_URL` and `MCP_OAUTH_ISSUER_URL` default to `APP_URL`.

---

## 14. Media Storage

`STORAGE_BACKEND` switches the backend (`base.py`):

```
STORAGE_BACKEND=local  →  FileSystemStorage at MEDIA_ROOT (Dokploy: /app/media, volume media_data)
STORAGE_BACKEND=s3     →  storages.backends.s3boto3.S3Boto3Storage (S3_* env vars)
```

**Serving:**
- `s3`: private ACL and presigned URLs that expire after 1 hour. `SERVE_MEDIA` is forced off. `AWS_S3_MAX_MEMORY_SIZE` (2 MB) must be non-zero, otherwise reads hold whole objects in RAM. Large uploads go straight to the bucket via presigned URLs (`create_pending_upload`, valid for `MEDIA_LIBRARY_PRESIGN_EXPIRE`). `sweep_pending_uploads` cleans up abandoned uploads.
- `local`: `SERVE_MEDIA` (default `true`) mounts `MEDIA_URL` → `MEDIA_ROOT` in `config/urls.py` under every settings module, not just `DEBUG`. The route has no authentication because platforms fetch publish media server-side (§6). `django.views.static.serve` does not support byte ranges, so videos can't be seeked. Switch to `s3` once video volume grows.
- Only the prefixes in `PUBLIC_MEDIA_PREFIXES` (`config/urls.py`) are publicly routed: `media_library/`, `avatars/`, `workspaces/icons/`. This is an allowlist, so any new `upload_to` prefix is private by default. `comment_attachments/` is excluded on purpose (internal comments) and is served by `apps.approvals.views.comment_attachment` behind a membership check.
- Uploads are stored with an extension derived from the sniffed MIME type (`apps/common/validators.storage_filename`), not from the client's filename.

**Processing** (`apps/media_library/tasks.py`, `services.py`): on upload, metadata is extracted and a thumbnail is generated with Pillow or FFmpeg. The image editor and video trim produce `MediaAssetVersion`s. Limits are set in `base.py`: 20 MB per image, 30 MP decoded, 1 GB per video, 5-minute FFmpeg timeout. `MEDIA_LIBRARY_MAX_CONCURRENT_TRANSCODES` is defined but nothing reads it. A daily sweep removes orphaned media.

---

## 15. Email

`EMAIL_BACKEND` is always `apps.common.mail.BudgetedEmailBackend`, which wraps `EMAIL_INNER_BACKEND`: SMTP when `EMAIL_BACKEND_TYPE=smtp`, otherwise the console backend. It enforces a global daily cap and per-recipient hourly and daily caps (`EMAIL_*_LIMIT`; a negative value means unlimited, `0` means send nothing). `EMAIL_SENDING_ENABLED=false` is the kill switch. Addresses suppressed through the Resend webhook (`RESEND_WEBHOOK_SECRET`) are skipped. Development removes the caps and prints mail to the console.

---

## 16. Internationalization

`LANGUAGE_CODE` defaults to `pt-br`. `LANGUAGES` contains `pt-br` and `en-us`. `LocaleMiddleware` runs after the session, and the sidebar language picker posts to `/i18n/setlang/`. Translations live in `locale/pt_BR/LC_MESSAGES/`. The `.mo` files are gitignored and compiled by `compilemessages` in the `Dockerfile` (which installs `gettext`). If they are missing, the interface silently falls back to English. The translation workflow and vocabulary are in the `translation` skill (`.claude/skills/translation/SKILL.md`).

---

## 17. Security

| Concern | Implementation |
|---------|----------------|
| Passwords | `BCryptSHA256PasswordHasher` (PBKDF2 fallback), min length 8 |
| Sessions | DB-backed, HTTP-only, SameSite=Lax, 14-day sliding window. Secure cookies and HSTS in production (`production.py`). |
| Login | django-allauth: email + Google (`GOOGLE_AUTH_*`). `SIGNUP_MODE=open\|invite\|closed`, `CUSTOM_SIGNUP_URL`. |
| 2FA | Not implemented. `User` has encrypted `totp_*` fields, but no flow uses them. |
| Magic links | `MagicLinkToken`: `secrets.token_urlsafe(48)`, stored as is (not hashed), 30-day expiry |
| API keys | HMAC-SHA256 of the secret, lookup prefix, scoped per workspace (§13) |
| Encryption at rest | AES-256-GCM model fields (`apps/common/encryption.py`). The key is derived from `SECRET_KEY` + `ENCRYPTION_KEY_SALT` via HKDF. There is no key-rotation command. Changing either value makes stored tokens unreadable. |
| Data isolation | Explicit `for_org` / `for_workspace` plus membership checks (§3) |
| CSRF / CSP | Django CSRF middleware. `django-csp` with script nonces. CSP is report-only in development. |
| Rate limiting | `django-ratelimit` on webhooks, invites, onboarding and social connects. `AuthRateLimitMiddleware` on auth. Its own limiter for the API. Disabled when `DEBUG`. |
| Audit | `ApiKeyAuditLog`, `BillingEvent`, `AutomationLog`, impersonation sessions. There is no general audit log. |
| Account deletion | Self-service in account settings, refused while the user is the sole owner of an org. No data-export feature. |
| Webhooks | Signature or token verification per source (§8, §11). Legal pages `/privacy/`, `/terms/`, `/data-deletion/` are public (required by Meta App Review). |

---

## 18. Deployment

**Production: Dokploy only**, using `docker-compose.dokploy.yml` (a standalone file, not combined with the dev files):

```
migrate:  one-shot `manage.py migrate --noinput`
app:      gunicorn config.wsgi:application --workers 1 --threads 4 (expose 8000, /health/ healthcheck)
worker:   manage.py run_studio_worker
postgres: postgres:16-alpine (optional; can be swapped for a Dokploy-managed DB via DATABASE_URL)
volumes:  postgres_data, media_data (mounted at /app/media in app and worker)
```

Dokploy's Traefik terminates TLS and proxies to `app:8000`. No host port is published and there is no second proxy. Env vars come from Dokploy's Environment tab. `config.settings.production` trusts `X-Forwarded-Proto` and exempts `/health/` from the SSL redirect. Gunicorn uses one worker because of memory (about 200 MB per warmed worker). `--max-requests` is left off on purpose (see the `Dockerfile` comment). `MALLOC_ARENA_MAX=2` is set on app and worker.

The image (`Dockerfile`, `python:3.12-slim`) installs FFmpeg, gettext and Node 20, builds Tailwind, compiles messages and runs `collectstatic`.

---

## 19. Backup and Restore

There is no backup management command. What needs backing up:

- **PostgreSQL**: all application data, including the job queue. Use `pg_dump` against `DATABASE_URL` (for example `docker compose -f docker-compose.dokploy.yml exec postgres pg_dump -U postgres brightbean | gzip > db.sql.gz`) or Dokploy's database backups.
- **Media**: the `media_data` volume when `STORAGE_BACKEND=local`. With S3, media lives in the bucket.
- **Secrets**: keep `SECRET_KEY` and `ENCRYPTION_KEY_SALT` with the backup. Without them, encrypted tokens and credentials can't be decrypted (§17).

To restore: load the dump into an empty database, restore the media volume, and deploy. The `migrate` service applies any newer migrations.

---

## 20. Configuration

`.env.example` is the reference for deployable settings. Groups:

- **Core:** `SECRET_KEY`, `ENCRYPTION_KEY_SALT`, `DEBUG`, `ALLOWED_HOSTS`, `APP_URL`, `LANGUAGE_CODE`, `SIGNUP_MODE`, `CUSTOM_SIGNUP_URL`, `DATABASE_URL`
- **Storage:** `STORAGE_BACKEND`, `MEDIA_ROOT`, `SERVE_MEDIA`, `S3_*`, `AWS_S3_MAX_MEMORY_SIZE`
- **Email:** `EMAIL_BACKEND_TYPE`, `EMAIL_HOST*`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL`
- **Platform apps:** `PLATFORM_FACEBOOK_*`, `PLATFORM_INSTAGRAM_*`, `PLATFORM_THREADS_*`, `PLATFORM_LINKEDIN_{PERSONAL,COMPANY}_*`, `PLATFORM_TIKTOK_*`, `PLATFORM_GOOGLE_*`, `PLATFORM_PINTEREST_*`, `YOUTUBE_COMPLIANCE_ENFORCEMENT`, `UNSPLASH_ACCESS_KEY`
- **Publisher:** `PUBLISHER_*` timeouts and concurrency
- **Webhooks:** `FACEBOOK_WEBHOOK_VERIFY_TOKEN`, `INSTAGRAM_LOGIN_WEBHOOK_VERIFY_TOKEN`, `YOUTUBE_WEBHOOK_SECRET`
- **Optional:** `INTELLIGENCE_*` / `STUDIO_*`, `MCP_PUBLIC_BASE_URL`, `MCP_OAUTH_ISSUER_URL`, `SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_PROFILES_SAMPLE_RATE`

These are read in `config/settings/base.py` but missing from `.env.example`: `SENTRY_DSN`, `REDIS_URL`, `BB_TRUSTED_PROXIES`, `RESEND_WEBHOOK_SECRET`, `EMAIL_SENDING_ENABLED`, `EMAIL_*_LIMIT`, `INVITE_*`, `ASAAS_*`, `BILLING_*`, `STORAGE_QUOTA_ENABLED`, `MAX_ACCOUNTS_PER_PLATFORM`, `INBOX_YOUTUBE_MIN_POLL_SECONDS`, `INBOX_DEEP_SWEEP_SECONDS`, `PUBLISHER_FIRST_COMMENT_*`.

---

## 21. Development and CI

- **Docker:** `docker compose up` merges `docker-compose.yml` with `docker-compose.override.yml`. You get `migrate`, `app` (runserver on :8000, source mounted), `worker`, `postgres` (:5432) and a `tailwind` watcher, all on `config.settings.development`.
- **Without Docker:** see README, "Fully Local Development". It uses `DATABASE_URL=sqlite:///db.sqlite3`, `npm install` + `npm run start` in `theme/static_src`, then `runserver` and `run_studio_worker` in separate terminals.
- **Development settings:** `ALLOWED_HOSTS=*`, console email with no caps, report-only CSP, `CSRF_TRUSTED_ORIGINS` for tunnels (ngrok), debug toolbar if installed.
- **CI** (`.github/workflows/ci.yml`): ruff check + format → mypy (`apps/ config/ providers/ tests/`) → pytest against Postgres (after `makemigrations --check`) → Docker image build → gitleaks. CI does not deploy anything. Deploys are triggered in Dokploy.
