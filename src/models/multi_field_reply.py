"""Schema for the LLM multi-field clarification-reply splitter.

Used by pipeline.resolve._split_multi_field_reply.
"""

from pydantic import BaseModel, Field


class FieldAnswer(BaseModel):
    """One pending field the reply clearly answers, plus the snippet that answers it."""

    field: str = Field(
        description="One of the given pending field keys, copied verbatim — never "
        "invented, reworded, or abbreviated."
    )
    answer: str = Field(
        description="The verbatim snippet of the user's reply that answers this "
        "field (not the original request, not a paraphrase)."
    )


class MultiFieldSplitResult(BaseModel):
    """The reply split into per-field answers.

    Omits any pending field the reply doesn't clearly address — never
    guesses one in to force full coverage.
    """

    answers: list[FieldAnswer] = Field(default_factory=list)
