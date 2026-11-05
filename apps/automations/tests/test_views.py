"""View and form tests for comment automations.

The cases here are chosen around the four things that fail *silently* if they
regress, rather than around line coverage:

* **The v1 AI gate** — ``ReplyMode.AI`` is a valid model choice, so nothing below
  the form stops it.
* **Cross-tenant binding** — an account from another workspace, and a rule from
  another workspace.
* **The two dead-rule states** — specific scope with no posts, and active with
  nothing to send (including the half-configured *inactive* save, which must
  still work).
* **The dry run writing nothing** — asserted on the tables, not on the response.
* **The editor's data round trip** — the JSON lists have to survive server →
  Alpine → POST → server. See ``test_edit_form_emits_parseable_json_arrays``,
  and ``test_the_form_closes_no_script_block_early`` for the script block that
  carries them.
"""

import json
import re
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.automations.models import AutomationLog, AutomationRule
from apps.inbox.models import InboxMessage, InboxReply
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Automations WS", organization=organization)


@pytest.fixture
def other_workspace(db, organization):
    """A second workspace in the same org — the cross-tenant counterparty.

    Same organization on purpose: two workspaces under one org is the case where
    the user is a legitimate member of both, so workspace scoping is the only
    thing standing between them. A separate org would also be caught by the
    membership check, which would make the test pass for the wrong reason.
    """
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Other WS", organization=organization)


@pytest.fixture
def account(db, workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="ig-1",
        account_name="Studio IG",
        oauth_access_token="tok",
    )


@pytest.fixture
def other_account(db, other_workspace):
    return SocialAccount.objects.create(
        workspace=other_workspace,
        platform="instagram",
        account_platform_id="ig-2",
        account_name="Foreign IG",
        oauth_access_token="tok",
    )


def _member(workspace, user, role):
    return WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)


def _url(workspace, path=""):
    return f"/workspace/{workspace.id}/automations/{path}"


def _valid_payload(**overrides):
    """A POST body that saves cleanly: public reply on, with text.

    Written out in full rather than built from a factory because what each test
    does is change exactly one key of it, and the diff between a passing and a
    failing payload is the point of the test.
    """
    payload = {
        "name": "Pricing keyword",
        "social_account": "",
        "priority": "0",
        "post_scope": "all",
        "post_ids": "[]",
        "include_keywords": '[{"text": "price", "mode": "contains"}]',
        "exclude_keywords": "[]",
        "public_reply_enabled": "on",
        "public_reply_mode": "static",
        "public_reply_texts": '["Sent you a DM!"]',
        "private_reply_mode": "static",
        "private_reply_texts": "[]",
        "max_per_hour": "30",
    }
    payload.update(overrides)
    return payload


def _comment(workspace, account, body, *, platform_message_id="pm-1"):
    return InboxMessage.objects.create(
        workspace=workspace,
        social_account=account,
        platform_message_id=platform_message_id,
        message_type=InboxMessage.MessageType.COMMENT,
        sender_name="Ada",
        sender_handle="1234567890",
        body=body,
        received_at=timezone.now() - timedelta(hours=1),
    )


# --- 1 & 2: list permissions --------------------------------------------------


@pytest.mark.django_db
def test_rule_list_ok_for_member_with_use_inbox(client, workspace, org_owner, user):
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.EDITOR)
    client.force_login(user)

    resp = client.get(_url(workspace))

    assert resp.status_code == 200
    assert b"Automations" in resp.content


@pytest.mark.django_db
def test_rule_list_denied_without_use_inbox(client, workspace, org_owner, user):
    # ``contributor`` has use_inbox=False in BUILTIN_ROLE_PERMISSIONS. The rules
    # are reply configuration, so somebody who cannot see the inbox at all must
    # not see what gets auto-replied in it either.
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.CONTRIBUTOR)
    client.force_login(user)

    resp = client.get(_url(workspace))

    assert resp.status_code == 403


# --- 3: create sets tenant + author ------------------------------------------


@pytest.mark.django_db
def test_rule_create_sets_workspace_and_author(client, workspace, org_owner, user):
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)

    resp = client.post(_url(workspace, "create/"), _valid_payload())

    assert resp.status_code == 302
    rule = AutomationRule.objects.get(name="Pricing keyword")
    # Both set by the view, neither settable through the form.
    assert rule.workspace == workspace
    assert rule.created_by == user
    assert rule.include_keywords == [{"text": "price", "mode": "contains"}]
    # Not posted, so it keeps the model default — a rule starts off.
    assert rule.is_active is False


