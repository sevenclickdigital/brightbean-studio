"""Views for the per-post flow builder — the canvas opened from a post.

Separate from :mod:`apps.automations.views` because these answer a different
question. That module edits *account-wide* rules from the automations section;
this one edits the single flow attached to one ``PlatformPost``, reached from
the composer (before publishing) or from the post itself (after). The permission
model, the lookup and the lifecycle all differ enough that folding them together
would mean branching on "did this rule come from a post" in every view.

**Permissions mirror the rule views**: reading takes ``use_inbox``, mutating
takes ``reply_from_inbox``. An automation is a reply a rule sends instead of a
person, so the permission that governs it is the permission to reply — see
:mod:`apps.automations.views`.

**Nothing here sends anything.** Saving a flow writes rows; arming it flips a
rule's scope and its ``is_active``. Delivery belongs to the action layer, driven
by the comment webhook.
"""

from __future__ import annotations

import contextlib
import json
import logging

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from apps.automations import channels
from apps.automations import flow as flow_compiler
from apps.automations import matching, services
from apps.automations.models import AutomationRule, PostAutomation
from apps.composer.models import PlatformPost
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

#: Cap on a stored canvas, in nodes. The graph is walked once per save and
#: rendered in full by the editor; it is not walked at send time (the compiled
#: rule fields are). This is a "nobody is maintaining this deliberately" limit,
#: the same reasoning as ``forms.MAX_KEYWORDS``.
MAX_NODES = 60


def _get_workspace(request, workspace_id):
    """Resolve workspace and enforce membership. Same shape as ``views._get_workspace``."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    if not WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).exists():
        raise PermissionDenied("You are not a member of this workspace.")
    return workspace


def _get_platform_post(workspace, platform_post_id):
    """One platform post in this workspace, or 404.

    Scoped through ``post__workspace`` rather than trusting the id: the id is a
    UUID in the URL, and the only thing establishing the caller may see it is
    their membership of *this* workspace. A post from another workspace is a
    404, not a 403 — its existence is not something to confirm.
    """
    return get_object_or_404(
        PlatformPost.objects.select_related("social_account", "post"),
        id=platform_post_id,
        post__workspace=workspace,
    )


def supports_automation(platform_post) -> bool:
    """Whether a flow can run on this post at all.

    The question the composer asks to decide whether to offer the button, and
    the one this module re-asks before saving — a client that renders the button
    anyway must not be able to create a flow that can never fire.

    It is **only** about the platform. Comment automations need a comment
    webhook to match on and a comment-addressed send endpoint to DM through, and
    both exist for exactly ``matching.COMMENT_PLATFORMS``. Publication status is
    deliberately *not* part of this: a draft post supports automation, it simply
    cannot arm yet, and conflating "this platform can never do it" with "this
    post is not live yet" would hide the feature on exactly the screen where it
    is configured.
    """
    account = getattr(platform_post, "social_account", None)
    if account is None:
        return False
    return account.platform in matching.COMMENT_PLATFORMS


def _flow_for(platform_post):
    """This post's flow, or ``None``. One query, with the rule."""
    return PostAutomation.objects.filter(platform_post=platform_post).select_related("rule").first()


def post_permalink(platform_post) -> str:
    """A link to the post on the platform, or ``""``.

    Built from ``platform_post_id`` rather than read from a stored
    ``permalink_url``. The stored one only exists once a comment has arrived
    carrying it, so a freshly published post would have no link at exactly the
    moment someone is setting up its automation — and it is platform-supplied
    data, which is the kind this codebase deliberately does not render as a
    link elsewhere (see ``actions.render_text``).

    Facebook's id is ``PAGEID_POSTID`` on some edges and bare on others; the
    bare form is what ``PlatformPost`` stores, and Facebook resolves
    ``/<id>`` on its own. Instagram has no id-addressable permalink at all
    without an extra API call, so it returns empty and the UI simply shows no
    link rather than a broken one.
    """
    post_id = (getattr(platform_post, "platform_post_id", "") or "").strip()
    if not post_id:
        return ""
    platform = getattr(platform_post.social_account, "platform", "")
    if platform == "facebook":
        return f"https://www.facebook.com/{post_id}"
    return ""


