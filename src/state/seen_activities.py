"""In-memory idempotency set for inbound activity.id values (Step 1).

Bounded by a TTL, not conversation-scoped state, so a Teams retry of the
same delivery within the window is caught and any delivery older than the
window is forgotten. Phase A stand-in only (no Redis, single process) for
the production two-point-idempotency design in
../../bot-docs/08-scalability-and-reliability.md §6.
"""

import time

from src.config import settings


class SeenActivityStore:
    """Tracks recently-seen `activity.id`s with a sliding TTL.

    A duplicate Teams delivery of the same activity can be detected and
    dropped before it re-runs the pipeline.
    """

    def __init__(self, ttl_seconds: float | None = None) -> None:
        """Initialize an empty store.

        `ttl_seconds` defaults to `config.settings.event_dedup_ttl_seconds`.
        """
        self._ttl = ttl_seconds if ttl_seconds is not None else settings.event_dedup_ttl_seconds
        self._seen: dict[str, float] = {}

    def claim(self, activity_id: str) -> bool:
        """Atomically check-and-claim an activity id.

        Returns:
            True if this is the first time `activity_id` has been claimed
            within the TTL window (caller should proceed); False if it was
            already claimed and hasn't expired yet (caller should treat
            this as a duplicate delivery and drop it).

        Side effects:
            Records/refreshes `activity_id`'s claim time on a True result.
            Opportunistically evicts expired entries on every call, so the
            store doesn't grow unbounded across a long-running process.
        """
        now = time.monotonic()
        self._evict_expired(now)

        if activity_id in self._seen:
            return False

        self._seen[activity_id] = now
        return True

    def _evict_expired(self, now: float) -> None:
        """Drop entries older than the TTL. Called on every `claim()`."""
        expired = [aid for aid, claimed_at in self._seen.items() if now - claimed_at > self._ttl]
        for aid in expired:
            del self._seen[aid]


seen_activities = SeenActivityStore()