# --- 4: the v1 AI gate --------------------------------------------------------


@pytest.mark.django_db
def test_rule_create_rejects_ai_reply_mode(client, workspace, org_owner, user):
    """``ReplyMode.AI`` is a legal model choice and an illegal form choice.

    Nothing below the form would stop it: the field's ``choices`` include it, so
    model validation passes and the column accepts the string. If this test ever
    fails, a rule can be saved with ``public_reply_mode="ai"`` and the action
    layer will meet a rule it has no code to execute.
    """
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)

    resp = client.post(_url(workspace, "create/"), _valid_payload(public_reply_mode="ai"))

    assert resp.status_code == 200  # re-rendered form, not a redirect
    assert AutomationRule.objects.count() == 0
    assert "public_reply_mode" in resp.context["form"].errors

    # And the same for the private half.
    resp = client.post(
        _url(workspace, "create/"),
        _valid_payload(private_reply_mode="ai", private_reply_enabled="on", private_reply_texts='["hi"]'),
    )
    assert AutomationRule.objects.count() == 0
    assert "private_reply_mode" in resp.context["form"].errors


# --- 5: keyword mode validation ----------------------------------------------


@pytest.mark.django_db
def test_rule_create_rejects_unknown_keyword_mode(client, workspace, org_owner, user):
    """An unknown mode must be refused, not stored.

    ``keywords.matches_any`` ignores a mode it does not recognise rather than
    raising, so a rule saved with one would never fire and nothing anywhere would
    report a problem.
    """
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)

    resp = client.post(
        _url(workspace, "create/"),
        _valid_payload(include_keywords='[{"text": "price", "mode": "regex"}]'),
    )

    assert resp.status_code == 200
    assert AutomationRule.objects.count() == 0
    assert "include_keywords" in resp.context["form"].errors


@pytest.mark.django_db
def test_rule_create_rejects_unknown_keyword_key(client, workspace, org_owner, user):
    """Unknown keys are named, not silently dropped — the mass-assignment guard."""
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)

    resp = client.post(
        _url(workspace, "create/"),
        _valid_payload(include_keywords='[{"text": "price", "mode": "contains", "is_active": true}]'),
    )

    assert AutomationRule.objects.count() == 0
    errors = resp.context["form"].errors["include_keywords"]
    assert any("is_active" in str(error) for error in errors)


@pytest.mark.django_db
def test_keyword_dedupe_is_case_insensitive(client, workspace, org_owner, user):
    """Matching is casefolded, so "Price" and "price" are one keyword, not two."""
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)

    client.post(
        _url(workspace, "create/"),
        _valid_payload(
            include_keywords='[{"text": "Price", "mode": "contains"}, {"text": " price ", "mode": "contains"}]'
        ),
    )

    rule = AutomationRule.objects.get()
    # First spelling wins, trimmed.
    assert rule.include_keywords == [{"text": "Price", "mode": "contains"}]


# --- 6: cross-tenant social_account ------------------------------------------


@pytest.mark.django_db
def test_rule_create_rejects_foreign_social_account(client, workspace, other_workspace, other_account, org_owner, user):
    """A rule must not be bound to another workspace's account.

    This is the one validation failure in the form that crosses a tenant
    boundary: a rule in workspace A pointed at workspace B's account would have
    the action layer send from B's token on A's behalf.
    """
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    _member(other_workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)

    resp = client.post(_url(workspace, "create/"), _valid_payload(social_account=str(other_account.id)))

    assert resp.status_code == 200
    assert AutomationRule.objects.count() == 0
    assert "social_account" in resp.context["form"].errors


@pytest.mark.django_db
def test_rule_create_accepts_own_social_account(client, workspace, account, org_owner, user):
    """The counterpart: the workspace's own account is accepted."""
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)

    resp = client.post(_url(workspace, "create/"), _valid_payload(social_account=str(account.id)))

    assert resp.status_code == 302
    assert AutomationRule.objects.get().social_account == account


