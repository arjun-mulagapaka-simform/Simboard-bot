"""Unit tests for pipeline.resolve.

Runs against mocked `simboard_client` calls (onboarding/billing projects,
sprint-42/q3-launch boards, Prerak Dave/Arjun Mulagapaka users) — the real
client now makes real HTTP calls, so this module supplies the same fixture
data `simboard_client` used to hardcode. No network calls needed for
project/board resolution (alias/exact match only, see
src/pipeline/resolve.py's module docstring) — the multi-field-split LLM
call is mocked via `_patch_split` only where exercised.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai import APIConnectionError

from src.integrations import azure_openai_client, simboard_client
from src.models.extraction import ExtractionResult, FieldValue
from src.models.normalized_message import MentionedUser, NormalizedMessage
from src.pipeline import resolve as resolve_module
from src.pipeline.resolve import (
    _match_by_email,
    _match_by_name,
    apply_clarification,
    resolve,
    resolve_sender,
)

_FIXTURE_PROJECTS = [
    {"id": "proj-1", "name": "onboarding"},
    {"id": "proj-2", "name": "billing"},
    {"id": "proj-3", "name": "billing-legacy"},
]

_FIXTURE_BOARDS = [
    {"id": "board-1", "project_id": "proj-1", "name": "sprint-42"},
    {"id": "board-2", "project_id": "proj-2", "name": "q3-launch"},
    {"id": "board-3", "project_id": "proj-1", "name": "sprint-43"},
]

_FIXTURE_USERS = [
    {"id": "user-1", "name": "Prerak Dave", "email": "prerak@simform.com"},
    {"id": "user-2", "name": "Arjun Mulagapaka", "email": "arjun@simform.com"},
]


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    monkeypatch.setattr(azure_openai_client.asyncio, "sleep", AsyncMock())


@pytest.fixture(autouse=True)
def _mock_simboard_client(monkeypatch):
    """Stand in for the real (now HTTP-backed) simboard_client calls.

    `list_users` ignores its `board_id` arg and always returns the full
    fixture roster — these tests aren't exercising board-membership
    scoping, just name resolution.
    """

    async def fake_list_projects():
        return _FIXTURE_PROJECTS

    async def fake_list_boards(project_id=None):
        if project_id is None:
            return _FIXTURE_BOARDS
        return [b for b in _FIXTURE_BOARDS if b["project_id"] == project_id]

    async def fake_list_users(board_id):
        return _FIXTURE_USERS

    monkeypatch.setattr(simboard_client, "list_projects", fake_list_projects)
    monkeypatch.setattr(simboard_client, "list_boards", fake_list_boards)
    monkeypatch.setattr(simboard_client, "list_users", fake_list_users)


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
    *,
    mentions: list[MentionedUser] | None = None,
    text: str = "answer",
    reply_to_id=None,
    sender_email: str | None = None,
) -> NormalizedMessage:
    return NormalizedMessage(
        workflow_id="wf-1",
        conversation_id="conv-1",
        text=text,
        hashtags=[],
        sender=MentionedUser(id="user-1", aad_object_id=None, name="Someone", email=sender_email),
        other_mentions=mentions or [],
        is_reply=reply_to_id is not None,
        reply_to_id=reply_to_id,
        has_attachment=False,
    )


@pytest.mark.asyncio
async def test_empty_title_falls_back_to_truncated_message_text():
    long_text = "a" * 60
    draft = await resolve(_extraction(title=None), _message(text=long_text))

    assert draft.title == f"{'a' * 50}…"
    assert "title" not in draft.unresolved_fields
    assert "title" not in draft.field_confidences


@pytest.mark.asyncio
async def test_empty_title_and_empty_message_falls_back_to_placeholder():
    draft = await resolve(_extraction(title=None), _message(text="   "))
    assert draft.title == "Untitled card"


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
    mention = MentionedUser(
        id="teams-1", aad_object_id=None, name="Prerak Dave", email="prerak@simform.com"
    )
    draft = await resolve(
        _extraction(project_hint="onboarding", board_hint="sprint-42"),
        _message(mentions=[mention]),
    )

    assert draft.assignee_user_ids == ["user-1"]
    assert "assignee" in draft.pending_confirmation_fields


@pytest.mark.asyncio
async def test_assignee_resolves_by_email_even_with_mismatched_name():
    mention = MentionedUser(
        id="teams-1", aad_object_id=None, name="Typo'd Name", email="prerak@simform.com"
    )
    draft = await resolve(
        _extraction(project_hint="onboarding", board_hint="sprint-42"),
        _message(mentions=[mention]),
    )

    assert draft.assignee_user_ids == ["user-1"]
    assert "assignee" in draft.pending_confirmation_fields


@pytest.mark.asyncio
async def test_assignee_email_match_is_case_insensitive():
    mention = MentionedUser(
        id="teams-1", aad_object_id=None, name="Prerak Dave", email="PRERAK@simform.com"
    )
    draft = await resolve(
        _extraction(project_hint="onboarding", board_hint="sprint-42"),
        _message(mentions=[mention]),
    )

    assert draft.assignee_user_ids == ["user-1"]


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
    mention = MentionedUser(
        id="teams-1", aad_object_id=None, name="Prerak Dave", email="prerak@simform.com"
    )
    draft = await resolve(
        _extraction(project_hint="onboarding", board_hint="sprint-42"),
        _message(mentions=[mention]),
    )

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


# --- project/board: alias + exact match only, no fuzzy -----------------
#
# 2026-09-18: fuzzy matching was removed for project/board resolution — a
# real live test showed a short/generic hint ("project-1") fuzzy-matching
# an unrelated real project above the 0.82 floor, with only one survivor,
# so it resolved silently with no confirmation. See
# src/pipeline/resolve.py's module docstring.


@pytest.mark.asyncio
async def test_typo_project_hint_no_longer_resolves():
    draft = await resolve(_extraction(project_hint="onboardin"), _message())
    assert draft.project_id is None
    assert "project_id" in draft.unresolved_fields


@pytest.mark.asyncio
async def test_typo_board_hint_no_longer_resolves():
    draft = await resolve(_extraction(project_hint="billing", board_hint="q3-launc"), _message())
    assert draft.board_id is None
    assert "board_id" in draft.unresolved_fields


@pytest.mark.asyncio
async def test_exact_project_hint_still_resolves():
    draft = await resolve(_extraction(project_hint="onboarding"), _message())
    assert draft.project_id == "proj-1"


@pytest.mark.asyncio
async def test_exact_board_hint_still_resolves():
    draft = await resolve(
        _extraction(project_hint="onboarding", board_hint="sprint-42"), _message()
    )
    assert draft.board_id == "board-1"


@pytest.mark.asyncio
async def test_assignee_name_only_no_longer_resolves_by_fuzzy_match():
    # 2026-09-16: fuzzy/name matching was dropped for assignee resolution
    # (unreliable in practice) — email is now the only signal. A mention
    # with no email (or an unmatched one) is left unresolved, even with an
    # exact or near-exact name match available.
    mention = MentionedUser(id="teams-1", aad_object_id=None, name="Prerak Dave")
    draft = await resolve(
        _extraction(project_hint="onboarding", board_hint="sprint-42"),
        _message(mentions=[mention]),
    )
    assert draft.assignee_user_ids == []
    assert "assignee:Prerak Dave" in draft.unresolved_fields


# --- _match_by_name (still used for an assignee named in a clarification
# reply — fuzzy matching wasn't removed there, see resolve.py's module
# docstring) -----------------------------------------------------------


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


def test_exact_match_takes_priority_over_fuzzy():
    # Both candidates would score high on fuzzy similarity to "sprint-4x",
    # but an exact match should never fall through to the fuzzy layer.
    candidates = [
        {"id": "a", "name": "sprint-4x"},
        {"id": "b", "name": "sprint-4y"},
    ]
    assert _match_by_name("sprint-4x", candidates, {}) == "a"


# --- _match_by_email ---------------------------------------------------


def test_match_by_email_none_when_mention_has_no_email():
    assert _match_by_email(None, _FIXTURE_USERS) is None


def test_match_by_email_finds_exact_match():
    assert _match_by_email("prerak@simform.com", _FIXTURE_USERS) == "user-1"


def test_match_by_email_is_case_insensitive():
    assert _match_by_email("PRERAK@SIMFORM.COM", _FIXTURE_USERS) == "user-1"


def test_match_by_email_none_when_no_candidate_matches():
    assert _match_by_email("nobody@simform.com", _FIXTURE_USERS) is None


def test_match_by_email_tolerates_candidates_missing_email():
    candidates = [{"id": "u1", "name": "No Email"}]
    assert _match_by_email("anyone@simform.com", candidates) is None


@pytest.mark.asyncio
async def test_resolve_sender_matches_by_email():
    message = _message(sender_email="prerak@simform.com")
    assert await resolve_sender(message, "board-1") == "user-1"


@pytest.mark.asyncio
async def test_resolve_sender_none_when_sender_has_no_email():
    message = _message(sender_email=None)
    assert await resolve_sender(message, "board-1") is None


@pytest.mark.asyncio
async def test_resolve_sender_none_when_email_matches_no_board_member():
    message = _message(sender_email="nobody@simform.com")
    assert await resolve_sender(message, "board-1") is None


# --- multi-field clarification replies ----------------------------------


def _fake_split_completion(pairs: list[tuple[str, str]]):
    parsed = SimpleNamespace(
        answers=[SimpleNamespace(field=field, answer=answer) for field, answer in pairs]
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))])


def _patch_split(monkeypatch, pairs: list[tuple[str, str]] | None = None):
    """Mock the multi-field splitter LLM call.

    `pairs=None` simulates a call failure; `pairs=[]` simulates an empty
    (non-answering) response.
    """
    if pairs is None:
        parse_mock = AsyncMock(
            side_effect=APIConnectionError(
                request=SimpleNamespace(method="POST", url="https://example.com")
            )
        )
    else:
        parse_mock = AsyncMock(return_value=_fake_split_completion(pairs))
    fake_client = SimpleNamespace(
        beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse_mock)))
    )
    monkeypatch.setattr(resolve_module, "get_client", lambda: fake_client)
    return parse_mock


@pytest.mark.asyncio
async def test_single_pending_item_never_calls_the_splitter(monkeypatch):
    parse_mock = _patch_split(monkeypatch, pairs=[])
    draft = await resolve(_extraction(project_hint="not-a-project"), _message())
    assert draft.unresolved_fields == ["project_id"]

    await apply_clarification(draft, _message(text="onboarding"))

    parse_mock.assert_not_called()


@pytest.mark.asyncio
async def test_multi_field_reply_resolves_two_pending_fields_at_once(monkeypatch):
    draft = await resolve(_extraction(project_hint="not-a-project"), _message())
    draft.unresolved_fields = ["project_id", "board_id"]

    _patch_split(
        monkeypatch,
        pairs=[
            ("project_id", "onboarding"),
            ("board_id", "sprint-42"),
        ],
    )

    updated = await apply_clarification(
        draft, _message(text="project is onboarding, board is sprint-42")
    )

    assert updated.project_id == "proj-1"
    assert updated.board_id == "board-1"
    assert updated.unresolved_fields == []


@pytest.mark.asyncio
async def test_multi_field_reply_leaves_unmatched_snippet_pending(monkeypatch):
    draft = await resolve(_extraction(project_hint="not-a-project"), _message())
    draft.unresolved_fields = ["project_id", "board_id"]

    _patch_split(
        monkeypatch,
        pairs=[("project_id", "not-a-real-project-name")],
    )

    updated = await apply_clarification(draft, _message(text="project is somewhere else"))

    assert updated.project_id is None
    assert "project_id" in updated.unresolved_fields
    assert "board_id" in updated.unresolved_fields


@pytest.mark.asyncio
async def test_multi_field_confirmation_plus_unresolved_in_one_reply(monkeypatch):
    draft = await resolve(_extraction(project_hint="not-a-project"), _message())
    draft.unresolved_fields = ["project_id"]
    draft.pending_confirmation_fields = ["title"]

    _patch_split(
        monkeypatch,
        pairs=[("title", "yes"), ("project_id", "onboarding")],
    )

    updated = await apply_clarification(draft, _message(text="yes, and project is onboarding"))

    assert updated.pending_confirmation_fields == []
    assert updated.project_id == "proj-1"
    assert updated.unresolved_fields == []


@pytest.mark.asyncio
async def test_multi_field_confirmation_no_reopens_field_alongside_other_answer(monkeypatch):
    draft = await resolve(
        _extraction(project_hint="not-a-project", title="Original title"), _message()
    )
    draft.unresolved_fields = ["project_id"]
    draft.pending_confirmation_fields = ["title"]

    _patch_split(
        monkeypatch,
        pairs=[("title", "no"), ("project_id", "onboarding")],
    )

    updated = await apply_clarification(draft, _message(text="no, but project is onboarding"))

    assert updated.title == ""
    assert "title" in updated.unresolved_fields
    assert updated.project_id == "proj-1"
    assert "project_id" not in updated.unresolved_fields


@pytest.mark.asyncio
async def test_splitter_failure_falls_back_to_single_field_whole_text(monkeypatch):
    mention = MentionedUser(id="teams-1", aad_object_id=None, name="Prerak Dave")
    draft = await resolve(
        _extraction(project_hint="not-a-project"),
        _message(mentions=[mention]),
    )
    assert set(draft.unresolved_fields) == {"project_id", "assignee:Prerak Dave"}

    _patch_split(monkeypatch, pairs=None)

    updated = await apply_clarification(draft, _message(text="onboarding"))

    # Fallback tries the whole reply against the first pending field only.
    assert updated.project_id == "proj-1"
    assert "assignee:Prerak Dave" in updated.unresolved_fields


@pytest.mark.asyncio
async def test_splitter_empty_response_falls_back_to_single_field_whole_text(monkeypatch):
    mention = MentionedUser(id="teams-1", aad_object_id=None, name="Prerak Dave")
    draft = await resolve(
        _extraction(project_hint="not-a-project"),
        _message(mentions=[mention]),
    )

    _patch_split(monkeypatch, pairs=[])

    updated = await apply_clarification(draft, _message(text="onboarding"))

    assert updated.project_id == "proj-1"
    assert "assignee:Prerak Dave" in updated.unresolved_fields


@pytest.mark.asyncio
async def test_splitter_drops_a_field_key_not_in_the_pending_list(monkeypatch):
    draft = await resolve(
        _extraction(project_hint="not-a-project", board_hint="sprint-42"), _message()
    )
    # Only project_id is actually pending (board never got attempted since
    # project didn't resolve) — but give the splitter 2+ pending fields to
    # take the multi-field path, then have it hallucinate an unrelated key.
    draft.unresolved_fields = ["project_id", "title"]

    _patch_split(
        monkeypatch,
        pairs=[("project_id", "onboarding"), ("board_id", "sprint-42")],
    )

    updated = await apply_clarification(draft, _message(text="onboarding, board sprint-42"))

    assert updated.project_id == "proj-1"
    assert updated.unresolved_fields == ["title"]
