"""LLM structured-output schema (Task 4). No `priority` field — SimBoard's
`card` table has no priority column (see ../../pm-questionnaire.md, Answered).
"""

from typing import Literal

from pydantic import BaseModel


class FieldValue(BaseModel):
    value: str | None
    confidence: float  # 0-1
    provenance: Literal["explicit", "inferred", "unset"]


class ExtractionResult(BaseModel):
    title: FieldValue
    description: FieldValue
    card_type: FieldValue  # e.g. "project" / "story" — enum TBD from Part B
    project_hint: FieldValue  # from a hashtag
    board_hint: FieldValue  # from a hashtag ("bucket" mapping still open — Q2)
    assignee_hints: list[str]  # display names pulled from other_mentions
