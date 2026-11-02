# Análise: resposta automática a comentários (e DMs) no BrightBean Studio

**Data:** 2026-11-02
**Fork analisado:** `sevenclickdigital/brightbean-studio` (branch `main`, commit `e0b448c`)
**Referência portada:** `brightbeanxyz/brightbean-chat` (commit `7bf959b`, 2026-10-21)
**Licenças:** ambos AGPL-3.0 — portar código entre eles é permitido, mantendo atribuição.

> Esta etapa é **apenas análise**. Nenhum arquivo do Studio foi alterado além da
> criação deste relatório.

---

## 0. Conclusão executiva

O Chat resolve o problema de forma mais granular do que o briefing assumia. O
que importa para a v1 **não é o flow builder** — é um subsistema específico,
`apps/flows/triggers/`, de ~4.700 linhas, que é quase todo agnóstico de
plataforma e de flow. Os três módulos centrais (`keywords.py`, `guards.py`,
`matching.py`) podem ser portados quase literalmente.

Três achados que mudam o plano:

1. **O Studio não sabe enviar private reply.** `providers/meta_messaging.py:274`
   (`build_send_payload`) só monta `{"recipient": {"id": psid}}`. A private reply
   da Meta exige `{"recipient": {"comment_id": <id>}}`. Isso é a única capacidade
   de provider realmente **nova** que a v1 precisa — todo o resto é orquestração.
   Ver §3.2 e §5.2.

2. **O ponto de plug é único e já existe.** Tanto o webhook quanto o polling
   convergem para criar `InboxMessage`, e ambos chamam `_notify_new_message`
   logo após um `created=True`. Esse é o gatilho natural, sem signals.

3. **O Chat usa fila própria (`apps/queueing`) com `idempotency_key`**, que o
   Studio não tem. Com `django-background-tasks` a idempotência tem que vir do
   banco (constraint `UNIQUE`), não da fila — e é exatamente assim que
   `guards.py` do Chat já funciona. Ver §5.3.

**Esforço estimado da v1:** 7 etapas, ~40–56 h. Ver §7.

---

## 1. Como o Chat implementa gatilhos e respostas

### 1.1 Modelagem dos gatilhos

`apps/flows/models.py:324` — `Trigger(WorkspaceScopedModel)`:

| Campo | Papel |
|---|---|
| `flow` (FK) | o que executar |
| `channel_connection` (FK, **nullable**) | `NULL` = "todas as conexões de plataforma compatível" |
| `type` | `comment`, `keyword`, `welcome`, `ref_url`, `story_mention`, `story_reply`, `follow`, `default_reply`, `rule`, `api` |
| `config_json` | configuração por tipo, validada por JSON Schema |
| `enabled` | liga/desliga |
| `priority` | int, menor primeiro |

Dois detalhes de projeto que vale copiar:

- **`Meta.ordering = ["priority", "created_at", "id"]`** — não é cosmético. Com
  `priority` sozinho, dois gatilhos empatados fazem a mesma mensagem iniciar
  flows diferentes em dias diferentes, conforme o plano do Postgres. O
  desempate explícito torna isso determinístico.
- **`save()` deriva `workspace` do flow e recusa conexão de outro workspace**
  (`WorkspaceMismatchError`). É a defesa contra cross-tenant no ponto de join.
- **Não há `unique_together`** em `(workspace, type, connection)`: dois gatilhos
  concorrentes são legais e resolvidos por prioridade. A UI avisa da duplicata
  em vez de o banco estourar `IntegrityError` num toggle.

### 1.2 Casamento de palavras-chave

`apps/flows/triggers/keywords.py` (71 linhas, **o módulo mais portável de todos**
— não importa nada do projeto):

- Três modos: `exact`, `contains`, `any_word`.
- A distinção que importa: `"cat"` em `any_word` casa *"my cat sleeps"* e **não**
  casa *"category"*; `contains` casa os dois.
- `normalise()` usa **`casefold()`, não `lower()`** — comentário explícito no
  código: `lower()` erra ß alemão e İ turco. Para pt-BR isso não é teórico com
  acentuação.
- **Não há normalização de acentos.** "açucar" não casa "acucar". Para o mercado
  brasileiro isso é uma lacuna real que eu recomendo fechar na porta (§5.1).
- `MAX_MATCH_CHARS = 8192` — teto de varredura, por latência e por
  SECURITY-BASELINE. O tokenizador `\w+` é Unicode, então "café" é uma palavra.
- Tokeniza a mensagem **no máximo uma vez** por chamada, mesmo com 100 keywords.

**Não há modo regex.** Decisão deliberada e, na minha opinião, correta: regex
fornecida por usuário num caminho que roda a cada evento inbound é ReDoS. Não
recomendo adicionar.

### 1.3 Escolha entre regras concorrentes

`apps/flows/triggers/matching.py:221` — `match()`:

- `EVENT_TRIGGER_TYPES` (linha 65) filtra *quais tipos* um evento pode
  selecionar. Para nós: `EventType.COMMENT → (TriggerType.COMMENT,)`.
- `candidates()` (linha 172) faz **uma query** com os filtros: `type__in`,
  `enabled=True`, `flow__status=ACTIVE`, conexão casando ou `NULL`, e um
  `Exists()` de versão publicada.
  - O `Exists` **não é otimização**: sem ele um gatilho apontando para flow sem
    versão publicada *ganharia* o match e depois engoliria o evento — o gatilho
    que deveria casar em seguida nunca rodaria. Vale guardar esse raciocínio: o
    equivalente no Studio é "regra cujo texto de resposta está vazio".
- `match()` para **no primeiro matcher que diz sim**; não coleta candidatos.
- Matchers são um **registry** (`register_matcher`), não uma cadeia de `if`s.
- Um matcher que levanta exceção é logado e **pulado** — o próximo em ordem de
  prioridade pode ser o certo. Config malformada não derruba o evento.
- **Matchers são livres de efeito colateral**, por contrato explícito
  (`matching.py:270`): o claim é escrito na stage, não no matcher, senão um
  gatilho que *perdeu* o match deixaria um claim para trás.

O matcher de comentário, `_match_comment` (linha 260), é o alvo direto da v1:

