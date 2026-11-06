"""The flow template library: the operator's shelf, and the picker that reads it.

Two audiences in one module, told apart by their decorator.

**The customer** gets one endpoint, :func:`template_picker` — the modal behind
the builder's "Templates" button. It lists published templates and returns
their graphs; applying one happens entirely in the browser, because a template
applied is just a canvas with different nodes on it and the flow is still
created by the author's first save. Nothing here writes on the customer's
behalf, which is what keeps this feature out of the save path: a bug in the
library cannot damage a saved automation.

**The operator** gets the rest, all behind ``_staff_required`` borrowed from the
billing console. The shelf is a product asset, not a workspace's — see
``FlowTemplate`` — so these paths carry no ``workspace_id`` and no membership
could grant access to them. ``is_staff`` is the flag Django already reserves for
operators and the one the billing console uses for exactly this reason.

**The operator edits a template on the real canvas.** ``template_edit`` renders
``automations/flow_builder.html`` with ``template_mode`` on, so the drawing tool
is the one the customers use rather than a second editor to keep in agreement
with it. What differs is where Save posts and which controls are drawn, and both
of those are decided in the template by that one flag.
"""

from __future__ import annotations

import contextlib
import json
import logging

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.automations import flow as flow_compiler
from apps.automations.models import FlowTemplate
from apps.billing.views import _staff_required
from apps.members.decorators import require_permission

logger = logging.getLogger(__name__)

#: Same ceiling the per-post builder enforces (``flow_views.MAX_NODES``).
#: Restated rather than imported so that raising one does not silently raise
#: the other: a template is drawn once and applied many times, so it is the
#: more conservative of the two places to loosen.
MAX_NODES = 60

#: What the thumbnail upload accepts. Checked by extension and by the browser's
#: declared type, which is the same pair ``media_library`` checks — neither is
#: proof, and the file is served from ``MEDIA_URL`` where it is never executed.
THUMBNAIL_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
#: 4 MB. A card in a grid; anything larger is a mistake rather than a need.
MAX_THUMBNAIL_BYTES = 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# The customer's side: one read
# ---------------------------------------------------------------------------


@require_GET
@require_permission("use_inbox")
def template_picker(request, workspace_id):
    """Published templates, as JSON, for the builder's picker.

    ``use_inbox`` (a read) rather than ``reply_from_inbox``: this lists what the
    product offers and changes nothing. Applying the graph is a change to an
    unsaved canvas, and the save behind it is already gated on
    ``reply_from_inbox`` — gating the *list* on it too would hide the library
    from someone who can legitimately look at the builder.

    The graph travels with each item rather than behind a second request. The
    payload is a few kilobytes, the picker needs it the instant a card is
    clicked, and a second round trip there is a spinner between "I chose this"
    and "it appeared".

    ``workspace_id`` is in the URL for the decorator's benefit — it resolves the
    membership — and is deliberately not used to filter: the library is global.
    """
    templates = FlowTemplate.objects.filter(is_published=True)

    return JsonResponse(
        {
            "ok": True,
            "items": [
                {
                    "id": str(template.id),
                    "name": template.name,
                    "description": template.description,
                    "thumbnail": request.build_absolute_uri(template.thumbnail.url) if template.thumbnail else "",
                    # Instantiated server-side so the ids the canvas receives
                    # cannot collide with the ones already on it — see
                    # ``flow.instantiate_graph``. Doing it here rather than in
                    # the browser also means the stored template graph is never
                    # exposed in a shape that could be posted back as a flow.
                    "graph": flow_compiler.instantiate_graph(template.graph),
                    "node_count": len(flow_compiler.instantiate_graph(template.graph)["nodes"]),
                }
                for template in templates
            ],
        }
    )


# ---------------------------------------------------------------------------
# The operator's side: the shelf
# ---------------------------------------------------------------------------


@_staff_required
@require_GET
def template_list(request):
    """The shelf: every template, published or not, in the order customers see.

    Unpublished ones are listed alongside rather than in a second tab. There
    are rarely more than a dozen rows, and the question the operator has on
    this screen is "what is on the shelf and what is still being written",
    which one list with a status column answers and two tabs hide half of.
    """
    templates = FlowTemplate.objects.all()
    return render(
        request,
        "automations/staff_template_list.html",
        {
            "templates": templates,
            "published_count": sum(1 for t in templates if t.is_published),
            "admin_active": "flow_templates",
        },
    )


