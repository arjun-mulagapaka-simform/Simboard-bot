"""Unit tests for pipeline.clarify.build_clarification_prompt.

Purely template-based, no LLM call, no mocking needed (see module
docstring in src/pipeline/clarify.py for why the prior LLM-generated
candidate-question path was removed).
"""

import pytest

from src.models.card_draft import CardDraft
from src.pipeline.clarify import build_clarification_prompt


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
async def test_unresolved_project_asks_which_project():
    draft = _draft(unresolved_fields=["project_id"])
    prompt = await build_clarification_prompt(draft)
    assert "Which project should this go under?" in prompt


@pytest.mark.asyncio
async def test_unresolved_assignee_by_name_asks_who():
    draft = _draft(unresolved_fields=["assignee:Prerak Dave"])
    prompt = await build_clarification_prompt(draft)
    assert "Prerak Dave" in prompt


@pytest.mark.asyncio
async def test_pending_confirmation_assignee_asks_yes_no():
    draft = _draft(project_id="proj-1", pending_confirmation_fields=["assignee"])
    prompt = await build_clarification_prompt(draft)
    assert "(yes/no)" in prompt


@pytest.mark.asyncio
async def test_pending_confirmation_title_quotes_the_title():
    draft = _draft(pending_confirmation_fields=["title"])
    prompt = await build_clarification_prompt(draft)
    assert "Fix login bug" in prompt
    assert "(yes/no)" in prompt


@pytest.mark.asyncio
async def test_combines_unresolved_and_confirmation_lines():
    draft = _draft(unresolved_fields=["board_id"], pending_confirmation_fields=["assignee"])
    prompt = await build_clarification_prompt(draft)
    assert "Which board should this go on?" in prompt
    assert "(yes/no)" in prompt
