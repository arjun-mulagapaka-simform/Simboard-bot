"""Unit tests for pipeline.resolve.

Runs against simboard_client's fixture data (onboarding/billing projects,
sprint-42/q3-launch boards, Prerak Dave/Arjun Mulagapaka users). No network
calls — the LLM re-rank path is mocked via `_patch_rerank`, defaulting to
"couldn't decide" so existing ambiguity tests keep their prior behavior.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai import APIConnectionError

from src.integrations import azure_openai_client
from src.models.extraction import ExtractionResult, FieldValue
from src.models.normalized_message import MentionedUser, NormalizedMessage
from src.pipeline import resolve as resolve_module
from src.pipeline.resolve import _match_by_name, apply_clarification, resolve


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    monkeypatch.setattr(azure_openai_client.asyncio, "sleep", AsyncMock())


def _fake_rerank_completion(chosen_name: str | None, confidence: float = 0.9, evidence: str = ""):
    parsed = SimpleNamespace(chosen_name=chosen_name, confidence=confidence, evidence=evidence)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))])


def _patch_rerank(
    monkeypatch, chosen_name: str | None = None, confidence: float = 0.9, evidence: str = ""
):
    """Mock the LLM re-rank call. Defaults to "couldn't decide" (chosen_name=None).

    `evidence` defaults to empty — pass a non-empty phrase alongside a
    `chosen_name` to simulate a grounded pick (see `resolve._llm_rerank_candidate`,
    which now discards any `chosen_name` accompanied by empty evidence).
    """
    parse_mock = AsyncMock(return_value=_fake_rerank_completion(chosen_name, confidence, evidence))
    fake_client = SimpleNamespace(
        beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse_mock)))
    )
    monkeypatch.setattr(resolve_module, "get_client", lambda: fake_client)
    return parse_mock


@pytest.fixture(autouse=True)
def _default_rerank(monkeypatch):
    _patch_rerank(monkeypatch, chosen_name=None)


def _extraction(
    *,
    title="Fix login bug",
    title_conf=0.9,
    project_hint=None,
    project_conf=0.9,
    board_hint=None,
    board_conf=0.9,
) -> ExtractionResult:
    return ExtractionResult(
        title=FieldValue(value=title, confidence=title_conf, provenance="explicit"),
        description=FieldValue(value=None, confidence=0.0, provenance="unset"),
        card_type=FieldValue(value="story", confidence=0.9, provenance="explicit"),
        project_hint=FieldValue(
            value=project_hint,
            confidence=project_conf,
            provenance="explicit" if project_hint else "unset",
        ),
        board_hint=FieldValue(
            value=board_hint,
            confidence=board_conf,
            provenance="explicit" if board_hint else "unset",
        ),
    )


def _message(
    *, mentions: list[MentionedUser] | None = None, text: str = "answer", reply_to_id=None
) -> NormalizedMessage:
    return NormalizedMessage(
        workflow_id="wf-1",
        conversation_id="conv-1",
        text=text,
        hashtags=[],
        sender=MentionedUser(id="user-1", aad_object_id=None, name="Someone"),
        other_mentions=mentions or [],
        is_reply=reply_to_id is not None,
        reply_to_id=reply_to_id,
        has_attachment=False,
    )


@pytest.mark.asyncio
async def test_resolved_project_carries_its_confidence():
    draft = await resolve(_extraction(project_hint="onboarding", project_conf=0.42), _message())
    assert draft.project_id == "proj-1"
    assert draft.field_confidences["project_id"] == 0.42


@pytest.mark.asyncio
async def test_unresolved_project_has_no_confidence_entry():
    draft = await resolve(_extraction(project_hint="not-a-project"), _message())
    assert draft.project_id is None
    assert "project_id" in draft.unresolved_fields
    assert "project_id" not in draft.field_confidences


@pytest.mark.asyncio
async def test_resolved_assignee_always_flagged_for_confirmation():
    mention = MentionedUser(id="teams-1", aad_object_id=None, name="Prerak Dave")
    draft = await resolve(_extraction(project_hint="onboarding"), _message(mentions=[mention]))

    assert draft.assignee_user_ids == ["user-1"]
    assert "assignee" in draft.pending_confirmation_fields


@pytest.mark.asyncio
async def test_no_assignee_mentions_means_no_pending_confirmation():
    draft = await resolve(_extraction(project_hint="onboarding"), _message())
    assert draft.pending_confirmation_fields == []


@pytest.mark.asyncio
async def test_apply_clarification_yes_confirms_and_clears_confidence_entry():
    draft = await resolve(_extraction(project_hint="onboarding", project_conf=0.3), _message())
    draft.pending_confirmation_fields = ["project_id"]

    updated = await apply_clarification(draft, _message(text="yes"))

    assert "project_id" not in updated.pending_confirmation_fields
    assert "project_id" not in updated.field_confidences


@pytest.mark.asyncio
async def test_apply_clarification_no_clears_value_and_reasks_openly():
    draft = await resolve(_extraction(project_hint="onboarding", project_conf=0.3), _message())
    draft.pending_confirmation_fields = ["project_id"]

    updated = await apply_clarification(draft, _message(text="no"))

    assert updated.project_id is None
    assert "project_id" in updated.unresolved_fields
    assert "project_id" not in updated.pending_confirmation_fields


@pytest.mark.asyncio
async def test_apply_clarification_no_for_assignee_clears_assignee_list():
    mention = MentionedUser(id="teams-1", aad_object_id=None, name="Prerak Dave")
    draft = await resolve(_extraction(project_hint="onboarding"), _message(mentions=[mention]))

    updated = await apply_clarification(draft, _message(text="no"))

    assert updated.assignee_user_ids == []
    assert "assignee" in updated.unresolved_fields
    assert "assignee" not in updated.pending_confirmation_fields


@pytest.mark.asyncio
async def test_apply_clarification_unrecognized_answer_leaves_confirmation_pending():
    draft = await resolve(_extraction(project_hint="onboarding", project_conf=0.3), _message())
    draft.pending_confirmation_fields = ["project_id"]

    updated = await apply_clarification(draft, _message(text="maybe"))

    assert "project_id" in updated.pending_confirmation_fields


# --- fuzzy-match paths -------------------------------------------------


@pytest.mark.asyncio
async def test_typo_project_hint_resolves_via_fuzzy_match():
    draft = await resolve(_extraction(project_hint="onboardin"), _message())
    assert draft.project_id == "proj-1"


@pytest.mark.asyncio
async def test_typo_board_hint_resolves_via_fuzzy_match():
    # billing has only one board (q3-launch), so this hint resolves
    # unambiguously — "sprint-4" is used by the ambiguity test below since
    # onboarding now has two similarly-named boards (sprint-42/sprint-43).
    draft = await resolve(_extraction(project_hint="billing", board_hint="q3-launc"), _message())
    assert draft.board_id == "board-2"


@pytest.mark.asyncio
async def test_ambiguous_board_hint_stays_unresolved_with_candidates():
    draft = await resolve(_extraction(project_hint="onboarding", board_hint="sprint-4"), _message())
    assert draft.board_id is None
    assert "board_id" in draft.unresolved_fields
    assert set(draft.ambiguous_candidates["board_id"]) == {"sprint-42", "sprint-43"}


@pytest.mark.asyncio
async def test_typo_assignee_name_resolves_via_fuzzy_match():
    mention = MentionedUser(id="teams-1", aad_object_id=None, name="Prerak Dav")
    draft = await resolve(_extraction(project_hint="onboarding"), _message(mentions=[mention]))
    assert draft.assignee_user_ids == ["user-1"]


@pytest.mark.asyncio
async def test_below_floor_hint_stays_unresolved():
    # "onboarding project" vs "onboarding" scores ~0.71, below the 0.82 floor.
    draft = await resolve(_extraction(project_hint="onboarding project"), _message())
    assert draft.project_id is None
    assert "project_id" in draft.unresolved_fields


def test_two_candidates_above_floor_are_left_ambiguous():
    candidates = [
        {"id": "a", "name": "sprint-42"},
        {"id": "b", "name": "sprint-43"},
    ]
    assert _match_by_name("sprint-4", candidates, {}) is None


def test_single_candidate_above_floor_resolves():
    candidates = [
        {"id": "a", "name": "sprint-42"},
        {"id": "b", "name": "q3-launch"},
    ]
    assert _match_by_name("sprint-4", candidates, {}) == "a"


@pytest.mark.asyncio
async def test_resolve_populates_ambiguous_candidates_for_project(monkeypatch):
    from src.integrations import simboard_client

    async def fake_list_projects():
        return [{"id": "p1", "name": "sprint-42"}, {"id": "p2", "name": "sprint-43"}]

    monkeypatch.setattr(simboard_client, "list_projects", fake_list_projects)

    draft = await resolve(_extraction(project_hint="sprint-4"), _message())

    assert draft.project_id is None
    assert "project_id" in draft.unresolved_fields
    assert set(draft.ambiguous_candidates["project_id"]) == {"sprint-42", "sprint-43"}


def test_exact_match_takes_priority_over_fuzzy():
    # Both candidates would score high on fuzzy similarity to "sprint-4x",
    # but an exact match should never fall through to the fuzzy layer.
    candidates = [
        {"id": "a", "name": "sprint-4x"},
        {"id": "b", "name": "sprint-4y"},
    ]
    assert _match_by_name("sprint-4x", candidates, {}) == "a"


# --- LLM re-rank path ----------------------------------------------------


@pytest.mark.asyncio
async def test_llm_rerank_resolves_ambiguous_board(monkeypatch):
    _patch_rerank(monkeypatch, chosen_name="sprint-43", confidence=0.9, evidence="sprint-43")

    draft = await resolve(
        _extraction(project_hint="onboarding", board_hint="sprint-4"),
        _message(text="fix the bug on sprint-43 #onboarding"),
    )

    assert draft.board_id == "board-3"
    assert "board_id" not in draft.unresolved_fields
    assert "board_id" not in draft.ambiguous_candidates


@pytest.mark.asyncio
async def test_llm_rerank_low_confidence_stays_ambiguous(monkeypatch):
    _patch_rerank(monkeypatch, chosen_name="sprint-43", confidence=0.4, evidence="sprint-43")

    draft = await resolve(_extraction(project_hint="onboarding", board_hint="sprint-4"), _message())

    assert draft.board_id is None
    assert "board_id" in draft.unresolved_fields
    assert set(draft.ambiguous_candidates["board_id"]) == {"sprint-42", "sprint-43"}


@pytest.mark.asyncio
async def test_llm_rerank_unknown_name_stays_ambiguous(monkeypatch):
    # LLM names something that isn't one of the actual candidates — treated
    # as a non-answer rather than trusted blindly.
    _patch_rerank(monkeypatch, chosen_name="sprint-99", confidence=0.95, evidence="sprint-99")

    draft = await resolve(_extraction(project_hint="onboarding", board_hint="sprint-4"), _message())

    assert draft.board_id is None
    assert "board_id" in draft.unresolved_fields


@pytest.mark.asyncio
async def test_llm_rerank_evidence_that_is_just_the_hint_stays_ambiguous(monkeypatch):
    # Regression test: the LLM cited the ambiguous hint itself ("billing-le")
    # as "evidence" for picking "billing-legacy" — it matched every
    # candidate precisely because it's the ambiguous hint, so restating it
    # proves nothing and must not be trusted even at high confidence.
    _patch_rerank(monkeypatch, chosen_name="sprint-43", confidence=0.9, evidence="sprint-4")

    draft = await resolve(_extraction(project_hint="onboarding", board_hint="sprint-4"), _message())

    assert draft.board_id is None
    assert set(draft.ambiguous_candidates["board_id"]) == {"sprint-42", "sprint-43"}


@pytest.mark.asyncio
async def test_llm_rerank_empty_evidence_stays_ambiguous_even_with_high_confidence(monkeypatch):
    # Regression test: a live Teams test showed the LLM naming a candidate
    # with high confidence but no actual textual basis (e.g. defaulting to
    # a "more common"-seeming project) — evidence must be non-empty for the
    # pick to be trusted at all, confidence alone isn't enough.
    _patch_rerank(monkeypatch, chosen_name="sprint-43", confidence=0.95, evidence="")

    draft = await resolve(_extraction(project_hint="onboarding", board_hint="sprint-4"), _message())

    assert draft.board_id is None
    assert set(draft.ambiguous_candidates["board_id"]) == {"sprint-42", "sprint-43"}


@pytest.mark.asyncio
async def test_llm_rerank_failure_falls_back_to_ambiguous(monkeypatch):
    parse_mock = AsyncMock(
        side_effect=APIConnectionError(
            request=SimpleNamespace(method="POST", url="https://example.com")
        )
    )
    fake_client = SimpleNamespace(
        beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse_mock)))
    )
    monkeypatch.setattr(resolve_module, "get_client", lambda: fake_client)

    draft = await resolve(_extraction(project_hint="onboarding", board_hint="sprint-4"), _message())

    assert draft.board_id is None
    assert set(draft.ambiguous_candidates["board_id"]) == {"sprint-42", "sprint-43"}


@pytest.mark.asyncio
async def test_llm_rerank_not_called_for_unambiguous_match(monkeypatch):
    parse_mock = _patch_rerank(monkeypatch, chosen_name=None)

    await resolve(_extraction(project_hint="onboarding"), _message())

    parse_mock.assert_not_called()
