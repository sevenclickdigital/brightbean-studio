# Marcação de strings para tradução

Como marcar texto visível ao usuário nos templates, e o vocabulário a usar em
português. Vale para qualquer pessoa — ou agente — que marque uma tela.

A infraestrutura já existe: `pt-br` é o idioma padrão, `en-us` segue
disponível, e o seletor fica no menu da conta. O trabalho restante é marcar as
strings tela a tela.

## O que marcar

Só o que o cliente lê na tela: textos de interface, labels, botões, títulos,
mensagens, placeholders, `aria-label`, `title` e `alt` que digam algo.

## O que NÃO marcar

- Nomes de plataforma: YouTube, Facebook, Instagram, LinkedIn, TikTok, Threads,
  Bluesky, Mastodon, Pinterest. São marcas registradas.
- Dados vindos do banco: `{{ account.name }}`, `{{ post.caption }}`.
- Classes CSS, atributos Alpine (`x-data`, `@click`), nomes de campos de
  formulário, chaves de JSON, URLs, nomes de rota em `{% url %}`.
- Texto dentro de `<script>`: exige outra abordagem (`JavaScriptCatalog`), que
  não faz parte desta etapa. Deixe como está.
- Comentários de template (`{# ... #}`, `{% comment %}`).

## Como marcar

Carregue a tag no topo do arquivo, junto aos outros `load`:

```django
{% load static i18n %}
```

Texto simples:

```django
<span>Publish</span>
<span>{% translate "Publish" %}</span>
```

Texto com variável — use `blocktranslate`, nunca concatene:

```django
{% blocktranslate with name=account.name %}Connected {{ name }} successfully.{% endblocktranslate %}
```

Plural:

```django
{% blocktranslate count counter=posts|length %}{{ counter }} post{% plural %}{{ counter }} posts{% endblocktranslate %}
```

Dentro de atributo HTML:

```django
<input placeholder="{% translate 'Search' %}">
```

Use aspas simples dentro do atributo, porque as duplas já delimitam o atributo.

Em filtros com valor padrão:

```django
{{ org.name|default:_("My Organization") }}
```

## Vocabulário

Consistência importa mais do que preferência pessoal. Siga esta tabela; se
precisar de um termo que não está nela, acrescente-o em vez de improvisar.

| Inglês | Português |
|---|---|
| Publish | Publicar |
| Post (substantivo) | Publicação |
| Post (verbo) | Publicar |
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

Convenções de estilo:

- Trate o usuário por **você**, nunca "tu" ou "vós".
- Botões e ações no infinitivo: "Criar publicação", não "Crie uma publicação".
- Títulos em caixa normal, não Title Case: "Criar ideia", não "Criar Ideia".
- Prefira o termo que o cliente brasileiro usa: "Caixa de entrada" em vez de
  "Entrada social".

## Depois de marcar

```bash
python manage.py makemessages -l pt_BR --ignore=".venv/*" --ignore="staticfiles/*" --ignore="node_modules/*"
python manage.py compilemessages -l pt_BR
```

`makemessages` junta as strings novas em `locale/pt_BR/LC_MESSAGES/django.po`,
onde cada `msgstr ""` vazio espera a tradução. O `.mo` compilado é artefato de
build e está no `.gitignore`.

## Verificação

O template tem que continuar carregando:

```python
from django.template.loader import get_template
get_template("composer/compose.html")
```

Um `{% translate %}` sem o `{% load i18n %}` no topo quebra o render inteiro
com `Invalid block tag`, e a tela some — então confira sempre os arquivos que
você tocou.
