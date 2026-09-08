"""NormalizedMessage -> ExtractionResult via LLM structured output.

Task 4. Prompt engineering + few-shot only — no fine-tuning planned for
Phase A.
"""

import logging

from openai import OpenAIError

from src.config import settings
from src.core.exceptions import UpstreamServiceException
from src.integrations.azure_openai_client import call_with_retry, get_client
from src.models.extraction import ExtractionResult
from src.models.normalized_message import NormalizedMessage

logger = logging.getLogger("extract")

_SYSTEM_PROMPT = """You extract structured task-card fields from a Teams \
message. For every field, set "provenance" to "explicit" if the user stated \
it directly, "inferred" if you derived it from context, or "unset" if there \
is no signal for it in the message — never guess a value just to fill the \
field. Set "confidence" (0-1) to reflect how sure you are the value is \
correct; "unset" fields should carry a low confidence. `project_hint` and \
`board_hint` should come only from hashtags in the message, never invented, \
and must be copied verbatim (minus the leading "#") — never shortened, \
normalized, or paraphrased, since even small changes (e.g. "#sprint-4" -> \
"sprint") can break exact/fuzzy matching downstream. \
When there is exactly one hashtag and nothing else distinguishes a board \
from a project, treat it as `project_hint` — `project` is the required \
field and the far more common single-hashtag intent; only use `board_hint` \
for a hashtag that is clearly a sub-grouping of an already-identified \
project (e.g. a sprint/bucket name alongside a separate project hashtag or \
project name mentioned in plain text)."""


async def extract(message: NormalizedMessage) -> ExtractionResult:
    """Call the LLM to pull structured card fields out of a message.

    Uses the normalized Teams message's clean text and hashtags. Uses
    Azure OpenAI structured outputs (JSON-schema-constrained
    response) against the `ExtractionResult` schema, so the result is
    guaranteed to parse without manual JSON repair/retry logic.

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
        UpstreamServiceException: if the Azure OpenAI call fails (auth,
            rate limit, timeout, deployment not found) or the model
            refuses to produce a parseable `ExtractionResult`.

    Side effects:
        Makes an outbound LLM API call (network, billable, latency-bound).
    """
    user_content = f"Message: {message.text}\nHashtags: {', '.join(message.hashtags) or '(none)'}"

    try:
        completion = await call_with_retry(
            lambda: get_client().beta.chat.completions.parse(
                model=settings.azure_openai_deployment,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                response_format=ExtractionResult,
            )
        )
    except OpenAIError as exc:
        raise UpstreamServiceException(f"Azure OpenAI extraction call failed: {exc}") from exc

    parsed = completion.choices[0].message.parsed
    if parsed is None:
        refusal = completion.choices[0].message.refusal
        raise UpstreamServiceException(f"Azure OpenAI refused to extract: {refusal}")

    logger.info(
        "extraction result",
        extra={
            "workflow_id": message.workflow_id,
            "extraction": parsed.model_dump(),
        },
    )

    return parsed
