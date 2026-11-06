---
name: worker-tasks
description: "Use when writing or changing background tasks, recurring jobs, publishing cycles or anything run by run_studio_worker in this project (django-background-tasks + the custom worker in apps/common/worker.py). Also when something 'looks healthy but nothing happens' — stuck locks, a lane blocked by a slow task, a job never registered."
---

# Background tasks and the worker

Read `docs/architecture.md` §7 (worker) and §6 (publishing) first.

## How it runs

- Tasks are `@background(schedule=0, ...)` functions (`background_task` package) in `apps/<app>/tasks.py`.
- `manage.py run_studio_worker` (`apps/common/worker.py`) runs **two lanes**: `publishing` (publish and confirmation) and `general` (inbox sync, analytics, health checks, media, maintenance, compliance). Each lane runs one task at a time. A slow general task delays every other general task.
- Recurring tasks are registered on `post_migrate` in the app's `AppConfig.ready()` with `register_recurring_task(func, repeat=..., verbose_name=...)` from `apps/common/background.py` (see `apps/accounts/apps.py`). A task that is defined but not registered never runs — check that it is registered.
- In production the worker is the `worker` service of `docker-compose.dokploy.yml`.

## Rules

- **Idempotent.** A task can run twice (retry, redeploy mid-run). Guard with state checks or `select_for_update`, not with hope.
- **Arguments are ids and plain values**, never model instances. Reload inside the task.
- **Bounded work per run.** Process in batches. Long loops starve the lane; split into a task per account or per page.
- **Errors are visible.** Log with context (account id, platform, task) and record the failure where a user or operator sees it. Swallowing exceptions is only acceptable with a comment explaining why (see `apps/common/tasks.py`).
- **Database connections are a budget.** Publisher threads each hold a connection. Peak demand must stay under 20 (`apps/publisher/test_connection_budget.py` enforces it against the gunicorn threads in `docker-compose.dokploy.yml`). Changing concurrency settings or `--threads` must keep that test green.
- **Locks.** A task killed mid-run keeps its lock row until `MAX_RUN_TIME`. The worker releases its own stale locks at startup (PID-named). Don't widen that to other names.

## Diagnosing "nothing happens"

1. Is the task registered? Look for its `Task` row and `repeat`.
2. Is it locked? `locked_by` / `locked_at` on the row.
3. Is the lane busy with something slow? Worker logs show `dispatch_delay_seconds` and `duration_seconds` per task.
4. Did it fail and get rescheduled? `attempts`, `last_error`, `CompletedTask` rows.

## Tests

Run the task synchronously with `.now(...)` (e.g. `check_social_account_health.now(str(account.pk))` in `apps/analytics/tests/test_backfill_queue.py`) and assert on state. For worker behaviour see `apps/common/tests/test_worker.py`.
