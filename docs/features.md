# Feature Reference

What the product does today, and where each feature lives in the code. It replaces the original planning spec (`development_specs/feature-spec-social-media-management-v2.md`, now only in git history). Section numbers (F-x.y) follow the spec where a feature came from it. Features added later have no number.

Conventions used below:

- Everything is scoped Organization → Workspace. Most models carry a `workspace` FK and use `WorkspaceScopedManager` (`apps/common/managers.py`).
- Workspace URLs are mounted under `/workspace/<workspace_id>/...` (see `config/urls.py`).
- Background work runs in the `process_tasks` worker (django-background-tasks; `apps/common/background.py`, `apps/common/worker.py`). There is no Celery or Redis requirement.
- Defaults referenced as `publishing.retry_max_attempts` etc. come from `apps/settings_manager/defaults.py` (`APP_DEFAULTS`), and can be overridden per org/workspace via `OrgSetting` / `WorkspaceSetting` rows. Those rows are edited in Django admin. The `/settings/` page is a static page with no editor.
- UI languages are Português (Brasil) and English (US) (`LANGUAGES` in `config/settings/base.py`). The language picker posts to `/i18n/`. Translation rules are in `.claude/skills/translation/SKILL.md`.

## Supported platforms

From `README.md`. Provider implementations are in `providers/` (one module per platform, all implementing `providers/base.py`).

| Platform | Publish | Comments | DMs | Insights |
|---|:-:|:-:|:-:|:-:|
| Facebook Pages | ✓ | ✓ | ✓ | ✓ |
| Instagram (via Facebook Login) | ✓ | ✓ | ✓ | ✓ |
| Instagram (Direct / Instagram Login) | ✓ | ✓ | ✓ | ✓ |
| LinkedIn Personal / Company | ✓ | ✓ | — | ✓ |
| TikTok | ✓ | — | — | ✓ |
| YouTube | ✓ | ✓ | — | ✓ |
| Pinterest | ✓ | — | — | ✓ |
| Threads | ✓ | ✓ | — | ✓ |
| Bluesky | ✓ | ✓ | — | — |
| Google Business Profile | ✓ | — | — | ✓ |
| Mastodon | ✓ | ✓ | — | — |
| DEV.to | ✓ | — | — | — |

X/Twitter is not supported. Instagram (Direct) still works but is hidden from the connect page by default, because new Meta apps no longer expose the Instagram App ID it needs. It is hidden through `PlatformVisibility` (`apps/social_accounts/models.py`) and can be turned back on in admin.

---

## 1. Organization, workspaces, members

### F-1.1 Organizations — `apps/organizations/`
- `Organization`: name, logo URL, default timezone, billing email.
- Settings page `/organizations/settings/`. The workspace list is at `/organizations/workspaces/`.
- Deletion: the owner requests it. That sets `deletion_requested_at` and schedules `execute_scheduled_org_deletion` (`apps/organizations/tasks.py`) for **14 days** later, and the deletion can be cancelled during that window. YouTube connections and copied YouTube API data are removed immediately when deletion is requested (`apps/social_accounts/youtube_compliance.py`). After deletion the owner is redirected to `CUSTOM_SIGNUP_URL` if it is set.

### F-1.2 Workspaces — `apps/workspaces/`
- `Workspace`: name, icon, description, timezone, primary/secondary brand colour, default hashtags, default first comment, approval workflow mode, `is_archived`.
- Create, list and settings pages, plus a separate approvals settings page (`/workspaces/<id>/settings/approvals/`).
- Cross-workspace calendar at `/organizations/calendar/` (`apps/organizations/views.py`).
- A workspace can connect at most `MAX_ACCOUNTS_PER_PLATFORM` (default **3**) accounts per platform (`apps/social_accounts/views.py`). Reconnecting an account that already exists is always allowed.

