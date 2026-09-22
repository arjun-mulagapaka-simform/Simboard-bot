"""In-memory `workflow_id -> card_id` idempotency store.

SimBoard's `POST /api/lists/:listId/cards` has no `Idempotency-Key`
support (confirmed from `helpers/cards/create-one.js` — no dedup lookup
before insert), so a retried create request after a transient failure
(e.g. `workflow.py` re-running for the same `workflow_id`, or a future
retry policy around `simboard_client.create_card`) would otherwise create
a second card. This store lets a caller check "did we already create a
card for this workflow?" before calling `create_card` again.

Only covers the case where the first call's response was actually
received (success recorded here before returning to the caller) — it does
NOT cover a request that times out with no response at all, since there's
nothing to record in that case. That gap needs a second mechanism (embed
`workflow_id` as a marker in the card's `description` at creation time,
scan the target list's existing cards for it on retry) — not built yet,
tracked as a separate follow-up in ../../tasks-list.md.

Same Phase A caveat as `workflow_store.py`: in-memory, single-process,
not shared across worker instances — must be replaced (Redis/Cosmos, per
bot-docs/09) before scaling out.
"""


class IdempotencyStore:
    """Maps `workflow_id` to the SimBoard `card_id` already created for it."""

    def __init__(self) -> None:
        """Initialize an empty workflow_id -> card_id map."""
        self._card_ids: dict[str, str] = {}

    def get_card_id(self, workflow_id: str) -> str | None:
        """Look up the card already created for a workflow, if any.

        Call this before `simboard_client.create_card` so a retried/
        re-run workflow (same `workflow_id`) doesn't create a duplicate
        card.

        Args:
            workflow_id: The workflow to check.

        Returns:
            The previously recorded SimBoard card id, or `None` if no
            card has been recorded for this `workflow_id` yet (safe to
            proceed with creation).
        """
        return self._card_ids.get(workflow_id)

    def record(self, workflow_id: str, card_id: str) -> None:
        """Record that `workflow_id` has already resulted in `card_id`.

        Call this immediately after a successful `simboard_client.create_card`
        response, before doing anything else that could fail — the whole
        point is to record success as early as possible so a subsequent
        crash/retry sees it.

        Args:
            workflow_id: The workflow the card was created for.
            card_id: The SimBoard card id from `create_card`'s response.

        Side effects:
            Overwrites any prior entry for `workflow_id` (shouldn't happen
            in practice — a workflow should only ever create one card —
            but this isn't enforced/asserted here).
        """
        self._card_ids[workflow_id] = card_id


idempotency_store = IdempotencyStore()
