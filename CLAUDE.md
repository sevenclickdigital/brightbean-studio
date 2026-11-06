# BrightBean Studio (Seven Click fork)

Social media management platform: compose, schedule, approve, publish, inbox, analytics and comment automations across 13 platforms, multi-workspace, with billing per workspace. Fork of the open-source BrightBean Studio, run by one developer.

## Quick facts

- **Stack:** Django 5.1, Python 3.12 in production (the local `.venv` is 3.13 — don't rely on 3.13-only features), PostgreSQL 16, HTMX + Alpine.js (vendored), Tailwind 4 via `django-tailwind` (`theme/`).
- **Background jobs:** `django-background-tasks` rows run by our own worker (`apps/common/worker.py`, `manage.py run_studio_worker`). No Celery, no Redis queue.
- **Production:** Dokploy only, `docker-compose.dokploy.yml`. There is no Heroku/Railway/Render/Caddy anymore; don't reintroduce their config.
- **Python env:** the project venv at `.venv/` (pip + `requirements.txt`). Always call tools through it: `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/ruff`, `.venv/bin/mypy`.
- **Default language:** `pt-br` (`en-us` also available). Code, comments, docs and commit messages are English; only user-facing text is Portuguese, through the `.po` catalogue.

## Documentation — read before changing a subsystem

- `docs/architecture.md`: how the codebase is put together today (tenancy, worker, publishing, inbox/webhooks, automations, billing, API/MCP/OAuth, storage, i18n, security, deploy). Read the section of the subsystem you touch.
- `docs/features.md`: what each feature does and where it lives in the code.
- `docs/style-guide.html`: design tokens and the component classes that already exist.
- `docs/meta-analytics-manual-tests.md`, `docs/youtube-compliance-rollout.md`: operational checklists.

When the code and a doc disagree, the code is right — fix the doc in the same change.

## Non-negotiable rules

- **Nothing is created before looking for what already does it.** Search the layer first (a service in `apps/<app>/services.py`, a helper in `apps/common/`, a provider helper in `providers/meta_*.py`, a partial in `templates/components/` or `templates/partials/`, a CSS class in `theme/static_src/src/styles.css`). Prefer extending — one more argument, one more branch, an early return — over a second thing that drifts from the first. The `reuse-check` skill has the method; the post-edit hook asks for it on every new file.
- **Tenant isolation is explicit.** Nothing filters querysets automatically. Every query on tenant data goes through `Model.objects.for_workspace(request.workspace.id)` / `for_org(...)` or an explicit `workspace=` filter, and every `get_object_or_404` includes the workspace. Never trust an id from the URL or the POST body on its own.
- **Every view is gated.** Use the decorators in `apps/members/decorators.py` (`require_workspace_role`, `require_permission`, `require_org_role`, `require_org_permission`). Staff-only screens check `is_staff`. `RBACMiddleware` sets `request.org`, `request.workspace` and their memberships.
- **All user-facing text is translatable** (method and vocabulary: the `translation` skill):
  - Templates: `{% load i18n %}` in every file that uses `{% translate %}` / `{% blocktranslate %}` (loads are not inherited by includes or children).
  - Python: `gettext` / `gettext_lazy` for messages, form labels, model choices, toasts.
  - JavaScript: never a user-visible string literal in JS. Translate in the template and hand it over with `json_script` (as the flow builder and composer do), or via a translated `data-*` attribute.
  - After `makemessages`, fix every `#, fuzzy` entry and drop the flag — `compilemessages` skips fuzzy entries, so they ship in English.
- **Django template comments:** `{# #}` is single-line only. Anything longer uses `{% comment %}…{% endcomment %}`, otherwise the text renders on the page.
- **Model change ⇒ migration in the same change.** Run `.venv/bin/python manage.py makemigrations` and make sure `makemigrations --check --dry-run` is clean (CI fails otherwise). Migrations that touch existing rows need a test over seeded data (see `apps/inbox/tests/test_reply_lifecycle_migrations.py`).
- **Never swallow provider errors.** Classify them with `providers/exceptions.py` and surface them to the user (toast, account status, failed `PlatformPost`). A silent failure is worse than a visible one — several production bugs here were "everything looked healthy, nothing happened".
- **Secrets** live only in `.env` / `.env.dokploy` (gitignored). New settings go into `.env.example` and, if used in production, `.env.dokploy.example`. gitleaks runs in CI.
- **Never write change history in code**: no "added in", "previously", "v2", ticket refs or commented-out old code. Git is the history.

## Conventions by layer

### Views, HTMX and templates
- Function-based views, explicit `@require_POST` / `require_http_methods` on mutations.
- HTMX actions that change state answer `204` + `HX-Trigger` (`showToast` with `tone`, refresh events) — see `apps/approvals/views.py` and its tests. Use `request.htmx` to choose partial vs full page.
- Partials live in `templates/<app>/partials/_name.html`; shared pieces in `templates/components/` and `templates/partials/`.
- Styles come from the tokens and component classes in `theme/static_src/src/styles.css` (`btn-*`, `.alert-*`, status pills…). Check `docs/style-guide.html` before inventing a class. After CSS/template class changes, the Tailwind watcher or `npm run build` in `theme/static_src` must run.
- Alpine: pointer gestures that must not select text use `preventDefault`, not `.stop` (which only stops Alpine propagation).
- Skill: `django-templates`.

### Services and models
- Business logic lives in `apps/<app>/services.py` (or a named module in the app), not in views or templates. Views parse input, call the service, choose the response.
- Tenant models inherit `WorkspaceScopedModel` / `OrgScopedModel` (`apps/common/managers.py`).
- Use `select_related` / `prefetch_related` on lists; the sidebar and context processors already run on every page.

### Providers (`providers/`)
- One module per platform, subclassing `SocialProvider` (`providers/base.py`), registered in `PROVIDER_REGISTRY`. HTTP goes through the base `_request`, never raw `requests` calls.
- Meta logic is split into `meta_*.py` helpers; extend those instead of duplicating Graph API code in `facebook.py` / `instagram.py`.
- When a platform answers with a generic error, verify the real cause against the API (endpoint, field, edge) before adding retries.
- Skill: `provider-integration`.

### Background tasks
- Tasks are `@background(...)` functions in `apps/<app>/tasks.py`; recurring ones are registered in the app's `AppConfig.ready()` via `post_migrate` → `register_recurring_task` (`apps/common/background.py`).
- Tasks must be idempotent and take ids, not model instances. Publishing runs in its own lane; everything else shares the general lane — a slow task blocks the others.
- Skill: `worker-tasks`.

## Testing

- Tests run against **PostgreSQL** (`config.settings.test`, database `brightbean_test`), never SQLite. Local Postgres: `docker compose up postgres -d`.
- Write tests for what protects the app: tenant isolation, permissions, money/billing, publishing, provider payloads, webhooks, migrations over existing rows, and every fixed bug. Pure copy/layout changes don't need tests.
- Mock only external boundaries (provider `_request`, Asaas, email). Use the fixtures in the root `conftest.py` (`user`, `organization`, `org_owner`).
- Run the narrowest set that covers the change: `.venv/bin/pytest path/to/test_file.py -x -q`.
- Skill: `pytest-project`.

## Before calling a task done

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy apps/ config/ providers/ tests/ --ignore-missing-imports   # when Python types changed
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/pytest <affected tests> -x -q
.venv/bin/python manage.py check
```

Never report a task as finished with lint, migrations or the affected tests failing. If something could not be run (e.g. Postgres down), say so.

## Hooks

`.claude/hooks/post_edit.py` runs after every Write/Edit (configured in `.claude/settings.json`). It:
- formats edited Python with ruff and reports remaining lint errors;
- flags multi-line `{# #}` comments and translation tags without `{% load i18n %}` in templates;
- flags `#, fuzzy` entries in `django.po`;
- checks that an edited `models.py` has its migration;
- asks the reuse question when a new file is created.

When the hook reports a problem, fix it before moving on. When it asks the reuse question, answer in one line with what you searched — don't restate the question.

## Skills (`.claude/skills/`)

- `reuse-check` — before creating any file; when the hook asks.
- `django-templates` — templates, HTMX, Alpine.
- `translation` — any user-facing text, the `.po` catalogue, Portuguese vocabulary.
- `provider-integration` — adding or changing a social platform integration.
- `worker-tasks` — background tasks, recurring jobs, the worker.
- `pytest-project` — writing and running tests in this project.
- `django-safe-migration` (plugin from `vintasoftware/django-ai-plugins`, enabled in `.claude/settings.json`) — writing or reviewing any migration. Use it every time a migration is created, not only when asked.

## django-safe-migration
- Django version: 5.1
- Deploy strategy: rolling deploy. On Dokploy the one-shot `migrate` service runs to completion before `app` and `worker` are recreated, so the previous release keeps serving traffic against the new schema while it runs. Every migration must be safe for the old code.
- Runtime guard: none
- Migration command: `.venv/bin/python manage.py sqlmigrate <app_label> <migration_name>`

## MCP: django-ai-boost

`.mcp.json` starts `django-ai-boost` (read-only Django introspection, local development settings). Prefer it over guessing or grepping when you need facts about the running project: `list_models` / `database_schema` (fields, nullability, FK names before writing a model, migration or test), `list_urls` / `reverse_url`, `list_migrations`, `get_setting`, `run_check`, `read_recent_logs`. `query_model` reads the local development database.

It is installed only in the local `.venv`, not in `requirements.txt` (it is not a runtime dependency). To reinstall without touching pinned packages (e.g. `cryptography<44`):

```bash
.venv/bin/python -m pip freeze > /tmp/constraints.txt
.venv/bin/python -m pip install -c /tmp/constraints.txt django-ai-boost
```

## Commands

```bash
docker compose up                         # full dev stack (app :8000, worker, postgres, tailwind)
.venv/bin/python manage.py runserver 8003 # without Docker (see README "Fully Local Development")
.venv/bin/python manage.py run_studio_worker
cd theme/static_src && npm run start      # Tailwind watcher
.venv/bin/python manage.py makemessages -l pt_BR --ignore=".venv/*" --ignore="staticfiles/*" --ignore="node_modules/*"
.venv/bin/python manage.py compilemessages -l pt_BR
```

## Git

- Work happens directly on `main` or on a short branch, at the developer's choice. Commit or push only when asked.
- Commit subject: one imperative sentence describing the behaviour from the user's side, no `feat:`/`fix:` prefix (e.g. "Publish the post, even when Meta refuses the feeling"). The body explains the cause and why the fix has that shape.
- Never commit `.env*` (except the `*.example` files), dumps (`_local_dumps/`), `media/` or `test_media/`.
