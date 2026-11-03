"""Shared Send API helpers for the Meta providers.

Facebook, Instagram (via Facebook Login) and Instagram (via Instagram Login)
all address a *person* rather than a message when replying, and all three build
the same request body. Keeping that in one place means a new payload shape only
has to be taught to the resolver once.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .exceptions import APIError, MessagingError, OAuthError, RateLimitError, TokenExpiredError
from .types import InboxMessage

logger = logging.getLogger(__name__)

META_DM_PLATFORMS = {"facebook", "instagram", "instagram_login"}


def enabled(value) -> bool:
    """Accept booleans and the strings stored by the credential admin."""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def message_timestamp(value) -> datetime | None:
    """Parse Meta's millisecond webhook timestamp or timezone-aware API date."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            stamp = datetime.fromtimestamp(value / 1000, UTC)
        else:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace("+0000", "+00:00"))
        return stamp if stamp.tzinfo is not None and stamp.year >= 2000 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def normalize_attachments(value) -> list[dict]:
    """Normalize webhook/API media without exposing raw payloads or unsafe URLs."""
    if isinstance(value, dict):
        value = value.get("data", [])
    if not isinstance(value, list):
        return []
    attachments = []
    for item in value:
        if not isinstance(item, dict):
            continue
        mime_type = item.get("mime_type")
        mime_type = mime_type if isinstance(mime_type, str) else ""
        kind = str(item.get("type") or mime_type.split("/")[0] or "file")
        payload = item.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        url = item.get("url") or payload.get("url")
        for key in ("image_data", "video_data", "audio_data", "file_data"):
            if isinstance(item.get(key), dict):
                kind = key.removesuffix("_data")
                url = url or item[key].get("url") or item[key].get("preview_url")
        try:
            parsed = urlsplit(str(url or ""))
            safe = parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
        except ValueError:
            safe = False
        attachments.append({"type": kind, "url": str(url) if safe else ""})
    return attachments


def dm_extra(message: dict, *, sender_id: str, conversation_id: str = "") -> dict:
    stamp = message_timestamp(message.get("created_time"))
    return {
        "sender_id": sender_id,
        "conversation_id": conversation_id,
        "platform_timestamp": stamp.isoformat() if stamp else None,
        "attachments": normalize_attachments(message.get("attachments", [])),
        "is_deleted": bool(message.get("is_deleted")),
        "is_unsupported": bool(message.get("is_unsupported")),
    }


def validate_instagram_text(text: str, *, platform: str) -> None:
    if not text.strip() or len(text.encode("utf-8")) > 1000:
        raise MessagingError(
            "Instagram DM replies must contain text and fit within 1,000 UTF-8 bytes.", platform=platform
        )


def require_message_id(data: dict, *, platform: str) -> str:
    message_id = data.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise MessagingError(
            "Delivery could not be confirmed. Check the conversation before retrying.", platform=platform
        )
    return message_id


class InstagramMessageBatch(list[InboxMessage]):
    """Messages and the continuation to commit after importing them.

    None means the DM channel did not complete a poll; an empty dict means the
    walk finished and the previous continuation can be cleared.
    """

    def __init__(self, messages=(), *, poll_state: dict | None = None):
        super().__init__(messages)
        self.poll_state = poll_state


def meta_subscription_needs_reconnect(exc: Exception) -> bool:
    """Recognize auth/permission failures without treating every 400 as auth."""
    if isinstance(exc, TokenExpiredError | OAuthError):
        return True
    if isinstance(exc, RateLimitError) or not isinstance(exc, APIError):
        return False
    if exc.status_code in {401, 403}:
        return True
    raw = exc.raw_response
    error = raw.get("error") if isinstance(raw, dict) else None
    if isinstance(error, dict):
        # Meta uses 400 for revoked tokens (190) and missing permissions (10/200).
        code = error.get("code")
        return isinstance(code, int) and code in {10, 190, 200}
    return isinstance(error, str) and error in {"invalid_token", "invalid_grant", "ExpiredToken", "InvalidToken"}


