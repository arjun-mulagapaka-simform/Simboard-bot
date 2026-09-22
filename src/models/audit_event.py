"""Audit trail record.

See bot-docs/03-low-level-design.md §4 (AuditEvent table) and
bot-docs/05-permissions-and-security.md §8 (audit logging requirements:
append-only, traces workflowId back to the Teams activityId,
PII-minimized).
"""

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class AuditEventType(str, Enum):
    """Pipeline transitions worth an audit record.

    Deliberately excludes "approved" — Phase A has no approval step (see
    workflow.py), only create/reject/error outcomes.
    """

    RECEIVED = "received"
    EXTRACTED = "extracted"
    RESOLVED = "resolved"
    GATED = "gated"
    REJECTED = "rejected"
    TICKET_CREATED = "ticket_created"
    ERROR = "error"


class AuditEvent(BaseModel):
    """One append-only audit record for a single pipeline transition.

    `payloadRedacted` must never contain raw message text (per
    bot-docs/05 §5's minimization rule) — only structured facts such as
    resolved ids, field names, or error types.
    """

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str
    workflow_id: str
    event_type: AuditEventType
    actor: str
    payload_redacted: dict = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