# --- 7: specific scope with no posts -----------------------------------------


@pytest.mark.django_db
def test_rule_create_rejects_specific_scope_without_posts(client, workspace, org_owner, user):
    """``matching`` reads this state as "matches nothing" — so it must not save."""
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)

    resp = client.post(_url(workspace, "create/"), _valid_payload(post_scope="specific", post_ids="[]"))

    assert resp.status_code == 200
    assert AutomationRule.objects.count() == 0
    assert "post_ids" in resp.context["form"].errors


# --- 8: active-without-text vs inactive-without-text -------------------------


@pytest.mark.django_db
def test_active_rule_without_reply_text_is_rejected(client, workspace, org_owner, user):
    """``candidates`` filters these rows out, so an "active" rule here is a lie."""
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)

    resp = client.post(
        _url(workspace, "create/"),
        _valid_payload(is_active="on", public_reply_enabled="", public_reply_texts="[]"),
    )

    assert resp.status_code == 200
    assert AutomationRule.objects.count() == 0
    assert "is_active" in resp.context["form"].errors


@pytest.mark.django_db
def test_inactive_rule_without_reply_text_is_accepted(client, workspace, org_owner, user):
    """The same state saves fine while off — a half-written rule is normal.

    The model defaults ``is_active`` to ``False`` for exactly this reason. Only
    *turning it on* is refused.
    """
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)

    resp = client.post(
        _url(workspace, "create/"),
        _valid_payload(public_reply_enabled="", public_reply_texts="[]"),
    )

    assert resp.status_code == 302
    rule = AutomationRule.objects.get()
    assert rule.is_active is False
    assert rule.public_reply_texts == []


@pytest.mark.django_db
def test_enabled_channel_without_text_points_at_the_text_field(client, workspace, org_owner, user):
    """Public replies on with nothing written: the error names the text, not the toggle."""
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)

    resp = client.post(_url(workspace, "create/"), _valid_payload(public_reply_texts="[]"))

    assert AutomationRule.objects.count() == 0
    assert "public_reply_texts" in resp.context["form"].errors


# --- 9: toggle ----------------------------------------------------------------


@pytest.mark.django_db
def test_rule_toggle_on_and_off(client, workspace, org_owner, user):
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)
    rule = AutomationRule.objects.create(
        workspace=workspace,
        name="Toggle me",
        public_reply_enabled=True,
        public_reply_texts=["hello"],
        is_active=False,
    )

    resp = client.post(_url(workspace, f"{rule.id}/toggle/"))
    assert resp.status_code == 200
    rule.refresh_from_db()
    assert rule.is_active is True

    resp = client.post(_url(workspace, f"{rule.id}/toggle/"))
    assert resp.status_code == 200
    rule.refresh_from_db()
    assert rule.is_active is False


@pytest.mark.django_db
def test_rule_toggle_refuses_to_activate_unactionable_rule(client, workspace, org_owner, user):
    """The toggle does not go through the form, so it re-checks "can act" itself.

    Without this, a rule saved half-configured (the normal starting state) could
    be switched on from the list page into a state ``matching.candidates``
    filters out — an automation the author believes is live and that answers
    nobody.
    """
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)
    rule = AutomationRule.objects.create(
        workspace=workspace,
        name="Nothing to say",
        public_reply_enabled=True,
        public_reply_texts=[],
        is_active=False,
    )

    resp = client.post(_url(workspace, f"{rule.id}/toggle/"))

    assert resp.status_code == 200
    rule.refresh_from_db()
    assert rule.is_active is False
    assert b"nothing to send" in resp.content.lower()


@pytest.mark.django_db
def test_rule_toggle_refuses_whitespace_only_reply_text(client, workspace, org_owner, user):
    """Whitespace is not text — the gate ``_has_text`` exists to catch."""
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)
    rule = AutomationRule.objects.create(
        workspace=workspace,
        name="Blank text",
        public_reply_enabled=True,
        public_reply_texts=["   "],
        is_active=False,
    )

    client.post(_url(workspace, f"{rule.id}/toggle/"))

    rule.refresh_from_db()
    assert rule.is_active is False


