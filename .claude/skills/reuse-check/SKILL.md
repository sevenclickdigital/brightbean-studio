---
name: reuse-check
description: "Use before creating any new module, view, service, task, template partial, template tag, JS or CSS in this project, and whenever the `[reuse-check]` hook asks whether a new file had to exist. Holds where twins usually hide in this codebase, how to decide between extending and creating, and when a new file IS right. Trigger on 'isso já não existe?', 'dá para reaproveitar?', 'precisa criar mesmo?', or any doubt about duplicating logic."
---

# Does this need to exist?

One question, answered with evidence: **could this have been an existing thing, slightly extended?**

"I think it's new" is not an answer. "I grepped `providers/meta_*.py` for `attached_media` and found only `meta_feed_extras.build_feed_payload`, which handles text posts and cannot stage photos" is.

## Where twins hide here

| Writing a… | Look first in | Typical twin |
|---|---|---|
| Provider call | `providers/base.py` (`_request`, `_poll_until`, error classification), `providers/meta_*.py` | the same Graph API edge already wrapped for another post type |
| Business logic | `apps/<app>/services.py`, `apps/common/` | the same verb on a neighbouring model |
| Permission check | `apps/members/decorators.py`, `effective_permissions` | a hand-written `if membership.role ==` |
| Background job | `apps/<app>/tasks.py`, `apps/common/background.py` | a task that already loops over the same accounts |
| Template partial | `templates/components/`, `templates/partials/`, `templates/<app>/partials/` | status pills, empty states, modals, toasts |
| Template tag/filter | `apps/*/templatetags/` (e.g. `approval_extras.status_badge`) | a filter formatting the same value |
| CSS | `theme/static_src/src/styles.css`, `docs/style-guide.html` | `btn-*`, `.alert-*`, cards |
| JS behaviour | inline Alpine components in the same template, `static/js/` | a drag/drop or picker already built for the flow builder or composer |
| Toast / HTMX response | `HX-Trigger` + `showToast` pattern in `apps/approvals/views.py` | a new ad-hoc message mechanism |
| Test fixture | root `conftest.py`, the app's `tests/conftest.py` | a local `make_user()` helper |

Useful sweeps:

```bash
git grep -n "def <verb>" -- apps providers        # the behaviour, not the name
git grep -n "<edge or field name>" -- providers   # for API work
ls templates/components templates/partials templates/<app>/partials
```

## Extend or create

**Extend** when the existing thing does the same job for a neighbouring case: one more parameter, one more branch, an early return.

**Create** when one of these is true — and say which:
- the reason to change is different (a change in one would never imply the other);
- extending needs a flag that makes the existing thing do two unrelated jobs;
- the existing thing is already too big to understand at a glance — split it, don't pile on.

## When the answer is "it should not exist"

Delete it and use what is there. Don't keep both "to unify later": a duplicate that ships stays.

## Not a license to over-abstract

Two things that merely look alike are not one thing. If the only way to unify is a boolean that switches behaviour wholesale, keep them apart and say so.
