"""Step 1 — Validate Event: accept-or-drop gate before normalize() runs.

See ../../bot-docs/06-agent-workflow.md §Step 1. Runs synchronously, before
any LLM/network call, so a malformed or duplicate activity is rejected as
cheaply as possible.
"""

import logging
from enum import Enum, auto

from microsoft_teams.api import MessageActivity
from microsoft_teams.apps import ActivityContext

from src.state.seen_activities import seen_activities
from src.state.workflow_store import workflow_store

logger = logging.getLogger("validate_event")


class ValidationOutcome(Enum):
    """Result of validate().

    Tells bot.py whether to proceed, and how to treat a drop.
    """

    ACCEPT = auto()
    DUPLICATE = auto()  # activity.id already claimed — original delivery already replied
    NOT_MENTIONED = auto()  # bot wasn't @mentioned — not for us
    MALFORMED = auto()  # missing fields normalize() needs — dropped before it can raise


def validate(ctx: ActivityContext[MessageActivity]) -> ValidationOutcome:
    """Decide whether an inbound activity should proceed into the pipeline.

    Tenant allowlisting and JWT/Bearer validation happen upstream, inside
    the Teams SDK (see ../../HANDOFF.md "Auth/tenant note") — not this
    function's job. This covers the rest of the Step 1 rules from
    ../../bot-docs/06-agent-workflow.md: activity well-formedness,
    message-mentions-bot (waived for a reply to our own pending
    clarification/confirmation question — see `_is_clarification_reply`),
    and activity.id idempotency, checked in that order so a malformed
    activity is never fed to `is_recipient_mentioned()` or claimed in the
    dedup set.

    Args:
        ctx: The activity context for an inbound activity, as received by
            `bot.py`'s `on_message` handler — not yet known to be
            well-formed or relevant to this bot.

    Returns:
        `ValidationOutcome.ACCEPT` if the activity should proceed to
        `normalize()`. Any other member means the caller must drop the
        activity without invoking the rest of the pipeline and send no
        reply — `DUPLICATE` is logged at a lower severity than
        `MALFORMED` since it's an expected occurrence (a Teams retry), not
        a bug signal.

    Side effects:
        On ACCEPT, claims `activity.id` in the in-memory dedup set
        (`seen_activities`) so a retry of the same delivery is caught by
        the DUPLICATE branch. Nothing is claimed for any other outcome, so
        an unmentioned or malformed activity doesn't consume a dedup slot.
        Logs a line for every non-ACCEPT outcome.
    """
    activity = ctx.activity

    if not _is_well_formed(activity):
        logger.warning("Dropping malformed activity: missing required fields")
        return ValidationOutcome.MALFORMED

    if not activity.is_recipient_mentioned() and not _is_clarification_reply(activity):
        return ValidationOutcome.NOT_MENTIONED

    if not seen_activities.claim(activity.id):
        logger.info("Dropping duplicate delivery of activity %s", activity.id)
        return ValidationOutcome.DUPLICATE

    return ValidationOutcome.ACCEPT


def _is_clarification_reply(activity: MessageActivity) -> bool:
    """True if this activity is a threaded reply to our own pending question.

    A reply (`reply_to_id`) to our own last-sent clarification/confirmation
    question for this conversation. Teams' "Reply" action doesn't require re-mentioning the bot, so without
    this check a genuine "yes"/"no" answer would fail the mention check and
    be silently dropped — the reported bug this exists to fix. Mirrors
    `workflow.handle`'s own `is_clarification_reply` check (see
    ../workflow.py and ../../todo.md item 7a for why conversation id alone
    isn't enough); duplicated here rather than shared because this runs
    before `normalize()` produces a `workflow_id`, and for this bot
    `workflow_id` is `activity.conversation.id` anyway (see
    `pipeline.normalize.normalize`).
    """
    if activity.reply_to_id is None or activity.conversation is None:
        return False

    pending_draft = workflow_store.get(activity.conversation.id)
    return (
        pending_draft is not None and activity.reply_to_id == pending_draft.clarification_prompt_id
    )


def _is_well_formed(activity: MessageActivity) -> bool:
    """Check the fields `normalize()` relies on unconditionally.

    A malformed activity is dropped here instead of raising a bare
    `AttributeError` deeper in the pipeline (bot-docs 06 §Step 2 failure
    condition).
    """
    return bool(
        getattr(activity, "id", None)
        and getattr(activity, "type", None) == "message"
        and getattr(activity, "conversation", None) is not None
        and getattr(activity, "from_", None) is not None
    )
