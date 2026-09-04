"""NormalizedMessage -> ExtractionResult via LLM structured output. Task 4.
Prompt engineering + few-shot only — no fine-tuning planned for Phase A.
"""

from src.models.extraction import ExtractionResult
from src.models.normalized_message import NormalizedMessage


async def extract(message: NormalizedMessage) -> ExtractionResult:
    """Call the LLM to pull structured card fields out of a normalized
    Teams message's clean text and hashtags.

    NOT YET IMPLEMENTED (Task 4). Intended to use structured/strict-mode
    output (see ../../bot-docs/01-research-and-requirements.md §4 on
    99.8% structured-output compliance) — not free-text parsing.

    Args:
        message: The `NormalizedMessage` produced by
            `pipeline.normalize.normalize`. Only `message.text` and
            `message.hashtags` are meant to inform extraction; mentions
            are resolved separately in `pipeline.resolve`, not by the LLM.

    Returns:
        An `ExtractionResult` where every field carries a `confidence`
        (0-1) and `provenance` (`"explicit"` | `"inferred"` | `"unset"`).
        A field with `provenance="unset"` means the LLM found no signal
        for it in the message — callers must not treat that as "confirmed
        empty"; it still needs a confidence-gate/clarification decision.

    Raises:
        NotImplementedError: always, until Task 4 is built.
        (Once implemented: expected to raise on LLM/API failure after
        exhausting retries — no fine-tuning fallback is planned for
        Phase A, per ../../pm-questionnaire.md.)

    Side effects:
        Makes an outbound LLM API call (network, billable, latency-bound).
    """
    raise NotImplementedError