#: Bounds on what the settings modal may set. The ceiling is a sanity limit
#: rather than a platform one — Meta's own rate limits bite well before this —
#: and exists so a typo cannot turn one viral post into thousands of sends.
MAX_PER_HOUR_CEILING = 500
MAX_PRIORITY = 1000


def _flow_settings(automation) -> dict:
    """The rule's guardrails, or the model defaults when there is no rule yet.

    Falling back to the defaults rather than to blanks matters: the modal opens
    on a flow that has never been saved too, and empty boxes there would read
    as "no limit" when the limit is in fact 30 an hour.
    """
    rule = getattr(automation, "rule", None)
    if rule is None:
        return {
            "name": "",
            "max_per_hour": AutomationRule._meta.get_field("max_per_hour").default,
            "once_per_sender_per_post": AutomationRule._meta.get_field("once_per_sender_per_post").default,
            "priority": AutomationRule._meta.get_field("priority").default,
        }
    return {
        # The rule's name. It is what the automations list shows, so without it
        # here a flow could only ever carry the one generated at creation —
        # fine for one automation, useless for telling two apart.
        "name": rule.name,
        "max_per_hour": rule.max_per_hour,
        "once_per_sender_per_post": rule.once_per_sender_per_post,
        "priority": rule.priority,
    }


def _post_preview(platform_post) -> dict:
    """What the builder shows about the post this flow is attached to.

    The builder used to say only "Published post", which leaves the one
    question an author actually has — *which* post? — unanswered on the screen
    where they are deciding what it should reply. A thumbnail, the opening of
    the caption and the publish date are enough to recognise it without
    leaving the page.

    The thumbnail falls back to the full file when no thumbnail was generated:
    images get no separate thumbnail in this codebase, so requiring one would
    show a blank frame for the most common case.
    """
    post = platform_post.post
    caption = (platform_post.platform_specific_caption or post.caption or "").strip()

    thumb = ""
    attachment = post.media_attachments.select_related("media_asset").first()
    if attachment is not None:
        asset = attachment.media_asset
        if getattr(asset, "thumbnail", None):
            thumb = asset.thumbnail.url
        elif getattr(asset, "file", None) and getattr(asset, "media_type", "") in {"image", "gif"}:
            thumb = asset.file.url

    return {
        "caption": caption,
        "snippet": caption[:120],
        "thumbnail": thumb,
        "published_at": platform_post.published_at or post.published_at,
        "permalink": post_permalink(platform_post),
        "platform_post_id": (platform_post.platform_post_id or "").strip(),
        "media_count": post.media_attachments.count(),
    }


def _default_rule_name(platform_post) -> str:
    """A name the author can recognise in the account-wide rule list.

    Flows create real ``AutomationRule`` rows, which show up in the rules list
    alongside hand-written ones. An unnamed rule there is indistinguishable
    noise, so the name carries the post's caption opening — truncated, because
    captions are long and ``name`` is a 200-char column.
    """
    post = platform_post.post
    caption = (platform_post.platform_specific_caption or post.caption or "").strip()
    snippet = caption[:60].strip()
    if snippet:
        return f"Post automation: {snippet}"
    return "Post automation"


# ---------------------------------------------------------------------------
# Choosing what a new flow answers for
# ---------------------------------------------------------------------------

#: What the chooser's second step offers, and the seeded canvas each produces.
#: The author can still change it afterwards — this decides what the canvas
#: *opens* with, not what it is allowed to become.
FLOW_KINDS = ("dm", "comment", "both")
DEFAULT_FLOW_KIND = "both"

#: Which posts the chooser offers to attach a flow to. Anything heading for
#: publication qualifies, not only what is already out: a flow on an unpublished
#: post saves unarmed and ``services.arm_for_platform_post`` switches it on when
#: the publisher confirms the post, which is the case ``PostAutomation`` exists
#: for. Failed and on-hold posts are left out — neither publishes without the
#: author going back to it first.
CHOOSABLE_POST_STATUSES = (
    PlatformPost.Status.PUBLISHED,
    PlatformPost.Status.SCHEDULED,
    PlatformPost.Status.APPROVED,
    PlatformPost.Status.PENDING_REVIEW,
    PlatformPost.Status.PENDING_CLIENT,
    PlatformPost.Status.DRAFT,
)


