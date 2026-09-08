"""Unit tests for the TTL-based idempotency store backing Step 1."""

from src.state.seen_activities import SeenActivityStore


def test_first_claim_succeeds():
    store = SeenActivityStore(ttl_seconds=300.0)
    assert store.claim("a") is True


def test_second_claim_of_same_id_fails():
    store = SeenActivityStore(ttl_seconds=300.0)
    store.claim("a")
    assert store.claim("a") is False


def test_expired_claim_can_be_reclaimed(monkeypatch):
    store = SeenActivityStore(ttl_seconds=10.0)
    times = iter([100.0, 200.0, 200.0])
    monkeypatch.setattr("src.state.seen_activities.time.monotonic", lambda: next(times))

    assert store.claim("a") is True  # claimed at t=100
    assert store.claim("a") is True  # t=200, 100s later than ttl=10s -> expired, reclaimed


def test_unrelated_ids_do_not_collide():
    store = SeenActivityStore(ttl_seconds=300.0)
    assert store.claim("a") is True
    assert store.claim("b") is True