```python
if config.get("post_scope") == "specific":   # filtro por post
    if not context.post_id or context.post_id not in post_ids: return False
if config.get("top_level_only") and not context.is_top_level_comment: return False
exclude = _plain_keywords(config.get("exclude_keywords"))
if exclude and keyword_matching.matches_any(context.text, exclude): return False
include = _plain_keywords(config.get("include_keywords"))
if include: return keyword_matching.matches_any(context.text, include)
return True   # sem include = casa tudo
```

### 1.4 Engine de execução

`apps/flows/triggers/hooks.py` + `stages.py` — pipeline de 5 estágios, cada um
um registry de hooks. Ordem (SPEC §9.3):

1. `hard_optout` — consome opt-out para nada mais agir sobre ele
2. `resume` — oferece o evento à execução esperando resposta
3. `trigger` — primeiro gatilho que casa, por prioridade ← **o nosso**
4. `default_reply` — ninguém casou, responde 1×/contato/24 h
5. `post_persist` — regras de inbox (labels, assign)

Cada hook retorna `Passed()` ou `Consumed(reason)`. Todas as recusas no caminho
de comentário são `Passed`, não `Consumed` — um comentário que não vamos
responder deve deixar a cadeia livre.

**Inline vs. fila:** `budget.py` dá 1,5 s de orçamento para o caminho inline do
webhook; estourando, enfileira. O responder do Instagram
(`instagram.py:1471`) sempre enfileira as respostas: "um INSERT contra dois
handshakes TLS". Para o Studio, cujo webhook já é rápido, **a regra é a mesma:
enfileire sempre**, via `@background`. Não precisamos do `budget.py`.

**Delays e condições** vivem nos nós do flow (`engine/nodes/smart_delay.py`,
`condition.py`), não nos gatilhos. **Fora de escopo da v1** e a parte que eu
recomendo não portar (§2).

### 1.5 Idempotência — a peça mais importante

`apps/flows/triggers/guards.py` (227 linhas). Toda a correção concorrente do
sistema está aqui, e o princípio é um: **"posso fazer isso uma vez?" é
respondido pelo banco, não por um read seguido de write.**

```python
# guards.py:91 — record_comment()
# "The IntegrityError is the guard."
try:
    with transaction.atomic():
        row = HandledComment(...); row.save(); return row
except IntegrityError:
    return None
```

`HandledComment` (`models.py:415`) carrega **duas constraints diferentes**:

| Chave | Regra |
|---|---|
| `(connection, comment_id)` | redelivery da Meta — nunca responder 2× ao mesmo comentário |
| `(connection, post_id, commenter_ref)` quando `once_per_contact_per_post` | 1 resposta por pessoa por post |

E duas travas de gasto único, ambas **compare-and-set**:

- `claim_public_reply()` (linha 187) — `UPDATE ... WHERE public_reply_sent_at IS NULL`.
  De duas execuções concorrentes, exatamente uma vê `updated == 1`. Tomado
  **antes** da chamada à API, não registrado depois: a falha que isso previne é
  um comentário duplicado no post de um cliente, e um claim gasto numa resposta
  que falhou custa só aquela resposta.
- `claim_default_reply()` (linha 48) — janela **rolante** de 24 h medida do
  último envio real, não alinhada ao relógio. Por isso não é um rate-limiter.

Três detalhes finos que eu recomendo copiar junto:

1. **IDs de plataforma são hasheados, não truncados** (`_bounded`, linha 133):
   dois comment_ids distintos com o mesmo prefixo de 200 chars virariam uma
   chave só, e o segundo comentário seria descartado como "já tratado".
2. **O relógio é nosso** (linha 124): `commented_at=min(commented_at, moment)`.
   Um comentário datado na semana que vem compraria dias extras de janela.
3. **`may_claim_comment` é checado ANTES de `record_comment`** (linha 213): um
   comentário que aparece 8 dias atrasado consumiria o único claim daquela pessoa
   naquele post em troca de uma resposta que a plataforma vai recusar.

### 1.6 Ações

**Resposta pública** — `instagram.py:1446`:
```python
def reply_to_comment(token, comment_id, text):
    call(token, f"{comment_id}/replies", {"message": text})
```
O Studio **já tem isso**, e melhor: `providers/facebook.py:1100` trata a
profundidade de 2 níveis do Facebook e o `parent_id` que nomeia o post.

**Variações de texto** — `instagram.py:1451` `_public_reply_text()`:
`{"mode": "static"|"random", "texts": [...]}`, com `secrets.choice` (ruff
flake8-bandit bane `random`). ~18 linhas, porta como está.

**Resposta privada (comentário→DM)** — é onde o Chat é esperto e o Studio é
cego. `instagram.py:1594` `_open_thread()`: em vez de chamar a Send API
diretamente, **re-despacha o comentário pelo pipeline de ingest** com um marcador
`private_reply_claimed`. O ingest então cria o contato, o registro de consent e a
janela de messaging — porque ele é o único write site desses campos. O roteamento
casa o gatilho uma segunda vez, agora com contato em mão, e a primeira mensagem
do flow *é* a private reply.

Esse rodeio existe por uma invariante de arquitetura do Chat (contract 3,
verificado por scan AST) que **o Studio não tem**. Para nós, a chamada direta é
mais simples e correta: ver §5.2.

**Variáveis** — o Studio já tem `SavedReply.render()` (`models.py:229`) com
`{sender_name}`, `{account_name}`, `{post_url}`. Reaproveitar, não reescrever.

### 1.7 Regras da Meta tratadas no Chat

| Regra | Onde | Observação |
|---|---|---|
| Private reply: 7 dias desde o comentário | `guards.py:39` `PRIVATE_REPLY_WINDOW` | ⚠️ O Chat usa **7 dias**, não 24 h. A janela de 24 h é para DM↔DM; private reply de comentário tem 7 dias. O briefing diz "24h" — está incorreto para este caso. |
| 1 private reply por comentário | `guards.py:165` `may_private_reply` + `private_reply_sent_at` | |
| 1 resposta pública por comentário | `guards.py:187` `claim_public_reply` | |
| Ignorar próprios comentários | `apps/channels/providers/*` no parse | O Studio já tem, melhor: `webhooks.py:184` `_is_own_activity` |
| Instagram não dá like em comentário | `instagram.py:1701` `supports_like=False` | Capacidade como **dado**, não branch de template |
| Rate limits / erros Graph | `providers/base.py`, `APIError.code` | Falha de resposta pública é logada e engolida; **não custa a privada** |
| Janela de 24 h / HUMAN_AGENT (DM) | `apps/messaging/compliance.py` | O Studio já tem equivalente superior: `reply_policy.py` |