def fetch_instagram_page_messages(
    request, *, base_url, page_id, owner_id, access_token, since=None, poll_state=None
) -> InstagramMessageBatch:
    """Walk conversations and messages with durable continuation and bounds.

    Keep at most one conversation page's remaining IDs and the current message
    cursor. A later worker resumes that exact walk instead of spending every
    poll's budget on the same early history. Rebuild URLs locally, never follow
    platform-provided URLs, and ignore the shared comment/DM high-water mark.
    """
    started = time.monotonic()
    remaining_calls = 100
    messages = []
    state = poll_state if isinstance(poll_state, dict) else {}
    if state.get("page_id") != str(page_id) or state.get("owner_id") != str(owner_id):
        state = {}
    conversations = list(state.get("conversations") or [])
    conversation_after = state.get("conversation_after")
    message_after = state.get("message_after")
    listed_conversations = bool(conversations or conversation_after)
    seen_conversation_cursors = {conversation_after} if conversation_after else set()
    seen_message_cursors = {message_after} if message_after else set()

    def page(url, params, after):
        nonlocal remaining_calls
        query = {**params, "limit": 100}
        if after:
            query["after"] = after
        remaining_calls -= 1
        return request(
            "GET",
            url,
            access_token=access_token,
            params=query,
            timeout=max(0.1, min(20 - (time.monotonic() - started), 10)),
        ).json()

    def next_cursor(data, seen):
        paging = data.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not paging.get("next") or not after or after in seen:
            return None
        seen.add(after)
        return after

    while remaining_calls and time.monotonic() - started < 20:
        if not conversations:
            if listed_conversations and not conversation_after:
                break
            data = page(
                f"{base_url}/{page_id}/conversations",
                {"platform": "instagram", "fields": "id"},
                conversation_after,
            )
            conversations = [str(convo["id"]) for convo in data.get("data", []) if convo.get("id")]
            conversation_after = next_cursor(data, seen_conversation_cursors)
            listed_conversations = True
            continue
        convo_id = conversations[0]
        data = page(
            f"{base_url}/{convo_id}/messages",
            {"fields": "id,message,from,created_time,attachments"},
            message_after,
        )
        for msg in data.get("data", []):
            sender = msg.get("from") or {}
            sender_id = str(sender.get("id") or "")
            stamp = message_timestamp(msg.get("created_time"))
            if not sender_id or sender_id in {str(owner_id), str(page_id)} or msg.get("is_echo"):
                continue
            # The sync engine's high-water mark includes comments. A newer
            # comment must never hide an older DM after a failed DM poll.
            if not msg.get("id") or stamp is None:
                continue
            messages.append(
                InboxMessage(
                    platform_message_id=str(msg["id"]),
                    sender_id=sender_id,
                    sender_name=sender.get("name") or sender.get("username") or sender_id,
                    text=msg.get("message") or "",
                    timestamp=stamp,
                    message_type="dm",
                    extra=dm_extra(msg, sender_id=sender_id, conversation_id=str(convo_id)),
                )
            )
        message_after = next_cursor(data, seen_message_cursors)
        if not message_after:
            conversations.pop(0)
            seen_message_cursors.clear()
    continuation = {}
    if conversations or conversation_after or not listed_conversations:
        logger.warning("Instagram DM poll reached its bounded request/time budget")
        continuation = {
            "page_id": str(page_id),
            "owner_id": str(owner_id),
            "conversations": conversations,
            "conversation_after": conversation_after,
            "message_after": message_after,
        }
    return InstagramMessageBatch(messages, poll_state=continuation)


def merge_page_message_subscription(
    request, *, base_url, page_id, access_token, app_id, required_fields=("messages",)
) -> bool:
    """Add messages while preserving this app's existing Page fields."""
    if not app_id:
        raise MessagingError("Configure the Meta App ID before enabling DM notifications.")
    url = f"{base_url}/{page_id}/subscribed_apps"
    data = request("GET", url, access_token=access_token).json()
    fields = set(required_fields)
    for app in data.get("data", []):
        if str(app.get("id")) == str(app_id):
            fields.update(app.get("subscribed_fields") or [])
    resp = request("POST", url, access_token=access_token, params={"subscribed_fields": ",".join(sorted(fields))})
    if not resp.json().get("success"):
        return False
    readback = request("GET", url, access_token=access_token).json()
    return any(
        str(app.get("id")) == str(app_id) and fields.issubset(app.get("subscribed_fields") or [])
        for app in readback.get("data", [])
    )


# Where the person's platform-scoped ID hides, in priority order. The inbox view
# passes ``recipient_id`` explicitly, so it wins; a webhook messaging event
# nests it under ``sender``; a polled conversation carries ``from``; and
# ``sender_id`` is what the provider stores when polling.
_RECIPIENT_KEYS = ("recipient_id", "sender", "from", "sender_id")


def resolve_recipient_id(extra: dict | None) -> str:
    """Return the scoped ID (PSID or IGSID) of the person being replied to.

    Returns an empty string when the payload carries no sender, which callers
    treat as "cannot send" rather than guessing a recipient.
    """
    extra = extra or {}
    for key in _RECIPIENT_KEYS:
        value = extra.get(key)
        if isinstance(value, dict):
            value = value.get("id")
        if value:
            return str(value)
    return ""


def resolve_private_reply_comment_id(extra: dict | None) -> str:
    """Return the comment a private reply should be addressed to, if any.

    The key is deliberately ``private_reply_to_comment_id`` and not a bare
    ``comment_id``: ``extra`` is Meta's raw payload merged with our own fields,
    and Meta already puts ``comment_id`` in Instagram mention payloads (see
    ``apps/inbox/webhooks.py::_upsert_instagram_mention``). Reading that key
    here would silently turn an ordinary DM reply into a private reply — which
    Meta allows only once per comment and only inside a short window, so the
    real reply would fail with no obvious cause.
    """
    value = (extra or {}).get("private_reply_to_comment_id")
    return str(value or "")


