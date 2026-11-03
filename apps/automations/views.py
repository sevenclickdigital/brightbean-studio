"""Views for comment automations — the rule list, the editor, the dry run and the log.

**Permissions.** Viewing takes ``use_inbox``; mutating takes ``reply_from_inbox``.
An automation is an inbox reply that a rule sends instead of a person, so the
permission that governs it is the permission to reply — not
``manage_workspace_settings``, which is ``False`` for the manager role and would
lock out exactly the people who run the inbox. Whoever may answer a comment by
hand may automate that answer; whoever may only read the inbox may only read the
rules.

**Nothing here sends anything.** :func:`rule_test` is a dry run against stored
messages, and the view that looks most like sending — :func:`rule_toggle` — only
flips a boolean. Delivery belongs to the action layer, driven by the webhook.
"""

import json
import logging

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.automations import matching
from apps.automations.forms import KEYWORD_MODE_CHOICES, AutomationRuleForm
from apps.automations.models import AutomationLog, AutomationRule
from apps.inbox.models import InboxMessage
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

LOGS_PER_PAGE = 50

#: How many stored comments the dry run replays. Bounded because this runs
#: synchronously in a request and ``matches_history`` walks every keyword of the
#: rule over every message; fifty recent comments is enough to tell whether a
#: keyword list is picking up what the author meant.
TEST_SAMPLE_SIZE = 50


def _get_workspace(request, workspace_id):
    """Resolve workspace and enforce membership check.

    Same shape as ``apps.inbox.views._get_workspace``, deliberately. The RBAC
    middleware has already raised for a non-member by the time a view with a
    ``workspace_id`` kwarg runs, but this is the check that does not depend on
    middleware ordering staying as it is.
    """
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    has_membership = WorkspaceMembership.objects.filter(
        user=request.user,
        workspace=workspace,
    ).exists()
    if not has_membership:
        raise PermissionDenied("You are not a member of this workspace.")
    return workspace


def _rule_can_act(rule):
    """Whether this rule has an enabled channel holding sendable text.

    The same question ``matching._rule_can_act`` answers, asked here because the
    UI needs it for an unsaved-to-the-form-cycle decision (the toggle) and for
    labelling. It is re-expressed rather than imported because that function is
    private to the matcher; if the two ever disagree the matcher wins, and the
    only cost of the duplication is a badge that is wrong in a direction that
    stops a send rather than causing one.
    """
    return (rule.public_reply_enabled and _has_text(rule.public_reply_texts)) or (
        rule.private_reply_enabled and _has_text(rule.private_reply_texts)
    )


def _has_text(values):
    """At least one non-blank string. Whitespace is not text — see ``matching._has_text``."""
    if not isinstance(values, list | tuple):
        return False
    return any(isinstance(value, str) and value.strip() for value in values)


def _rule_row_context(workspace, rule, *, sent_24h, error=None):
    """Context for the ``_rule_row.html`` partial, which the toggle swaps in place."""
    return {
        "workspace": workspace,
        "rule": rule,
        "rule_can_act": _rule_can_act(rule),
        "sent_24h": sent_24h,
        "row_error": error,
    }


#: The five JSON list fields the Alpine editors own.
_JSON_LIST_FIELDS = (
    "post_ids",
    "include_keywords",
    "exclude_keywords",
    "public_reply_texts",
    "private_reply_texts",
)


def _initial_list(form, field_name):
    """The current value of one JSON list field, as a real list for ``json_script``.

    Needed because ``BoundField.value()`` does not return the list. These fields
    are rendered with a ``HiddenInput``, whose ``format_value`` serialises the
    JSON to a *string* — so the template would hand ``json_script`` the text
    ``'[{"text": "price", ...}]'`` and get a JSON string back rather than a JSON
    array. The editors would then find a string where they expect a list, reject
    it, and initialise empty: on the first edit of any existing rule, every
    keyword and every reply text would silently vanish.

    On a bound form that failed validation the raw POST string is what is
    available, so it is parsed back; a string that will not parse yields an empty
    list rather than raising, because this runs while re-rendering a form that is
    *already* reporting an error about that same field.
    """
    value = form[field_name].value()
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except (TypeError, ValueError):
            return []
    return value if isinstance(value, list) else []


def _form_context(workspace, form, rule=None):
    return {
        "workspace": workspace,
        "form": form,
        "rule": rule,
        "keyword_mode_choices": KEYWORD_MODE_CHOICES,
        # Passed as real lists, keyed by field name — see ``_initial_list``.
        "initial_lists": {name: _initial_list(form, name) for name in _JSON_LIST_FIELDS},
    }


# --- List ---------------------------------------------------------------------


