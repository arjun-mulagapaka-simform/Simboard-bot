"""Apply confidence-gate rules from ../../bot-docs/06-agent-workflow.md.
Task 6.
"""

from src.config import settings
from src.models.card_draft import CardDraft


def needs_clarification(draft: CardDraft) -> bool:
    """Decide whether a resolved card draft is ready to create or needs a
    follow-up question first.

    Args:
        draft: The `CardDraft` produced by `pipeline.resolve.resolve`.

    Returns:
        True if `draft.unresolved_fields` is non-empty (at least one field
        fell below the confidence/resolution threshold and needs
        clarification or approval); False if the draft is fully resolved
        and safe to pass to `simboard_client.create_card`.
    """
    return bool(draft.unresolved_fields)