### 1.8 Handoff para humano

O Chat **não usa a string "Needs a person"** em nenhum lugar do código — o
briefing descreve um rótulo de UI que não corresponde a um identificador. O
mecanismo real é `Conversation.automation_paused_until`
(`apps/messaging/models.py:298`):

- `pause_automation(conversation, until)` (`messaging/services.py:168`) — **único
  write site**, por contrato.
- Resposta de agente humano no inbox → pausa de 30 min
  (`AGENT_AUTOMATION_PAUSE`, `views.py:1128`).
- A pausa é **por conversa/contato**, não por regra.

### 1.9 Webhooks: Chat vs. Studio

| | Chat | Studio |
|---|---|---|
| Rotas | `apps/channels/urls_webhooks.py`, por plataforma | `/webhooks/facebook/`, `/webhooks/instagram_login/` (`apps/inbox/webhook_urls.py`) |
| Normalização | `NormalizedEvent` + `EventPayload` + `parse_events` por adapter | `_handle_facebook_change` → `_upsert_*` → `_create_if_new`; shape = `extra` dict |
| Persistência | `apps/messaging/ingest.py` (`persist_events`, 1 transação por evento) | `_create_if_new` com `get_or_create` |
| Pós-persistência | pipeline de 5 stages | `_notify_new_message` apenas |
| Fila | `apps/queueing` própria, com `idempotency_key` | `django-background-tasks` |

**A diferença que mais afeta a porta:** o Chat tem uma camada de normalização
(`NormalizedEvent`) e o Studio não — o Studio guarda o payload bruto da Meta em
`InboxMessage.extra`, já parcialmente normalizado (`stored_post_id`, `post_id`,
`parent_id` saneado). Isso é **suficiente** e na prática mais simples: o
`MatchContext` do Studio pode ler `InboxMessage` diretamente, sem camada
intermediária. Ver §4.1.

---

## 2. O que reaproveitar

| Componente do Chat | Arquivo(s) | Decisão | Justificativa |
|---|---|---|---|
| Matching de palavras-chave | `apps/flows/triggers/keywords.py` (71 l.) | **Portar como está** + 1 adição | Não importa nada do projeto. Único ajuste: modo `ignore_accents` para pt-BR (§5.1). |
| Guards de idempotência | `apps/flows/triggers/guards.py` (227 l.) | **Adaptar** (mecânica idêntica, nomes de model trocados) | A lógica — IntegrityError-como-guard, compare-and-set, hash de ID, clamp de relógio — é o núcleo da correção. Trocar `HandledComment`→`AutoReplyLog`, `connection`→`social_account`. Remover `DefaultReplyState` (fora da v1). |
| Variações de texto público | `instagram.py:1451` `_public_reply_text` (~18 l.) | **Portar como está** | `static`/`random` + `secrets.choice`. Encaixa direto. |
| Matcher de comentário | `matching.py:260` `_match_comment` (~28 l.) | **Portar como está** | post_scope, include/exclude, top_level_only. Lê de `config_json` já no formato certo. |
| Resolução de prioridade | `matching.py:172-238` + `Trigger.Meta.ordering` | **Adaptar** | Query + "primeiro match vence" + registry de matchers + matcher que levanta é pulado. Simplificar: só 1 tipo na v1, mas manter o registry (a v2 de IA e de DM entram por registro). |
| Registry de responders | `apps/flows/triggers/comments.py` (168 l.) | **Reescrever (muito menor)** | Desacopla plataforma de decisão, mas existe porque no Chat os adapters se auto-registram e `INSTALLED_APPS` não pode importar de volta. O Studio já tem `providers.get_provider(platform, credentials)` — o despacho polimórfico já está resolvido. Manter só `supports_like` como dado. |
| Schema de validação de config | `triggers/schema.py` (188 l.) + `flows/schema/fields.py` | **Reescrever como Django Forms** | `additionalProperties: false` estrutural é elegante, mas depende de um interpretador JSON Schema próprio (`flows/schema/`) que o Studio não tem. Um `ModelForm` + `clean_*` entrega a mesma garantia no padrão do Studio e sem dependência nova. |
| Pipeline de 5 stages | `triggers/hooks.py` (276 l.) + `stages.py` (323 l.) | **Reescrever (1 função)** | Generalidade comprada para `resume`, `default_reply`, `opt_out` e nós de flow. A v1 tem **um** estágio. Uma função de ~60 l. no lugar de 600. |
| Orçamento inline | `triggers/budget.py` (188 l.) | **Não portar** | Depende de `django.core.cache` e de um circuito de conexão lenta. A regra para o Studio é mais simples: enfileire sempre. |
| Fila com `idempotency_key` | `apps/queueing/` | **Não portar** | Briefing proíbe broker; `django-background-tasks` fica. A idempotência vem do `UNIQUE` do banco (que é como `guards.py` já funciona). |
| Private reply (recipient por `comment_id`) | `instagram.py:800`, `messenger.py:1687` | **Adaptar — capacidade nova** | `{"recipient": {"comment_id": id}}`. O Studio não tem. Entra em `providers/meta_messaging.py`. |
| Re-despacho pelo ingest | `instagram.py:1594` `_open_thread` | **Não portar** | Rodeio por invariante de arquitetura do Chat (write-site único de `window_expires_at`) que o Studio não tem. Chamada direta é mais simples e correta aqui. |
| Flow builder / engine de nós | `apps/flows/engine/` (12 módulos) | **Não portar** | Fora de escopo. Delay/condição/randomizer pressupõem `FlowExecution` com estado e um worker de waits. |
| Post picker | `instagram.py:1648` `recent_media` | **Adaptar (opcional)** | O Studio já tem `composer.PlatformPost` — a v1 pode oferecer um select dos posts já conhecidos, sem chamar a Graph API. |
| Inbox rules | `apps/inbox/rules.py` (406 l.) | **Referência de design** | Não é resposta automática (ações: label/assign/done). Mas `RuleInput.from_event` / `.from_message` — um matcher, duas portas, uma para webhook e outra para dry-run — é o padrão exato que recomendo para o "testar regra" da v1. |

---

## 3. Pontos de integração no Studio

### 3.1 Onde as mensagens da inbox são criadas

Os dois caminhos convergem, e **ambos já marcam o momento exato do plug**:

