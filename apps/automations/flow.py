"""The canvas's compiler: a drawn graph in, executable rule fields out.

The flow builder lets an author draw nodes and connect them. The engine
(:mod:`apps.automations.matching`, :mod:`apps.automations.actions`) knows
nothing about nodes — it reads keywords and two reply bodies off an
:class:`~apps.automations.models.AutomationRule`. This module is the only place
those two representations meet.

**The direction is one-way, and deliberately so.** The canvas compiles *down*
into rule fields; the rule is never decompiled back into a canvas. The stored
``PostAutomation.graph`` is what reopens in the editor. Deriving the canvas from
the rule instead would discard node positions and any node the v1 engine cannot
execute, which means reopening a flow would silently redraw it — the one
behaviour that makes an author stop trusting a builder.

**What v1 actually executes.** Three node types compile to something:

* ``trigger_comment`` — the keyword list and the post scope.
* ``reply_comment`` — the public reply under the comment.
* ``send_dm`` — the private reply to the comment.

That is exactly the set ``AutomationRule`` can express, and the limit is the
*engine's*, not the canvas's: the rule has two reply channels and fires them as
two independent halves, with no per-conversation state to sequence a third step
against. Node types beyond these three are **preserved in the graph and
reported as inert** by :func:`unsupported_nodes` rather than dropped. Dropping
them would delete an author's work on reopen; executing them is not possible
yet; so the honest third option is to keep them, draw them, and have the editor
say plainly that they will not run. When a multi-step engine lands, those nodes
are already drawn and already stored.

**Multiple nodes of an executable type collapse into one channel's text list.**
Two ``reply_comment`` nodes in a row cannot be sequenced — the engine posts one
public reply — so their bodies become two entries in ``public_reply_texts``,
which the rule's ``random`` mode picks between. That is a real reading of "two
replies" rather than a silent loss, and :func:`compile_graph` reports it so the
editor can say so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "EXECUTABLE_NODE_TYPES",
    "CompiledFlow",
    "compile_graph",
    "default_graph",
    "rule_can_act",
    "unsupported_nodes",
]

#: Node types the engine can carry out.
#:
#: ``send_card`` and ``condition`` joined this set when
#: :mod:`apps.automations.engine` landed; before that the runtime had two reply
#: channels and no way to sequence or branch. They are executed by the graph
#: executor rather than by the two-channel path — see ``actions._runs_as_graph``
#: for which flows take which route.
EXECUTABLE_NODE_TYPES = frozenset({"trigger_comment", "reply_comment", "send_dm", "send_card", "condition"})

#: Node types the editor can draw but the engine cannot run yet. Listed rather
#: than inferred so the editor can label each one with why it is inert, and so a
#: typo in a node type is distinguishable from a known-future node.
KNOWN_INERT_NODE_TYPES = frozenset({"add_tag", "delay"})

#: Caps mirroring ``apps.automations.forms``. The compiled output goes through
#: that form's cleaners, which enforce these for real; applying them here too
#: means the editor can refuse a twenty-first node instead of showing a
#: validation error from a field the canvas does not render.
MAX_REPLY_NODES = 20


@dataclass
class CompiledFlow:
    """The rule fields a graph compiles to, plus what the author should be told.

    ``warnings`` is not an error channel — a flow that produced warnings still
    compiles and still saves. It carries the two things that are true about the
    drawing but invisible in the result: nodes that will not run, and nodes that
    were merged into one channel.
    """

    include_keywords: list[dict[str, str]] = field(default_factory=list)
    #: Keywords that **veto** a match, checked before the include list — see
    #: ``matching._match_comment``, where exclude deliberately wins.
    exclude_keywords: list[dict[str, str]] = field(default_factory=list)
    public_reply_enabled: bool = False
    public_reply_mode: str = "static"
    public_reply_texts: list[str] = field(default_factory=list)
    private_reply_enabled: bool = False
    private_reply_mode: str = "static"
    private_reply_texts: list[str] = field(default_factory=list)
    top_level_only: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def can_act(self) -> bool:
        """Whether this flow has anything to send.

        The same question ``matching._rule_can_act`` asks of a saved rule, asked
        here of an unsaved graph so the editor can disable "activate" before a
        round trip rather than after one.
        """
        return (self.public_reply_enabled and bool(self.public_reply_texts)) or (
            self.private_reply_enabled and bool(self.private_reply_texts)
        )


def rule_can_act(rule: Any) -> bool:
    """Whether a *saved* rule has an enabled channel holding sendable text.

    The same question :attr:`CompiledFlow.can_act` asks of an unsaved graph, and
    the same one ``matching._rule_can_act`` asks of a candidate. It is restated
    here rather than imported because that one is private to the matcher — the
    convention this codebase already follows in ``views._rule_can_act``, for the
    stated reason: if the two ever disagree the matcher wins, and the only cost
    of the duplication is a flow refused activation that would in fact have run,
    which is the failure direction that stops a send rather than causing one.

    Whitespace is not text, for the reason given in ``matching._has_text``: a
    body of ``"   "`` posts an empty comment or is refused by Meta, so a rule
    holding only that cannot do the thing it won the match to do.
    """

    def has_text(values: Any) -> bool:
        if not isinstance(values, list | tuple):
            return False
        return any(isinstance(value, str) and value.strip() for value in values)

    return (rule.public_reply_enabled and has_text(rule.public_reply_texts)) or (
        rule.private_reply_enabled and has_text(rule.private_reply_texts)
    )


def default_graph(kind: str = "dm") -> dict[str, Any]:
    """The canvas a brand-new flow opens with: a trigger and what ``kind`` asks for.

    Seeded rather than empty because an empty canvas makes the author's first
    decision "which node comes first", which has exactly one correct answer —
    the trigger — and no way to discover it.

    ``kind`` is what the new-rule chooser asked for, and only decides what is
    already drawn — the author can add or delete either reply afterwards:

    * ``"dm"`` — trigger → DM. The default, and the historical shape: the
      public reply is left out because answering publicly to every commenter
      is a choice with an audience-visible cost.
    * ``"comment"`` — trigger → public reply.
    * ``"both"`` — trigger → public reply, and trigger → DM.

    Positions are in canvas coordinates and are the editor's to change.
    """
    trigger = {
        "id": "trigger",
        "type": "trigger_comment",
        "x": 80,
        "y": 160,
        "data": {
            "keywords": [],
            "exclude_keywords": [],
            "match_mode": "contains",
            "top_level_only": False,
        },
    }
    dm = {"id": "dm", "type": "send_dm", "x": 440, "y": 160, "data": {"text": ""}}
    reply = {"id": "reply", "type": "reply_comment", "x": 440, "y": 20, "data": {"text": ""}}

    if kind == "comment":
        return {"nodes": [trigger, reply], "edges": [{"from": "trigger", "to": "reply"}]}

    if kind == "both":
        # The DM sits below the public reply, matching the order they happen
        # in: the comment is answered where it was left, then the DM follows.
        dm["y"] = 300
        return {
            "nodes": [trigger, reply, dm],
            "edges": [{"from": "trigger", "to": "reply"}, {"from": "trigger", "to": "dm"}],
        }

    return {"nodes": [trigger, dm], "edges": [{"from": "trigger", "to": "dm"}]}


def _nodes(graph: Any) -> list[dict[str, Any]]:
    """Every well-formed node in a stored graph, in drawn order.

    Tolerant by necessity: ``graph`` is a ``JSONField`` written by a JavaScript
    editor, so a malformed entry is a front-end bug rather than an attack, and
    the right response is to compile what is readable instead of refusing the
    whole flow. An unreadable node simply contributes nothing.
    """
    if not isinstance(graph, dict):
        return []
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, dict) and isinstance(node.get("type"), str)]


def unsupported_nodes(graph: Any) -> list[str]:
    """Node types present in this graph that the v1 engine will not run.

    Returned as a sorted list of types rather than node ids: the editor's
    message is about the *kind* of step ("buttons don't run yet"), and one
    message per node would repeat itself on a flow with four buttons.
    """
    found = {node["type"] for node in _nodes(graph) if node["type"] not in EXECUTABLE_NODE_TYPES}
    return sorted(found)


def _keyword_entries(data: Any, key: str = "keywords") -> list[dict[str, str]]:
    """One of the trigger node's keyword lists, in the ``{"text", "mode"}`` shape rules store.

    ``key`` selects the list: ``keywords`` to fire on, ``exclude_keywords`` to
    veto. Both carry identical rules and are read by the identical code path
    (``keywords.matches_any``), so they share this function for the reason
    ``forms.clean_keyword_list`` gives — two copies would drift, and the
    direction they drift in is "exclude accepts something include rejects",
    which is how a rule ends up with an exclude list that silently never
    matches.

    The canvas edits keywords as plain strings with one match mode for the whole
    node, because per-keyword match modes are a power-user control that would
    crowd a node the author meets first. The rule format is per-keyword, so the
    node's single mode is stamped onto each entry on the way out — which also
    means a rule hand-edited to mixed modes still round-trips through the node's
    own list without the canvas needing to render that difference.

    Blank and duplicate keywords are dropped here rather than passed to the
    form's cleaner, so the editor's own preview of "this fires on N keywords"
    matches what is saved.
    """
    if not isinstance(data, dict):
        return []
    mode = data.get("match_mode")
    if not isinstance(mode, str) or not mode:
        mode = "contains"
    raw = data.get(key)
    if not isinstance(raw, list):
        return []

    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in raw:
        # Accept both the canvas's plain strings and the rule's dict shape, so a
        # graph written by an import or an older editor build still compiles.
        if isinstance(value, dict):
            text = value.get("text")
            entry_mode = value.get("mode") if isinstance(value.get("mode"), str) else mode
        else:
            text = value
            entry_mode = mode
        if not isinstance(text, str) or not text.strip():
            continue
        text = text.strip()
        fingerprint = text.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        entries.append({"text": text, "mode": entry_mode})
    return entries


def _has_buttons(node: dict[str, Any]) -> bool:
    """Whether this node carries at least one button."""
    data = node.get("data")
    return isinstance(data, dict) and bool(data.get("buttons"))


def _has_image(node: dict[str, Any]) -> bool:
    """Whether this node carries a card image."""
    data = node.get("data")
    return isinstance(data, dict) and bool(data.get("image_url"))


def _node_text(node: dict[str, Any]) -> str:
    """One message node's body, trimmed. Empty means the author left it blank."""
    data = node.get("data")
    if not isinstance(data, dict):
        return ""
    text = data.get("text")
    if not isinstance(text, str):
        return ""
    return text.strip()


