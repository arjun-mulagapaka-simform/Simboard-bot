"""In-memory per-workflow state store.

Bounded to the clarification/approval loop, NOT full chat history (see
../../bot-docs/06-agent-workflow.md). No DynamoDB in Phase A: infra is
just a place to put stuff.
"""

from src.models.card_draft import CardDraft


class WorkflowStore:
    """In-memory, per-workflow draft + clarification-turn state.

    Keyed by `workflow_id`. Not thread-safe across multiple worker
    processes — fine for Phase A's single-process bot, must be replaced
    before scaling out.
    """

    def __init__(self) -> None:
        """Initialize empty draft, turn-count, and in-progress maps."""
        self._drafts: dict[str, CardDraft] = {}
        self._turns: dict[str, int] = {}
        self._in_progress: set[str] = set()

    def try_start(self, workflow_id: str) -> bool:
        """Atomically check-and-mark a workflow as in progress.

        Guards against two concurrent messages in the same conversation
        (same `workflow_id`) both racing through `handle()`'s read ->
        await -> write span at once. Safe without a real lock because
        this check-and-set has no `await` in it, so it can't be
        interleaved with another task on the single-threaded event loop.

        Returns:
            True if `workflow_id` was not already in progress (caller
            should proceed, and must call `finish()` when done); False if
            it was already in progress (caller should tell the user to
            wait and drop this message).
        """
        if workflow_id in self._in_progress:
            return False
        self._in_progress.add(workflow_id)
        return True

    def finish(self, workflow_id: str) -> None:
        """Mark a workflow as no longer in progress.

        Must be called (typically in a `finally` block) after a
        successful `try_start()`, regardless of how `handle()` exits.
        Safe to call even if `workflow_id` isn't marked in progress.
        """
        self._in_progress.discard(workflow_id)

    def get(self, workflow_id: str) -> CardDraft | None:
        """Look up the current draft for a workflow, if one exists.

        Returns:
            The stored `CardDraft`, or `None` if no draft has been saved
            yet for this `workflow_id` (e.g. first turn, or an unknown id).
        """
        return self._drafts.get(workflow_id)

    def save(self, draft: CardDraft) -> None:
        """Store (or overwrite) the draft for `draft.workflow_id`.

        Always replaces any prior draft for the same workflow — callers
        must pass the full, up-to-date `CardDraft`, not a partial patch.
        """
        self._drafts[draft.workflow_id] = draft

    def clear(self, workflow_id: str) -> None:
        """Drop the stored draft and turn count for a workflow.

        Used to abandon a stuck clarification loop so the next mention
        for this `workflow_id` starts fresh. Safe to call even if no
        state exists for this id.
        """
        self._drafts.pop(workflow_id, None)
        self._turns.pop(workflow_id, None)

    def increment_turn(self, workflow_id: str) -> int:
        """Increment and return the clarification-turn counter for a workflow.

        Starts at 0 for an unseen `workflow_id`, so the first call returns
        1. Intended to be checked against
        `config.settings.max_clarification_turns` to cut off endless
        back-and-forth.
        """
        self._turns[workflow_id] = self._turns.get(workflow_id, 0) + 1
        return self._turns[workflow_id]


workflow_store = WorkflowStore()