@_staff_required
@require_POST
def template_create(request):
    """Make an empty template and open its canvas.

    A POST that redirects into the editor, rather than a "new template" form:
    the name is the only field needed to start, and every other field is edited
    on the canvas screen that follows. Asking for a description and a thumbnail
    before a single node is drawn puts the two decisions that depend on the
    flow *before* the flow.

    The graph it opens with is the builder's own seeded default, so a new
    template starts with a trigger already on it — the same first step the
    customer's canvas offers, and for the same reason.
    """
    name = (request.POST.get("name") or "").strip()
    if not name:
        messages.error(request, _("Give the template a name."))
        return redirect("automations_staff:template_list")

    template = FlowTemplate.objects.create(
        name=name[:120],
        graph=flow_compiler.default_graph("dm"),
        created_by=request.user,
        # Last on the shelf, so creating one never reorders what is already
        # there. ``position`` is the operator's to set afterwards.
        position=_next_position(),
    )
    return redirect("automations_staff:template_edit", template_id=template.id)


def _next_position() -> int:
    """One past the highest position in use, or 0 on an empty shelf."""
    from django.db.models import Max

    highest = FlowTemplate.objects.aggregate(top=Max("position"))["top"]
    return 0 if highest is None else highest + 1


@_staff_required
@require_GET
def template_edit(request, template_id):
    """The canvas for one template — the customer's builder, in template mode.

    Renders ``automations/flow_builder.html`` rather than a copy of it. The
    editor is a thousand lines of canvas behaviour (drag, connect, zoom, the
    node palette), and a second copy for the operator would be the same feature
    maintained twice, diverging the first time a node type is added.

    The context it does *not* get is as deliberate as what it does: there is no
    ``platform_post``, no ``automation`` and no status, because a template
    answers for no post and runs nowhere. The template reads ``template_mode``
    to decide that, so every one of those is a single conditional rather than a
    sentinel value standing in for a post.
    """
    template = get_object_or_404(FlowTemplate, id=template_id)
    graph = template.graph or flow_compiler.default_graph("dm")

    from apps.automations.flow_views import _keyword_mode_choices

    return render(
        request,
        "automations/flow_builder.html",
        {
            "template_mode": True,
            "flow_template": template,
            "graph": graph,
            # Compiled for the warnings strip only. A template is never armed,
            # so "this flow has nothing to send" is advice here rather than the
            # blocker it is on a real flow.
            "compiled": flow_compiler.compile_graph(graph),
            "keyword_mode_choices": _keyword_mode_choices(),
            # The canvas asks this before drawing the edit affordances. An
            # operator on this screen has already passed ``_staff_required``.
            "can_manage": True,
            "admin_active": "flow_templates",
        },
    )


@_staff_required
@require_POST
def template_save(request, template_id):
    """Persist a template's canvas. Returns JSON — the builder posts by fetch.

    The response shape mirrors ``flow_views.flow_save`` exactly (``ok``,
    ``error``, ``warnings``) because the same JavaScript reads both. Where that
    view then compiles the graph into a rule and may arm it, this one stops at
    the graph: a template has no rule, no post and nothing to arm.
    """
    template = get_object_or_404(FlowTemplate, id=template_id)

    graph, error = _parse_graph(request)
    if error:
        return JsonResponse({"ok": False, "error": error}, status=400)

    template.graph = graph
    template.save(update_fields=["graph", "updated_at"])

    compiled = flow_compiler.compile_graph(graph)
    return JsonResponse(
        {
            "ok": True,
            # Named ``warnings`` and not ``errors``: every one of them describes
            # a template that saved. See ``flow._warnings``.
            "warnings": compiled.warnings,
        }
    )


def _parse_graph(request):
    """Read and bound-check the posted graph. Returns ``(graph, error)``.

    The same three checks ``flow_views._parse_graph`` makes, for the same
    reasons: the body is JSON from a browser, so malformed input is expected
    rather than exceptional, and the node cap stops a runaway editor from
    storing a canvas nobody can open.
    """
    raw = request.POST.get("graph")
    if not raw:
        return None, _("The canvas was empty.")
    try:
        graph = json.loads(raw)
    except (TypeError, ValueError):
        return None, _("The canvas could not be read.")
    if not isinstance(graph, dict):
        return None, _("The canvas could not be read.")

    nodes = graph.get("nodes")
    if isinstance(nodes, list) and len(nodes) > MAX_NODES:
        return None, _("This flow has too many steps.")
    return graph, None


