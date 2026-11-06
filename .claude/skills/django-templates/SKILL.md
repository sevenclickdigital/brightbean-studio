---
name: django-templates
description: "Use when creating or editing Django templates, HTMX interactions, Alpine.js components or user-facing strings in templates/JS in this project. Covers partial layout, the HX-Trigger/showToast contract, translation in templates and JavaScript (json_script), Tailwind/token classes, and the template traps that already shipped (multi-line {# #}, missing load i18n, fuzzy catalogue entries, text selection on drag)."
---

# Templates, HTMX and Alpine in BrightBean

## Layout

- Pages extend `base.html`; app pages live in `templates/<app>/`, HTMX fragments in `templates/<app>/partials/_name.html`.
- Shared pieces: `templates/components/` and `templates/partials/`. Search them before writing markup (`reuse-check`).
- Error pages (`templates/errors/`, `400/403/404/500.html`) deliberately extend nothing and load no stylesheet — Django may render them with an empty context.

## HTMX contract

- Mutations are `POST` with CSRF (`base.html` adds `X-CSRFToken` to every HTMX request from its `{% csrf_token %}`).
- A state-changing action answers **204 + `HX-Trigger`**: `{"showToast": {"message": ..., "tone": "success|warn|error"}, "<refresh-event>": ...}`. Reference: `apps/approvals/views.py` and `apps/approvals/test_workflow.py`.
- Use `request.htmx` to return a partial for HTMX and the full page otherwise.
- Failures must reach the user: an error toast or an inline error partial, never a silent 200.

## Translation — every user-visible string

- `{% load i18n %}` in **every** template that uses `{% translate %}`/`{% blocktranslate %}`. Loads are not inherited by children or includes.
- Variables: `{% blocktranslate with name=account.name %}Connected {{ name }}.{% endblocktranslate %}` — never concatenate.
- Attributes: `placeholder="{% translate 'Search' %}"`. `aria-label`, `title`, `alt` and `data-*` attributes shown to the user count as visible text.
- **JavaScript:** a string literal in JS is invisible to `makemessages`. Translate in the template and pass it in:
  ```django
  {{ labels|json_script:"composer-labels" }}
  ```
  where `labels` is built in the view with `gettext`, or render the translated string into a `data-*` attribute. The composer's `cLabel` and the flow builder follow this pattern.
- Don't translate platform names, data from the database, CSS classes, Alpine attributes, URL names.
- After `makemessages`, open every new `#, fuzzy` entry: it was guessed from a similar string ("Insert" became "Inserir emoji", "Reference code" became "Preferências"). Fix the text and remove the flag. Full method and vocabulary: the `translation` skill.

## Comments

`{# #}` is **single-line only**. Spread over two lines it renders as page text (this shipped in the billing staff console). Use `{% comment %}…{% endcomment %}` for anything longer.

## Styling

- Use the tokens and component classes in `theme/static_src/src/styles.css` (`btn-brand`, `btn-*`, `.alert-*`, status pills via `approval_extras.status_badge`). See `docs/style-guide.html`.
- Component classes are not in a cascade layer, so Tailwind utilities can't override them (`btn-brand px-4` keeps the component padding). Add a variant instead of fighting it.
- New utility classes need the Tailwind watcher (`npm run start` in `theme/static_src`) or a build to appear.
- Check long text and narrow widths: overflowing columns ("0 / 10 GB0%") and clipped toolbars have shipped before.

## Alpine

- Gestures (drag, connect ports, pan): call `preventDefault` on the pointer event — `.stop` only stops Alpine propagation and the browser still selects text. Don't `preventDefault` when the press lands on an input, textarea, select or button, or the field can't take focus.
- Keep component state in `x-data` on the closest container; large builders (`templates/automations/flow_builder.html`) read server data from `json_script` tags.

## Before finishing

- The post-edit hook checks multi-line comments and `load i18n`; still render the page (runserver or a view test) when the change is more than copy.
- New strings: run `makemessages`, translate, no fuzzy left, `compilemessages`.