### F-1.3 Members & RBAC — `apps/members/`
- Org roles: Owner, Admin, Member (`OrgMembership`). Org-level permissions are `manage_intelligence_billing`, `use_intelligence` and `manage_api_keys`.
- Workspace roles: Owner, Manager, Editor, Contributor, Client, Viewer (`WorkspaceMembership`). The permission keys in `PERMISSION_KEYS` are create/edit-others/approve/publish-directly posts, manage social accounts, view analytics, use inbox, reply from inbox, manage workspace settings, and upload/edit/delete/manage media. The role-to-permission map is `BUILTIN_ROLE_PERMISSIONS`.
- `CustomRole` exists and is honoured by permission checks (`effective_permissions`), but custom roles can only be created and assigned in Django admin.
- Invitations (`Invitation`): invite, resend, revoke, accept-by-token. Invites expire after 7 days (`org.invitation_expiry_days`) and are single-use even if two signups use them at the same time. Per-member workspace assignment is at `members/<id>/workspaces/`.
- Enforcement: `apps/members/decorators.py` (`require_permission`, `require_org_permission`) and `apps/members/middleware.py`.

### F-1.4 Client portal — `apps/client_portal/`
- Clients are `WorkspaceMembership` rows with the Client role. They are invited from workspace settings → Clients (`urls_admin.py`).
- Access is passwordless: a magic link (`MagicLinkToken`) is valid for 30 days, and the expired-link page lets the client ask for a new one.
- Portal pages under `/portal/`: dashboard, approval queue (approve / request changes / reject / **request hold**), published posts, activity. `/portal/reports/` is a placeholder page.

### F-1.5 Platform API credentials — `apps/credentials/`
- `PlatformCredential` stores encrypted per-org app credentials for each platform and is edited in Django admin.
- `resolve_platform_credentials()` uses `.env` values (`PLATFORM_*`) when they cover all the keys a platform requires, and falls back to the org's admin-entered row otherwise. The connect page only offers platforms that have credentials configured.

### F-1.6 Configurable defaults — `apps/settings_manager/`
- The three-level lookup (app default → org → workspace) is in `helpers.py`. The keys in use include approval reminder hours, first-comment delay (120 s), retry attempts/backoff (3, at 1/5/30 min), inbox SLA target, notification quiet hours/digest, and client connection-link expiry.

### F-1.7 Client onboarding — `apps/onboarding/`
- **Connection links** (`ConnectionLink`): a manager creates a tokenised link with an expiry of 1 to 90 days (default 7), sends it by email, and can revoke it. The client opens a public page with no login, connects accounts through OAuth (or Bluesky/Mastodon forms), and the accounts land in the workspace. Each connection is recorded in `ConnectionLinkUsage` and triggers a `client_connected_accounts` notification.
- **Get Started checklist** on the workspace dashboard (`checklist.py`): connect accounts, create first post, create first idea, invite team. It can be dismissed per user (`OnboardingChecklist`).

---

## 2. Content creation & publishing

### F-2.1 Composer — `apps/composer/`
- Model: `Post` holds the shared content (title, caption, first comment, internal notes, tags, category, schedule). It has one `PlatformPost` per target `SocialAccount`, which can override title, caption, media and first comment and carries platform-specific options in `platform_extra`. **Status lives on `PlatformPost`.** The `Post` status is derived from its children (`apps/composer/status.py`).
- `PlatformPost.Status`: draft, pending_review, pending_client, approved, changes_requested, rejected, scheduled, publishing, published, failed, on_hold.
- Post types (`providers/types.py`): text, image, video, carousel, story, reel, link, article, poll, pin, short. The publisher resolves the type from the media actually attached.
- Live per-platform preview (`compose/preview/`) follows each platform's caption override. The Facebook preview renders as a feed grid.
- Media: pick several items from the library at once, upload, import from Unsplash (needs `UNSPLASH_ACCESS_KEY`), and drag carousel items into publish order. Video thumbnails come from a picker or an upload.
- Platform helpers: Pinterest board list, TikTok creator info (privacy/interaction options), Facebook places and feed extras (`providers/meta_feed_extras.py`).
- First comment: per post or per platform. It is posted after the publish confirms (see F-2.4).
- Autosave, version history (`PostVersion`, with diffs at `approvals/<post_id>/versions/`), clone, delete, drafts list.
- Templates (`PostTemplate`): save-as-template and a template picker, plus built-in templates (`builtin_templates.py`).
- Content categories (`ContentCategory`, with colours) and tags (`Tag` with kind internal / hashtag / mention; a tag can both file the post and be published with it).
- **Idea board**: a Kanban of `Idea` items (unassigned / todo / in progress / done) in user-defined `IdeaGroup` columns, with media. An idea can be turned into a post.
- **RSS feeds** (`Feed`, `curated_feeds.py`): subscribe to feeds per workspace, with an "explore" catalogue of curated feeds, for content inspiration.
- **CSV import** (`import/csv/`): upload a CSV (max 5 MB), preview it, then confirm to bulk-create posts.
- **Per-post comment automation**: a flow can be attached to a post from the composer (see Automations).

