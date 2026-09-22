"""Build a follow-up question for unresolved/low-confidence fields.

Task 5. Copy tone per ../../bot-docs/06-agent-workflow.md §6. Every
question here is a static template — no LLM call. (Prior to 2026-09-18
this also had an LLM-generated "did you mean X or Y?" question for a
genuinely ambiguous field, fed by `resolve.py`'s fuzzy-match layer for
project/board; that layer was removed after a fuzzy match produced a
false-positive project resolution live, which also removed the only
source of ambiguous candidates, so the LLM path here was removed too as
dead code — see `pipeline.resolve`'s module docstring.)
"""

import logging

from src.models.card_draft import CardDraft

logger = logging.getLogger("clarify")

_FIELD_QUESTIONS = {
    "project_id": "Which project should this go under?",
    "board_id": "Which board should this go on?",
    "title": "What should the title be?",
    "assignee": "Who should this be assigned to?",
}

_CONFIRMATION_PROMPTS = {
    "assignee": "I matched this to a SimBoard user for the assignee — is that correct? (yes/no)",
    "title": 'Title: "{value}" — is that correct? (yes/no)',
    "project_id": "I matched this to a project with lower confidence — is that correct? (yes/no)",
    "board_id": "I matched this to a board with lower confidence — is that correct? (yes/no)",
}


async def build_clarification_prompt(draft: CardDraft) -> str:
    """Compose the follow-up question to send back to the user.

    Used when a draft has one or more unresolved fields and/or fields
    pending yes/no confirmation. One line per field: a static template for
    an unresolved field (a name-only miss for an assignee gets a slightly
    more specific template), a static yes/no template for a
    pending-confirmation field (project/board/assignee confirmation
    questions are generic — the draft only carries resolved ids, not
    display names; see `pipeline.resolve.resolve`).

    Args:
        draft: A `CardDraft` where `unresolved_fields` and/or
            `pending_confirmation_fields` is non-empty (as determined by
            `pipeline.confidence.needs_clarification`). Passing a draft
            where both are empty is a caller error — behavior in that
            case is undefined until implemented.

    Returns:
        A single plain-text message asking the user to resolve the
        listed `unresolved_fields` and/or confirm the listed
        `pending_confirmation_fields`.
    """
    lines = ["I need a bit more info before creating this card:"]
    for field in draft.unresolved_fields:
        if field.startswith("assignee:"):
            name = field.split(":", 1)[1]
            lines.append(
                f'- I couldn\'t find a SimBoard user matching "{name}". Who should this be assigned to?'
            )
        else:
            lines.append(f"- {_FIELD_QUESTIONS.get(field, f'Please clarify: {field}')}")

    for field in draft.pending_confirmation_fields:
        prompt = _CONFIRMATION_PROMPTS.get(field, f"Please confirm: {field} (yes/no)")
        lines.append(f"- {prompt.format(value=draft.title)}")

    return "\n".join(lines)
