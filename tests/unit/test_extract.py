"""Unit tests for pipeline.extract — mocked Azure OpenAI client.

No network call and no credentials required. Complements the
live-credential test in tests/integration/test_extract.py, which is
network-gated and skipped without creds.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai import APIConnectionError

from src.core.exceptions import UpstreamServiceException
from src.integrations import azure_openai_client
from src.models.extraction import ExtractionResult, FieldValue
from src.models.normalized_message import MentionedUser, NormalizedMessage
from src.pipeline import extract


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """Skip the real sleep between retries so the retry/error tests run fast."""
    monkeypatch.setattr(azure_openai_client.asyncio, "sleep", AsyncMock())


def _message(
    *, text: str = "Fix the login bug #onboarding", hashtags: list[str] | None = None
) -> NormalizedMessage:
    return NormalizedMessage(
        workflow_id="wf-1",
        conversation_id="conv-1",
        text=text,
        hashtags=hashtags if hashtags is not None else ["onboarding"],
        sender=MentionedUser(id="user-1", aad_object_id=None, name="Someone"),
        other_mentions=[],
        is_reply=False,
        reply_to_id=None,
        has_attachment=False,
    )


def _extraction_result() -> ExtractionResult:
    return ExtractionResult(
        title=FieldValue(value="Fix the login bug", confidence=0.9, provenance="explicit"),
        description=FieldValue(value=None, confidence=0.0, provenance="unset"),
        card_type=FieldValue(value="story", confidence=0.8, provenance="inferred"),
        project_hint=FieldValue(value="onboarding", confidence=0.95, provenance="explicit"),
        board_hint=FieldValue(value=None, confidence=0.0, provenance="unset"),
    )


def _fake_completion(parsed: ExtractionResult | None = None, refusal: str | None = None):
    message = SimpleNamespace(parsed=parsed, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _patch_client(monkeypatch, parse_mock: AsyncMock) -> None:
    fake_client = SimpleNamespace(
        beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse_mock)))
    )
    monkeypatch.setattr(extract, "get_client", lambda: fake_client)


@pytest.mark.asyncio
async def test_extract_returns_parsed_result(monkeypatch):
    result = _extraction_result()
    parse_mock = AsyncMock(return_value=_fake_completion(parsed=result))
    _patch_client(monkeypatch, parse_mock)

    got = await extract.extract(_message())

    assert got == result


@pytest.mark.asyncio
async def test_extract_sends_message_text_and_hashtags(monkeypatch):
    parse_mock = AsyncMock(return_value=_fake_completion(parsed=_extraction_result()))
    _patch_client(monkeypatch, parse_mock)

    await extract.extract(
        _message(
            text="Fix the login bug #onboarding #sprint-42", hashtags=["onboarding", "sprint-42"]
        )
    )

    _, kwargs = parse_mock.call_args
    user_message = kwargs["messages"][1]["content"]
    assert "Fix the login bug #onboarding #sprint-42" in user_message
    assert "onboarding, sprint-42" in user_message


@pytest.mark.asyncio
async def test_extract_with_no_hashtags_sends_none_placeholder(monkeypatch):
    parse_mock = AsyncMock(return_value=_fake_completion(parsed=_extraction_result()))
    _patch_client(monkeypatch, parse_mock)

    await extract.extract(_message(hashtags=[]))

    _, kwargs = parse_mock.call_args
    user_message = kwargs["messages"][1]["content"]
    assert "Hashtags: (none)" in user_message


@pytest.mark.asyncio
async def test_extract_uses_extraction_result_response_format(monkeypatch):
    parse_mock = AsyncMock(return_value=_fake_completion(parsed=_extraction_result()))
    _patch_client(monkeypatch, parse_mock)

    await extract.extract(_message())

    _, kwargs = parse_mock.call_args
    assert kwargs["response_format"] is ExtractionResult


@pytest.mark.asyncio
async def test_extract_raises_on_refusal(monkeypatch):
    parse_mock = AsyncMock(
        return_value=_fake_completion(parsed=None, refusal="cannot help with that")
    )
    _patch_client(monkeypatch, parse_mock)

    with pytest.raises(UpstreamServiceException, match="refused"):
        await extract.extract(_message())


@pytest.mark.asyncio
async def test_extract_wraps_openai_error(monkeypatch):
    parse_mock = AsyncMock(
        side_effect=APIConnectionError(
            request=SimpleNamespace(method="POST", url="https://example.com")
        )
    )
    _patch_client(monkeypatch, parse_mock)

    with pytest.raises(UpstreamServiceException, match="Azure OpenAI extraction call failed"):
        await extract.extract(_message())