### F-2.2 Approval workflow — `apps/approvals/`
- Workspace mode (`Workspace.ApprovalWorkflowMode`): none, optional, required internal, or required internal + client.
- Actions: submit, approve, request changes, reject, resubmit, resume, and hold (from the client). They apply to the whole post or to a single `PlatformPost`, and every action is logged in `ApprovalAction`. A bulk action endpoint exists.
- Threaded comments (`PostComment`) with internal or external visibility, edit/delete (soft-deleted with `deleted_at`), @mentions (which send a `comment_mention` notification), and attachments served through a permission-checked view.
- Reminders and escalation (`tasks.py`): internal reviewers after 24 h, clients after 48 h, at most 2 reminders per post, then a stalled-approval escalation.

### F-2.3 Calendar & scheduling — `apps/calendar/`
- Month/week/day calendar plus a list mode. Drag-to-reschedule (`reschedule/`) and bulk actions.
- The Publish tabs (queue / drafts / approvals / sent) are in `views.py`.
- **Posting slots** (`PostingSlot`): recurring weekly time slots per account.
- **Queues** (`Queue`, `QueueEntry`): named queues that put posts into the next free slot. Entries can be reordered, removed and re-slotted.
- **Recurring posts** (`RecurrenceRule`: daily / weekly / monthly, interval, end date). The posts are generated by `calendar/tasks.py`, 90 days ahead.
- **Custom calendar events** (`CustomCalendarEvent`), and a holiday / awareness-day overlay from bundled JSON (`holidays.py`, `data/`).

### F-2.4 Publishing engine — `apps/publisher/engine.py`
- `PublishEngine.poll_and_publish` finds due `scheduled` PlatformPosts, moves them to `publishing`, and dispatches them in parallel. Concurrency is bounded per post, per platform and globally (`infra.max_concurrent_publish_jobs`), and publishing capacity is reserved in the shared worker.
- Retries use exponential backoff (default 3 attempts). Non-retryable errors fail at once with a readable message (`apps/social_accounts/error_messages.py`), and a `post_failed` notification is sent.
- Publish confirmation: posts that publish asynchronously (for example video processing) are polled until confirmed, and the engine gives up on stale ones (`confirm_pending_publishes`).
- After a publish confirms, the engine posts the first comment (2-minute delay by default) and arms any comment automation attached to the post.
- `PublishLog` keeps a publish audit log for 90 days, and `RateLimitState` tracks per-account upstream rate limits.
- `apps/common/quota.py` is a shared circuit breaker for platform quotas, mainly for YouTube's per-client Data API budget.
- Posts that can only fail are refused at schedule time.

### F-2.5 Social account connection — `apps/social_accounts/`
- OAuth connect with PKCE where supported (`oauth_pkce.py`) and an account picker for Pages and locations, including Google Business location selection. Bluesky (app password), Mastodon (instance plus a per-instance app registration, `MastodonAppRegistration`) and DEV.to (API key) connect through forms.
- `SocialAccount` tracks a connection status (connected / token_expiring / disconnected / error) and a separate analytics status.
- Token refresh runs ahead of expiry (`token_refresh.py`), and there is a periodic health check. Accounts can be reconnected, have their webhooks retried, or be disconnected, which revokes the token upstream where the platform supports it.
- Meta webhook subscriptions are managed in `social_accounts/webhooks.py`.

