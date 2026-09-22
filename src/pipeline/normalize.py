"""Activity -> NormalizedMessage. Task 3.

Scope is bounded by ../../message-schema.md's "Reliably extractable" list:
clean text, sender identity, other mentions, reply flag, attachment presence.
No Graph calls, no attachment content, no quoted-message content.
"""

import logging
import re

from microsoft_teams.api import MessageActivity, TeamsChannelAccount
from microsoft_teams.apps import ActivityContext

from src.models.normalized_message import MentionedUser, NormalizedMessage

logger = logging.getLogger("normalize")

# Captures everything after "#" up to the next "#" or end of message, not
# just the first word — a hashtag project/board name can contain spaces
# (e.g. "#Apollo Web Revamp"). Trailing whitespace/sentence punctuation is
# stripped separately below. Only end-of-message (or next "#") terminates
# a hashtag; a hashtag mid-sentence followed by other words will swallow
# them too — there's no way to tell where a multi-word hashtag ends
# otherwise (see tasks-list.md for this tradeoff).
HASHTAG_RE = re.compile(r"#(\w[^#]*)")
_HASHTAG_TRAILING_PUNCTUATION = " .,!?;:\n\t"
_AT_TAG_RE = re.compile(r"</?at>")


def _account_to_mentioned_user(
    account: TeamsChannelAccount, name: str | None = None
) -> MentionedUser:
    """Convert an SDK account into our `MentionedUser` model.

    Covers sender/recipient/mentioned accounts, keeping only the fields we
    rely on downstream.

    Args:
        account: The SDK account object. Typed as `TeamsChannelAccount` for
            documentation purposes (a strict superset of the plain
            `Account` Teams actually sends today — same `id`/`name`/
            `aad_object_id`, plus `email`/`user_principal_name`/
            `tenant_id`), but the SDK still constructs a plain `Account`
            at runtime regardless of this annotation (confirmed live
            2026-09-16: direct `.email` access raised `AttributeError`) —
            use `getattr` for every field beyond `id`/`name`, never direct
            attribute access.
        name: Display name to use instead of `account.name`. Needed for a
            multi-word mention (see `_extract_other_mentions`): real Teams
            was observed setting `mentioned.name` to only one word of the
            name (matching that entity's `.text`), not the full name —
            contrary to this module's prior assumption. Defaults to
            `account.name`, which is still correct for a single-word name
            (e.g. `activity.from_`, which isn't split into entities).
    """
    # `getattr`, not `account.email` — the type annotation above is our
    # code's claim about the shape, not what the SDK actually constructs.
    # Confirmed live (2026-09-16): the SDK still hands us a plain `Account`
    # (no `email` attribute at all, not even `None`) regardless of this
    # annotation, so a direct `.email` access raises AttributeError instead
    # of degrading gracefully. Keep `getattr` until the SDK itself returns
    # a real `TeamsChannelAccount`.
    return MentionedUser(
        id=account.id,
        aad_object_id=getattr(account, "aad_object_id", None),
        name=name if name is not None else account.name,
        email=getattr(account, "email", None),
    )


