"""Unit tests for state.audit_store."""

from src.models.audit_event import AuditEventType
from src.state.audit_store import AuditStore


def test_for_workflow_returns_empty_list_for_unknown_workflow():
    store = AuditStore()
    assert store.for_workflow("wf-1") == []


def test_record_returns_the_appended_event():
    store = AuditStore()
    event = store.record(
        correlation_id="corr-1",
        workflow_id="wf-1",
        event_type=AuditEventType.RECEIVED,
        actor="user-1",
    )
    assert event.correlation_id == "corr-1"
    assert event.workflow_id == "wf-1"
    assert event.event_type == AuditEventType.RECEIVED
    assert event.actor == "user-1"
    assert event.payload_redacted == {}


def test_record_defaults_payload_redacted_to_empty_dict():
    store = AuditStore()
    event = store.record(
        correlation_id="corr-1",
        workflow_id="wf-1",
        event_type=AuditEventType.RECEIVED,
        actor="user-1",
        payload_redacted=None,
    )
    assert event.payload_redacted == {}


def test_record_keeps_a_given_payload_redacted():
    store = AuditStore()
    event = store.record(
        correlation_id="corr-1",
        workflow_id="wf-1",
        event_type=AuditEventType.TICKET_CREATED,
        actor="user-1",
        payload_redacted={"card_id": "card-1"},
    )
    assert event.payload_redacted == {"card_id": "card-1"}


def test_for_workflow_returns_events_in_recorded_order():
    store = AuditStore()
    store.record(
        correlation_id="corr-1",
        workflow_id="wf-1",
        event_type=AuditEventType.RECEIVED,
        actor="user-1",
    )
    store.record(
        correlation_id="corr-1",
        workflow_id="wf-1",
        event_type=AuditEventType.EXTRACTED,
        actor="user-1",
    )
    events = store.for_workflow("wf-1")
    assert [e.event_type for e in events] == [
        AuditEventType.RECEIVED,
        AuditEventType.EXTRACTED,
    ]


def test_for_workflow_is_scoped_per_workflow_id():
    store = AuditStore()
    store.record(
        correlation_id="corr-1",
        workflow_id="wf-1",
        event_type=AuditEventType.RECEIVED,
        actor="user-1",
    )
    assert store.for_workflow("wf-2") == []


def test_for_workflow_returns_a_new_list_each_call():
    store = AuditStore()
    store.record(
        correlation_id="corr-1",
        workflow_id="wf-1",
        event_type=AuditEventType.RECEIVED,
        actor="user-1",
    )
    events = store.for_workflow("wf-1")
    events.append("bogus")
    assert len(store.for_workflow("wf-1")) == 1


def test_each_event_gets_a_unique_event_id():
    store = AuditStore()
    e1 = store.record(
        correlation_id="corr-1",
        workflow_id="wf-1",
        event_type=AuditEventType.RECEIVED,
        actor="user-1",
    )
    e2 = store.record(
        correlation_id="corr-1",
        workflow_id="wf-1",
        event_type=AuditEventType.EXTRACTED,
        actor="user-1",
    )
    assert e1.event_id != e2.event_id


def test_record_never_mutates_a_prior_event():
    store = AuditStore()
    store.record(
        correlation_id="corr-1",
        workflow_id="wf-1",
        event_type=AuditEventType.RECEIVED,
        actor="user-1",
        payload_redacted={"n": 1},
    )
    first_before = store.for_workflow("wf-1")[0]
    store.record(
        correlation_id="corr-2",
        workflow_id="wf-1",
        event_type=AuditEventType.EXTRACTED,
        actor="user-1",
        payload_redacted={"n": 2},
    )
    first_after = store.for_workflow("wf-1")[0]
    assert first_before.event_id == first_after.event_id
    assert first_before.payload_redacted == {"n": 1}