---

## 3. Inbox & engagement

### F-3.1 Unified inbox — `apps/inbox/`
- `InboxMessage` types: comment, mention, DM, review. Statuses: unread, open, resolved, archived.
- Messages arrive two ways:
  - Webhooks at `/webhooks/facebook/`, `/webhooks/instagram_login/` and `/webhooks/youtube/`. Meta requests are signature-checked.
  - The polling sync engine (`tasks.py` `InboxSyncEngine`, every 5 min by default), with historical backfill. YouTube polling uses bounded, resumable history walks to save quota.
- Replies follow a draft → send lifecycle (`InboxReply`: draft / sent / failed). The inbox UI, the REST API and MCP all go through the same layer (`services.py`).
- **DMs** (Facebook, Instagram via Facebook Login, Instagram Direct). Send eligibility is in `reply_policy.py`, which enforces Meta's messaging windows: a standard reply within 24 h of the customer's last message, and a human-agent reply up to 7 days. Outside those windows sending is blocked with a reason. Instagram DM OAuth scopes are controlled by `PLATFORM_INSTAGRAM_FACEBOOK_DM_SCOPES_ENABLED` (on by default).
- Assignment, internal notes (`InternalNote`), saved replies (`SavedReply`), bulk actions, and auto-resolve on reply.
- Sentiment: a keyword-based classifier (`sentiment.py`) labels messages positive / neutral / negative, and a user can override it manually.
- SLA (`InboxSLAConfig`): a response-time target per workspace, with an `inbox_sla_overdue` notification.

### Automations (comment-to-reply / comment-to-DM) — `apps/automations/`
Not in the original spec. It is adapted from BrightBean Chat and runs on **Facebook, Instagram and Instagram Direct** comments only (`matching.COMMENT_PLATFORMS`). In the "new rule" chooser, Google Business is shown as "coming soon".

- **Rules** (`AutomationRule`) belong to one account, or to all compatible accounts when no account is set.
  - Matching: include/exclude keywords with five modes (`keywords.py`): exact, contains, any_word, and accent-insensitive contains_loose / any_word_loose. Rules can be scoped to all posts or to specific posts, and to top-level comments only.
  - Rules are evaluated in priority order and the first match wins (`matching.py`).
  - Replies: a public comment reply and/or a private reply (DM). Each uses a fixed text or a random variation. The `ai` mode is declared but inert.
  - Guards (`guards.py`): once per sender per post, `max_per_hour` (default 30), and the account's own comments are skipped. Every decision is logged in `AutomationLog`, with outcomes sent / skipped_* / no_match / failed.
  - New rules start disabled. A rule has a "test" action, and the log is at `automations/log/`.
- **Flow builder** (`flow_views.py`, `flow.py`, `engine.py`, `templates/automations/flow_builder.html`): a canvas of nodes and edges.
  - Node types: `trigger_comment`, `reply_comment`, `send_dm`, `send_card` (image card, picked from the media library), and `condition`. A condition branches on comment text or on the sender's profile: follows us, we follow them, verified, follower count.
  - Messages can carry buttons. A postback button parks the run in `FlowRun`, keyed by account and sender, until the person taps it.
  - Execution: `flow.py` compiles the canvas into the rule's fields. `engine.py` walks the graph, sends at most 5 messages per wake (`MAX_STEPS_PER_WAKE`), stops cycles, and abandons the run on the first send failure.
