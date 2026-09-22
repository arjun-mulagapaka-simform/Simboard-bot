"""Resolved + validated card draft — ready for create_card() once approved."""

from pydantic import BaseModel, Field


class CardDraft(BaseModel):
    """Resolved + validated card draft — ready for create_card() once approved."""

    workflow_id: str
    original_text: str = ""
    """The originating request message's text (before any clarification
    replies), set once in `pipeline.resolve.resolve` and left untouched by
    `apply_clarification`. Gives a multi-field clarification reply
    background context it wouldn't otherwise have — see
    `pipeline.resolve._split_multi_field_reply`. Empty string only for a
    `CardDraft` built directly in a test without going through `resolve()`.
    """
    title: str
    description: str | None
    card_type: str
    project_id: str | None  # resolved, not the hashtag text
    board_id: str | None
    assignee_user_ids: list[str]
    unresolved_fields: list[str]  # fields with no resolved value — need an open clarifying question
    pending_confirmation_fields: list[str] = Field(default_factory=list)
    """Fields that DO have a resolved value but still need an explicit
    yes/no from the user before create_card() runs — distinct from
    `unresolved_fields` (no value at all). Two cases populate this (see
    ../pipeline/confidence.py):
    - "assignee" whenever any assignee resolved, unconditionally — a
      security control (bot-docs/05-permissions-and-security.md §6.4), not
      a confidence heuristic.
    - any of "title"/"project_id"/"board_id" whose extraction confidence
      fell below `settings.confidence_floor`, per `field_confidences`.
    """
    field_confidences: dict[str, float] = Field(default_factory=dict)
    """Extraction confidence (0-1) for whichever of "title"/"project_id"/
    "board_id" the LLM gave a confidence score for, keyed by the
    `CardDraft` field name (not the extraction hint name) so
    `pipeline.confidence` can look it up directly. Populated in
    `pipeline.resolve.resolve`, not touched by `apply_clarification`.
    """
    clarification_prompt_id: str | None = None
    """Activity id of the bot's own last-sent clarification question for
    this workflow (from `SentActivity.id`, see `workflow.handle`). A later
    inbound message is only treated as answering this draft's
    clarification if its `reply_to_id` matches this value — see
    ../pipeline/resolve.py's `apply_clarification` and
    ../../todo.md item 7a for why conversation id alone isn't enough.
    """
