---
name: translation
description: "Use whenever user-facing text is added or changed in this project — templates, Python messages/labels/choices/toasts, JavaScript labels — and when running makemessages/compilemessages or editing locale/pt_BR/LC_MESSAGES/django.po. Holds what to mark and what not to, how to mark it in templates, Python and JS, the Portuguese vocabulary table and style rules, and how to handle fuzzy entries. Trigger on 'traduzir', 'tradução', 'português', 'está em inglês', 'i18n', 'makemessages', '.po'."
---

# Translation (pt-BR)

`pt-br` is the default language and `en-us` stays available; the picker is in the account menu. Source strings are written in **English** and translated in `locale/pt_BR/LC_MESSAGES/django.po`. The `.mo` is a build artefact (gitignored, compiled in the `Dockerfile`); if it is missing the UI silently falls back to English.

## What to mark

Only what the customer reads: interface text, labels, buttons, titles, messages, placeholders, toasts, and `aria-label` / `title` / `alt` / `data-*` attributes whose value is shown.

## What NOT to mark

- Platform names: YouTube, Facebook, Instagram, LinkedIn, TikTok, Threads, Bluesky, Mastodon, Pinterest (trademarks).
- Data from the database: `{{ account.name }}`, `{{ post.caption }}`.
- CSS classes, Alpine attributes (`x-data`, `@click`), form field names, JSON keys, URLs, route names in `{% url %}`.
- Template comments and log messages.

## Templates

Load the tag in **every** file that uses it — loads are not inherited by child templates or includes, and a `{% translate %}` without it breaks the whole render with `Invalid block tag`:

```django
{% load static i18n %}
```

```django
<span>{% translate "Publish" %}</span>

{% blocktranslate with name=account.name %}Connected {{ name }} successfully.{% endblocktranslate %}

{% blocktranslate count counter=posts|length %}{{ counter }} post{% plural %}{{ counter }} posts{% endblocktranslate %}

<input placeholder="{% translate 'Search' %}">          {# single quotes inside the attribute #}

{{ org.name|default:_("My Organization") }}
```

Never build a sentence by concatenating translated pieces — use `blocktranslate` with variables so the translator sees the whole sentence.

## Python

- Module level (model fields, choices, form labels, constants): `from django.utils.translation import gettext_lazy as _`.
- Inside a function (messages, toasts, `HX-Trigger` text, emails): `from django.utils.translation import gettext as _`.
- Plurals: `ngettext`. Placeholders: `_("“%(name)s” was deleted.") % {"name": name}` — named, never positional or f-strings (f-strings can't be extracted).
- Settings modules: `config/settings/base.py` defines its own no-op `gettext_noop`; importing `django.utils.translation` there re-enters the settings loader.

## JavaScript

A string literal in JS is invisible to `makemessages`, so it can never be translated. Translate in the view or template and hand it over:

```django
{{ labels|json_script:"composer-labels" }}
```

with `labels` built with `gettext` in the view (or `{% translate %}` values in a dict), read in JS from the `json_script` tag. The composer's `cLabel` and the flow builder (`templates/automations/flow_builder.html`) follow this pattern. A translated `data-*` attribute is fine for a single label.

## Vocabulary

Consistency beats personal preference. Use this table; if a term is missing, add it here instead of improvising.

| English | Português |
|---|---|
| Publish | Publicar |
| Post (noun) | Publicação |
| Post (verb) | Publicar |
| Schedule | Agendar |
| Scheduled | Agendado |
| Draft | Rascunho |
| Idea | Ideia |
| Create | Criar |
| Edit | Editar |
| Delete | Excluir |
| Remove | Remover |
| Save | Salvar |
| Cancel | Cancelar |
| Back | Voltar |
| Next | Avançar |
| Settings | Configurações |
| Channel | Canal |
| Account | Conta |
| Connect | Conectar |
| Disconnect | Desconectar |
| Reconnect | Reconectar |
| Workspace | Workspace |
| Organization | Organização |
| Member | Membro |
| Team | Equipe |
| Owner | Proprietário |
| Admin | Administrador |
| Social Inbox | Caixa de entrada |
| Comment | Comentário |
| Reply | Resposta |
| Mention | Menção |
| Message | Mensagem |
| Automation | Automação |
| Rule | Regra |
| Trigger | Gatilho |
| Keyword | Palavra-chave |
| Flow | Fluxo |
| Analytics | Análises |
| Insights | Insights |
| Engagement | Engajamento |
| Followers | Seguidores |
| Reach | Alcance |
| Media | Mídia |
| Upload | Enviar |
| Preview | Prévia |
| Template | Modelo |
| Tag | Tag |
| Caption | Legenda |
| Loading | Carregando |
| Search | Buscar |
| Filter | Filtrar |
| No results | Nenhum resultado |
| Try again | Tentar novamente |
| Learn more | Saiba mais |

Known drift in the current catalogue (fix when touching those screens): `Delete` is translated "Deletar" in places — it should be "Excluir"; standalone `Template` became "Modelos" — singular "Modelo" unless the UI shows a list title.

Style:
- Address the user as **você**, never "tu" or "vós".
- Buttons and actions in the infinitive: "Criar publicação", not "Crie uma publicação".
- Sentence case, not Title Case: "Criar ideia", not "Criar Ideia".
- Prefer the term Brazilian customers use: "Caixa de entrada", not "Entrada social".

## After marking

```bash
.venv/bin/python manage.py makemessages -l pt_BR --ignore=".venv/*" --ignore="staticfiles/*" --ignore="node_modules/*"
# translate every empty msgstr "" in locale/pt_BR/LC_MESSAGES/django.po
.venv/bin/python manage.py compilemessages -l pt_BR
```

**Fuzzy entries:** `makemessages` pre-fills new strings with a guess from a similar one and flags them `#, fuzzy` ("Insert" became "Inserir emoji", "Reference code" became "Preferências"). `compilemessages` skips fuzzy entries, so they ship in English. Open each one, write the right translation and delete the `fuzzy` flag. The post-edit hook lists any fuzzy entry left in the `.po`.

## Verify

- Every template you touched still loads:
  ```python
  from django.template.loader import get_template
  get_template("composer/compose.html")
  ```
- No `#, fuzzy` and no empty `msgstr ""` left for strings you added.
- Switch the language to Português in the app and look at the screen: untranslated strings stand out.
