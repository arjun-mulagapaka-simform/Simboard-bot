"""In-memory per-workflow state store — bounded to the clarification/approval
loop, NOT full chat history (see ../../bot-docs/06-agent-workflow.md). No
Redis/Cosmos in Phase A: infra is just a place to put stuff.
"""

from src.models.card_draft import CardDraft


class WorkflowStore:
    """In-memory, per-workflow draft + clarification-turn state, keyed by
    `workflow_id`. Not thread-safe across multiple worker processes — fine
    for Phase A's single-process bot, must be replaced before scaling out.
    """

    def __init__(self) -> None:
        """Initialize empty draft and turn-count maps."""
        self._drafts: dict[str, CardDraft] = {}
        self._turns: dict[str, int] = {}

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

    def increment_turn(self, workflow_id: str) -> int:
        """Increment and return the clarification-turn counter for a
        workflow. Starts at 0 for an unseen `workflow_id`, so the first
        call returns 1. Intended to be checked against
        `config.settings.max_clarification_turns` to cut off endless
        back-and-forth.
        """
        self._turns[workflow_id] = self._turns.get(workflow_id, 0) + 1
        return self._turns[workflow_id]


workflow_store = WorkflowStore()
