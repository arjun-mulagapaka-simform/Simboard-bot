"""In-memory, append-only audit trail.

See bot-docs/03-low-level-design.md §4 (AuditEvent table) and
bot-docs/05-permissions-and-security.md §8.

Same Phase A caveat as `workflow_store.py`/`idempotency_store.py`:
in-memory, single-process, lost on restart, not shared across worker
instances — must be replaced (DynamoDB, per bot-docs/09) before scaling
out or before this is relied on for real compliance/incident review.
"""

from src.models.audit_event import AuditEvent, AuditEventType


class AuditStore:
    """Append-only list of `AuditEvent`s, queryable by `workflow_id`."""

    def __init__(self) -> None:
        """Initialize an empty audit trail."""
        self._events: list[AuditEvent] = []

    def record(
        self,
        *,
        correlation_id: str,
        workflow_id: str,
        event_type: AuditEventType,
        actor: str,
        payload_redacted: dict | None = None,
    ) -> AuditEvent:
        """Append one audit event to the trail.

        Args:
            correlation_id: Ties this event to the inbound Teams
                `activity.id` that triggered it (see `workflow.py`).
            workflow_id: The pipeline's `workflow_id` (conversation id —
                see `normalize.py`), so all events for one card-creation
                attempt can be pulled together.
            event_type: Which pipeline transition this is — see
                `AuditEventType`.
            actor: The Teams sender's account id (`message.sender.id`),
                not a display name or email — those aren't reliably
                available (see tasks-list.md's Track 2 entry).
            payload_redacted: Structured facts only (resolved ids, field
                names, error types) — never raw message text. Defaults to
                `{}`.

        Returns:
            The `AuditEvent` that was appended.

        Side effects:
            Appends to the in-memory list. Never mutates or removes a
            prior event — append-only.
        """
        event = AuditEvent(
            correlation_id=correlation_id,
            workflow_id=workflow_id,
            event_type=event_type,
            actor=actor,
            payload_redacted=payload_redacted or {},
        )
        self._events.append(event)
        return event

    def for_workflow(self, workflow_id: str) -> list[AuditEvent]:
        """Return all recorded events for `workflow_id`, oldest first.

        Returns:
            A new list (safe to mutate) — empty if no events have been
            recorded for this `workflow_id`.
        """
        return [e for e in self._events if e.workflow_id == workflow_id]


audit_store = AuditStore()
