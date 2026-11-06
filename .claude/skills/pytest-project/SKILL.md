---
name: pytest-project
description: "Use when writing, fixing or running tests in this project: what deserves a test, PostgreSQL test setup, fixtures, mocking providers and external services, HTMX/HX-Trigger assertions, migration tests over seeded data, and how to run the narrowest set."
---

# Tests in BrightBean

## Setup

- Settings: `config.settings.test` (from `pyproject.toml`), database **PostgreSQL** `brightbean_test`. Never SQLite.
- `factory-boy` is in `requirements.txt` but no test uses it; build objects with `Model.objects.create(...)` like the existing tests.
- Local Postgres: `docker compose up postgres -d` (defaults `postgres/postgres@localhost:5432`).
- Run: `.venv/bin/pytest path/to/test_file.py -x -q`, or `-k name`. Full suite only when the change is broad: `.venv/bin/pytest -q`.
- Test files: `apps/<app>/tests/test_*.py`, `apps/<app>/test_*.py` or `tests.py`; provider tests in `tests/providers/`.

## What deserves a test

Tenant isolation (a user of workspace A can't read or mutate B's data — always assert the 404/403), permissions per role, billing and disk limits, publishing payloads and status transitions, provider payloads and error classification, webhook signature checks and parsing, migrations that touch existing rows, and **every bug fix** (the test fails before the fix).

Copy, layout and styling changes don't need tests.

## Style

- Both pytest functions and `django.test.TestCase` classes are used; follow the file you're in.
- Fixtures: `user`, `organization`, `org_owner` in the root `conftest.py`; `accounts`, `api`, `inbox` and `mcp` have their own `tests/conftest.py`. Reuse them before writing a local factory.
- Use `override_settings` for settings-dependent behaviour.
- `TransactionTestCase` only when the code under test needs real commits (threads, `select_for_update`, the publisher).
- Build realistic objects: models have defaults and required FKs (workspace, organization). Read the model before constructing it.

## Mocking

Mock only boundaries:
- Providers: replace `provider._request` with a `MagicMock` (see `tests/providers/test_facebook.py`).
- Asaas, email, Intelligence: patch the client function the service calls.
- Never mock the ORM or the code under test.

## HTMX views

Post to the view (add `HTTP_HX_REQUEST="true"` when it branches on `request.htmx`), then assert the status and decode `HX-Trigger`:
```python
trig = json.loads(response.headers["HX-Trigger"])
assert trig["showToast"]["tone"] == "error"
```
Reference: `apps/approvals/test_workflow.py`.

## Migrations

When a migration transforms existing rows, test it applied over seeded data with `MigrationExecutor` (see `apps/inbox/tests/test_reply_lifecycle_migrations.py`). CI also runs `makemigrations --check`.

## Before finishing

Run the affected tests and report the result honestly: if Postgres wasn't available or a test was skipped, say so.
