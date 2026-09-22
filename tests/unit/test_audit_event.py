"""Unit tests for models.audit_event."""

from datetime import datetime

from src.models.audit_event import AuditEvent, AuditEventType


def _event(**overrides):
    defaults = dict(
        correlation_id="corr-1",
        workflow_id="wf-1",
        event_type=AuditEventType.RECEIVED,
        actor="user-1",
    )
    defaults.update(overrides)
    return AuditEvent(**defaults)


def test_event_id_is_auto_generated_and_unique():
    e1 = _event()
    e2 = _event()
    assert e1.event_id != e2.event_id


def test_timestamp_is_auto_generated_as_a_datetime():
    e = _event()
    assert isinstance(e.timestamp, datetime)


def test_payload_redacted_defaults_to_empty_dict():
    e = _event()
    assert e.payload_redacted == {}


def test_approved_is_not_a_valid_event_type():
    assert not hasattr(AuditEventType, "APPROVED")


def test_all_expected_event_types_exist():
    assert {t.value for t in AuditEventType} == {
        "received",
        "extracted",
        "resolved",
        "gated",
        "rejected",
        "ticket_created",
        "error",
    }