def compile_graph(graph: Any) -> CompiledFlow:
    """Compile a drawn canvas into the rule fields the engine executes.

    Never raises. The input is a ``JSONField`` filled in by a browser, and the
    caller is a view saving somebody's work — a malformed graph must degrade to
    "this flow has nothing to send" (which the editor already knows how to
    report) rather than to a 500 that loses the drawing.

    The trigger node is read for keywords and the top-level rule; message nodes
    are read in drawn order. **Edges are deliberately not walked.** With three
    executable node types and no per-conversation state, the engine fires the
    public reply and the DM as two independent halves regardless of how they are
    connected, so honouring edge order would imply a sequencing the runtime does
    not perform. Edges are stored and drawn; they become load-bearing when a
    multi-step engine makes them so.
    """
    compiled = CompiledFlow()
    nodes = _nodes(graph)

    for node in nodes:
        if node["type"] != "trigger_comment":
            continue
        data = node.get("data")
        compiled.include_keywords = _keyword_entries(data)
        compiled.exclude_keywords = _keyword_entries(data, "exclude_keywords")
        if isinstance(data, dict):
            compiled.top_level_only = bool(data.get("top_level_only"))
        # First trigger wins. A second one is a drawing the engine cannot
        # express (one rule, one keyword set), and it is reported below.
        break

    public_texts: list[str] = []
    private_texts: list[str] = []
    blank_nodes = 0
    for node in nodes:
        if node["type"] == "reply_comment":
            text = _node_text(node)
            if text:
                public_texts.append(text)
            else:
                blank_nodes += 1
        elif node["type"] in {"send_dm", "send_card"}:
            text = _node_text(node)
            if text:
                private_texts.append(text)
            elif _has_buttons(node) or _has_image(node):
                # A card with an image and buttons but no caption is a complete
                # message, not a blank step — counting it as empty would warn
                # the author about a node that works.
                private_texts.append(" ")
            else:
                blank_nodes += 1

    # Deduplicate while preserving the order the author drew. Two identical
    # bodies in ``random`` mode would skew the pick without the author being
    # able to see why.
    public_texts = list(dict.fromkeys(public_texts))[:MAX_REPLY_NODES]
    private_texts = list(dict.fromkeys(private_texts))[:MAX_REPLY_NODES]

    compiled.public_reply_enabled = bool(public_texts)
    compiled.public_reply_texts = public_texts
    # Several bodies on one channel can only mean "vary between these" — the
    # engine sends one. ``static`` would silently use the first and ignore the
    # rest, which reads as the later nodes being broken.
    compiled.public_reply_mode = "random" if len(public_texts) > 1 else "static"

    compiled.private_reply_enabled = bool(private_texts)
    compiled.private_reply_texts = private_texts
    compiled.private_reply_mode = "random" if len(private_texts) > 1 else "static"

    compiled.warnings = _warnings(
        graph,
        nodes=nodes,
        public_count=len(public_texts),
        private_count=len(private_texts),
        blank_nodes=blank_nodes,
    )
    return compiled