@login_required
@require_permission("use_inbox")
def rule_list(request, workspace_id):
    """Every rule in this workspace, in match order, with its last-24h send count.

    The count is annotated rather than counted per row. ``AutomationLog`` is the
    highest-volume table in this app — one row per handled comment — so a
    per-rule query would be N queries against it on a page whose whole purpose is
    to list N rules.

    Only ``outcome="sent"`` rows count. The log also holds skips (rate limit,
    outside the reply window, duplicate) and failures, and a number labelled
    "sent today" that included them would overstate what the public actually saw.
    """
    workspace = _get_workspace(request, workspace_id)

    since = timezone.now() - timezone.timedelta(hours=24)
    rules = (
        AutomationRule.objects.for_workspace(workspace.id)
        # ``post_automation`` is read by ``_rule_row.html`` to decide whether
        # Edit opens the flow builder or the form editor; without it that is a
        # query per row.
        .select_related("social_account", "post_automation")
        .annotate(
            sent_24h=Count(
                "logs",
                filter=Q(logs__outcome=AutomationLog.Outcome.SENT, logs__created_at__gte=since),
            )
        )
    )
    # Ordering is ``Meta.ordering`` — priority, then created_at, then id — and it
    # is the real match order, not a display preference. See
    # ``AutomationRule.Meta``.
    rules = list(rules)

    # Attached per instance rather than passed as a parallel dict: a Django
    # template cannot look a dict up by a loop variable, and "does this JSON
    # list hold sendable text" is not a question a template can ask at all.
    for rule in rules:
        rule.can_act = _rule_can_act(rule)

    context = {
        "workspace": workspace,
        "rules": rules,
        "can_manage": request.workspace_membership.effective_permissions.get("reply_from_inbox", False),
    }
    return render(request, "automations/list.html", context)


# --- Create / edit ------------------------------------------------------------


@login_required
@require_permission("reply_from_inbox")
def rule_create(request, workspace_id):
    """Create a rule. Saved inactive unless the author explicitly turns it on."""
    workspace = _get_workspace(request, workspace_id)

    if request.method == "POST":
        form = AutomationRuleForm(request.POST, workspace=workspace)
        if form.is_valid():
            rule = form.save(commit=False)
            # Set on the view, never through the form: these two are the
            # tenant binding and the audit trail, and a form field for either
            # would be a field a POST body can choose.
            rule.workspace = workspace
            rule.created_by = request.user
            rule.save()
            return redirect("automations:list", workspace_id=workspace.id)
    else:
        form = AutomationRuleForm(workspace=workspace)

    return render(request, "automations/form.html", _form_context(workspace, form))


@login_required
@require_permission("reply_from_inbox")
def rule_edit(request, workspace_id, rule_id):
    """Edit a rule. A rule belonging to another workspace is a 404, not a 403.

    The ``workspace=workspace`` filter on the lookup is what makes that true: the
    rule id is a UUID in the URL and the only thing that establishes the caller
    may see it is its membership of *this* workspace.
    """
    workspace = _get_workspace(request, workspace_id)
    rule = get_object_or_404(AutomationRule, id=rule_id, workspace=workspace)

    if request.method == "POST":
        form = AutomationRuleForm(request.POST, instance=rule, workspace=workspace)
        if form.is_valid():
            form.save()
            return redirect("automations:list", workspace_id=workspace.id)
    else:
        form = AutomationRuleForm(instance=rule, workspace=workspace)

    return render(request, "automations/form.html", _form_context(workspace, form, rule=rule))


# --- Toggle -------------------------------------------------------------------


@login_required
@require_permission("reply_from_inbox")
@require_POST
def rule_toggle(request, workspace_id, rule_id):
    """Flip ``is_active``, re-validating the "can act" rule on the way **on**.

    The toggle does not go through the form, so the check
    ``AutomationRuleForm.clean`` performs is absent here — and this is precisely
    the path on which it matters. A rule is created inactive and
    half-configured; the list page offers a switch next to it; flipping that
    switch without the check would produce an "active" rule that
    ``matching.candidates`` filters out, which reads to the author as a working
    automation that silently answers nobody.

    Turning a rule **off** is never refused. Whatever state it is in, "stop" must
    always be available.
    """
    workspace = _get_workspace(request, workspace_id)
    rule = get_object_or_404(
        AutomationRule.objects.select_related("social_account", "post_automation"),
        id=rule_id,
        workspace=workspace,
    )

    error = None
    if not rule.is_active:
        if _rule_can_act(rule):
            rule.is_active = True
            rule.save(update_fields=["is_active", "updated_at"])
        else:
            error = "This rule has nothing to send, so it was left off. Add a public reply or a DM first."
    else:
        rule.is_active = False
        rule.save(update_fields=["is_active", "updated_at"])

    since = timezone.now() - timezone.timedelta(hours=24)
    sent_24h = AutomationLog.objects.filter(
        rule=rule,
        outcome=AutomationLog.Outcome.SENT,
        created_at__gte=since,
    ).count()

    return render(
        request,
        "automations/partials/_rule_row.html",
        _rule_row_context(workspace, rule, sent_24h=sent_24h, error=error),
    )


# --- Delete -------------------------------------------------------------------


