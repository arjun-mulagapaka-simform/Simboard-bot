"""Unit tests for pipeline.clarify.build_clarification_prompt.

The candidate-question LLM path is mocked, same pattern as
tests/unit/test_extract.py — no network call, no credentials required.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.exceptions import UpstreamServiceException
from src.integrations import azure_openai_client
from src.models.card_draft import CardDraft
from src.pipeline import clarify
from src.pipeline.clarify import build_clarification_prompt


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    monkeypatch.setattr(azure_openai_client.asyncio, "sleep", AsyncMock())


def _fake_completion(content: str | None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _patch_llm_response(monkeypatch, content: str | None = "Did you mean sprint-42 or sprint-43?"):
    create_mock = AsyncMock(return_value=_fake_completion(content))
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_mock))
    )
    monkeypatch.setattr(clarify, "get_client", lambda: fake_client)
    return create_mock


def _draft(**overrides) -> CardDraft:
    base = dict(
        workflow_id="wf-1",
        title="Fix login bug",
        description=None,
        card_type="story",
        project_id=None,
        board_id=None,
        assignee_user_ids=[],
        unresolved_fields=[],
    )
    base.update(overrides)
    return CardDraft(**base)


@pytest.mark.asyncio
async def test_unresolved_project_asks_which_project(monkeypatch):
    _patch_llm_response(monkeypatch)
    draft = _draft(unresolved_fields=["project_id"])
    prompt = await build_clarification_prompt(draft)
    assert "Which project should this go under?" in prompt


@pytest.mark.asyncio
async def test_unresolved_assignee_by_name_asks_who(monkeypatch):
    _patch_llm_response(monkeypatch)
    draft = _draft(unresolved_fields=["assignee:Prerak Dave"])
    prompt = await build_clarification_prompt(draft)
    assert "Prerak Dave" in prompt


@pytest.mark.asyncio
async def test_pending_confirmation_assignee_asks_yes_no(monkeypatch):
    _patch_llm_response(monkeypatch)
    draft = _draft(project_id="proj-1", pending_confirmation_fields=["assignee"])
    prompt = await build_clarification_prompt(draft)
    assert "(yes/no)" in prompt


@pytest.mark.asyncio
async def test_pending_confirmation_title_quotes_the_title(monkeypatch):
    _patch_llm_response(monkeypatch)
    draft = _draft(pending_confirmation_fields=["title"])
    prompt = await build_clarification_prompt(draft)
    assert "Fix login bug" in prompt
    assert "(yes/no)" in prompt


@pytest.mark.asyncio
async def test_combines_unresolved_and_confirmation_lines(monkeypatch):
    _patch_llm_response(monkeypatch)
    draft = _draft(unresolved_fields=["board_id"], pending_confirmation_fields=["assignee"])
    prompt = await build_clarification_prompt(draft)
    assert "Which board should this go on?" in prompt
    assert "(yes/no)" in prompt


# --- ambiguous-candidate LLM path ---------------------------------------


@pytest.mark.asyncio
async def test_ambiguous_project_uses_llm_generated_question(monkeypatch):
    create_mock = _patch_llm_response(monkeypatch, "Did you mean sprint-42 or sprint-43?")
    draft = _draft(
        unresolved_fields=["project_id"],
        ambiguous_candidates={"project_id": ["sprint-42", "sprint-43"]},
    )

    prompt = await build_clarification_prompt(draft)

    assert "Did you mean sprint-42 or sprint-43?" in prompt
    assert "Which project should this go under?" not in prompt
    _, kwargs = create_mock.call_args
    user_message = kwargs["messages"][1]["content"]
    assert "sprint-42, sprint-43" in user_message


@pytest.mark.asyncio
async def test_ambiguous_assignee_uses_llm_generated_question(monkeypatch):
    _patch_llm_response(monkeypatch, "Did you mean Sweta Patel or Sweta Rao?")
    draft = _draft(
        unresolved_fields=["assignee:Sweta"],
        ambiguous_candidates={"assignee:Sweta": ["Sweta Patel", "Sweta Rao"]},
    )

    prompt = await build_clarification_prompt(draft)

    assert "Did you mean Sweta Patel or Sweta Rao?" in prompt
    assert "I couldn't find a SimBoard user" not in prompt


@pytest.mark.asyncio
async def test_flat_miss_does_not_call_llm(monkeypatch):
    create_mock = _patch_llm_response(monkeypatch)
    draft = _draft(unresolved_fields=["project_id"])

    await build_clarification_prompt(draft)

    create_mock.assert_not_called()


@pytest.mark.asyncio
async def test_empty_llm_response_raises(monkeypatch):
    _patch_llm_response(monkeypatch, content="")
    draft = _draft(
        unresolved_fields=["project_id"],
        ambiguous_candidates={"project_id": ["sprint-42", "sprint-43"]},
    )

    with pytest.raises(UpstreamServiceException, match="empty"):
        await build_clarification_prompt(draft)