- **Per-post flows** (`PostAutomation`): a flow is bound one-to-one to a `PlatformPost`. If the post is still a draft or scheduled, the flow waits as `pending`. The publisher arms it (`services.arm_for_platform_post`) when the post is confirmed live, and on an already-published post it arms on save. Existing scheduled or published posts can be chosen from `flow_choose_posts`.
- **Flow templates** (`FlowTemplate`): a global library edited by staff at `/automations/staff/templates/` (create, edit canvas, details, thumbnail, duplicate, publish, reorder). Authors start a flow from a published template through `templates/`. The graph is copied, so later edits to the template do not reach flows already created from it.

---

## 4. Analytics — `apps/analytics/`

### F-4.1 Per-account and per-post analytics
- Snapshot tables: `AccountInsightsSnapshot` (daily account metrics), `PostInsightsSnapshot`, and `YouTubePeriodReport` (native YouTube reports, cache-only for display).
- The metric catalog for each platform is in `metrics.py`. Engagement rate and period deltas are computed in `derive.py`, and values the product calculates itself are labelled as estimates.
- Pages: workspace overview (`index`), per account (KPI cards, 7/30/90-day trend charts, sortable post table), and post detail.
- Sync cadence (`tasks.py`):
  - On connect: a backfill.
  - Account metrics: daily.
  - Post metrics: hourly for posts under 24 h old, every 6 h up to 7 days, daily up to 30 days, weekly up to 90 days, then no more syncing.
  - Days with no activity are stored as zeros. Days the platform did not report are drawn as gaps.
- Instagram analytics have their own access check, separate from Page publishing (`instagram_access.py`). Analytics are paused when a provider quota is hit (`ProviderQuotaBlock`).

### History import (not in spec) — `history_import.py`
- From an account's analytics page (`<account_id>/history/`), the user can import posts the channel already had. The import uses `SocialProvider.list_existing_posts`. Imported posts become ordinary `PlatformPost` rows, marked in `platform_extra`, and the normal sync then collects their metrics.
- The account chart is also backfilled for the days before the connection, where Meta's API returns a daily series: Facebook page insights (all four metrics) and Instagram reach. Days the product already measured, and today, are never overwritten.

---

## 5. Platform configuration

### F-5.1 Authentication — `apps/accounts/`
- Email/password signup and login through django-allauth, Google SSO (`GOOGLE_AUTH_CLIENT_ID`), terms acceptance, and an account settings page.
- `SIGNUP_MODE` = `open` | `invite` | `closed`. `CUSTOM_SIGNUP_URL` replaces every "Sign up" link and, in `invite` or `closed` mode, is where blocked signups are sent (`signup_links.py`, `adapters.py`).
- Auth POSTs are rate-limited per IP: 10 attempts per 60 s (`apps/accounts/middleware.py`). Sessions last 14 days (`SESSION_COOKIE_AGE`).
- 2FA: the TOTP fields exist on `User`, but there is no setup or enforcement flow yet.
- **Operator impersonation** (`impersonation.py`, `views_impersonation.py`): staff can sign in as a customer for support. Staff cannot impersonate other staff. Start and stop are POST-only, and every session is recorded in `ImpersonationSession`, which can be viewed in the staff console.

### F-5.2 Branding
- Per-workspace logo/icon and primary/secondary colours (`Workspace`), plus the organization logo URL. There is no custom domain or email white-labelling.

### F-5.3 Integrations
- Unsplash stock search and import in the composer.
- Outgoing webhooks are a notification channel (see F-7.1).
- The REST API and MCP server are described below. There are no other design, communication or storage integrations.

### Agent API — `apps/api/`, `apps/api_keys/`
- REST API built with Django Ninja at `/api/v1/`, with OpenAPI docs at `/api/v1/docs`. Routers: `me`, `accounts`, `posts` (create, get, patch, schedule, cancel), `media` (upload, get), `analytics` (account, post), `inbox` (list, get, reply drafts, send).
- **API keys** (`ApiKey`, managed at `/organizations/api-keys/`, requires `manage_api_keys`):
  - A key belongs to one workspace and has an explicit allowlist of social accounts and a subset of permission keys. On each request its permissions are intersected with the issuer's current permissions, so the key stops working if the issuer loses membership.
  - Token format is `bb_studio_<secret>_<lookup>`. Only an HMAC hash of the secret is stored. Keys can have an expiry and can be revoked or edited.
  - `ApiKeyAuditLog` records audited actions.
