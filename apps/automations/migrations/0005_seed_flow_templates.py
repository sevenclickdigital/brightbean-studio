"""Stock the template shelf, so it is not empty on the first deploy.

Five flows covering the shapes authors actually draw, from the simplest
(one public reply) to the one this feature was modelled on (a lead magnet
gated on following the account). The last of those is a real flow lifted
off a working automation rather than written from scratch, which is why
its node positions look hand-placed: they were.

**Seeded as data, not as a fixture.** A ``loaddata`` would need a deploy
step somebody has to remember; a migration runs itself exactly once, in
the same place every other schema change does.

**Idempotent by name, and additive only.** ``get_or_create`` keyed on the
name means re-running this never duplicates a row, and — more
importantly — never overwrites one. The operator owns these the moment
the deploy finishes: a reword, a reorder, an unpublish, a rewritten
message are all edits this migration must not undo on the next deploy.
A template deleted on purpose stays deleted for the same reason, which
is what ``get_or_create`` buys over ``update_or_create``.

**No thumbnails.** An ``ImageField`` needs a file on disk, and a
migration that writes into ``MEDIA_ROOT`` breaks on any deploy where that
is a bucket rather than a directory. The picker already falls back to a
drawn placeholder, so these ship recognisable by name and the operator
adds images from the admin screen.

**No image on the card node either.** The flow this was taken from
pointed at a file in one workspace's media library; in a template, that
is a URL every *other* customer's card fails to load. The node ships
without one and the author adds theirs — see the comment beside the URL
field in ``flow_builder.html``.

The reverse is deliberately a no-op rather than a delete: by the time
anybody migrates backwards these rows may have been edited, and throwing
away the operator's work to undo a schema change is not a trade a
migration gets to make.
"""

from django.db import migrations