@pytest.mark.django_db
def test_rule_toggle_off_always_allowed(client, workspace, org_owner, user):
    """ "Stop" is never refused, whatever state the rule is in."""
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)
    rule = AutomationRule.objects.create(
        workspace=workspace,
        name="Already on, now broken",
        public_reply_enabled=True,
        public_reply_texts=[],
        is_active=True,
    )

    client.post(_url(workspace, f"{rule.id}/toggle/"))

    rule.refresh_from_db()
    assert rule.is_active is False


# --- 10: the dry run writes nothing ------------------------------------------


@pytest.mark.django_db
def test_rule_test_sends_nothing(client, workspace, account, org_owner, user):
    """The dry run must create no reply and no log row.

    Asserted on the tables rather than on the response, because the thing that
    matters is not what the panel says — it is that a customer did not receive a
    comment or a DM from a preview.
    """
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)
    _comment(workspace, account, "what is the price?", platform_message_id="pm-1")
    _comment(workspace, account, "lovely photo", platform_message_id="pm-2")
    rule = AutomationRule.objects.create(
        workspace=workspace,
        name="Price",
        include_keywords=[{"text": "price", "mode": "contains"}],
        public_reply_enabled=True,
        public_reply_texts=["Sent!"],
        is_active=True,
    )

    resp = client.post(_url(workspace, f"{rule.id}/test/"))

    assert resp.status_code == 200
    assert resp.context["matched_count"] == 1
    assert InboxReply.objects.count() == 0
    assert AutomationLog.objects.count() == 0
    # And it says so, in the panel.
    assert b"Nothing was sent" in resp.content


@pytest.mark.django_db
def test_rule_test_warns_when_rule_would_not_run(client, workspace, account, org_owner, user):
    """``matches_history`` skips the platform and "can act" gates by design.

    So a match count on its own reads as "it works". The view has to raise both
    conditions separately or the preview misleads: here the rule matches a
    comment, is off, and has nothing to send.
    """
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)
    _comment(workspace, account, "price please")
    rule = AutomationRule.objects.create(
        workspace=workspace,
        name="Half written",
        include_keywords=[{"text": "price", "mode": "contains"}],
        public_reply_enabled=True,
        public_reply_texts=[],
        is_active=False,
    )

    resp = client.post(_url(workspace, f"{rule.id}/test/"))

    assert resp.context["matched_count"] == 1
    assert resp.context["rule_is_active"] is False
    assert resp.context["rule_can_act"] is False
    assert b"This rule is off" in resp.content
    assert b"nothing to send" in resp.content.lower()


@pytest.mark.django_db
def test_rule_test_does_not_leak_other_workspace_comments(
    client, workspace, account, other_workspace, other_account, org_owner, user
):
    """The sample is workspace-scoped: a preview must not read another tenant's inbox."""
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)
    _comment(other_workspace, other_account, "price in the other workspace", platform_message_id="pm-foreign")
    rule = AutomationRule.objects.create(
        workspace=workspace,
        name="Price",
        include_keywords=[{"text": "price", "mode": "contains"}],
        public_reply_enabled=True,
        public_reply_texts=["Sent!"],
    )

    resp = client.post(_url(workspace, f"{rule.id}/test/"))

    assert resp.context["sample_size"] == 0
    assert resp.context["matched_count"] == 0


# --- 11: cross-workspace rule access -----------------------------------------


@pytest.mark.django_db
def test_rule_edit_of_foreign_rule_is_404(client, workspace, other_workspace, org_owner, user):
    """A rule id from another workspace is not found, even for a member of both."""
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    _member(other_workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)
    foreign = AutomationRule.objects.create(workspace=other_workspace, name="Foreign rule")

    resp = client.get(_url(workspace, f"{foreign.id}/edit/"))

    assert resp.status_code == 404


@pytest.mark.django_db
def test_rule_toggle_of_foreign_rule_is_404(client, workspace, other_workspace, org_owner, user):
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    _member(other_workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)
    foreign = AutomationRule.objects.create(
        workspace=other_workspace,
        name="Foreign rule",
        public_reply_enabled=True,
        public_reply_texts=["hi"],
    )

    resp = client.post(_url(workspace, f"{foreign.id}/toggle/"))

    assert resp.status_code == 404
    foreign.refresh_from_db()
    assert foreign.is_active is False


# --- 12: log ------------------------------------------------------------------