@login_required
@require_permission("reply_from_inbox")
@require_POST
def rule_delete(request, workspace_id, rule_id):
    """Delete a rule. Its history survives.

    ``AutomationLog.rule`` is ``SET_NULL`` (see the model), so deleting a rule
    does not erase the record of what it already said in public, nor release the
    uniqueness guards those rows hold — a redelivered webhook for a comment this
    rule already answered still finds a claim and still declines to answer twice.
    The confirmation copy in the template says so, because "delete the rule" and
    "delete the evidence" are different things and users reasonably assume the
    first implies the second.
    """
    workspace = _get_workspace(request, workspace_id)
    rule = get_object_or_404(AutomationRule, id=rule_id, workspace=workspace)
    rule.delete()
    return redirect("automations:list", workspace_id=workspace.id)


# --- Dry run ------------------------------------------------------------------


@login_required
@require_permission("use_inbox")
@require_POST
def rule_test(request, workspace_id, rule_id):
    """Replay this rule against recent stored comments. **Sends nothing.**

    It reads ``InboxMessage`` rows that are already in the database and calls
    ``matching.matches_history``, which is pure — the module performs no writes
    at all, by design — so no ``InboxReply``, no ``AutomationLog`` and no
    platform call can come out of this view. Taking ``use_inbox`` rather than
    ``reply_from_inbox`` follows from that: it is a read.

    **The two warnings this view must raise itself.** ``matches_history``
    deliberately applies only the keyword/post predicate, skipping the platform
    gate and the "can act" gate, so that a rule whose reply text is not written
    yet still previews its keywords instead of returning a confusing empty list.
    The consequence is that a positive result does **not** mean the rule would
    fire: an inactive rule and a rule with nothing to send both match here and
    both do nothing in production. Reporting "3 comments matched" without saying
    so would be read as "it works", so ``is_active`` and ``can_act`` are passed
    to the template as explicit, separate warnings.
    """
    workspace = _get_workspace(request, workspace_id)
    rule = get_object_or_404(
        AutomationRule.objects.select_related("social_account", "post_automation"),
        id=rule_id,
        workspace=workspace,
    )

    messages = (
        InboxMessage.objects.for_workspace(workspace.id)
        .filter(message_type=InboxMessage.MessageType.COMMENT)
        .select_related("social_account")
        .order_by("-received_at")
    )
    if rule.social_account_id is not None:
        # A rule bound to one account should preview against that account only;
        # otherwise the dry run would show matches on comments the live path
        # would never offer it.
        messages = messages.filter(social_account_id=rule.social_account_id)
    else:
        # ``social_account=NULL`` means "every account of a compatible
        # platform", and compatible is a set rather than a column — the same
        # gate ``matching.eligible_rules`` applies. Without it the preview would
        # include the comment-typed rows the YouTube receiver writes, which
        # carry a video title rather than a comment body.
        messages = messages.filter(social_account__platform__in=sorted(matching.COMMENT_PLATFORMS))

    sample = list(messages[:TEST_SAMPLE_SIZE])
    matched = matching.matches_history(rule, sample)

    context = {
        "workspace": workspace,
        "rule": rule,
        "matched_messages": matched,
        "sample_size": len(sample),
        "matched_count": len(matched),
        # The two "it matched but it would not run" warnings — see the docstring.
        "rule_can_act": _rule_can_act(rule),
        "rule_is_active": rule.is_active,
    }
    return render(request, "automations/partials/_test_result.html", context)


# --- Log ----------------------------------------------------------------------


@login_required
@require_permission("use_inbox")
def log_list(request, workspace_id):
    """Paginated automation history, filterable by rule and by outcome.

    ``select_related`` on all three FKs: the template shows the rule name, the
    account name and the originating comment on every row, and this page shows
    fifty rows.
    """
    workspace = _get_workspace(request, workspace_id)

    logs = (
        AutomationLog.objects.for_workspace(workspace.id)
        .select_related("rule", "social_account", "inbox_message")
        .order_by("-created_at")
    )

    # Both filters are validated against the workspace's own data / the model's
    # own choices rather than passed through: ``rule`` is a UUID from a query
    # string, and filtering on it unscoped would let a log row from another
    # workspace be addressed by id. The ``for_workspace`` call above already
    # scopes the queryset, so this is about rejecting junk rather than about
    # isolation.
    rule_filter = request.GET.get("rule", "").strip()
    if rule_filter:
        if AutomationRule.objects.filter(id=rule_filter, workspace=workspace).exists():
            logs = logs.filter(rule_id=rule_filter)
        else:
            rule_filter = ""

    outcome_filter = request.GET.get("outcome", "").strip()
    if outcome_filter:
        if outcome_filter in AutomationLog.Outcome.values:
            logs = logs.filter(outcome=outcome_filter)
        else:
            outcome_filter = ""

    paginator = Paginator(logs, LOGS_PER_PAGE)
    page = paginator.get_page(request.GET.get("page"))

    context = {
        "workspace": workspace,
        "page_obj": page,
        "logs": page.object_list,
        "rules": AutomationRule.objects.for_workspace(workspace.id).only("id", "name"),
        "outcome_choices": AutomationLog.Outcome.choices,
        "rule_filter": rule_filter,
        "outcome_filter": outcome_filter,
    }
    return render(request, "automations/log.html", context)