#: Written out in full rather than built by a helper. These are content,
#: and a reader checking what a customer will see should be able to read
#: the message text without reconstructing it from arguments.
TEMPLATES = [
    {
        "name": "Resposta no comentário",
        "description": "Responde publicamente quem comenta uma palavra-chave.",
        "position": 0,
        "graph": {
            "nodes": [
                {
                    "id": "trigger",
                    "type": "trigger_comment",
                    "x": 80,
                    "y": 160,
                    "data": {
                        "keywords": [],
                        "exclude_keywords": [],
                        "match_mode": "contains",
                        "top_level_only": True,
                    },
                },
                {
                    "id": "reply",
                    "type": "reply_comment",
                    "x": 440,
                    "y": 160,
                    "data": {"text": "Obrigado pelo comentário, {sender_name}! 🧡"},
                },
            ],
            "edges": [{"from": "trigger", "to": "reply"}],
        },
    },
    {
        "name": "Direct com link",
        "description": "Manda uma DM com um botão que abre o seu link.",
        "position": 1,
        "graph": {
            "nodes": [
                {
                    "id": "trigger",
                    "type": "trigger_comment",
                    "x": 80,
                    "y": 160,
                    "data": {
                        "keywords": [],
                        "exclude_keywords": [],
                        "match_mode": "contains",
                        "top_level_only": True,
                    },
                },
                {
                    "id": "dm",
                    "type": "send_dm",
                    "x": 440,
                    "y": 160,
                    "data": {
                        "text": "Oi {sender_name}! É só tocar no botão abaixo 👇",
                        "buttons": [{"type": "url", "title": "Abrir", "url": ""}],
                    },
                },
            ],
            "edges": [{"from": "trigger", "to": "dm"}],
        },
    },
    {
        "name": "Isca simples",
        "description": "Responde o comentário e manda o material no direct.",
        "position": 2,
        "graph": {
            "nodes": [
                {
                    "id": "trigger",
                    "type": "trigger_comment",
                    "x": 80,
                    "y": 200,
                    "data": {
                        "keywords": [],
                        "exclude_keywords": [],
                        "match_mode": "contains",
                        "top_level_only": True,
                    },
                },
                {
                    "id": "public",
                    "type": "reply_comment",
                    "x": 440,
                    "y": 40,
                    "data": {"text": "Te mandei no direct, {sender_name}! 💌"},
                },
                {
                    "id": "dm",
                    "type": "send_dm",
                    "x": 440,
                    "y": 300,
                    "data": {
                        "text": "Oi {sender_name}! Aqui está o que você pediu 🎁",
                        "buttons": [{"type": "url", "title": "Acessar agora", "url": ""}],
                    },
                },
            ],
            "edges": [
                {"from": "trigger", "to": "public"},
                {"from": "trigger", "to": "dm"},
            ],
        },
    },
    {
        "name": "Lista de espera",
        "description": "Confirma o interesse e pergunta se a pessoa quer ser avisada.",
        "position": 3,
        "graph": {
            "nodes": [
                {
                    "id": "trigger",
                    "type": "trigger_comment",
                    "x": 80,
                    "y": 200,
                    "data": {
                        "keywords": [],
                        "exclude_keywords": [],
                        "match_mode": "contains",
                        "top_level_only": True,
                    },
                },
                {
                    "id": "public",
                    "type": "reply_comment",
                    "x": 440,
                    "y": 40,
                    "data": {"text": "Anotado, {sender_name}! Te chamo no direct 👀"},
                },
                {
                    "id": "ask",
                    "type": "send_dm",
                    "x": 440,
                    "y": 300,
                    "data": {
                        "text": ("Oi {sender_name}! As vagas abrem em breve.\n\nQuer que eu te avise em primeira mão?"),
                        "buttons": [{"type": "reply", "title": "Quero ser avisado"}],
                    },
                },
                {
                    "id": "confirm",
                    "type": "send_dm",
                    "x": 840,
                    "y": 300,
                    "data": {"text": "Prontinho! Você está na lista 🎉"},
                },
            ],
            "edges": [
                {"from": "trigger", "to": "public"},
                {"from": "trigger", "to": "ask"},
                # ``branch`` is the button index the engine resumes by — see
                # ``flow.instantiate_graph``, which carries it through
                # untouched for exactly this reason.
                {"from": "ask", "to": "confirm", "branch": "0"},
            ],
        },
    },
    {
        "name": "Isca para seguidores",
        "description": "Entrega o material só para quem segue, e convida quem ainda não segue.",
        "position": 4,
        "graph": {
            "nodes": [
                {
                    "id": "trigger",
                    "type": "trigger_comment",
                    "x": 60,
                    "y": 200,
                    "data": {
                        "keywords": ["guia"],
                        "exclude_keywords": ["grátis", "spam"],
                        "match_mode": "contains",
                        "top_level_only": True,
                    },
                },
                {
                    "id": "public",
                    "type": "reply_comment",
                    "x": 453,
                    "y": -9,
                    "data": {"text": "Te mandei no direct, {sender_name}! 💌"},
                },
                {
                    "id": "open",
                    "type": "send_dm",
                    "x": 437,
                    "y": 331,
                    "data": {
                        "text": (
                            "Oi {sender_name}! Que bom que você quer o guia 😊\n\n"
                            "Toca no botão abaixo e eu te envio agora."
                        ),
                        "buttons": [{"type": "reply", "title": "Quero o guia"}],
                    },
                },
                {
                    "id": "check",
                    "type": "condition",
                    "x": 851,
                    "y": 170,
                    "data": {"field": "follows_us", "operator": "is_true", "value": ""},
                },
                {
                    "id": "deliver",
                    "type": "send_card",
                    "x": 1331,
                    "y": 91,
                    "data": {
                        "text": "Aqui está o seu guia 🎁",
                        # No ``image_url``: see the module docstring.
                        "image_url": "",
                        "buttons": [{"type": "url", "title": "Baixar o guia", "url": ""}],
                    },
                },
                {
                    "id": "ask",
                    "type": "send_dm",
                    "x": 873,
                    "y": 528,
                    "data": {
                        "text": (
                            "Quase lá! O guia é exclusivo para seguidores.\n\n"
                            'Me segue aqui e depois toca em "Já segui" 👇'
                        ),
                        "buttons": [{"type": "reply", "title": "Já segui"}],
                    },
                },
            ],
            "edges": [
                {"from": "trigger", "to": "public"},
                {"from": "trigger", "to": "open"},
                {"from": "open", "to": "check", "branch": "0"},
                {"from": "check", "to": "deliver", "branch": "true"},
                {"from": "check", "to": "ask", "branch": "false"},
                # Back to the check after they say they followed, so the
                # answer is re-read rather than taken on trust.
                {"from": "ask", "to": "check", "branch": "0"},
            ],
        },
    },
]


def seed(apps, schema_editor):
    FlowTemplate = apps.get_model("automations", "FlowTemplate")
    for entry in TEMPLATES:
        FlowTemplate.objects.get_or_create(
            name=entry["name"],
            defaults={
                "description": entry["description"],
                "graph": entry["graph"],
                "position": entry["position"],
                # Published: an empty-looking shelf after a deploy reads as a
                # broken feature, and these are finished flows. The operator
                # unpublishes any they disagree with.
                "is_published": True,
            },
        )


def unseed(apps, schema_editor):
    """Intentionally does nothing — see the module docstring."""


class Migration(migrations.Migration):
    dependencies = [
        ("automations", "0004_flowtemplate"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