#: Buttons Meta accepts on a ``button`` template, by our own type name.
#:
#: Only these two, and the restriction is the platform's rather than ours.
#: ``web_url`` opens a link; ``postback`` sends the payload back to the webhook.
#: Instagram's Send API rejects the other Messenger button types (call, login,
#: game_play) outright, and a flow that rendered differently per platform would
#: be a flow whose author cannot predict what their audience sees.
BUTTON_TYPES = frozenset({"web_url", "postback"})

#: Meta's own ceiling on a button template. Exceeding it fails the whole send,
#: so the extras are dropped rather than allowed to cost the message.
MAX_BUTTONS = 3

#: Longest a button's visible label may be before Meta refuses it.
MAX_BUTTON_TITLE = 20


def build_buttons(buttons) -> list[dict]:
    """Normalise a flow's button list into Send API button objects.

    Returns ``[]`` for anything unusable, which the caller reads as "send this
    as plain text". That is the important failure mode: a malformed button must
    cost the *button*, never the message, because the message is what the
    person is waiting for.

    Each entry is ``{"type", "title", "url"|"payload"}``. A ``web_url`` button
    with no URL and a ``postback`` with no payload are both dropped — the first
    would render a button that does nothing, the second one whose click the
    webhook cannot route.

    Titles are truncated rather than rejected: Meta refuses the whole send over
    a long label, and losing a 25-character label's tail is better than losing
    the message it was attached to.
    """
    if not isinstance(buttons, list | tuple):
        return []
    built: list[dict] = []
    for entry in buttons:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind not in BUTTON_TYPES:
            continue
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        title = title.strip()[:MAX_BUTTON_TITLE]

        if kind == "web_url":
            url = entry.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            # Scheme-checked here rather than trusted: this value reaches a
            # person's browser, and a ``javascript:`` URL in a message the
            # *account* appears to have sent is the account's problem.
            url = url.strip()
            if not url.lower().startswith(("http://", "https://")):
                continue
            built.append({"type": "web_url", "title": title, "url": url})
        else:
            payload = entry.get("payload")
            if not isinstance(payload, str) or not payload.strip():
                continue
            built.append({"type": "postback", "title": title, "payload": payload.strip()})

        if len(built) >= MAX_BUTTONS:
            break
    return built


def build_send_payload(
    recipient_id: str = "",
    text: str = "",
    *,
    human_agent: bool = False,
    comment_id: str = "",
    buttons=None,
    image_url: str = "",
) -> dict:
    """Build a Send API request body.

    Past 24 hours Meta only accepts a tagged message, and HUMAN_AGENT is the tag
    for a person (never a bot) answering within 7 days.

    ``comment_id`` switches the body to a **private reply**: it addresses a
    *comment* instead of a person — ``{"recipient": {"comment_id": ...}}`` — and
    Meta opens the DM thread itself. That is the only way to DM someone who has
    never messaged the account, because until the thread exists there is no
    PSID/IGSID to address. It wins over ``recipient_id`` when both are given.

    A private reply is never sent as HUMAN_AGENT, so ``human_agent`` is ignored
    whenever ``comment_id`` is set. HUMAN_AGENT asserts that a *person* is
    answering outside the 24-hour window; an automated private reply is the
    opposite claim, and Meta may reject or penalize the Page for making it.

    ``buttons`` promotes the body from a plain text message to a **button
    template**, and ``image_url`` to a **generic template** (a card: image,
    title, buttons). The three shapes are mutually exclusive in the API, so the
    choice is made here, once, rather than by each provider. Buttons that do not
    survive :func:`build_buttons` are dropped and the message degrades to plain
    text — see that function for why losing the button must not cost the
    message.
    """
    # Padrão de brightbean-chat apps/channels/providers/instagram.py (AGPL-3.0).
    recipient = {"comment_id": comment_id} if comment_id else {"id": recipient_id}
    # Forced rather than merely unset so no caller can smuggle the tag in with a
    # comment_id — the guard has to hold at the single place the body is built.
    human_agent = human_agent and not comment_id

    built = build_buttons(buttons)
    if image_url and built:
        # Generic template: the only shape that carries an image *and* buttons.
        # ``text`` becomes the card's title, which is what the person reads.
        message: dict = {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": [
                        {
                            "title": (text or " ")[:80],
                            "image_url": image_url,
                            "buttons": built,
                        }
                    ],
                },
            }
        }
    elif built:
        message = {
            "attachment": {
                "type": "template",
                "payload": {"template_type": "button", "text": text or " ", "buttons": built},
            }
        }
    elif image_url:
        message = {"attachment": {"type": "image", "payload": {"url": image_url, "is_reusable": True}}}
    else:
        message = {"text": text}

    payload: dict = {
        "recipient": recipient,
        "message": message,
        "messaging_type": "MESSAGE_TAG" if human_agent else "RESPONSE",
    }
    if human_agent:
        payload["tag"] = "HUMAN_AGENT"
    return payload