def _is_sequential(nodes: list[dict[str, Any]], graph: Any = None) -> bool:
    """Whether this drawing is one the graph executor runs step by step.

    The editor's half of ``actions._runs_as_graph``, asked of an unsaved
    drawing rather than a saved rule. The two must agree about which engine a
    flow uses, because the answer changes what the author is told: a sequential
    flow sends its messages in order, a two-channel one picks one at random.

    Restated rather than imported for the same reason ``rule_can_act`` is —
    that function reads a persisted rule and this one reads a graph — and the
    cost of a disagreement is only a warning that is wrong, never a send.
    """
    message_nodes = 0
    for node in nodes:
        node_type = node.get("type")
        if node_type in {"condition", "send_card"}:
            return True
        if node_type in {"send_dm", "reply_comment"}:
            message_nodes += 1
            if _has_buttons(node):
                return True
    if message_nodes > 2:
        return True
    # A node wired to two successors fans out, which only the graph executor
    # walks. Mirrors ``actions._fans_out``.
    edges = (graph or {}).get("edges") if isinstance(graph, dict) else None
    if isinstance(edges, list):
        seen: set[str] = set()
        for edge in edges:
            if not isinstance(edge, dict) or edge.get("branch"):
                continue
            source = edge.get("from")
            if not isinstance(source, str) or not source:
                continue
            if source in seen:
                return True
            seen.add(source)
    return False