- Limits (`limits.py`): per-key, per-workspace and per-IP throttles; hourly and daily call caps counted in `ApiKeyUsageHourly`; and per-platform daily post caps per account (`PLATFORM_DAILY_POST_LIMIT`, e.g. Instagram 25, TikTok 15, Threads 250). Writes accept idempotency keys (`IdempotencyRecord`).

### MCP server — `apps/mcp/`
- Streamable HTTP MCP endpoint (POST only, tools only) at `/api/v1/mcp`. It accepts the same bearer API keys, or OAuth tokens (below).
- 20 tools (`handlers.py`):
  - Posts: list_accounts, create_draft, schedule_post, schedule_draft, get_post, list_posts, cancel_post.
  - Media: search_media, get_media, upload_media, request_media_upload, finalize_media_upload.
  - Analytics: get_account_analytics, get_post_analytics.
  - Inbox: list_inbox_messages, get_inbox_message, create_reply_draft, update_reply_draft, discard_reply_draft, send_reply.

### OAuth server for MCP clients — `apps/oauth_server/`
- An OAuth 2.1 authorization server (django-oauth-toolkit) so MCP clients such as Claude Desktop can sign users in directly. Endpoints: `/oauth/authorize/`, `/oauth/token/`, `/oauth/revoke_token/`.
- Open Dynamic Client Registration (`/oauth/register`, RFC 7591, rate-limited), with metadata documents under `/.well-known/`.
- PKCE is required and only `S256` is accepted (`validator.py`).

### Intelligence — `apps/intelligence/`
- An optional integration with the separate BrightBean Intelligence service. It is enabled only when `INTELLIGENCE_INTERNAL_URL`, `INTELLIGENCE_PUBLIC_URL` and the related secrets are set, otherwise every Intelligence surface is hidden.
- Org-scoped at `/orgs/<org_id>/intelligence/`.
  - Subscription: plan picker, checkout through Intelligence (Stripe), recovery after a closed tab, portal, billing contact. State is kept in `IntelligenceSubscription`, `StudioCheckoutAttempt` and `PendingActivation`.
  - Tools playground: score packaging, score video hook, benchmark channel or video, research content gaps, list niches.
- Calls go through `services/client.py`, which signs internal calls with HMAC and uses a bearer key per org for tool calls. Using the tools needs `use_intelligence` and an active subscription. The Intelligence plan also sets the org storage tier (`STORAGE_QUOTA_TIERS`).

---

## 6. Media library — `apps/media_library/`

### F-6.1
- Workspace libraries (`/workspace/<id>/media/`) and a shared org library (`/organizations/media/shared/`).
- Nested folders, tags with autocomplete, starring, search, move, download, and alt text, title and attribution fields.
- Background processing (`tasks.py`) creates thumbnails and platform-optimised variants (`processed_variants`) and reports processing status. Images can be edited and videos trimmed. Each edit is a `MediaAssetVersion`, and earlier versions can be restored.
- Large uploads go through `PendingUpload` (a direct-upload handshake also used by the API and MCP). Orphaned uploads are swept.
- Storage limits, both of which must pass (`quotas.py`):
  - Org cap: an infrastructure guard, 5 GB by default or the Intelligence tier.
  - Workspace cap: the billed quota (see Billing).

---

## 7. Notifications — `apps/notifications/`

### F-7.1
- Channels: in-app, email, and webhook (HTTP POST with an HMAC-SHA256 signature).
- Events (`EventType`): post submitted, approved, changes requested, rejected, published and failed; new inbox message; inbox SLA overdue; client approval requested; team member invited; social account disconnected; comment mention; approval reminder; stalled approval; client hold requested; client connected accounts. `report_generated` and `engagement_alert` are defined but nothing sends them.
- Per-user preferences for each event and channel, quiet hours (`QuietHours`), digest/batched email, an in-app drawer with an unread count, and unsubscribe links in emails.
- Delivery attempts are tracked with a retry cap (`NotificationDelivery`).
- Outgoing email is rate-limited (`EmailSendCounter`). Bounces and complaints add the address to a suppression list (`EmailSuppression`) through the Resend webhook at `/webhooks/resend/` (`apps/common/webhooks.py`).

