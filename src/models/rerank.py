"""Schema for the LLM re-rank call in pipeline.resolve (bot-docs §06 Step 4d)."""

from pydantic import BaseModel, Field


class RerankResult(BaseModel):
    """LLM's pick among 2+ fuzzy-matched candidates, or none if genuinely unclear."""

    evidence: str = Field(
        description=(
            "The exact word or phrase from the message that points to one candidate "
            'over the others (e.g. "legacy billing system", "sprint 43"). Empty '
            "string if the message contains no such phrase — do not paraphrase or "
            "infer one that isn't literally there."
        )
    )
    chosen_name: str | None = Field(
        description=(
            "The exact name of the candidate the user most likely meant, copied "
            "verbatim from the candidate list, or null if `evidence` is empty or no "
            "single candidate is clearly the intended one."
        )
    )
    confidence: float = Field(
        ge=0,
        le=1,
        description="How confident this pick is, 0-1. Low if the message gives little to go on.",
    )
