"""Resolved + validated card draft — ready for create_card() once approved."""

from pydantic import BaseModel


class CardDraft(BaseModel):
    workflow_id: str
    title: str
    description: str | None
    card_type: str
    project_id: str | None  # resolved, not the hashtag text
    board_id: str | None
    assignee_user_ids: list[str]
    unresolved_fields: list[str]  # fields still needing clarification/approval
    clarification_prompt_id: str | None = None
    """Activity id of the bot's own last-sent clarification question for
    this workflow (from `SentActivity.id`, see `workflow.handle`). A later
    inbound message is only treated as answering this draft's
    clarification if its `reply_to_id` matches this value — see
    ../pipeline/resolve.py's `apply_clarification` and
    ../../todo.md item 7a for why conversation id alone isn't enough.
    """