| Caminho | Arquivo:linha | Função |
|---|---|---|
| Webhook | `apps/inbox/webhooks.py:445` | `_create_if_new(...)` |
| ↳ ponto de plug | `apps/inbox/webhooks.py:484-492` | `get_or_create(...)` → `if created:` → `_notify_new_message(obj)` |
| Polling (5 min) | `apps/inbox/tasks.py:583` | `InboxSyncEngine._upsert_message(...)` |
| ↳ ponto de plug | `apps/inbox/tasks.py:608-625` | `get_or_create(...)` → `if created:` → `_notify_new_message(obj)` |

**Recomendação:** um único ponto de entrada,
`apps.automations.services.on_inbox_message_created(message)`, chamado nos dois
locais imediatamente após `_notify_new_message`. Vantagens:

- Nada de signals (consistente com o Studio, que não usa `post_save` para isso).
- `created=True` já é garantido pelo `get_or_create`, então o fan-out da
  automação não roda em redelivery de webhook — **a primeira camada de
  idempotência sai de graça**, antes mesmo do guard.
- A função é um `@background` enqueue de uma linha: zero latência no webhook.

Cuidado em `webhooks.py:294` (`FACEBOOK_FEED_EDIT_VERBS`): um comentário editado
cai no caminho de `update`, não de `create`, e portanto **não** dispara
automação. Isso é o comportamento desejado — não responder duas vezes porque a
pessoa corrigiu um typo.

### 3.2 O serviço que envia respostas hoje

`apps/inbox/services.py` — usado por HTMX, REST `/api/v1/inbox/{id}/replies` e
MCP `send_reply`. O docstring é explícito: "os três não podem divergir".

| Função | Linha | Uso na automação |
|---|---|---|
| `send_reply(message=, body=, author=None, origin=)` | `:240` | **Reaproveitar direto** para resposta pública |
| `send_reply_now(reply, actor=, origin=)` | `:178` | Lock de linha + estados `draft`/`sent`/`failed` |
| `_dispatch_to_platform(...)` | `:72` | Resolve credenciais, provider e edge (comment vs. messaging) |
| `reply_failure_reason(exc)` | `:50` | Mensagens de erro legíveis já mapeadas |
| `create_reply_draft(...)` | `:134` | **Chave para a v2 de IA**: modo rascunho já existe no model |

A automação deve chamar `send_reply(message=msg, body=texto, author=None,
origin=PROGRAMMATIC)`. `author=None` já é suportado (`InboxReply.author` é
`null=True`), e com `origin=PROGRAMMATIC` a `reply_eligibility` **recusa**
automaticamente DM fora da janela de 24 h sem HUMAN_AGENT (`reply_policy.py:112`)
— ou seja, a trava de janela para DM já vem embutida, sem código novo.

Isso também dá, de graça: `InboxReply` como log de envio, `platform_reply_id`,
`send_error`, e o side-effect de SLA (`_apply_post_send_side_effects`).

**A lacuna:** `providers/meta_messaging.py:274` `build_send_payload` monta só
`{"recipient": {"id": recipient_id}}`. Para private reply é preciso
`{"recipient": {"comment_id": <comment_id>}}`. Mudança sugerida — aditiva e
retrocompatível:

```python
def build_send_payload(recipient_id=None, text="", *, human_agent=False, comment_id=None):
    recipient = {"comment_id": comment_id} if comment_id else {"id": recipient_id}
    payload = {"recipient": recipient, "message": {"text": text},
               "messaging_type": "MESSAGE_TAG" if human_agent else "RESPONSE"}
    ...
```

E `reply_to_message` em `providers/facebook.py:1066` e
`providers/instagram_login.py:624` passa `comment_id` quando `extra` o traz.
`providers/instagram.py:617` idem.

### 3.3 Models para amarrar as regras

| Model | Arquivo | Como amarrar |
|---|---|---|
| `Workspace` | `apps/workspaces/models.py` | `FK` da regra; escopo primário |
| `SocialAccount` | `apps/social_accounts/models.py:23` | `FK` **nullable** da regra = "todas as contas de plataforma compatível" (padrão do Chat). Campos úteis: `platform`, `account_platform_id`, `webhook_target_id`, `needs_reconnect`, `oauth_access_token`, `missing_scopes` |
| `InboxMessage` | `apps/inbox/models.py:11` | Entrada do gatilho. `message_type`, `body`, `extra`, `related_post`, `sender_handle` |
| `InboxReply` | `apps/inbox/models.py:135` | **Saída já modelada**: `draft`/`sent`/`failed`, `platform_reply_id`, `send_error` |
| `SavedReply` | `apps/inbox/models.py:200` | `render()` para variáveis |
| `PlatformPost` | `apps/composer/models.py` | Filtro por post específico, sem chamar a Graph API |
| `WorkspaceScopedManager` | `apps/common/managers.py` | `objects = WorkspaceScopedManager()` + `.for_workspace(id)` |

### 3.4 Como as tarefas em background são registradas

Padrão em três partes, uniforme nos 15 apps que o usam:

1. `@background(schedule=0)` na função (`apps/inbox/tasks.py:699`).
2. Constante de intervalo ao lado (`INBOX_SYNC_INTERVAL_SECONDS = 5 * 60`).
3. `AppConfig.ready()` conecta `post_migrate` → `register_recurring_task(...)`
   (`apps/inbox/apps.py` + `apps/common/background.py`).

Para a v1 precisamos de **duas** tasks, e só a segunda é recorrente:

- `run_automation_for_message(message_id)` — **não recorrente**, enfileirada por
  evento. É só `@background(schedule=0)` chamada com o UUID.
- `purge_automation_logs()` — recorrente, housekeeping (o Chat tem
  `triggers/housekeeping.py` equivalente).

Nota: `@background` serializa argumentos em JSON, então passe `str(message.id)`,
não a instância.

---

## 4. Proposta para a v1

### 4.1 App novo: `apps/automations/`

Separado de `apps/inbox/` de propósito: o inbox já tem 2.800 linhas, e a
automação precisa de models, views, forms, tasks e templates próprios. Segue
`apps/` conforme o briefing.

