"""Build a follow-up question for ambiguous/low-confidence fields. Task 5.
Copy tone per ../../bot-docs/06-agent-workflow.md §6.
"""

from src.models.card_draft import CardDraft


def build_clarification_prompt(draft: CardDraft) -> str:
    """Compose the follow-up question to send back to the user for a draft
    that has one or more unresolved fields.

    NOT YET IMPLEMENTED (Task 5).

    Args:
        draft: A `CardDraft` where `unresolved_fields` is non-empty (as
            determined by `pipeline.confidence.needs_clarification`).
            Passing a fully-resolved draft (empty `unresolved_fields`) is
            a caller error — behavior in that case is undefined until
            implemented.

    Returns:
        A single plain-text message asking the user to resolve the
        listed `unresolved_fields`, worded per the UX copy conventions in
        ../../bot-docs/06-agent-workflow.md §6 (e.g. naming the ambiguous
        value and, where applicable, listing the candidate options).

    Raises:
        NotImplementedError: always, until Task 5 is built.
    """
    raise NotImplementedError
