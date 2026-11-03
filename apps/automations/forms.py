"""The rule editor's form — and the four invariants the model cannot hold on its own.

``AutomationRule`` is deliberately permissive at the database level: its JSON
fields accept any JSON, and ``ReplyMode`` declares an ``AI`` member that exists
so the v2 rollout is a code deploy rather than a migration against a populated
table. Neither of those is a statement about what a human may submit through the
Studio, and this form is where that distinction is enforced.

Four things live here and nowhere else:

**The v1 AI gate.** ``ReplyMode.AI`` is a valid model choice and an invalid form
choice — see :func:`_v1_reply_mode_choices`.

**JSON shape.** ``include_keywords``, ``exclude_keywords``, ``post_ids`` and the
two reply-text lists arrive as strings from hidden inputs the Alpine editors
serialise. A ``JSONField`` will happily store ``{"drop": "table"}`` in a column
the matcher then reads as a list; the ``clean_*`` hooks below reject anything
that is not the exact ``{"text", "mode"}`` shape, naming unknown keys rather than
discarding them silently.

**Cross-tenant account binding.** ``social_account`` is filtered to the
workspace in :meth:`AutomationRuleForm.__init__`. An unfiltered ``ModelChoiceField``
would accept any account id in the installation, which is the one validation
failure here that crosses a tenant boundary rather than merely producing a bad
rule.

**The "silently dead rule" states.** Two configurations save without error and
then never fire: ``post_scope="specific"`` with no post ids, and
``is_active=True`` with no sendable reply text. Both are refused in
:meth:`AutomationRuleForm.clean`, because ``apps.automations.matching`` treats
them as "matches nothing" and "not a candidate" respectively, with no error
anywhere for the author to see.
"""

import json
from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.automations import keywords as keyword_matching
from apps.automations import matching
from apps.automations.models import AutomationRule
from apps.social_accounts.models import SocialAccount

#: Caps on the editable JSON lists.
#:
#: These are product limits, not storage limits — a ``JSONField`` has no opinion
#: about list length. They exist because every one of these lists is walked once
#: per inbound comment (see ``keywords.matches_any``), and because a rule with a
#: thousand keywords is not a rule anybody is maintaining deliberately.
MAX_KEYWORDS = 100
MAX_KEYWORD_CHARS = 200
MAX_POST_IDS = 50
MAX_POST_ID_CHARS = 200
MAX_REPLY_TEXTS = 20
MAX_REPLY_TEXT_CHARS = 500

#: Human labels for the five keyword modes, keyed by the stored value.
#:
#: Derived from :data:`apps.automations.keywords.KEYWORD_MODES` at import time so
#: a mode added there without a label here fails loudly at startup rather than
#: rendering a blank ``<option>``. The wording avoids the implementation words
#: ("contains", "any_word") on purpose: *"Appears anywhere"* versus *"Matches a
#: whole word"* is the distinction users actually get wrong, and naming the
#: behaviour rather than the mode is what makes it choosable.
#: ``gettext_lazy``, not ``gettext``: this dict is built at import time, before
#: any request has set a language, so translating eagerly would freeze whichever
#: locale the process started in for every user.
_KEYWORD_MODE_LABELS = {
    "exact": _("Matches the whole comment"),
    "contains": _("Appears anywhere"),
    "contains_loose": _("Appears anywhere (ignore accents)"),
    "any_word": _("Matches a whole word"),
    "any_word_loose": _("Matches a whole word (ignore accents)"),
}

KEYWORD_MODE_CHOICES = tuple((mode, _KEYWORD_MODE_LABELS[mode]) for mode in keyword_matching.KEYWORD_MODES)


def _v1_reply_mode_choices() -> list[tuple[str, str]]:
    """``ReplyMode`` minus ``AI`` — the v1 gate, and the only place it is applied.

    The model keeps ``AI`` as a choice because the column and the inert ``ai_*``
    fields ship in the initial migration; what must not happen in v1 is a rule
    whose mode is ``"ai"``, because no code path knows how to produce AI text yet
    and the action layer would find a rule it cannot execute. Django's model
    validation will not catch it — ``"ai"`` is a legitimate choice as far as the
    field is concerned — so the restriction has to be the form's.

    **v2 removes this function and passes ``ReplyMode.choices`` straight
    through.** Filtering rather than hardcoding ``[("static", …), ("random", …)]``
    is what makes that a one-line change instead of a hunt for every literal.
    """
    return [(value, label) for value, label in AutomationRule.ReplyMode.choices if value != AutomationRule.ReplyMode.AI]


