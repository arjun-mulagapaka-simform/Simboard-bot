"""SimBoard-facing client. STUB for Phase A.

Real auth mechanism, endpoint schema, and bucket (board/list) mapping are
open questions (see ../../pm-questionnaire.md, Part A Q1/Q2 and Part B).
Every function here mirrors the eventual real client's signature so
workflow.py never needs to change when the real implementation lands.
"""

from src.models.card_draft import CardDraft

_FIXTURE_PROJECTS = [
    {"id": "proj-1", "name": "onboarding"},
    {"id": "proj-2", "name": "billing"},
    # Near-duplicate of proj-2, added to exercise the fuzzy-match ambiguity
    # path (2+ candidates >= settings.fuzzy_match_floor, none exact) — see
    # pipeline.resolve._match_with_ambiguity. A hashtag like "#billing-le"
    # scores ~0.82 against "billing" and ~0.83 against "billing-legacy",
    # so neither wins outright and both surface via
    # CardDraft.ambiguous_candidates instead of guessing one.
    {"id": "proj-3", "name": "billing-legacy"},
]

_FIXTURE_BOARDS = [
    {"id": "board-1", "project_id": "proj-1", "name": "sprint-42"},
    {"id": "board-2", "project_id": "proj-2", "name": "q3-launch"},
    # Same ambiguity purpose as proj-3 above, but for board resolution: a
    # hashtag like "#sprint-4" scores ~0.94 against both "sprint-42" and
    # "sprint-43" (same project, so board lookup isn't blocked by an
    # unresolved project).
    {"id": "board-3", "project_id": "proj-1", "name": "sprint-43"},
]

_FIXTURE_USERS = [
    {"id": "user-1", "name": "Prerak Dave"},
    {"id": "user-2", "name": "Arjun Mulagapaka"},
]


async def list_projects() -> list[dict]:
    """Fetch all SimBoard projects available for resolution/lookup.

    Used by `pipeline.resolve.resolve` as the candidate pool when matching
    a message's project hashtag hint to a real SimBoard `project`.

    STUB (Phase A): returns hardcoded `_FIXTURE_PROJECTS` — no network
    call, no auth, no pagination. The real implementation will need
    the not-yet-decided bot auth mechanism (see
    ../../pm-questionnaire.md Q1) and SimBoard's (currently undocumented)
    project-listing endpoint (see Part B).

    Args:
        None.

    Returns:
        A list of dicts, each shaped `{"id": str, "name": str}` — `id` is
        the SimBoard project id to use as `CardDraft.project_id`, `name`
        is the human-readable name to fuzzy-match hashtags against. Never
        returns `None`; an empty list means no projects exist/are visible
        (not yet a real possibility with fixture data).

    Raises:
        None currently (stub). The real client is expected to raise on
        auth failure or a non-2xx response — contract TBD once the
        endpoint is documented.

    Side effects:
        None currently (stub). The real client will make a network call.
    """
    return _FIXTURE_PROJECTS


async def list_boards(project_id: str | None = None) -> list[dict]:
    """Fetch SimBoard boards, optionally scoped to one project.

    Used by `pipeline.resolve.resolve` as the candidate pool when matching
    a message's board/"bucket" hashtag hint. Note: whether "bucket" in the
    original design actually means SimBoard `board` or `list` is still an
    open question (../../pm-questionnaire.md Q2) — this function currently
    assumes `board`.

    Args:
        project_id: If given, restrict results to boards belonging to that
            project's SimBoard id (as returned by `list_projects`). If
            `None` (default), return boards across all projects.

    Returns:
        A list of dicts shaped `{"id": str, "project_id": str, "name":
        str}`. Returns an empty list if `project_id` is given but matches
        no known project — this is not treated as an error.

    Raises:
        None currently (stub).

    Side effects:
        None currently (stub). The real client will make a network call.
    """
    if project_id is None:
        return _FIXTURE_BOARDS
    return [b for b in _FIXTURE_BOARDS if b["project_id"] == project_id]


async def list_users() -> list[dict]:
    """Fetch all SimBoard users, for display-name matching of @mentions.

    Matched against real SimBoard accounts. Used by `pipeline.resolve.resolve` in place of a Graph/AAD lookup —
    Phase A resolves assignees purely by matching Teams display names
    against this directory (see module docstring).

    Args:
        None.

    Returns:
        A list of dicts shaped `{"id": str, "name": str}` — `id` is the
        SimBoard user id to place in `CardDraft.assignee_user_ids`, `name`
        is matched fuzzily against `NormalizedMessage.other_mentions[].name`.
        A Teams user with no matching entry here should be treated as
        unresolved (added to `CardDraft.unresolved_fields`), not silently
        dropped.

    Raises:
        None currently (stub).

    Side effects:
        None currently (stub). The real client will make a network call.
    """
    return _FIXTURE_USERS


async def create_card(draft: CardDraft) -> dict:
    """Create a card in SimBoard from a fully-resolved draft.

    Call this only after `pipeline.confidence.needs_clarification(draft)`
    is False — i.e. `draft.unresolved_fields` is empty. Calling it with
    unresolved fields is a caller error; the stub does not validate this,
    but the real client should reject it once the card-creation schema is
    known (see ../../pm-questionnaire.md Part B).

    Args:
        draft: A `CardDraft` with `project_id`, `board_id`, `title`, and
            `assignee_user_ids` resolved to real SimBoard ids (not
            hashtag/mention text).

    Returns:
        A dict shaped `{"status": str, "card_id": str, "workflow_id":
        str}` on success. `card_id` is the newly created SimBoard card's
        id; `workflow_id` echoes `draft.workflow_id` so the caller can
        correlate the response back to the originating Teams message.

    Raises:
        None currently (stub — always "succeeds" with a fake id). The
        real client is expected to raise (or return an error shape TBD)
        on auth failure, validation errors (missing required card
        fields), or network failure. Idempotency/retry-safety on this
        call is an open question (see Part B: "whether the create-card
        endpoint is idempotent on retry").

    Side effects:
        None currently (stub — no real SimBoard write happens). The real
        client will perform a network call that creates a persistent
        card and may trigger SimBoard-side notifications/webhooks/activity
        log entries (see Part B).
    """
    return {"status": "created", "card_id": "fake-card-1", "workflow_id": draft.workflow_id}
