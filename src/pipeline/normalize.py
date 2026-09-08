"""Activity -> NormalizedMessage. Task 3.

Scope is bounded by ../../message-schema.md's "Reliably extractable" list:
clean text, sender identity, other mentions, reply flag, attachment presence.
No Graph calls, no attachment content, no quoted-message content.
"""

import re

from microsoft_teams.api import MessageActivity
from microsoft_teams.apps import ActivityContext

from src.models.normalized_message import MentionedUser, NormalizedMessage

HASHTAG_RE = re.compile(r"#(\w+)")
_AT_TAG_RE = re.compile(r"</?at>")


def _account_to_mentioned_user(account, name: str | None = None) -> MentionedUser:
    """Convert an SDK `Account` into our `MentionedUser` model.

    Covers sender/recipient/mentioned accounts, keeping only the fields we
    rely on downstream.

    Args:
        account: The SDK `Account` object.
        name: Display name to use instead of `account.name`. Needed for a
            multi-word mention (see `_extract_other_mentions`): real Teams
            was observed setting `mentioned.name` to only one word of the
            name (matching that entity's `.text`), not the full name —
            contrary to this module's prior assumption. Defaults to
            `account.name`, which is still correct for a single-word name
            (e.g. `activity.from_`, which isn't split into entities).
    """
    return MentionedUser(
        id=account.id,
        aad_object_id=getattr(account, "aad_object_id", None),
        name=name if name is not None else account.name,
    )


def _extract_other_mentions(activity: MessageActivity) -> list[MentionedUser]:
    """Pull every @mentioned user out of an activity except the bot itself.

    De-duplicated by id — a multi-word display name produces multiple raw
    mention entities for the same person (see inline note below).
    """
    # Exclude by mentioned.id, not by `is not activity.get_account_mention(...)`
    # — the bot's own multi-word display name is split across several
    # entities sharing its id (same as any other mentioned user's), and
    # `get_account_mention` only returns the first of them, so comparing
    # against that single entity object left the rest through as a bogus
    # "other mention" (observed 2026-09-08: bot named "Mention Bot POC"
    # produced a spurious mention "Bot POC").
    other_mention_entities = [
        e
        for e in (activity.entities or [])
        if e.type == "mention" and e.mentioned.id != activity.recipient.id
    ]

    # Teams splits a multi-word display name (e.g. "Prerak Dave") into
    # multiple mention entities sharing the same mentioned.id. Confirmed
    # against real Teams (2026-09-08): each split entity carries only its
    # own word in BOTH `.text` and `.mentioned.name` (name="Prerak"/
    # text="<at>Prerak</at>", then name="Dave"/text="<at>Dave</at>") — so
    # the full name must be reconstructed by joining each id's entities'
    # `.text` in order; `.mentioned.name` alone is not the full name.
    name_words: dict[str, list[str]] = {}
    accounts: dict[str, object] = {}
    for e in other_mention_entities:
        accounts.setdefault(e.mentioned.id, e.mentioned)
        name_words.setdefault(e.mentioned.id, []).append(_AT_TAG_RE.sub("", e.text or "").strip())

    return [
        _account_to_mentioned_user(accounts[mentioned_id], name=" ".join(words))
        for mentioned_id, words in name_words.items()
    ]


def normalize(ctx: ActivityContext[MessageActivity]) -> NormalizedMessage:
    """Convert a raw Teams message activity into a `NormalizedMessage`.

    Bounded to what's reliably extractable per ../../message-schema.md (no
    Graph calls, no attachment/quoted-message content).

    Args:
        ctx: The activity context for an inbound "message" activity where
            the bot was @mentioned (callers should already have checked
            `activity.is_recipient_mentioned()` — this function does not
            re-check it).

    Returns:
        A `NormalizedMessage` with:
        - `workflow_id`: set to `activity.conversation.id`, NOT a fresh
          id per message. Phase A allows at most one in-flight
          card-creation workflow per conversation, so a clarification
          reply (a new activity, same conversation) resolves to the same
          `workflow_id` as the original request and `workflow_store.get()`
          can find the pending draft. Keying on `activity.id` instead
          would make every clarification reply look like a brand-new,
          unrelated workflow — see `workflow.handle` for how this is used.
        - `text`: message text with all mention markup stripped.
        - `hashtags`: every `#word` found in the stripped text, in order,
          without the `#` — an empty list if none are present.
        - `sender`/`other_mentions`: identity info available on the raw
          activity only (no email/UPN — see `MentionedUser`).
        - `is_reply`/`reply_to_id`: `is_reply` is True iff
          `activity.reply_to_id` is set; `reply_to_id` carries that id
          verbatim (or `None`), used by `workflow.handle` to check whether
          this message is a direct reply to the bot's own clarification
          question (see `CardDraft.clarification_prompt_id`).
        - `has_attachment`: True iff `activity.attachments` is non-empty;
          says nothing about attachment content.

    Raises:
        AttributeError: if `ctx.activity` is missing expected fields
            (e.g. `conversation`, `from_`) — should not happen for a
            genuine "message" activity from the Bot Framework, but is not
            defensively checked here.
    """
    activity = ctx.activity

    clean_text = activity.strip_mentions_text().text
    hashtags = HASHTAG_RE.findall(clean_text)

    workflow_id = activity.conversation.id

    return NormalizedMessage(
        workflow_id=workflow_id,
        conversation_id=activity.conversation.id,
        text=clean_text,
        hashtags=hashtags,
        sender=_account_to_mentioned_user(activity.from_),
        other_mentions=_extract_other_mentions(activity),
        is_reply=activity.reply_to_id is not None,
        reply_to_id=activity.reply_to_id,
        has_attachment=bool(activity.attachments),
    )
