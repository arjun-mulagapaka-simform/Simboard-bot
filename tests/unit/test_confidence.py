"""Unit tests for the confidence gate (pipeline.confidence.needs_clarification)."""

from src.config import settings
from src.models.card_draft import CardDraft
from src.pipeline.confidence import needs_clarification


def _draft(**overrides) -> CardDraft:
    base = dict(
        workflow_id="wf-1",
        title="Fix login bug",
        description=None,
        card_type="story",
        project_id="proj-1",
        board_id=None,
        assignee_user_ids=[],
        unresolved_fields=[],
    )
    base.update(overrides)
    return CardDraft(**base)


def test_fully_resolved_confident_draft_does_not_need_clarification():
    draft = _draft(field_confidences={"title": 0.95, "project_id": 0.9})
    assert needs_clarification(draft) is False


def test_unresolved_field_needs_clarification():
    draft = _draft(project_id=None, unresolved_fields=["project_id"])
    assert needs_clarification(draft) is True


def test_low_confidence_field_is_moved_into_pending_confirmation():
    low = settings.confidence_floor - 0.01
    draft = _draft(field_confidences={"project_id": low})

    assert needs_clarification(draft) is True
    assert "project_id" in draft.pending_confirmation_fields


def test_confidence_at_or_above_floor_does_not_trigger_confirmation():
    draft = _draft(field_confidences={"project_id": settings.confidence_floor})
    assert needs_clarification(draft) is False


def test_assignee_pending_confirmation_forces_clarification_regardless_of_confidence():
    draft = _draft(
        assignee_user_ids=["user-1"],
        pending_confirmation_fields=["assignee"],
        field_confidences={"title": 0.99, "project_id": 0.99},
    )
    assert needs_clarification(draft) is True


def test_low_confidence_field_is_not_appended_twice():
    low = settings.confidence_floor - 0.01
    draft = _draft(
        field_confidences={"project_id": low}, pending_confirmation_fields=["project_id"]
    )

    needs_clarification(draft)

    assert draft.pending_confirmation_fields.count("project_id") == 1