def _loaded_json(value: Any) -> Any:
    """Decode a JSON string from a hidden input, or pass through already-parsed data.

    The Alpine list editors post ``JSON.stringify`` output into a hidden input, so
    the raw value is a string. ``JSONField.clean`` has usually already decoded it
    by the time a ``clean_<field>`` hook runs, but not when the field was given
    initial data programmatically (the dry-run path builds an unbound rule), so
    both shapes have to be handled. A string that is not valid JSON raises
    ``ValidationError`` rather than being treated as a one-element list — a
    truncated POST body must not be read as a keyword.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            return json.loads(text)
        except (TypeError, ValueError) as exc:
            raise forms.ValidationError("This field did not contain valid JSON.") from exc
    return value


def clean_keyword_list(value: Any) -> list[dict[str, str]]:
    """Validate one ``{"text", "mode"}`` keyword list. Shared by include and exclude.

    One function for both lists because they carry identical rules and are read by
    the identical code path (``keywords.matches_any``, via
    ``matching._keyword_entries``). Two copies would drift, and the direction they
    would drift in is "exclude accepts something include rejects", which is how a
    rule ends up with an exclude list that silently never matches.

    Unknown keys are rejected **by name** rather than stripped. That is a
    mass-assignment guard: the entries are stored verbatim in a ``JSONField``, so
    a key the UI never renders (``{"text": "x", "mode": "exact", "is_active":
    true}``) would persist invisibly and be available to any later code that
    iterates the dict. Naming it also tells an author who hand-edited JSON exactly
    what is wrong, instead of silently dropping the part they cared about.

    Dedupe is case-insensitive on ``(casefold(text), mode)`` because matching is
    casefolded too — ``"Price"`` and ``"price"`` in ``contains`` are the same
    keyword to the matcher, so keeping both would misrepresent the rule's size
    against :data:`MAX_KEYWORDS` while changing nothing about its behaviour. The
    *first* spelling wins, so the order the author typed is preserved.
    """
    value = _loaded_json(value)
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise forms.ValidationError("Keywords must be a list.")
    if len(value) > MAX_KEYWORDS:
        raise forms.ValidationError(f"Use at most {MAX_KEYWORDS} keywords; this list has {len(value)}.")

    cleaned: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            raise forms.ValidationError(f"Keyword {index} is not a keyword object.")

        unknown = sorted(set(entry) - {"text", "mode"})
        if unknown:
            raise forms.ValidationError(f"Keyword {index} has unexpected key(s): {', '.join(unknown)}.")

        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            raise forms.ValidationError(f"Keyword {index} is missing its text.")
        text = text.strip()
        if len(text) > MAX_KEYWORD_CHARS:
            raise forms.ValidationError(f"Keyword {index} is longer than {MAX_KEYWORD_CHARS} characters.")

        mode = entry.get("mode")
        if mode not in keyword_matching.KEYWORD_MODES:
            # ``matches_any`` ignores an unknown mode rather than raising, so a
            # rule saved with one would simply never fire — the exact failure
            # that is invisible in the UI. Refuse it at the door instead.
            raise forms.ValidationError(f"Keyword {index} has an unknown match type: {mode!r}.")

        fingerprint = (text.casefold(), mode)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        cleaned.append({"text": text, "mode": mode})

    return cleaned


def clean_string_list(value: Any, *, label: str, max_items: int, max_chars: int, drop_blank: bool) -> list[str]:
    """Validate a flat list of strings — post ids and reply texts both.

    ``drop_blank`` is the one difference between the two callers, and it is a real
    difference rather than a flag for its own sake. A blank reply text is a
    textarea the author added and never filled in, which is routine while
    drafting and should quietly disappear on save. A blank post id is a post
    reference that does not identify a post, and because ``post_scope="specific"``
    with an unmatched id matches nothing, accepting it would narrow the rule to a
    post that cannot exist. One is noise, the other is a dead rule.
    """
    value = _loaded_json(value)
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise forms.ValidationError(f"{label} must be a list.")

    cleaned: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, str):
            raise forms.ValidationError(f"{label} entry {index} is not text.")
        entry = entry.strip()
        if not entry:
            if drop_blank:
                continue
            raise forms.ValidationError(f"{label} entry {index} is blank.")
        if len(entry) > max_chars:
            raise forms.ValidationError(f"{label} entry {index} is longer than {max_chars} characters.")
        if entry in seen:
            continue
        seen.add(entry)
        cleaned.append(entry)

    # Counted after blanks are dropped: an author who added three empty
    # textareas has not used three of their twenty slots.
    if len(cleaned) > max_items:
        raise forms.ValidationError(f"Use at most {max_items} entries in {label}; this list has {len(cleaned)}.")
    return cleaned


class AutomationRuleForm(forms.ModelForm):
    """Create and edit one comment automation rule.

    ``workspace``, ``created_by`` and ``trigger_type`` are **not** fields.
    The first two are set by the view from the request, which is the only place
    they can be established safely; ``trigger_type`` has exactly one value in v1
    and exposing a one-choice select would invite a second value the matcher
    registry has no matcher for. The ``ai_*`` fields are excluded for the reason
    given in the model: nothing may read them yet.
    """

    class Meta:
        model = AutomationRule
        fields = [
            "name",
            "social_account",
            "is_active",
            "priority",
            "post_scope",
            "post_ids",
            "top_level_only",
            "include_keywords",
            "exclude_keywords",
            "public_reply_enabled",
            "public_reply_mode",
            "public_reply_texts",
            "private_reply_enabled",
            "private_reply_mode",
            "private_reply_texts",
            "once_per_sender_per_post",
            "max_per_hour",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-input w-full", "placeholder": "e.g. Send the pricing guide"}),
            "priority": forms.NumberInput(attrs={"class": "form-input w-full", "min": 0}),
            "max_per_hour": forms.NumberInput(attrs={"class": "form-input w-full", "min": 0}),
            # The three JSON lists are driven by Alpine editors in the template
            # and submitted as JSON strings; the visible controls are rendered
            # by hand there, so the form field itself is the hidden carrier.
            "post_ids": forms.HiddenInput(),
            "include_keywords": forms.HiddenInput(),
            "exclude_keywords": forms.HiddenInput(),
            "public_reply_texts": forms.HiddenInput(),
            "private_reply_texts": forms.HiddenInput(),
        }

    #: ``"all"`` / ``"specific"`` — the model stores a bare ``CharField`` with no
    #: choices, so the valid set is declared once, here.
    POST_SCOPE_CHOICES = (
        ("all", "Every post on the account"),
        ("specific", "Only the posts I list"),
    )

    def __init__(self, *args: Any, workspace: Any = None, **kwargs: Any) -> None:
        """Bind the form to one workspace, and narrow every choice to the v1 set.

        ``workspace`` is required in practice: without it the ``social_account``
        queryset would have to be either "every account in the installation" or
        empty, and the first of those is a cross-tenant hole. It is accepted as a
        keyword rather than read off an instance because a create form has no
        instance to read it from.
        """
        super().__init__(*args, **kwargs)
        self.workspace = workspace

        self.fields["post_scope"] = forms.ChoiceField(
            choices=self.POST_SCOPE_CHOICES,
            initial="all",
            widget=forms.Select(attrs={"class": "form-input w-full"}),
        )

        # The v1 AI gate. Applied to the field's *choices* so Django's own
        # ChoiceField validation does the refusing — a mode of "ai" fails
        # validation the same way "banana" does, with no extra code path to
        # forget. v2: drop these two lines.
        for name in ("public_reply_mode", "private_reply_mode"):
            self.fields[name].choices = _v1_reply_mode_choices()
            self.fields[name].widget.attrs.setdefault("class", "form-input w-full")

        # Cross-tenant guard. Scoping the queryset (rather than only checking in
        # ``clean``) means Django rejects a foreign id during field validation,
        # before any coherence check runs, and the rendered select cannot offer
        # one in the first place.
        #
        # Platform-filtered too: a rule bound to a YouTube or LinkedIn account
        # could never fire, because the comment webhooks that produce matchable
        # rows only exist for ``matching.COMMENT_PLATFORMS``.
        accounts = SocialAccount.objects.none()
        if workspace is not None:
            accounts = SocialAccount.objects.filter(
                workspace=workspace,
                platform__in=sorted(matching.COMMENT_PLATFORMS),
            ).order_by("account_name")
        self.fields["social_account"].queryset = accounts
        self.fields["social_account"].empty_label = "All connected Instagram and Facebook accounts"
        self.fields["social_account"].widget.attrs.setdefault("class", "form-input w-full")

    # --- Per-field cleaning -------------------------------------------------

    def clean_include_keywords(self) -> list[dict[str, str]]:
        return clean_keyword_list(self.cleaned_data.get("include_keywords"))

    def clean_exclude_keywords(self) -> list[dict[str, str]]:
        return clean_keyword_list(self.cleaned_data.get("exclude_keywords"))

    def clean_post_ids(self) -> list[str]:
        return clean_string_list(
            self.cleaned_data.get("post_ids"),
            label="Posts",
            max_items=MAX_POST_IDS,
            max_chars=MAX_POST_ID_CHARS,
            drop_blank=False,
        )

    def clean_public_reply_texts(self) -> list[str]:
        return clean_string_list(
            self.cleaned_data.get("public_reply_texts"),
            label="Public replies",
            max_items=MAX_REPLY_TEXTS,
            max_chars=MAX_REPLY_TEXT_CHARS,
            drop_blank=True,
        )

    def clean_private_reply_texts(self) -> list[str]:
        return clean_string_list(
            self.cleaned_data.get("private_reply_texts"),
            label="DMs",
            max_items=MAX_REPLY_TEXTS,
            max_chars=MAX_REPLY_TEXT_CHARS,
            drop_blank=True,
        )

    def clean_max_per_hour(self) -> int:
        """Accept ``0``, which means **this rule never sends**.

        Zero is allowed rather than rejected because it is the only way to pause
        one rule's sending without losing its configuration or its place in the
        priority order — and, unlike ``is_active=False``, a rule at zero still
        *wins* its matches, so it keeps swallowing comments that would otherwise
        fall through to a lower-priority rule. That is a legitimate thing to want
        (stop this rule speaking, keep it absorbing) and an easy thing to do by
        accident, so the template labels the field with what zero does. The
        throttle in ``guards.hourly_count`` needs no special case: a count of
        zero sends is never below a limit of zero.
        """
        return self.cleaned_data.get("max_per_hour") or 0

    def clean_social_account(self) -> Any:
        """Re-assert workspace ownership on the resolved object.

        The queryset in ``__init__`` already makes a foreign id fail field
        validation. This is the belt to that braces, and it is cheap: the one way
        the queryset guard can be bypassed is a caller constructing the form
        without ``workspace``, and that caller would otherwise get an unscoped
        ``ModelChoiceField`` on a tenant-scoped model.
        """
        account = self.cleaned_data.get("social_account")
        if account is None:
            return None
        if self.workspace is None or account.workspace_id != self.workspace.id:
            raise forms.ValidationError("That account does not belong to this workspace.")
        if account.platform not in matching.COMMENT_PLATFORMS:
            raise forms.ValidationError("Comment automations only run on Instagram and Facebook accounts.")
        return account

    # --- Cross-field coherence ---------------------------------------------

    def clean(self) -> dict[str, Any]:
        """Refuse the two configurations that save cleanly and then never fire.

        Both checks exist because ``apps.automations.matching`` reads these states
        as "no", silently:

        * ``post_scope="specific"`` with an empty ``post_ids`` — ``_match_comment``
          requires ``context.post_id in rule.post_ids``, so an empty list matches
          nothing at all. The rule would be saved, shown as configured, and never
          fire on any comment.
        * ``is_active=True`` with nothing sendable — ``candidates`` filters these
          rows out entirely, so an "active" rule would never even be considered.

        The second is deliberately conditional on ``is_active``. A rule with no
        reply text yet is the normal state of a rule being written (the model
        defaults ``is_active`` to ``False`` for exactly this reason), so saving a
        half-configured rule has to work. What must not be possible is *turning
        it on* in that state, because that is the moment the author believes it
        is working.
        """
        cleaned = super().clean()

        if cleaned.get("post_scope") == "specific" and not cleaned.get("post_ids"):
            self.add_error(
                "post_ids",
                "Choose at least one post, or switch the scope back to every post.",
            )

        public_enabled = bool(cleaned.get("public_reply_enabled"))
        private_enabled = bool(cleaned.get("private_reply_enabled"))
        public_texts = cleaned.get("public_reply_texts") or []
        private_texts = cleaned.get("private_reply_texts") or []

        # An enabled channel with no text is reported on the text field, not the
        # toggle: the author turned the channel on deliberately and the thing
        # that is missing is the message.
        if public_enabled and not public_texts:
            self.add_error("public_reply_texts", "Write the public reply, or turn public replies off.")
        if private_enabled and not private_texts:
            self.add_error("private_reply_texts", "Write the DM, or turn DMs off.")

        can_act = (public_enabled and bool(public_texts)) or (private_enabled and bool(private_texts))
        if cleaned.get("is_active") and not can_act:
            self.add_error(
                "is_active",
                "This rule has nothing to send, so it would never run. "
                "Add a public reply or a DM before turning it on.",
            )

        return cleaned