@login_required
@require_permission("reply_from_inbox")
def flow_new(request, workspace_id):
    """The builder with nothing chosen yet: an empty canvas behind a chooser.

    "New rule" lands here instead of on a form. The canvas cannot be drawn
    until it knows what it answers for, so the page opens the chooser —
    channel, then kind, then which posts — and only then redirects to the real
    builder. Writes nothing: every step is a GET, and the flow is still
    created by the first save.
    """
    workspace = _get_workspace(request, workspace_id)
    return render(
        request,
        "automations/flow_new.html",
        {
            "workspace": workspace,
            "channels": channels.choosable_accounts(workspace),
        },
    )


@login_required
@require_permission("reply_from_inbox")
def flow_choose_posts(request, workspace_id, account_id):
    """The chooser's third step: which posts on this account a rule answers for.

    Returns the modal's panel, not a page — the chooser swaps it in place so
    the earlier steps stay on screen and Back costs nothing.

    Scheduled and draft posts are listed alongside published ones. A flow on a
    post that is not live yet is the case this feature was built around: it is
    saved unarmed, matching nothing, and
    :func:`apps.automations.services.arm_for_platform_post` binds it to the
    real post id and switches it on the moment the publisher confirms the post
    (see ``PostAutomation``). Listing only published posts would have meant
    writing the automation *after* publishing, which is the wrong order — the
    reply is part of planning the post.

    Failed and on-hold posts are left out: neither is going to publish without
    the author returning to it first.
    """
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(
        SocialAccount,
        id=account_id,
        workspace=workspace,
    )

    can_automate, reason = channels.channel_support(account.platform)
    if not can_automate:
        # Re-asked here, not just in the template that drew the list: a client
        # that posts the id of a disabled channel must not get a post list it
        # can start a rule from.
        return JsonResponse({"error": reason}, status=400)

    posts = (
        PlatformPost.objects.filter(
            post__workspace=workspace,
            social_account=account,
            status__in=CHOOSABLE_POST_STATUSES,
        )
        .select_related("post", "social_account")
        .prefetch_related("post__media_attachments__media_asset")
        # Live posts first, then everything still on its way — the order the
        # author thinks about them in. ``F(...).desc(nulls_last=True)`` rather
        # than a bare ``-published_at``: Postgres sorts NULLs *first* in DESC,
        # so plain field ordering would float every unpublished post to the
        # top, which is the opposite of what this says.
        .order_by(
            models.F("published_at").desc(nulls_last=True),
            models.F("scheduled_at").desc(nulls_last=True),
            "-created_at",
        )[:50]
    )

    return render(
        request,
        "automations/partials/_flow_new_posts.html",
        {
            "workspace": workspace,
            "account": account,
            "posts": [
                {
                    "platform_post": p,
                    "preview": _post_preview(p),
                    "is_live": p.status == PlatformPost.Status.PUBLISHED,
                    "status_label": p.get_status_display(),
                }
                for p in posts
            ],
            "kind": request.GET.get("kind", DEFAULT_FLOW_KIND),
        },
    )


# ---------------------------------------------------------------------------
# Open the builder
# ---------------------------------------------------------------------------


