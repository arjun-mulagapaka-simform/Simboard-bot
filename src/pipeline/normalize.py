"""Activity -> NormalizedMessage. Task 3.

Scope is bounded by ../../message-schema.md's "Reliably extractable" list:
clean text, sender identity, other mentions, reply flag, attachment presence.
No Graph calls, no attachment content, no quoted-message content.
"""

import re

from microsoft_teams.apps import ActivityContext
from microsoft_teams.api import MessageActivity

from src.models.normalized_message import MentionedUser, NormalizedMessage

HASHTAG_RE = re.compile(r"#(\w+)")


def _account_to_mentioned_user(account) -> MentionedUser:
    """Convert an SDK `Account` (sender/recipient/mentioned) into our
    `MentionedUser` model, keeping only the fields we rely on downstream.
    """
    return MentionedUser(
        id=account.id,
        aad_object_id=getattr(account, "aad_object_id", None),
        name=account.name,
    )


def _extract_other_mentions(activity: MessageActivity) -> list[MentionedUser]:
    """Pull every @mentioned user out of an activity except the bot itself,
    de-duplicated by id (a multi-word display name produces multiple raw
    mention entities for the same person — see inline note below).
    """
    bot_mention = activity.get_account_mention(activity.recipient.id)
    other_mention_entities = [
        e
        for e in (activity.entities or [])
        if e.type == "mention" and e is not bot_mention
    ]

    # Teams splits a multi-word display name (e.g. "Prerak Dave") into
    # multiple mention entities that share the same mentioned.id, each
    # carrying one word in .text. Group by id to reconstruct one
    # MentionedUser per person (name comes from `mentioned.name`, which is
    # already the full display name — unlike the raw per-word `.text`).
    seen: dict[str, MentionedUser] = {}
    for e in other_mention_entities:
        seen.setdefault(e.mentioned.id, _account_to_mentioned_user(e.mentioned))
    return list(seen.values())


def normalize(ctx: ActivityContext[MessageActivity]) -> NormalizedMessage:
    """Convert a raw Teams message activity into a `NormalizedMessage`,
    bounded to what's reliably extractable per ../../message-schema.md
    (no Graph calls, no attachment/quoted-message content).

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