def _warnings(
    graph: Any,
    *,
    nodes: list[dict[str, Any]],
    public_count: int,
    private_count: int,
    blank_nodes: int,
) -> list[str]:
    """What is true about this drawing but not visible in the compiled result.

    Each one names a specific way the canvas and the runtime disagree, in the
    author's words rather than the engine's. They are warnings and not errors
    because every one of them describes a flow that still saves and still runs —
    just not in the shape the drawing suggests.
    """
    warnings: list[str] = []

    inert = unsupported_nodes(graph)
    if inert:
        known = [name for name in inert if name in KNOWN_INERT_NODE_TYPES]
        if known:
            warnings.append("These steps are saved but will not run yet: " + ", ".join(known) + ".")
        unknown = [name for name in inert if name not in KNOWN_INERT_NODE_TYPES]
        if unknown:
            warnings.append("These steps were not recognised and will not run: " + ", ".join(unknown) + ".")

    triggers = sum(1 for node in nodes if node["type"] == "trigger_comment")
    if triggers == 0:
        warnings.append("This flow has no trigger, so nothing will start it.")
    elif triggers > 1:
        warnings.append("Only the first trigger is used; the others are ignored.")

    # The "picked at random" wording only applies to the two-channel path. A
    # flow the graph executor runs sends its messages in order, following the
    # edges, so telling its author they are randomised would describe the
    # opposite of what happens. ``actions._runs_as_graph`` owns that decision;
    # this mirrors its question rather than importing it, because that function
    # reads a saved rule and this one has only the drawing.
    sequential = _is_sequential(nodes, graph)
    if public_count > 1 and not sequential:
        warnings.append(f"The {public_count} comment replies are sent one at a time, picked at random.")
    if private_count > 1 and not sequential:
        warnings.append(f"The {private_count} direct messages are sent one at a time, picked at random.")
    if blank_nodes:
        warnings.append(f"{blank_nodes} message step(s) are empty and were skipped.")

    return warnings