```
apps/automations/
  __init__.py
  apps.py              # AppConfig + post_migrate → register_recurring_task
  models.py            # AutomationRule, AutomationLog
  keywords.py          # PORTADO de flows/triggers/keywords.py (+ acentos)
  guards.py            # ADAPTADO de flows/triggers/guards.py
  matching.py          # MatchContext + registry de matchers + match()
  actions.py           # resposta pública e DM, via inbox.services
  services.py          # on_inbox_message_created() — ponto de entrada único
  tasks.py             # run_automation_for_message, purge_automation_logs
  forms.py             # AutomationRuleForm
  views.py             # list/create/edit/toggle/log/test
  urls.py
  admin.py
  migrations/
  tests/
templates/automations/
  list.html  form.html  log.html
  partials/_rule_row.html  _log_row.html  _test_result.html
```

### 4.2 Models

```python
class AutomationRule(models.Model):
    """Regra de resposta automática. Portado conceitualmente de
    brightbean-chat apps/flows/models.py::Trigger (AGPL-3.0,
    github.com/brightbeanxyz/brightbean-chat)."""

    class TriggerType(models.TextChoices):
        COMMENT = "comment", "Comentário em post"
        # v2: DM = "dm", MENTION = "mention"

    class ReplyMode(models.TextChoices):
        STATIC = "static", "Texto fixo"
        RANDOM = "random", "Variação aleatória"
        AI     = "ai",     "IA (v2)"        # declarado, não implementado

    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace  = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                   related_name="automation_rules")
    # NULL = todas as contas de plataforma compatível (padrão do Chat).
    # CASCADE, não SET_NULL: NULL já significa algo específico aqui, e uma
    # conta deletada alargando a regra de 1 conta para todas é o único
    # resultado que não pode acontecer.
    social_account = models.ForeignKey("social_accounts.SocialAccount",
                                   on_delete=models.CASCADE, null=True, blank=True,
                                   related_name="automation_rules")
    name         = models.CharField(max_length=200)
    trigger_type = models.CharField(max_length=20, choices=TriggerType.choices,
                                   default=TriggerType.COMMENT)
    is_active    = models.BooleanField(default=False)   # nasce desligada
    priority     = models.IntegerField(default=0)

    # --- condições ---
    include_keywords = models.JSONField(default=list, blank=True)  # [{"text","mode"}]
    exclude_keywords = models.JSONField(default=list, blank=True)
    post_scope       = models.CharField(max_length=10, default="all")  # all|specific
    post_ids         = models.JSONField(default=list, blank=True)
    top_level_only   = models.BooleanField(default=False)

    # --- ação pública ---
    public_reply_enabled = models.BooleanField(default=True)
    public_reply_mode    = models.CharField(max_length=10, choices=ReplyMode.choices,
                                            default=ReplyMode.STATIC)
    public_reply_texts   = models.JSONField(default=list, blank=True)

    # --- ação privada (DM) ---
    private_reply_enabled = models.BooleanField(default=False)
    private_reply_mode    = models.CharField(max_length=10, choices=ReplyMode.choices,
                                             default=ReplyMode.STATIC)
    private_reply_texts   = models.JSONField(default=list, blank=True)

    # --- travas ---
    once_per_sender_per_post = models.BooleanField(default=True)
    max_per_hour             = models.PositiveIntegerField(default=30)

    # --- v2 IA: declarado agora, inerte ---
    ai_provider    = models.CharField(max_length=20, blank=True, default="")  # openai|ollama
    ai_model       = models.CharField(max_length=100, blank=True, default="")
    ai_prompt      = models.TextField(blank=True, default="")
    ai_draft_only  = models.BooleanField(default=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, related_name="created_automation_rules")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "automation_rule"
        # Desempate explícito: com priority sozinho, duas regras empatadas
        # fazem o mesmo comentário disparar regras diferentes conforme o
        # plano do Postgres (Chat: flows/models.py::Trigger.Meta).
        ordering = ["priority", "created_at", "id"]
        indexes = [
            models.Index(fields=["workspace", "trigger_type", "is_active"],
                         name="autom_rule_ws_type_active"),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(priority__gte=0),
                                   name="autom_rule_priority_non_negative"),
        ]
```

```python
class AutomationLog(models.Model):
    """Uma execução + as duas travas duráveis. Adaptado de brightbean-chat
    apps/flows/models.py::HandledComment (AGPL-3.0)."""

    class Outcome(models.TextChoices):
        SENT            = "sent", "Enviado"
        SKIPPED_LIMIT   = "skipped_limit", "Limite por hora"
        SKIPPED_WINDOW  = "skipped_window", "Fora da janela"
        SKIPPED_OWN     = "skipped_own", "Comentário próprio"
        NO_MATCH        = "no_match", "Nenhuma regra casou"
        FAILED          = "failed", "Falha no envio"

    id             = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace      = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                       related_name="automation_logs")
    social_account = models.ForeignKey("social_accounts.SocialAccount",
                                       on_delete=models.CASCADE, related_name="automation_logs")
    rule           = models.ForeignKey(AutomationRule, on_delete=models.SET_NULL, null=True,
                                       related_name="logs")
    inbox_message  = models.ForeignKey("inbox.InboxMessage", on_delete=models.SET_NULL,
                                       null=True, related_name="automation_logs")

    # Chaves das travas. HASHEADAS, não truncadas: dois IDs distintos com o
    # mesmo prefixo de 200 chars virariam uma chave só e o segundo comentário
    # seria descartado como "já tratado" (Chat: guards.py::_bounded).
    comment_key = models.CharField(max_length=200, db_index=True)
    post_key    = models.CharField(max_length=200, blank=True, default="")
    sender_key  = models.CharField(max_length=200, blank=True, default="")

    once_per_sender_per_post = models.BooleanField(default=True)
    # min(timestamp_da_plataforma, agora) — o relógio é nosso.
    commented_at = models.DateTimeField()

    public_reply_sent_at  = models.DateTimeField(null=True, blank=True)
    private_reply_sent_at = models.DateTimeField(null=True, blank=True)
    outcome    = models.CharField(max_length=20, choices=Outcome.choices, blank=True, default="")
    detail     = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "automation_log"
        ordering = ["-created_at"]
        constraints = [
            # Trava 1 — redelivery: nunca responder 2× ao mesmo comentário.
            models.UniqueConstraint(fields=["social_account", "comment_key"],
                                    name="autom_log_once_per_comment"),
            # Trava 2 — 1 resposta por pessoa por post, quando a regra pede.
            models.UniqueConstraint(
                fields=["social_account", "post_key", "sender_key"],
                condition=models.Q(once_per_sender_per_post=True),
                name="autom_log_once_per_sender_post"),
        ]
        indexes = [
            models.Index(fields=["workspace", "-created_at"], name="autom_log_ws_created"),
            models.Index(fields=["rule", "-created_at"], name="autom_log_rule_created"),
        ]
```