@_staff_required
@require_POST
def template_details(request, template_id):
    """Save the fields that are not the canvas: name, description, thumbnail, shelf.

    One endpoint for the card's whole form rather than one per field, because
    they are submitted together by one Save button and a partial write would
    leave the operator unsure which half landed.

    A failed thumbnail does not discard the text. The file is the part most
    likely to be rejected (wrong type, too large), and losing a rewritten
    description to it would be the screen's most annoying behaviour — so the
    text is saved either way and the image error is reported on top.
    """
    template = get_object_or_404(FlowTemplate, id=template_id)

    name = (request.POST.get("name") or "").strip()
    if not name:
        messages.error(request, _("Give the template a name."))
        return redirect("automations_staff:template_edit", template_id=template.id)

    template.name = name[:120]
    template.description = (request.POST.get("description") or "").strip()[:240]
    template.is_published = request.POST.get("is_published") == "on"
    # A non-numeric position is a typo, not a reason to refuse the rest of the
    # form, so the stored value is left as it was.
    with contextlib.suppress(TypeError, ValueError):
        template.position = int(request.POST.get("position") or 0)

    fields = ["name", "description", "is_published", "position", "updated_at"]

    upload = request.FILES.get("thumbnail")
    thumbnail_error = None
    if upload:
        thumbnail_error = _thumbnail_error(upload)
        if thumbnail_error is None:
            template.thumbnail = upload
            fields.append("thumbnail")
    elif request.POST.get("remove_thumbnail") == "on" and template.thumbnail:
        # The file itself is left on disk: deleting it here would break any
        # page still holding the old URL, and an orphaned 40 KB image is the
        # cheaper of the two mistakes.
        template.thumbnail = ""
        fields.append("thumbnail")

    template.save(update_fields=fields)

    if thumbnail_error:
        messages.error(request, thumbnail_error)
    else:
        messages.success(request, _("Template saved."))
    return redirect("automations_staff:template_edit", template_id=template.id)


def _thumbnail_error(upload):
    """Why this upload is not usable as a thumbnail, or ``None`` if it is."""
    if upload.size > MAX_THUMBNAIL_BYTES:
        return _("The image is too large. Use one under 4 MB.")
    if (upload.content_type or "") not in THUMBNAIL_TYPES:
        return _("Use a PNG, JPEG, WebP or GIF image.")
    return None


@_staff_required
@require_POST
def template_delete(request, template_id):
    """Remove a template from the shelf.

    Hard delete, and safe to be one: nothing references a ``FlowTemplate``.
    Flows started from a template keep no link back to it (see the model), so
    deleting one cannot reach a customer's running automation.
    """
    template = get_object_or_404(FlowTemplate, id=template_id)
    name = template.name
    template.delete()
    messages.success(request, _("“%(name)s” was deleted.") % {"name": name})
    return redirect("automations_staff:template_list")


@_staff_required
@require_POST
def template_duplicate(request, template_id):
    """Copy a template and open the copy.

    The shelf's most common edit is "like that one, but for Instagram" — and
    the alternative to this button is redrawing nine nodes by hand.

    The copy is unpublished whatever the original was: a duplicate is by
    definition about to be changed, and publishing it immediately would put two
    identical cards in every customer's picker for however long the edit takes.
    """
    source = get_object_or_404(FlowTemplate, id=template_id)
    copy = FlowTemplate.objects.create(
        name=_("%(name)s (copy)") % {"name": source.name},
        description=source.description,
        # Instantiated rather than assigned: the two rows must not share node
        # ids, or a later feature keying on them would treat them as one.
        graph=flow_compiler.instantiate_graph(source.graph),
        is_published=False,
        position=_next_position(),
        created_by=request.user,
    )
    # The image is intentionally not copied: the copy is about to be a
    # different flow, and a thumbnail showing the original is worse than none.
    return redirect("automations_staff:template_edit", template_id=copy.id)
