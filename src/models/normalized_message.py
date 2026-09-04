"""Output of the ingestion step (Task 3): what we can reliably pull from a
single inbound Teams MessageActivity, per ../../message-schema.md's scope
conclusions. No Graph lookups performed here.
"""

from pydantic import BaseModel


class MentionedUser(BaseModel):
    id: str  # channel-scoped id
    aad_object_id: str | None
    name: str


class NormalizedMessage(BaseModel):
    workflow_id: str  # conversation.id — see pipeline/normalize.py docstring
    conversation_id: str
    text: str  # mentions already stripped
    hashtags: list[str]
    sender: MentionedUser
    other_mentions: list[MentionedUser]
    is_reply: bool
    reply_to_id: str | None  # activity.reply_to_id verbatim, for correlating
    # this message to a specific prior bot message (e.g. a clarification
    # question) — see CardDraft.clarification_prompt_id.
    has_attachment: bool