**Nota sobre a trava 2:** a `UniqueConstraint` condicional exige `post_key` e
`sender_key` não-vazios para ser útil. Quando a plataforma não informar o post
(mention de caption do Instagram, p.ex.), use um sentinela derivado do
`comment_key` para não colidir regras distintas numa string vazia compartilhada.

### 4.3 Migrations

Uma única migration inicial, `apps/automations/migrations/0001_initial.py`,
gerada por `makemigrations`. Sem alteração de tabela existente.

Se o private reply de §3.2 for feito (recomendado), ele **não gera migration** —
é mudança de provider, não de model.

### 4.4 Tasks

```python
# apps/automations/tasks.py
AUTOMATION_LOG_RETENTION_DAYS = 90
PURGE_INTERVAL_SECONDS = 24 * 60 * 60

@background(schedule=0)
def run_automation_for_message(message_id: str):
    """Casa as regras e executa as ações para uma mensagem da inbox.

    Enfileirada, nunca inline: resposta pública + DM são duas idas à Graph API,
    e o handler de webhook tem orçamento de latência (Chat: triggers/budget.py
    documenta a mesma troca). Deve ser segura para rodar mais de uma vez — as
    travas de AutomationLog garantem isso.
    """

@background(schedule=0)
def purge_automation_logs():
    """Remove logs além da retenção."""
```

Registro em `apps/automations/apps.py`, idêntico a `apps/inbox/apps.py`:
`post_migrate` → `register_recurring_task(purge_automation_logs,
repeat=PURGE_INTERVAL_SECONDS, verbose_name="purge_automation_logs")`.

### 4.5 URLs e views

Em `config/urls.py`, ao lado do inbox:
```python
path("workspace/<uuid:workspace_id>/automations/", include("apps.automations.urls")),
```

```python
# apps/automations/urls.py
app_name = "automations"
urlpatterns = [
    path("",                              views.rule_list,   name="list"),
    path("create/",                       views.rule_create, name="create"),
    path("<uuid:rule_id>/edit/",          views.rule_edit,   name="edit"),
    path("<uuid:rule_id>/toggle/",        views.rule_toggle, name="toggle"),   # POST, HTMX
    path("<uuid:rule_id>/delete/",        views.rule_delete, name="delete"),   # POST
    path("<uuid:rule_id>/test/",          views.rule_test,   name="test"),     # POST, HTMX
    path("log/",                          views.log_list,    name="log"),
]
```

Todas as views com `@login_required` + `@require_permission(...)` e
`@require_POST` nas mutações, exatamente como `apps/inbox/views.py`.

**`rule_test` é a view que eu mais recomendo não cortar.** É o padrão
`RuleInput.from_event` / `.from_message` do `apps/inbox/rules.py:112` do Chat: um
matcher, duas portas. Roda a regra contra os últimos N `InboxMessage` reais do
workspace e mostra quais casariam — **sem enviar nada**. Dá ao usuário confiança
antes de ligar a regra, e o custo é baixo porque reusa o mesmo
`matching.match()`.

### 4.6 UI: o que portar para HTMX e o que simplificar

| Tela do Chat | Decisão para o Studio |
|---|---|
| Flow builder (canvas, nós, arestas, drag-and-drop) | **Não portar.** É um grafo em JSON com editor próprio. A v1 não tem grafo. |
| Painel de triggers (`views_triggers.py`, 460 l.) | **Simplificar para um form único.** O Chat separa flow e trigger porque um flow tem N triggers; na v1 regra e ação são a mesma entidade. |
| Post picker com thumbnails (`instagram.py:1648`) | **Adaptar sem Graph API.** O Studio tem `composer.PlatformPost`; um `<select>` dos posts já conhecidos cobre a v1. |
| Editor de keywords com modo por palavra | **Portar o conceito.** Alpine component: lista de `{text, mode}`, botão "adicionar", `<select>` de modo por linha. Submete como JSON num hidden input. |
| Variações de texto | **Portar.** Mesma mecânica de lista dinâmica Alpine. |
| Dry-run de regras (`apps/inbox/rules.py` dry-run) | **Portar como `rule_test`.** Alto valor, baixo custo. |
| Log de execuções | **Novo, simples.** Tabela paginada com filtro por regra e outcome. |

Layout proposto para `form.html` — accordion de 4 seções, padrão do Studio:
1. **Quando** — tipo de gatilho, conta, escopo de post, keywords, top-level
2. **Responder publicamente** — on/off, modo, textos
3. **Enviar DM** — on/off, modo, textos (+ aviso da janela de 7 dias)
4. **Travas** — once-per-sender-per-post, max/hora

E um cartão "IA (em breve)" desabilitado na seção 2/3, para a v2 não ser surpresa.

---

## 5. Decisões que recomendo mudar em relação ao briefing

### 5.1 Acentuação pt-BR

`keywords.py` do Chat é case-insensitive mas **não** accent-insensitive.
"Açúcar" não casa "acucar". Para comentários brasileiros, onde a mesma palavra
aparece com e sem acento constantemente, isso faz a regra falhar de forma
invisível. Adição sugerida ao módulo portado:

```python
import unicodedata

def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text)
                   if unicodedata.category(c) != "Mn")
```

Como um **quarto modo** (`contains_loose`) ou um booleano `ignore_accents` por
keyword — não como comportamento global, para não alterar a semântica de
`exact`. Aplicar `strip_accents` nos dois lados do match.

### 5.2 Janela da private reply: 7 dias, não 24 h

O briefing diz "janela de 24h". Isso está correto para DM↔DM (e o Studio já
trata em `reply_policy.py:102`), mas **incorreto para private reply de
comentário**: a Meta dá 7 dias desde o comentário. O Chat usa
`PRIVATE_REPLY_WINDOW = timedelta(days=7)` (`guards.py:39`).

Consequência prática: se a v1 aplicar 24 h, ela vai recusar private replies que
a Meta aceitaria — perdendo 6 dias de alcance. Use 7 dias, e aplique o check
**antes** de gravar o `AutomationLog`, pelo motivo de `guards.py:213`.

### 5.3 Idempotência não pode depender da fila

