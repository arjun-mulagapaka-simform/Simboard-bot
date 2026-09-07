"""Azure OpenAI client construction + shared retry policy.

Single place that reads `settings.azure_openai_*` and builds the
`AsyncAzureOpenAI` client, so `pipeline/extract.py` (and anything else that
needs the LLM) never touches credentials or endpoint config directly. Also
the single place any Azure OpenAI call point (extraction now; the future
resolve.py LLM-reranker and clarify.py LLM-generation) should route
retry/backoff through, so that policy isn't duplicated per call site — see
bot-docs/06-agent-workflow.md Step 3: "LLM API error/timeout -> retry with
exponential backoff (3 attempts, capped at ~30s total)".

Async client + `asyncio.sleep` backoff deliberately, not the sync client:
this app runs on a single-threaded event loop (see `src/main.py`), so a
blocking network call or `time.sleep` here would freeze every other inbound
Teams message for the duration — see the incident this was built to avoid,
where one slow/retried extraction call starves every concurrent request.
"""

import asyncio
import logging
from functools import lru_cache
from typing import Awaitable, Callable, TypeVar

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncAzureOpenAI,
    InternalServerError,
    RateLimitError,
)

from src.config import settings

logger = logging.getLogger("azure_openai")

_T = TypeVar("_T")

# Only transient/retryable errors — auth, bad-request, not-found, etc. won't
# succeed on retry, so those propagate immediately instead.
_RETRYABLE_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 2  # attempt delays: 2s, 4s -> ~6s total, well under the 30s cap


@lru_cache
def get_client() -> AsyncAzureOpenAI:
    """Return a process-wide cached `AsyncAzureOpenAI` client.

    Call this instead of constructing `AsyncAzureOpenAI(...)` directly, so
    the client (and its underlying HTTP connection pool) is built once per
    process rather than per call.

    Args:
        None.

    Returns:
        An `AsyncAzureOpenAI` client configured from
        `settings.azure_openai_endpoint`, `settings.azure_openai_api_key`,
        and `settings.azure_openai_api_version`.

    Raises:
        openai.OpenAIError: if `settings.azure_openai_endpoint` or
            `settings.azure_openai_api_key` is empty/invalid — raised by the
            `AsyncAzureOpenAI` constructor itself, not validated here.
    """
    return AsyncAzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
    )


async def call_with_retry(fn: Callable[[], Awaitable[_T]]) -> _T:
    """Await an Azure OpenAI call, retrying transient failures with backoff.

    Call this instead of awaiting the client directly wherever a call point
    (extraction, and any future LLM-reranker/clarification-generation call)
    needs the shared retry policy, so it isn't reimplemented per call site.

    Args:
        fn: A zero-argument async callable that performs one Azure OpenAI
            request, e.g.
            `lambda: get_client().beta.chat.completions.parse(...)`.

    Returns:
        Whatever `await fn()` returns, from the first attempt that
        succeeds.

    Raises:
        openai.OpenAIError: the last error seen, if `fn()` fails on every
            attempt, or immediately (no retry) if the error isn't one of
            `_RETRYABLE_ERRORS` (e.g. auth/bad-request/not-found — retrying
            those can't succeed).

    Side effects:
        Awaits `asyncio.sleep` between attempts on a retryable failure
        (yields the event loop to other requests instead of blocking it);
        logs a warning per retried attempt.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return await fn()
        except _RETRYABLE_ERRORS as exc:
            if attempt == _MAX_ATTEMPTS:
                raise

            delay = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "Azure OpenAI call failed, retrying",
                extra={"attempt": attempt, "max_attempts": _MAX_ATTEMPTS, "delay_s": delay},
                exc_info=exc,
            )
            await asyncio.sleep(delay)

    raise AssertionError("unreachable")  # loop always returns or raises
