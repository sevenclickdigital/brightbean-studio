---
name: implementation-checklist
description: "Mandatory checklist for any implementation or code change in this project. Read it BEFORE starting to implement (so the plan already covers it) and run it AFTER finishing, before saying the task is done. Covers reuse, comments, tenant isolation and data exposure, scope, leftovers, consistency, edge cases, impact on existing callers, performance and verification (targeted tests, a quick script or the Chrome DevTools MCP). Trigger on any request to implement, build, add, change, fix or refactor code."
---

# Implementation checklist

Use this checklist **twice**:

- **Before implementing:** read it and plan with it in mind — where existing code lives, which tenant and permission rules apply, how you will verify the result.
- **After implementing:** run it on every file you wrote or changed, before saying the task is done.

For each question, **check the code for real** (read the files, grep), don't answer from memory. If an answer reveals a problem, **fix it before moving on** to the next question.

---

## 1. Reuse — is this code really needed?

- Search the project (grep names, terms and similar patterns) for a function, helper, service, template partial, template tag, provider helper or class that already does this or something close. Where to look: the `reuse-check` skill.
- Check whether an installed dependency already solves it (`requirements.txt`).
- If something reusable exists: use or extend it and remove your duplicate.
- If you created something new, make sure it is general enough to be reused later and lives where other agents would look for it.

## 2. Comments — useful information only, no history

Review every comment in the files you touched (yours and the existing ones near what you changed).

**Remove or rewrite** comments that only tell history:
- "Changed to fix bug X", "It used to be…, now…", "Added at the client's request"
- "New version", "Refactored", old TODOs already done
- Comments that repeat what the code says (`# increment i`)
- Commented-out code left behind

**Keep or write** comments that help the reader (human or agent):
- What a function/module does and returns, when the name doesn't make it obvious
- Why a non-obvious decision was made (business rule, API limitation, necessary workaround — e.g. "feelings only publish through /feed")
- Parameters, expected formats, side effects
- Important warnings ("must run inside a transaction", "order matters")

Change history belongs in git, not in the code.

## 3. Isolation and data exposure

If you implemented a view, URL, API endpoint (`apps/api`), MCP tool (`apps/mcp`), webhook, task, query or anything that can expose data:

- **Authentication/authorization:** is the view gated with the decorators in `apps/members/decorators.py` (or `is_staff` for staff screens, `portal_auth_required` for the client portal)? Can a user reach another workspace's or organization's data by changing an id in the URL or the POST body?
- **Scope:** does every query filter by the right tenant — `for_workspace(request.workspace.id)`, `for_org(...)`, or `workspace=` inside `get_object_or_404`? Nothing does this automatically.
- **Output:** does it return only the fields needed? No tokens, encrypted credentials, password hashes, internal ids of other tenants, or whole model dumps.
- **Input:** is input validated (forms, Ninja schemas) and safe against SQL injection (no raw SQL with string formatting), XSS (no `|safe`/`mark_safe` on user data), path traversal and mass assignment?
- **Webhooks:** is the signature checked before the payload is trusted?
- **Secrets:** no key, token or password hard-coded, logged or returned in an error. New settings go to `.env.example`.
- **Errors:** error messages don't leak stack traces, paths or database structure.

## 4. Scope — did I do only what was asked?

- Did I change files or behaviour unrelated to the task? If so, undo it or explain why to the user.
- Did I leave anything half-done (placeholder, `TODO`, empty function, hard-coded test value)?

## 5. Leftovers and junk

- Remove `print`, `breakpoint()`, `pdb`, `console.log`, `debugger` and debug logging.
- Remove imports, variables, functions, templates and files left unused.
- Remove temporary or test files you created that are not part of the delivery (scripts in `/tmp`, scratch templates).

## 6. Consistency with the project

- Same naming, folders, style and architecture as the rest of the project (`CLAUDE.md`, `docs/architecture.md`)?
- Same libraries and approaches already in use (forms/Ninja schemas for validation, services for business logic, `providers/base._request` for HTTP, `HX-Trigger` + `showToast` for HTMX feedback, `gettext` for text) instead of a new way?
- All user-facing text translatable and translated (`translation` skill), no `#, fuzzy` left?
- Did I add a dependency? Was it really needed, and does it respect the existing pins?

## 7. Errors and edge cases

- What happens with empty, null, invalid or very large input?
- Are external failures (platform API down or returning an error, Asaas, email, storage, missing file) handled and shown to the user without breaking everything?
- Are operations that must be atomic inside `transaction.atomic()`? Are background tasks idempotent (`worker-tasks` skill)?

## 8. Impact on existing callers

- Did I change a function signature, response format, field name, URL or database structure? Are all callers updated (grep them — views, tasks, templates, `apps/api`, `apps/mcp`)?
- Does a database change have a migration, safe for the old code still running during deploy (`django-safe-migration`)?
- Could anything in production that relied on the old behaviour break?

## 9. Performance

- Queries inside loops (N+1)? Use `select_related` / `prefetch_related`.
- Loading more data than needed (no pagination, whole tables)?
- A heavy operation on every request that could be cached or moved to a background task? Remember context processors run on every page.

## 10. Verification

Verify proportionally to the change — only what is actually needed, never more:

- **Always:** the code runs. Ruff passes on the changed files (the post-edit hook already checks this), `.venv/bin/python manage.py check` is clean, and `makemigrations --check --dry-run` is clean when models changed.
- **Tests — only the specific area, and only when needed.** If the change touches logic that existing tests cover, or logic that deserves protection (tenant isolation, permissions, billing, publishing, provider payloads, webhooks, a bug fix), run only the tests of that area: `.venv/bin/pytest apps/<app>/tests/test_<area>.py -x -q` (or `-k <name>`). Add or update a test there when the change needs one (`pytest-project` skill). Don't run the full suite — CI does that.
- **When an elaborate test isn't needed,** check the behaviour directly instead:
  - a quick script, e.g. `.venv/bin/python manage.py shell -c "…"` or a throwaway file in `/tmp`, deleted afterwards;
  - or the app in the browser with the **Chrome DevTools MCP** (`mcp__chrome-devtools__*`): start `.venv/bin/python manage.py runserver 8003`, open the page, interact with it, take a snapshot/screenshot, and check the console and network requests for errors. Use it for screens, HTMX swaps, Alpine interactions and layout (including a narrow viewport).
- **Copy, layout or styling only:** no test; look at the screen (Chrome DevTools MCP) instead.
- **Documentation:** if the change affects behaviour described in `docs/architecture.md`, `docs/features.md`, `README.md`, `CLAUDE.md` or a skill, update it in the same change. If the task fixed a mistake that could happen again, add it as a rule in `CLAUDE.md` or the relevant skill.

---

## Final report

When done, tell the user in a few lines:

- What was fixed because of the checklist (if anything).
- How the change was verified (which tests, script or browser check) — or what could not be verified and why.
- Anything that **could not be resolved** or needs the user's decision (e.g. a security risk, a compatibility break).

Don't list the 10 questions with "ok" one by one. Report only what changed or needs attention.