`django-background-tasks` não tem `idempotency_key` (o Chat depende da fila
própria para isso). Mas o Chat também **não** confia nela para este caso — a
garantia real está nas constraints do banco. Com três camadas, estamos
cobertos sem broker:

1. `get_or_create` no `_create_if_new` / `_upsert_message` → redelivery de
   webhook não gera segundo evento.
2. `UniqueConstraint(social_account, comment_key)` → IntegrityError-como-guard.
3. `claim_public_reply` compare-and-set → re-execução da task não posta 2×.

A camada 3 é a que importa se o worker morrer entre o envio e o commit, e é
exatamente por isso que `guards.py:187` toma o claim **antes** da chamada HTTP.

### 5.4 Regras nascem desligadas

`is_active = models.BooleanField(default=False)`. Uma regra de resposta
automática escreve em público no post de um cliente. O custo de um salvamento
acidental com `default=True` é alto e irreversível; o custo de um clique extra é
zero.

---

## 6. Riscos

### 6.1 Conflito de webhooks — **risco baixo**

Não há conflito. A v1 não adiciona rota de webhook: reusa
`/webhooks/facebook/` e `/webhooks/instagram_login/` e pluga depois da
persistência. As assinaturas da Meta (`FACEBOOK_WEBHOOK_FIELDS`) já cobrem
`feed`/`comments`.

**Atenção real:** o `@ratelimit(key="ip", rate="60/m", block=True)` em
`webhooks.py:131`. Um post viral pode fazer a Meta exceder 60 req/min e os
eventos serem **descartados com 403**. Isso já é um risco existente no fork, mas
a automação o torna visível (comentários sem resposta). Vale medir antes de
ligar a v1 em conta de alto volume.

### 6.2 Regras da Meta — **risco médio**

| Risco | Mitigação |
|---|---|
| Private reply fora dos 7 dias | `may_claim_comment` antes do claim (§5.2) |
| 2ª private reply no mesmo comentário | `private_reply_sent_at` + check antes do envio |
| 2ª resposta pública | `claim_public_reply` compare-and-set |
| Responder ao próprio comentário | `_is_own_activity` (`webhooks.py:184`) já existe — **reusar, não reescrever** |
| Like em comentário do Instagram | Não oferecer. `supports_like` como dado (§1.7) |
| Rate limit da Graph API | Falha pública é logada e engolida, **não custa a privada** (`instagram.py:1559`) |
| Políticas de automação da Meta | A automação responde a quem iniciou contato — dentro da política. Mas vale um disclaimer na UI. |

### 6.3 Duplicidade de respostas — **risco médio-alto se feito ingenuamente**

É o risco principal, e a razão de `guards.py` ser o módulo que eu mais insisto em
portar fielmente. As quatro armadilhas, todas documentadas no Chat:

1. **Check-then-insert.** `if not exists(): create()` passa nas duas metades de
   uma corrida. Duas entregas do mesmo webhook chegam juntas e cada uma tem sua
   própria transação. → IntegrityError-como-guard.
2. **Claim depois do envio.** Worker morre entre `POST /replies` e o `UPDATE` →
   re-execução posta de novo. → claim antes.
3. **Efeito colateral no matcher.** Gatilho que perde o match deixa claim. →
   claim na stage, nunca no matcher.
4. **Truncar ID em vez de hashear.** Dois comment_ids com prefixo comum viram
   uma chave; o segundo comentário é silenciosamente descartado.

Teste que eu escreveria primeiro: dois `run_automation_for_message` concorrentes
para o mesmo `message_id`, asserção de exatamente 1 `InboxReply` com
`status=sent`.

### 6.4 Compatibilidade de versões — **risco baixo, mas há uma incompatibilidade real**

| | Studio | Chat |
|---|---|---|
| Django | `>=5.1,<5.2` | `==5.2.17` (LTS) |
| Python | 3.13 | 3.12 |
| psycopg | `>=3.1,<4.0` | `==3.3.4` |
| Pins | ranges | exatos |

**A incompatibilidade:** `models.CheckConstraint(condition=...)` — usado em
`Trigger.Meta` do Chat e na minha proposta de `AutomationRule` — é Django **5.1+**.
Em 5.0 o kwarg era `check=`. O Studio está em `>=5.1`, então **está ok**, mas o
range permite resolver 5.1.x, e o `condition=` só entrou no 5.1. Confirmar com
`python -c "import django; print(django.VERSION)"` antes de escrever a migration.

**Dependências novas: zero.** `keywords.py` usa só `re` e `unicodedata`;
`guards.py` só `django.db`; variações usam `secrets`. Nada de Redis, nada de
broker — conforme o briefing.

**Nota de histórico do repo:** o `requirements.txt` do Studio documenta que
ranges já causaram "509 testes falhando no CI e passando local". O Chat migrou
para pins exatos por causa disso. Não é escopo da v1, mas é uma dívida real.

### 6.5 Risco não listado no briefing: custo de API em post viral

Um post com 5.000 comentários em uma hora, com a regra ligada, tenta 5.000
respostas públicas + 5.000 DMs. `max_per_hour` (default 30) é a trava, mas ela
precisa ser **verificada antes de gastar o claim**, senão 4.970 comentários
consomem o claim de once-per-sender-per-post em troca de nada. A ordem correta,
dentro de `run_automation_for_message`:

1. casar a regra (sem efeito colateral)
2. checar janela de 7 dias
3. checar `max_per_hour` (count em `AutomationLog` da última hora)
4. só então `record_comment` (gravar o claim)
5. `claim_public_reply` → enviar
6. `may_private_reply` → enviar DM → `mark_private_reply_sent`

---

## 7. Plano de implementação

Cada etapa é testável isoladamente e deixa o sistema em estado válido. Nenhuma
etapa antes da 6 envia nada para a Meta.