@login_required
@require_permission("use_inbox")
def flow_builder(request, workspace_id, platform_post_id):
    """The canvas for one post's flow. Creates nothing — a GET must not write.

    A post with no flow yet renders the seeded default canvas
    (``flow.default_graph``) *without* persisting it, so opening the builder to
    look around leaves no row behind. The flow is created by the first save.
    """
    workspace = _get_workspace(request, workspace_id)
    platform_post = _get_platform_post(workspace, platform_post_id)

    automation = _flow_for(platform_post)
    if automation and automation.graph:
        graph = automation.graph
    else:
        # ``kind`` arrives from the new-rule chooser and only decides which
        # nodes are already on the canvas. Ignored once a flow exists: a saved
        # graph is the author's, and a stray query param must not redraw it.
        kind = request.GET.get("kind", "")
        graph = flow_compiler.default_graph(kind if kind in FLOW_KINDS else "dm")
    compiled = flow_compiler.compile_graph(graph)

    membership = request.workspace_membership
    context = {
        "workspace": workspace,
        "platform_post": platform_post,
        "automation": automation,
        # The dict itself, not a JSON string: the template renders it through
        # ``json_script``, which does the serialising. Passing pre-dumped text
        # would serialise it twice, and ``JSON.parse`` would hand the editor a
        # string where it expects an object — a canvas that silently opens empty.
        "graph": graph,
        "compiled": compiled,
        "supports_automation": supports_automation(platform_post),
        "is_published": platform_post.status == PlatformPost.Status.PUBLISHED,
        "has_platform_id": bool((platform_post.platform_post_id or "").strip()),
        # Which post this flow answers for — the question the header could not
        # answer before. See ``_post_preview``.
        "post_preview": _post_preview(platform_post),
        # The guardrails, for the settings modal. Read off the rule when one
        # exists and from the model's own defaults otherwise, so an unsaved
        # flow shows what it *would* be rather than blanks.
        "settings": _flow_settings(automation),
        "max_per_hour_ceiling": MAX_PER_HOUR_CEILING,
        "max_priority": MAX_PRIORITY,
        "keyword_mode_choices": _keyword_mode_choices(),
        "can_manage": membership.effective_permissions.get("reply_from_inbox", False),
    }
    return render(request, "automations/flow_builder.html", context)


def _keyword_mode_choices():
    """The match modes the trigger node offers, reusing the rule form's labels."""
    from apps.automations.forms import KEYWORD_MODE_CHOICES

    return KEYWORD_MODE_CHOICES


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


@login_required
@require_permission("reply_from_inbox")
@require_POST
def flow_save(request, workspace_id, platform_post_id):
    """Persist the canvas and recompile it into the rule. Returns JSON.

    The editor is a single-page canvas, so this is an API endpoint rather than a
    form post: a full page render on every save would lose the viewport, the
    selection and any node being dragged.

    **Create-or-update, in one transaction with the rule.** A flow and its rule
    are one thing to the author; a half-written pair — a flow whose rule failed
    to save, or a rule with no flow pointing at it — is a row the UI cannot
    render and the engine might still match on. ``select_for_update`` on the
    existing flow serialises two tabs saving the same canvas, so the later save
    wins whole rather than interleaving with the earlier one.

    **Arming is attempted on every save, not only the first.** A flow saved
    against an already-published post arms immediately; one saved against a
    draft does not, and will be armed by the publisher instead
    (``services.on_platform_post_published``). ``arm_for_platform_post`` is
    idempotent on the stored id, so calling it here unconditionally is what
    makes "edit a live flow" re-apply the new text without a separate path.
    """
    workspace = _get_workspace(request, workspace_id)
    platform_post = _get_platform_post(workspace, platform_post_id)

    if not supports_automation(platform_post):
        # Re-asked server-side: the composer hides the button for unsupported
        # platforms, but a hidden button is a UI state, not a guarantee.
        return JsonResponse(
            {"ok": False, "error": "Comment automations only run on Instagram and Facebook accounts."},
            status=400,
        )

    graph, error = _parse_graph(request)
    if error:
        return JsonResponse({"ok": False, "error": error}, status=400)

    compiled = flow_compiler.compile_graph(graph)
    # ``activate`` is the author pressing "turn on", carried as part of the save
    # rather than as a separate endpoint so that drawing a flow and switching it
    # on is one action. A flow with nothing to send is saved but refused
    # activation — the same rule ``views.rule_toggle`` applies, for the same
    # reason: an "active" rule the matcher filters out reads as working.
    wants_active = str(request.POST.get("activate", "")).lower() in {"1", "true", "on", "yes"}
    if wants_active and not compiled.can_act:
        return JsonResponse(
            {
                "ok": False,
                "error": "This flow has nothing to send. Write the comment reply or the DM before turning it on.",
                "warnings": compiled.warnings,
            },
            status=400,
        )

    with transaction.atomic():
        automation = (
            PostAutomation.objects.select_for_update()
            .filter(platform_post=platform_post)
            .select_related("rule")
            .first()
        )
        if automation is None:
            automation = _create_flow(request, workspace, platform_post)

        _apply_compiled(automation.rule, compiled, active=wants_active)
        _apply_settings(automation.rule, request.POST)
        automation.graph = graph
        if not wants_active and automation.is_armed:
            automation.status = PostAutomation.Status.PAUSED
        automation.save(update_fields=["graph", "status", "updated_at"])

    armed = False
    if wants_active:
        armed = services.arm_for_platform_post(platform_post)

    automation.refresh_from_db()
    return JsonResponse(
        {
            "ok": True,
            "flow_id": str(automation.pk),
            "status": automation.status,
            "status_label": automation.get_status_display(),
            "armed": armed or automation.is_armed,
            "can_act": compiled.can_act,
            "warnings": compiled.warnings,
        }
    )