@pytest.mark.django_db
def test_log_list_is_workspace_scoped(client, workspace, account, other_workspace, other_account, org_owner, user):
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    _member(other_workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)

    mine = AutomationRule.objects.create(workspace=workspace, name="Mine")
    theirs = AutomationRule.objects.create(workspace=other_workspace, name="Theirs")
    AutomationLog.objects.create(
        workspace=workspace,
        social_account=account,
        rule=mine,
        comment_key="hash-mine",
        commented_at=timezone.now(),
        outcome=AutomationLog.Outcome.SENT,
    )
    AutomationLog.objects.create(
        workspace=other_workspace,
        social_account=other_account,
        rule=theirs,
        comment_key="hash-theirs",
        commented_at=timezone.now(),
        outcome=AutomationLog.Outcome.SENT,
    )

    resp = client.get(_url(workspace, "log/"))

    assert resp.status_code == 200
    assert b"Mine" in resp.content
    assert b"Theirs" not in resp.content
    assert list(resp.context["logs"]) == [AutomationLog.objects.get(comment_key="hash-mine")]


@pytest.mark.django_db
def test_log_list_denied_without_use_inbox(client, workspace, org_owner, user):
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.CONTRIBUTOR)
    client.force_login(user)

    assert client.get(_url(workspace, "log/")).status_code == 403


@pytest.mark.django_db
def test_log_list_survives_rule_deletion(client, workspace, account, org_owner, user):
    """``AutomationLog.rule`` is SET_NULL — the history outlives the rule.

    Deleting a rule must not erase the record of what it already posted in
    public, which is also what the delete confirmation in the UI promises.
    """
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)
    rule = AutomationRule.objects.create(
        workspace=workspace,
        name="Doomed",
        public_reply_enabled=True,
        public_reply_texts=["hi"],
    )
    AutomationLog.objects.create(
        workspace=workspace,
        social_account=account,
        rule=rule,
        comment_key="hash-1",
        commented_at=timezone.now(),
        outcome=AutomationLog.Outcome.SENT,
    )

    resp = client.post(_url(workspace, f"{rule.id}/delete/"))
    assert resp.status_code == 302
    assert AutomationRule.objects.count() == 0

    log = AutomationLog.objects.get()
    assert log.rule_id is None
    assert log.outcome == AutomationLog.Outcome.SENT

    resp = client.get(_url(workspace, "log/"))
    assert b"Deleted rule" in resp.content


# --- The editor's data round trip --------------------------------------------


@pytest.mark.django_db
def test_edit_form_emits_parseable_json_arrays(client, workspace, account, org_owner, user):
    """The Alpine editors must receive real JSON arrays, correctly escaped.

    This is a regression test for a failure with no visible symptom at save
    time. The lists are rendered with a ``HiddenInput``, which serialises the
    JSON to a *string*; handing that to ``json_script`` produces a JSON string
    rather than a JSON array, the editors reject the wrong type and initialise
    empty, and the next save writes empty lists over the author's keywords and
    reply texts. Nothing errors — the rule just quietly stops matching.

    The payloads are also the one place platform-ish text lands inside a
    ``<script>`` block, so the ``</script>`` and the embedded quote below are
    the escaping half of the same test.
    """
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)
    rule = AutomationRule.objects.create(
        workspace=workspace,
        name="Round trip",
        social_account=account,
        include_keywords=[{"text": "pre'ço", "mode": "any_word_loose"}],
        post_scope="specific",
        post_ids=["123", "456"],
        public_reply_enabled=True,
        public_reply_texts=["a </script> b", 'say "hi"'],
    )

    html = client.get(_url(workspace, f"{rule.id}/edit/")).content.decode()

    payloads = dict(re.findall(r'<script id="initial-([a-z_]+)" type="application/json">(.*?)</script>', html))
    # Every editor gets a list, not a string and not a blank.
    assert json.loads(payloads["post_ids"]) == ["123", "456"]
    assert json.loads(payloads["include_keywords"]) == [{"text": "pre'ço", "mode": "any_word_loose"}]
    assert json.loads(payloads["public_reply_texts"]) == ["a </script> b", 'say "hi"']
    # The literal closing tag must not survive, or it would end the script block.
    assert "</script> b" not in html