| # | Etapa | Entregável | Teste de aceitação | Esforço |
|---|---|---|---|---|
| **1** | **Porta de `keywords.py`** | `apps/automations/keywords.py` com os 3 modos + `strip_accents` | `"cat"/any_word` casa "my cat sleeps" e não "category"; `"açúcar"/contains_loose` casa "acucar" | 3–4 h |
| **2** | **Models + migration** | `AutomationRule`, `AutomationLog`, as 2 `UniqueConstraint`, admin | `migrate` roda limpo; 2º `AutomationLog` com mesmo `(account, comment_key)` levanta `IntegrityError` | 5–7 h |
| **3** | **Porta de `guards.py`** | `record_comment`, `claim_public_reply`, `may_private_reply`, `mark_private_reply_sent`, `may_claim_comment`, `_bounded` | **Teste de concorrência**: 2 `record_comment` paralelos → 1 row. Comentário datado no futuro é clampado. ID de 300 chars não colide com outro de prefixo igual | 5–7 h |
| **4** | **Matching** | `MatchContext.from_inbox_message()`, registry, `match()` com prioridade | Regra de prioridade 0 vence a de 10; matcher que levanta é pulado e a próxima casa; `exclude` tem precedência sobre `include` | 5–6 h |
| **5** | **UI (sem executar)** | `urls.py`, views CRUD + toggle, forms, 3 templates HTMX, Alpine para keywords/variações, **`rule_test`** | Criar regra, editar, ligar/desligar via HTMX; `rule_test` mostra quais das últimas 50 mensagens casariam, **sem enviar** | 10–14 h |
| **6** | **Ação: resposta pública** | `actions.py` + `run_automation_for_message` + plug em `webhooks.py:490` e `tasks.py:625` | Comentário de teste em post real → 1 resposta pública; 2ª entrega do mesmo webhook → 0 respostas novas, log `skipped`; `max_per_hour` respeitado | 7–9 h |
| **7** | **Ação: private reply (DM)** | `build_send_payload(comment_id=...)` em `providers/meta_messaging.py` + `reply_to_message` nos 3 providers Meta + ação de DM | Comentário → DM recebida; 2ª tentativa recusada por `private_reply_sent_at`; comentário de 8 dias → `skipped_window` sem gastar claim | 7–9 h |
| | **Total v1** | | | **42–56 h** |

**Etapa 8 (fora da v1, preparada):** resposta por IA. A infraestrutura já está
pronta nos dois lados — `AutomationRule.ai_*` nos models e
`inbox.services.create_reply_draft()` para o modo rascunho. Falta só um
`providers/ai.py` com client compatível com OpenAI (`base_url` configurável
cobre Ollama) e a ramificação em `actions.py`. Estimo 10–14 h.

### Ordem recomendada de merge

Etapas 1–5 podem ir para `main` com segurança: criam tabelas e UI, mas nada
executa. A etapa 6 é o primeiro merge que muda comportamento de produção —
recomendo ligar em **uma** conta de teste, com `max_per_hour=5`, por alguns dias
antes de liberar.

---

## 8. Créditos e conformidade AGPL-3.0

Ambos os projetos são AGPL-3.0, então o código pode ser portado. Para manter a
atribuição:

1. **Cabeçalho nos arquivos portados** — `keywords.py` e `guards.py` (porta
   próxima do literal):

```python
"""Keyword matching para regras de resposta automática.

Portado de BrightBean Chat, apps/flows/triggers/keywords.py
Copyright (c) BrightBean (github.com/brightbeanxyz/brightbean-chat)
Licenciado sob AGPL-3.0. Modificações: normalização de acentos para pt-BR.
"""
```

2. **Docstring de referência** nos arquivos adaptados (`models.py`,
   `matching.py`), apontando o arquivo de origem — como já está nos exemplos de
   §4.2.

3. **`NOTICE` ou seção no README** registrando a origem e o commit
   (`7bf959b`, 2026-10-21).

4. A AGPL-3.0 exige **disponibilizar o código-fonte aos usuários da rede**.
   `automator.sevenclick.dev` é um serviço de rede, então o fork precisa ter o
   fonte acessível aos usuários — um link no rodapé para
   `github.com/sevenclickdigital/brightbean-studio` cumpre isso, desde que o
   repositório esteja público. **Vale verificar**: se o fork for privado, a
   obrigação não está satisfeita.

---

## Apêndice: índice de arquivos citados

### BrightBean Chat (referência)
| Arquivo | Linhas | Relevância |
|---|---|---|
| `apps/flows/triggers/keywords.py` | 71 | ⭐ Portar |
| `apps/flows/triggers/guards.py` | 227 | ⭐ Portar |
| `apps/flows/triggers/matching.py` | 386 | ⭐ Adaptar |
| `apps/flows/triggers/comments.py` | 167 | Registry de responders |
| `apps/flows/triggers/stages.py` | 323 | Pipeline (simplificar) |
| `apps/flows/triggers/schema.py` | 188 | Validação (→ Forms) |
| `apps/flows/triggers/budget.py` | 188 | Não portar |
| `apps/flows/models.py` | 700 | `Trigger`:324, `HandledComment`:415 |
| `apps/channels/providers/instagram.py` | ~1700 | `_public_reply_text`:1451, `_answer_comment`:1513, `_open_thread`:1594 |
| `apps/channels/providers/messenger.py` | ~2300 | Private reply:1687 |
| `apps/inbox/rules.py` | 406 | Padrão de dry-run |
| `apps/messaging/services.py` | — | `pause_automation`:168 |

### BrightBean Studio (fork — pontos de integração)
| Arquivo:linha | Papel |
|---|---|
| `apps/inbox/webhooks.py:445` | `_create_if_new` — **plug (webhook)** |
| `apps/inbox/webhooks.py:490` | `if created:` → chamar automação aqui |
| `apps/inbox/webhooks.py:184` | `_is_own_activity` — reusar |
| `apps/inbox/tasks.py:583` | `_upsert_message` — **plug (polling)** |
| `apps/inbox/tasks.py:624` | `if notify:` → chamar automação aqui |
| `apps/inbox/tasks.py:699` | Padrão `@background` |
| `apps/inbox/services.py:240` | `send_reply` — **reaproveitar** |
| `apps/inbox/services.py:134` | `create_reply_draft` — modo rascunho (v2 IA) |
| `apps/inbox/reply_policy.py:34` | `reply_eligibility` — janela 24 h/HUMAN_AGENT |
| `providers/meta_messaging.py:274` | `build_send_payload` — **modificar para private reply** |
| `providers/facebook.py:1100` | `reply_to_comment` — já trata 2 níveis |
| `apps/inbox/models.py:229` | `SavedReply.render` — variáveis |
| `apps/common/background.py` | `register_recurring_task` |
| `apps/common/managers.py` | `WorkspaceScopedManager` |
| `apps/inbox/apps.py` | Padrão `post_migrate` |
| `config/urls.py:39` | Onde incluir `automations` |
