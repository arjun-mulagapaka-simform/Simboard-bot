"""Apply confidence-gate rules from ../../bot-docs/06-agent-workflow.md.

Task 6.
"""

from src.config import settings
from src.models.card_draft import CardDraft


def needs_clarification(draft: CardDraft) -> bool:
    """Decide whether a resolved card draft is ready to create.

    Or needs a follow-up question first. Moves any resolved-but-low-confidence field (per `draft.field_confidences`
    vs. `settings.confidence_floor`) from an already-resolved value into
    `draft.pending_confirmation_fields`, as a side effect, before deciding —
    so a low-confidence match is confirmed rather than silently accepted.
    `draft.pending_confirmation_fields` may also already carry `"assignee"`
    (set unconditionally in `pipeline.resolve.resolve` — a security control,
    not a confidence heuristic; see bot-docs/05 §6.4).

    Args:
        draft: The `CardDraft` produced by `pipeline.resolve.resolve` (or
            `resolve.apply_clarification`).

    Returns:
        True if `draft.unresolved_fields` or `draft.pending_confirmation_fields`
        is non-empty; False if the draft is fully resolved, confident, and
        confirmed, and safe to pass to `simboard_client.create_card`.

    Side effects:
        May append to `draft.pending_confirmation_fields` (see above).
    """
    for field, confidence in draft.field_confidences.items():
        if (
            confidence < settings.confidence_floor
            and field not in draft.pending_confirmation_fields
        ):
            draft.pending_confirmation_fields.append(field)

    return bool(draft.unresolved_fields) or bool(draft.pending_confirmation_fields)
