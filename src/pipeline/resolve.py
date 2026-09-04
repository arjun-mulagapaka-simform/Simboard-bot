"""ExtractionResult -> resolved ids for project/board/assignees.

Layered resolution per ../../bot-docs/03-low-level-design.md:
  alias-table lookup -> fuzzy/embedding similarity (floor in config) ->
  LLM-as-reranker (only for 2+ surviving candidates).

Assignee resolution here matches by display name against
simboard_client.list_users() (a name/email directory), NOT via Graph —
graph_client is intentionally excluded from Phase A per user decision.
"""

from src.models.card_draft import CardDraft
from src.models.extraction import ExtractionResult
from src.models.normalized_message import NormalizedMessage


async def resolve(extraction: ExtractionResult, workflow_id: str) -> CardDraft:
    """Turn an LLM extraction result's free-text hints (project/board
    hashtags, assignee display names) into resolved SimBoard ids.

    NOT YET IMPLEMENTED (Task 1-partial / entity resolution).

    Resolution strategy per field (see module docstring): alias-table
    lookup first, then fuzzy/embedding similarity against
    `config.settings.fuzzy_match_floor`, then an LLM reranker only when
    2+ candidates survive fuzzy matching. Assignees are matched by display
    name against `simboard_client.list_users()` — no Graph/AAD lookup is
    performed in Phase A.

    Args:
        extraction: The `ExtractionResult` from `pipeline.extract.extract`.
        workflow_id: The originating message's workflow id (from
            `NormalizedMessage.workflow_id`), stamped onto the returned
            `CardDraft` so it can be saved in `workflow_store` and matched
            back up on a later clarification reply.

    Returns:
        A `CardDraft` with `project_id`/`board_id`/`assignee_user_ids`
        populated wherever resolution succeeded uniquely and confidently.
        Any field that had no match, multiple ambiguous matches, or fell
        below the confidence/similarity floor is instead listed by name in
        `unresolved_fields` (its resolved-id field is left `None`/empty),
        which `pipeline.confidence.needs_clarification` checks.

    Raises:
        NotImplementedError: always, until this resolution logic is built.

    Side effects:
        Calls `simboard_client.list_projects`/`list_boards`/`list_users`
        (stubbed with fixture data in Phase A) and, when ambiguity
        remains after fuzzy matching, an LLM reranker call.
    """
    raise NotImplementedError


async def apply_clarification(draft: CardDraft, reply: NormalizedMessage) -> CardDraft:
    """Re-attempt resolution of a pending draft's unresolved fields using a
    user's clarification reply, in the same conversation as the original
    request.

    NOT YET IMPLEMENTED (Task 5/1-partial). Called by `workflow.handle`
    instead of `resolve()` when `workflow_store.get(message.workflow_id)`
    already returns a draft with non-empty `unresolved_fields` — i.e. this
    inbound message is treated as an answer to a previously-asked
    clarification question, not a new card request.

    Args:
        draft: The previously saved `CardDraft` for this workflow, with
            one or more entries in `unresolved_fields` (e.g. `"board_id"`
            because the board hashtag matched two candidates).
        reply: The new `NormalizedMessage` — the user's free-text answer
            to the clarification question built by
            `pipeline.clarify.build_clarification_prompt`. Its `text` is
            expected to disambiguate exactly the fields named in
            `draft.unresolved_fields` (e.g. picking one of the listed
            candidate board names); nothing guarantees it does, since the
            user can reply with anything.

    Returns:
        An updated `CardDraft` for the same `workflow_id`. Fields the
        reply successfully disambiguated are moved out of
        `unresolved_fields` and have their resolved id populated; fields
        still unresolved (reply didn't address them, or was itself
        ambiguous) remain listed. A draft with a still-non-empty
        `unresolved_fields` will trigger another clarification round via
        `pipeline.confidence.needs_clarification`.

    Raises:
        NotImplementedError: always, until this is built.

    Side effects:
        May call `simboard_client.list_projects`/`list_boards`/
        `list_users` again and/or an LLM reranker call, same as `resolve`.
    """
    raise NotImplementedError