def _parse_graph(request):
    """The posted canvas, validated for shape and size. Returns ``(graph, error)``.

    Only the envelope is validated here — that it is an object with a bounded
    list of nodes. Per-node meaning is the compiler's, and the compiler is
    deliberately tolerant (see :func:`apps.automations.flow._nodes`), because a
    malformed node is a front-end bug and refusing the whole save would lose the
    author's other work.
    """
    raw = request.POST.get("graph", "")
    if not raw:
        return None, "No flow was submitted."
    try:
        graph = json.loads(raw)
    except (TypeError, ValueError):
        return None, "The flow could not be read."
    if not isinstance(graph, dict):
        return None, "The flow could not be read."

    nodes = graph.get("nodes")
    if nodes is not None and not isinstance(nodes, list):
        return None, "The flow could not be read."
    if isinstance(nodes, list) and len(nodes) > MAX_NODES:
        return None, f"A flow can hold at most {MAX_NODES} steps."

    edges = graph.get("edges")
    if edges is not None and not isinstance(edges, list):
        return None, "The flow could not be read."

    # Stored as exactly these two keys. The canvas may carry viewport state and
    # other editor scratch; persisting it would let an arbitrary client write
    # unbounded JSON into a column nothing validates.
    return {"nodes": nodes or [], "edges": edges or []}, None


def _create_flow(request, workspace, platform_post):
    """Create the flow and the rule behind it, both inactive.

    Inactive and unscoped is the only safe initial state: a rule created
    ``is_active`` with ``post_scope="all"`` would, for the instant before the
    compile below narrows it, match every comment on the account. Creating it
    switched off and scoped to an empty post list means the window where it
    could misfire does not exist.
    """
    rule = AutomationRule.objects.create(
        workspace=workspace,
        social_account=platform_post.social_account,
        name=_default_rule_name(platform_post)[:200],
        trigger_type=AutomationRule.TriggerType.COMMENT,
        is_active=False,
        post_scope="specific",
        post_ids=[],
        public_reply_enabled=False,
        private_reply_enabled=False,
        created_by=request.user,
    )
    return PostAutomation.objects.create(
        workspace=workspace,
        platform_post=platform_post,
        rule=rule,
        status=PostAutomation.Status.PENDING,
        created_by=request.user,
    )


def _apply_settings(rule, post) -> None:
    """Write the settings-modal fields onto the rule. Absent keys are left alone.

    **Absent means unchanged, not default.** The canvas saves on every edit and
    does not always carry the settings; treating a missing key as "reset to 30
    an hour" would silently undo somebody's limit the next time they moved a
    node.

    Out-of-range values are clamped rather than refused. These are guardrails,
    not content: a save rejected over a mistyped limit would lose the flow the
    author was actually editing, and clamping lands somewhere they can see and
    correct.
    """
    if "max_per_hour" in post:
        # Zero is meaningful and kept: it pauses one rule's sending while it
        # still wins matches, which ``forms.clean_max_per_hour`` documents as a
        # deliberate configuration.
        with contextlib.suppress(TypeError, ValueError):
            rule.max_per_hour = max(0, min(MAX_PER_HOUR_CEILING, int(post["max_per_hour"])))
    if "priority" in post:
        # Non-negative, matching the model's CheckConstraint — a negative value
        # would raise at save time and lose the whole flow.
        with contextlib.suppress(TypeError, ValueError):
            rule.priority = max(0, min(MAX_PRIORITY, int(post["priority"])))
    if "once_per_sender_per_post" in post:
        rule.once_per_sender_per_post = str(post["once_per_sender_per_post"]).lower() in {"1", "true", "on", "yes"}
    if "name" in post:
        # Blank is ignored rather than stored: ``name`` is what identifies this
        # automation in the rules list, and an empty row there is unfindable.
        name = str(post["name"]).strip()
        if name:
            rule.name = name[:200]
    rule.save(update_fields=["name", "max_per_hour", "priority", "once_per_sender_per_post", "updated_at"])