---

## Billing (not in spec) — `apps/billing/`

- The billable unit is the **workspace**. There is one plan with every feature, priced per workspace per month: R$ 50,00 (`BILLING_WORKSPACE_PRICE_CENTS`), with 10 GB of disk included. Extra disk is sold as `WorkspaceStorageAddon` rows, at R$ 2,00 per GB per month by default (a placeholder price). Money is stored in integer cents and disk in bytes.
- Gateway: **Asaas** (`gateways/asaas.py`; `ASAAS_API_KEY`, `ASAAS_API_BASE` defaults to sandbox).
  - The Organization is the Asaas customer, and each workspace is its own Asaas subscription (`contracting.py`). Contracting is idempotent.
  - Subscribing (`/workspace/<id>/billing/`) leaves the subscription `pending`. Only the Asaas webhook (`/webhooks/asaas/`, authenticated by the `ASAAS_WEBHOOK_TOKEN` header) makes it `active`. The webhook maps payment confirmed/received to active, overdue, refund or chargeback to past_due, and subscription deleted to canceled. Events are deduplicated in `BillingEvent`.
- Courtesy (comp) access: per workspace (`courtesy` status) or per account (`OrganizationBillingProfile.courtesy_workspace_quota` with an optional expiry date, expired by `tasks.expire_courtesies`).
- Disk enforcement (`storage_usage.py`): a warning banner at 90% in the media library and composer, and uploads blocked at 100%.
- Paid status is answered by `services.is_workspace_active`. Today it gates only storage. Other features do not yet check it.
- **Staff console** (`/billing/staff/`, `is_staff` only, with its own admin layout):
  - Customer accounts table: grant or revoke courtesy, set the courtesy quota, edit the account, send a password reset, activate or deactivate users, impersonate.
  - Plans and prices: editable at runtime, stored in `ProductPrice` with a history in `ProductPriceChange`, and override the env defaults.
  - Subscription list with counts, and the impersonation log.

---

## Operational and compliance

- Public legal pages for Meta App Review: `/privacy/`, `/terms/` and `/data-deletion/` (`apps/common/legal_views.py`).
- YouTube compliance (`youtube_compliance.py`, `YouTubeRevocation`): handles grant revocation and the retention lifecycle of YouTube data.
- Field-level encryption of tokens and credentials (`apps/common/encryption.py`, keyed by `ENCRYPTION_KEY_SALT`).
- `/health/` endpoint. Stale task locks are released when the worker starts.

---

## Not implemented from the original spec

- F-1.3 Custom roles UI (the model exists but is admin-only).
- F-1.4 Client portal reports (the page is a placeholder) and per-portal branding.
- F-1.6 Settings UI for org/workspace defaults (overrides only through admin).
- F-1.7 "First content batch" onboarding step.
- F-2.1 AI writing assist in the composer.
- F-2.3 Optimal posting-time suggestions (only the setting keys exist).
- F-4.2 Cross-account / cross-workspace analytics dashboard and engagement alerts.
- F-4.3 Report builder (sections, templates, PDF export, scheduled reports, share links).
- F-5.1 Two-factor authentication (TOTP fields only; no setup or enforcement).
- F-5.2 Custom domain and email white-labelling.
- F-5.3 Design tools (e.g. Canva), communication (Slack/Teams), and external storage integrations.
- F-8.1 Global search.
- F-8.3 Post trash / soft delete with restore.
- F-8.4 GDPR data export (ZIP).
- F-8.6 Link shortening, UTM management and link-click tracking.
- F-8.7 Category analytics (categories and their calendar colours exist).
