"""Keyword matching for auto-reply rules — five modes, and the one distinction that matters.

``exact`` and ``contains`` are what they sound like. ``any_word`` is the one
worth writing down, because it is the only thing that separates it from
``contains`` and it is the thing people complain about: ``"cat"`` in ``any_word``
matches *"my cat sleeps"* and does **not** match *"category"*, where ``contains``
matches both.

The ``_loose`` variants add accent-insensitivity on top of ``contains`` and
``any_word``, for pt-BR comments where "açúcar" and "acucar" are the same word
to the person typing.

Matching is case-insensitive and trimmed on both sides. ``casefold()`` rather
than ``lower()``: ``lower()`` gets German ß and Turkish İ wrong, which is not
hypothetical for a product whose commenters are strangers on the internet.

Ported from BrightBean Chat, apps/flows/triggers/keywords.py
Copyright (c) BrightBean (github.com/brightbeanxyz/brightbean-chat)
Licensed under AGPL-3.0. Source commit: 7bf959b (2026-10-21).
Modifications: ``contains_loose`` and ``any_word_loose`` modes for
accent normalisation (pt-BR).
"""

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

__all__ = ["KEYWORD_MODES", "MAX_MATCH_CHARS", "matches_any", "normalise", "strip_accents"]

#: How much of a message is scanned.
#:
#: Stored message bodies are far longer than this, because a bounded row is the
#: point there. Scanning all of it against a hundred keywords once per inbound
#: comment is a different question, and the reply budget is measured in
#: hundreds of milliseconds. Nothing legitimate hides a keyword 8k characters
#: into an Instagram comment.
MAX_MATCH_CHARS = 8192

#: ``\w+`` under Unicode, so "café" and "naïve" are single words.
_WORD_RE = re.compile(r"\w+", re.UNICODE)

#: The modes a rule may declare, in the order forms and validation should offer
#: them: each strict mode followed by its accent-insensitive variant. Anything
#: outside this tuple is ignored at match time rather than raising, so a stale
#: or hand-edited rule degrades to "never fires" instead of breaking the whole
#: inbound pipeline.
KEYWORD_MODES = ("exact", "contains", "contains_loose", "any_word", "any_word_loose")


def strip_accents(text: str) -> str:
    """Drop combining marks, so "açúcar" and "acucar" compare equal.

    NFD splits "ç" into "c" + cedilla; dropping category ``Mn`` (nonspacing
    mark) keeps the base letters. This is deliberately not applied to the
    strict modes — see ``matches_any``.
    """
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def normalise(text: Any) -> str:
    """Trimmed, casefolded and capped — the form both sides of a match are in."""
    if not isinstance(text, str):
        return ""
    return text.strip()[:MAX_MATCH_CHARS].casefold()


def matches_any(text: str, keywords: Iterable[Any]) -> bool:
    """Whether ``text`` matches any of these ``{"text", "mode"}`` entries.

    Both word sets are built lazily and at most once each, however many
    ``any_word`` / ``any_word_loose`` keywords there are — a hundred keywords
    over one message should tokenise the message once, not a hundred times.

    ``exact``, ``contains`` and ``any_word`` keep their accent-sensitive
    semantics untouched: a user who deliberately wrote the acute accent in a
    rule meant it, and silently widening ``exact`` would change the meaning of
    rules already in production. Accent-insensitivity is opt-in, via the
    ``_loose`` modes.
    """
    haystack = normalise(text)
    if not haystack:
        return False

    words: set[str] | None = None
    loose_haystack: str | None = None
    loose_words: set[str] | None = None
    for keyword in keywords:
        if not isinstance(keyword, dict):
            continue
        needle = normalise(keyword.get("text"))
        if not needle:
            continue
        mode = keyword.get("mode")
        if mode == "exact":
            if haystack == needle:
                return True
        elif mode == "contains":
            if needle in haystack:
                return True
        elif mode == "contains_loose":
            if loose_haystack is None:
                loose_haystack = strip_accents(haystack)
            if strip_accents(needle) in loose_haystack:
                return True
        elif mode == "any_word":
            if words is None:
                words = set(_WORD_RE.findall(haystack))
            if needle in words:
                return True
        elif mode == "any_word_loose":
            if loose_words is None:
                # Strip first, then tokenise: stripping each token separately
                # would cost one pass per word for the same result.
                loose_words = set(_WORD_RE.findall(strip_accents(haystack)))
            if strip_accents(needle) in loose_words:
                return True
    return False