def _apply_compiled(rule, compiled, *, active):
    """Write a compiled canvas onto its rule.

    ``post_scope`` and ``post_ids`` are **not** touched here. They are arming's
    business (``services.arm_for_platform_post``), and a save that reset them
    would un-arm a live flow every time its text was edited — the post id would
    be dropped and the rule would match nothing until the next publish, which
    never comes for an already-published post.

    ``is_active`` is only ever set to ``True`` for a rule that can act, and only
    when the author asked. Turning off is unconditional: "stop" must always be
    available, the same contract as ``views.rule_toggle``.
    """
    rule.include_keywords = compiled.include_keywords
    rule.exclude_keywords = compiled.exclude_keywords
    rule.top_level_only = compiled.top_level_only
    rule.public_reply_enabled = compiled.public_reply_enabled
    rule.public_reply_mode = compiled.public_reply_mode
    rule.public_reply_texts = compiled.public_reply_texts
    rule.private_reply_enabled = compiled.private_reply_enabled
    rule.private_reply_mode = compiled.private_reply_mode
    rule.private_reply_texts = compiled.private_reply_texts
    rule.is_active = bool(active and compiled.can_act)
    rule.save(
        update_fields=[
            "include_keywords",
            "exclude_keywords",
            "top_level_only",
            "public_reply_enabled",
            "public_reply_mode",
            "public_reply_texts",
            "private_reply_enabled",
            "private_reply_mode",
            "private_reply_texts",
            "is_active",
            "updated_at",
        ]
    )


# ---------------------------------------------------------------------------
# Toggle
# ---------------------------------------------------------------------------


@login_required
@require_permission("reply_from_inbox")
@require_POST
def flow_toggle(request, workspace_id, platform_post_id):
    """Pause or resume one post's flow without reopening the canvas.

    Resuming re-arms rather than only flipping a boolean: a flow that was paused
    while its post was still a draft has no post id on its rule, and switching
    ``is_active`` alone would produce an active rule scoped to nothing — active
    in the UI, matching nothing in production.
    """
    workspace = _get_workspace(request, workspace_id)
    platform_post = _get_platform_post(workspace, platform_post_id)

    automation = _flow_for(platform_post)
    if automation is None:
        return JsonResponse({"ok": False, "error": "This post has no automation yet."}, status=404)

    rule = automation.rule
    if automation.status == PostAutomation.Status.ACTIVE:
        rule.is_active = False
        rule.save(update_fields=["is_active", "updated_at"])
        automation.status = PostAutomation.Status.PAUSED
        automation.save(update_fields=["status", "updated_at"])
    else:
        if not flow_compiler.rule_can_act(rule):
            return JsonResponse(
                {"ok": False, "error": "This flow has nothing to send, so it was left off."},
                status=400,
            )
        services.arm_for_platform_post(platform_post)
        automation.refresh_from_db()
        if not automation.is_armed:
            # Still waiting on publication. Recorded as pending rather than
            # active so the UI does not claim a flow is running when the post it
            # watches does not exist yet.
            automation.status = PostAutomation.Status.PENDING
            automation.save(update_fields=["status", "updated_at"])

    return JsonResponse(
        {
            "ok": True,
            "status": automation.status,
            "status_label": automation.get_status_display(),
            "armed": automation.is_armed,
        }
    )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@login_required
@require_permission("reply_from_inbox")
@require_POST
def flow_delete(request, workspace_id, platform_post_id):
    """Remove a post's flow and the rule behind it. History survives.

    Deleting the ``AutomationRule`` cascades the ``PostAutomation`` away with it
    (the FK is CASCADE both ways), but ``AutomationLog.rule`` is ``SET_NULL`` —
    so what this rule already said in public stays on the record, and the
    uniqueness guards those log rows hold are not released. A redelivered
    webhook for a comment this flow already answered still finds a claim and
    still declines to answer twice.
    """
    workspace = _get_workspace(request, workspace_id)
    platform_post = _get_platform_post(workspace, platform_post_id)

    automation = _flow_for(platform_post)
    if automation is None:
        return JsonResponse({"ok": True, "deleted": False})

    with transaction.atomic():
        rule = automation.rule
        automation.delete()
        rule.delete()
    return JsonResponse({"ok": True, "deleted": True})


