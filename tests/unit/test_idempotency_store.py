"""Unit tests for state.idempotency_store."""

from src.state.idempotency_store import IdempotencyStore


def test_get_card_id_returns_none_for_unknown_workflow():
    store = IdempotencyStore()
    assert store.get_card_id("wf-1") is None


def test_record_then_get_returns_the_card_id():
    store = IdempotencyStore()
    store.record("wf-1", "card-1")
    assert store.get_card_id("wf-1") == "card-1"


def test_record_is_scoped_per_workflow_id():
    store = IdempotencyStore()
    store.record("wf-1", "card-1")
    assert store.get_card_id("wf-2") is None


def test_record_overwrites_a_prior_entry_for_the_same_workflow():
    store = IdempotencyStore()
    store.record("wf-1", "card-1")
    store.record("wf-1", "card-2")
    assert store.get_card_id("wf-1") == "card-2"