async def _backfill_from_roster(ctx: ActivityContext, user: MentionedUser) -> MentionedUser:
    """Fill in a mentioned user's `aad_object_id`/`email` via a roster lookup.

    Confirmed live (2026-09-18, post-RSC): a mention entity's `mentioned`
    object reliably carries only the conversation-scoped `id` (`29:...`),
    not `aad_object_id` — unlike the sender, whose `aad_object_id` comes
    populated on `activity.from_` directly. This is documented Teams
    behavior, not something RSC fixes (RSC only authorizes the roster call
    below, it doesn't change what's inline on the entity). No-ops (returns
    `user` unchanged) only if both `aad_object_id` and `email` are already
    set — called for the sender too (2026-09-21: `activity.from_` carries
    `aad_object_id` but never `email`, so the sender needs this same
    lookup for `resolve_sender`'s email match to ever succeed).

    Args:
        ctx: The activity context for the inbound message, used for its
            `api.conversations.get_member_by_id` roster client and
            `activity.conversation.id`.
        user: The `MentionedUser` to backfill, as produced by
            `_account_to_mentioned_user`.

    Returns:
        A new `MentionedUser` with `aad_object_id`/`email` filled in from
        the roster response where the roster had them, `id`/`name`
        unchanged. Returns `user` unchanged (not a new instance) if no
        lookup was needed or the lookup failed.

    Side effects:
        One Bot Framework API call (`GET /v3/conversations/{id}/members/{id}`)
        per mention needing backfill. Failures (missing RSC/roster
        permission, member not in this conversation's roster, transient
        HTTP error) are logged and swallowed — a mention that can't be
        resolved this way falls through to the existing email-only
        assignee-resolution path in `pipeline.resolve`, same as before this
        change.
    """
    if user.aad_object_id is not None and user.email is not None:
        return user

    try:
        account = await ctx.api.conversations.get_member_by_id(
            ctx.activity.conversation.id, user.id
        )
    except Exception:
        logger.warning("roster lookup failed for mention id=%s", user.id, exc_info=True)
        return user

    return MentionedUser(
        id=user.id,
        aad_object_id=user.aad_object_id or getattr(account, "aad_object_id", None),
        name=user.name,
        email=user.email or getattr(account, "email", None),
    )


async def _extract_other_mentions(ctx: ActivityContext) -> list[MentionedUser]:
    """Pull every @mentioned user out of an activity except the bot itself.

    De-duplicated by id — a multi-word display name produces multiple raw
    mention entities for the same person (see inline note below). Each
    mention is then backfilled via a roster lookup (see
    `_backfill_from_roster`) since the raw entity doesn't reliably carry
    `aad_object_id`.
    """
    activity = ctx.activity
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

    mentioned_users = [
        await _backfill_from_roster(
            ctx, _account_to_mentioned_user(accounts[mentioned_id], name=" ".join(words))
        )
        for mentioned_id, words in name_words.items()
    ]

    # Debug-only: was the Track 2 empirical check (tasks-list.md) for
    # whether aad_object_id is populated on the mention entity itself —
    # settled 2026-09-18: it isn't, hence `_backfill_from_roster` above.
    # Kept to confirm the backfill is actually landing. Logs presence
    # only, never the id value itself.
    for user in mentioned_users:
        logger.info(
            "mention id=%s name=%r aad_object_id set=%s email set=%s",
            user.id,
            user.name,
            user.aad_object_id is not None,
            user.email is not None,
        )

    return mentioned_users


async def normalize(ctx: ActivityContext[MessageActivity]) -> NormalizedMessage:
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
        - `sender`: identity info available on the raw activity (no
          email/UPN — see `MentionedUser`).
        - `other_mentions`: same, plus a roster-lookup backfill for
          `aad_object_id`/`email` (see `_backfill_from_roster`) — the raw
          mention entity doesn't reliably carry `aad_object_id` the way
          `activity.from_` does for the sender.
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
    hashtags = [h.rstrip(_HASHTAG_TRAILING_PUNCTUATION) for h in HASHTAG_RE.findall(clean_text)]

    workflow_id = activity.conversation.id

    sender = await _backfill_from_roster(ctx, _account_to_mentioned_user(activity.from_))
    # Debug-only: same Track 2 empirical check as the mention log in
    # _extract_other_mentions, applied to the sender. Presence only.
    logger.info(
        "sender id=%s name=%r aad_object_id set=%s email set=%s",
        sender.id,
        sender.name,
        sender.aad_object_id is not None,
        sender.email is not None,
    )

    return NormalizedMessage(
        workflow_id=workflow_id,
        conversation_id=activity.conversation.id,
        text=clean_text,
        hashtags=hashtags,
        sender=sender,
        other_mentions=await _extract_other_mentions(ctx),
        is_reply=activity.reply_to_id is not None,
        reply_to_id=activity.reply_to_id,
        has_attachment=bool(activity.attachments),
    )