# ---------------------------------------------------------------------------
# Media picker
# ---------------------------------------------------------------------------

#: How many assets the picker offers at once. The grid is a chooser, not a
#: browser — the library has its own page for that — and an unbounded list
#: would send every image a workspace has ever uploaded over one request.
MEDIA_PAGE_SIZE = 60


def _sibling_workspace(user, workspace, requested_id):
    """One of the user's workspaces **in this organization**, or ``None``.

    Both halves are the point: membership, so a workspace id cannot be guessed
    into, and the organization, because that is the tenant boundary — two
    organizations are two customers, and an image crossing it could be posted
    to the wrong client's account.
    """
    from apps.members.models import WorkspaceMembership

    try:
        return Workspace.objects.get(
            id=requested_id,
            organization_id=workspace.organization_id,
            id__in=WorkspaceMembership.objects.filter(user=user).values("workspace_id"),
        )
    except (Workspace.DoesNotExist, ValidationError, ValueError):
        # A malformed UUID raises rather than missing, and a bad query string
        # is not an error worth a 500.
        return None


@login_required
@require_permission("use_inbox")
def flow_media(request, workspace_id):
    """Images from the media library, as JSON, for the card node's picker.

    A card's image used to be a URL the author pasted in. That works and keeps
    working — a flow may legitimately point at an image hosted anywhere — but
    it asks someone to leave the builder, find the file, copy a link and come
    back, for the one asset they almost certainly already uploaded here.

    **Images only.** The node renders a Meta *generic template*, whose
    ``image_url`` is exactly that; offering a video would produce a card that
    fails to send with nothing on screen explaining why.

    Returns absolute URLs, because the value is sent to Meta and fetched by
    Meta's servers — a relative path would resolve against graph.facebook.com
    and 404. Takes ``use_inbox`` (a read) rather than ``reply_from_inbox``: it
    lists what the workspace already owns and changes nothing.
    """
    workspace = _get_workspace(request, workspace_id)

    from apps.media_library.models import MediaAsset

    # A flow may use an image that lives in a *sibling* workspace: one brand's
    # logo is often the same file every workspace in the organization reaches
    # for, and re-uploading it per workspace is how two copies drift apart.
    #
    # **The organization is the boundary, and it is checked here rather than
    # trusted from the request.** ``from`` is a workspace id off a query
    # string; without this check it would read any workspace in the
    # installation, which is one client's image posted to another client's
    # account.
    source = workspace
    requested = (request.GET.get("from") or "").strip()
    if requested and requested != str(workspace.id):
        source = _sibling_workspace(request.user, workspace, requested)
        if source is None:
            return JsonResponse({"ok": False, "error": "That workspace is not available."}, status=404)

    assets = (
        MediaAsset.objects.for_workspace_with_shared(source.id, source.organization_id)
        .filter(media_type=MediaAsset.MediaType.IMAGE)
        .order_by("-created_at")
    )

    query = (request.GET.get("q") or "").strip()
    if query:
        assets = assets.filter(title__icontains=query)

    items = []
    for asset in assets[:MEDIA_PAGE_SIZE]:
        if not asset.file:
            continue
        items.append(
            {
                "id": str(asset.id),
                "title": asset.title or "",
                "url": request.build_absolute_uri(asset.file.url),
                # The grid renders the thumbnail where one exists; the full
                # file is what gets sent either way.
                "thumbnail": request.build_absolute_uri(asset.thumbnail.url) if asset.thumbnail else "",
            }
        )
    from apps.media_library.views import _sibling_workspaces

    return JsonResponse(
        {
            "ok": True,
            "items": items,
            "current": str(source.id),
            # Sent with every response so the picker's tabs cannot fall out of
            # step with what the server will actually serve.
            "workspaces": [{"id": str(w.id), "name": w.name} for w in _sibling_workspaces(request.user, workspace)],
        }
    )