@pytest.mark.django_db
def test_the_form_closes_no_script_block_early(client, workspace, org_owner, user):
    """The page's own script source must not contain a closing tag either.

    The sibling test above covers *user data* reaching a ``<script>`` block,
    which ``json_script`` escapes. This covers the other half, which no escaping
    reaches: the block's own source closing it early. Both ways of doing that
    had shipped here, in the same ``//`` comment describing this very hazard —

    * a literal ``</script>`` in the prose. The HTML parser does not read JS
      comments, so it ends the block there.
    * the text ``{{ ... |json_script }}``, written to *name* the filter. Django
      renders template syntax inside comments like anywhere else, so it emitted
      a whole nested ``<script type="application/json">`` block, whose closing
      tag ended the outer one.

    Either way the remaining editor source renders as visible text at the bottom
    of the page and the Alpine components are never defined, so every list
    editor on the form is dead.

    Asserted by parsing rather than by eye: the symptom is far from the cause,
    and prose is exactly what gets rewritten without re-reading the tests.
    """
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)

    # ``?account=`` because a bare GET now redirects to the flow chooser: the
    # form is reached only by the account-wide path, which is the one shape a
    # canvas cannot hold.
    html = client.get(_url(workspace, "create/"), {"account": "x"}).content.decode()

    # Every inline block, matched by taking the tag's own attributes only
    # (``[^>]*`` before the ``>``) and then its body non-greedily. A stray
    # closing tag inside the source would split one block into two, so the
    # editor definitions would land outside every body.
    bodies = [
        body for attrs, body in re.findall(r"<script([^>]*)>(.*?)</script>", html, re.DOTALL) if "src=" not in attrs
    ]
    for body in bodies:
        assert "</script" not in body
    # The editors are what breaks, so pin that their definitions are inside a
    # block rather than stranded as text after an early close.
    assert any("Alpine.data('keywordEditor'" in body for body in bodies)


@pytest.mark.django_db
def test_invalid_submission_preserves_entered_lists(client, workspace, org_owner, user):
    """A rejected form must hand the author's lists back, not an empty editor.

    The re-render reads the raw POST strings rather than a model instance, which
    is the other half of ``_initial_list``. Losing them here would mean a single
    validation error wipes the keyword list the author just typed.
    """
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)

    # Fails on the empty post list, with keywords and reply text filled in.
    resp = client.post(
        _url(workspace, "create/"),
        _valid_payload(post_scope="specific", post_ids="[]"),
    )

    html = resp.content.decode()
    payloads = dict(re.findall(r'<script id="initial-([a-z_]+)" type="application/json">(.*?)</script>', html))
    assert json.loads(payloads["include_keywords"]) == [{"text": "price", "mode": "contains"}]
    assert json.loads(payloads["public_reply_texts"]) == ["Sent you a DM!"]


# --- Mutation permissions -----------------------------------------------------


@pytest.mark.django_db
def test_mutations_require_reply_permission(client, workspace, org_owner, user):
    """``viewer`` has use_inbox=False, so it reaches neither surface.

    The split that matters is tested above (contributor on the read surface);
    this pins the mutating endpoints so a future loosening of the read
    permission does not silently loosen the write one too.
    """
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.VIEWER)
    client.force_login(user)
    rule = AutomationRule.objects.create(workspace=workspace, name="Hands off")

    assert client.get(_url(workspace, "create/")).status_code == 403
    assert client.post(_url(workspace, f"{rule.id}/toggle/")).status_code == 403
    assert client.post(_url(workspace, f"{rule.id}/delete/")).status_code == 403
    assert AutomationRule.objects.count() == 1


@pytest.mark.django_db
def test_toggle_and_delete_reject_get(client, workspace, org_owner, user):
    """Mutations are POST-only: a GET must not change state."""
    _member(workspace, user, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(user)
    rule = AutomationRule.objects.create(
        workspace=workspace,
        name="POST only",
        public_reply_enabled=True,
        public_reply_texts=["hi"],
    )

    assert client.get(_url(workspace, f"{rule.id}/toggle/")).status_code == 405
    assert client.get(_url(workspace, f"{rule.id}/delete/")).status_code == 405
    assert client.get(_url(workspace, f"{rule.id}/test/")).status_code == 405
    rule.refresh_from_db()
    assert rule.is_active is False
