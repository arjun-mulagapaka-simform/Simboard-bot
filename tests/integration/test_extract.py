"""Live Azure OpenAI check for pipeline.extract.

Real network calls — skipped automatically when no Azure OpenAI endpoint is
configured (e.g. in CI), so this only runs against a real deployment.
"""

import pytest

from src.config import settings
from src.models.normalized_message import MentionedUser, NormalizedMessage
from src.pipeline.extract import extract

pytestmark = pytest.mark.skipif(
    not settings.azure_openai_endpoint or not settings.azure_openai_api_key,
    reason="Azure OpenAI credentials not configured",
)


def _message(text: str, hashtags: list[str]) -> NormalizedMessage:
    """Build a minimal NormalizedMessage for a given text/hashtags pair."""
    sender = MentionedUser(id="user-1", aad_object_id=None, name="Arjun Mulagapaka")
    return NormalizedMessage(
        workflow_id="wf-test",
        conversation_id="conv-test",
        text=text,
        hashtags=hashtags,
        sender=sender,
        other_mentions=[],
        is_reply=False,
        reply_to_id=None,
        has_attachment=False,
    )


@pytest.mark.asyncio
async def test_extract_explicit_title_and_project_hint():
    """An explicit title and hashtag should extract with high confidence."""
    message = _message(
        text="Please create a card titled 'Fix login bug' for the onboarding flow",
        hashtags=["onboarding"],
    )

    result = await extract(message)

    assert result.title.value
    assert result.title.provenance == "explicit"
    assert result.title.confidence > 0.5
    assert result.project_hint.value == "onboarding"


@pytest.mark.asyncio
async def test_extract_unset_when_no_signal():
    """A vague message with no hashtags should leave hint fields unset."""
    message = _message(text="hey can someone look into this", hashtags=[])

    result = await extract(message)

    assert result.project_hint.provenance == "unset"
    assert result.board_hint.provenance == "unset"
