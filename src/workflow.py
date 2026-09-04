"""Orchestrator (Task 8): normalize -> extract -> resolve -> confidence gate
-> clarify (loop) or create. bot.py should call handle() and send back
whatever text it returns.
"""

from microsoft_teams.apps import ActivityContext
from microsoft_teams.api import MessageActivity

from src.pipeline import clarify, confidence, extract, normalize, resolve
from src.state.workflow_store import workflow_store
from src.integrations import simboard_client


async def handle(ctx: ActivityContext[MessageActivity]) -> None:
    """Run the full Phase A pipeline for one inbound @mention and send the
    resulting reply back on the same conversation.

    Two paths, chosen by whether this message is a direct reply to the
    bot's own pending clarification question — NOT merely whether a
    pending draft exists for the conversation (see item 7a in
    ../../todo.md for why conversation id alone was insufficient and
    caused workflow-id collisions on concurrent requests):
    - `message.reply_to_id` matches the pending draft's
      `clarification_prompt_id`: treat this as the user's answer.
      normalize -> apply_clarification (merges the reply into the
      existing draft instead of starting over).
    - Otherwise (no pending draft, or this message isn't a reply to it):
      always a fresh request, even if a pending draft exists for the same
      conversation. normalize -> extract -> resolve (overwrites any
      stale/abandoned pending draft for this workflow_id).

    Either path continues: confidence gate -> (send clarification
    question, recording its sent id | create card and confirm).

    Args:
        ctx: The Teams activity context for the inbound mention, as passed
            to `bot.py`'s `on_message` handler. Must be a "message" activity
            where the bot was directly mentioned — callers are responsible
            for that check (see `bot.py`).

    Returns:
        None. The reply is sent directly via `ctx.send()` inside this
        function (not returned to the caller), because the clarification
        path needs the `SentActivity.id` that only `ctx.send()`'s return
        value provides, to store as `CardDraft.clarification_prompt_id`.

    Raises:
        NotImplementedError: `normalize` is implemented, but `extract`,
            `resolve`, and `resolve.apply_clarification` are not yet —
            calling this end-to-end will raise until Tasks 4/1(resolution)
            land.

    Side effects:
        Saves/overwrites the resolved `CardDraft` in `workflow_store` keyed
        by `message.workflow_id`. Sends exactly one message back on the
        conversation via `ctx.send()`. When confidence is sufficient,
        calls `simboard_client.create_card`, which is a stub in Phase A
        and performs no real network call or SimBoard write.
    """
    message = normalize.normalize(ctx)
    pending_draft = workflow_store.get(message.workflow_id)

    is_clarification_reply = (
        pending_draft is not None
        and pending_draft.unresolved_fields
        and message.reply_to_id is not None
        and message.reply_to_id == pending_draft.clarification_prompt_id
    )

    if is_clarification_reply:
        workflow_store.increment_turn(message.workflow_id)
        draft = await resolve.apply_clarification(pending_draft, message)
    else:
        extraction = await extract.extract(message)
        draft = await resolve.resolve(extraction, message.workflow_id)

    if confidence.needs_clarification(draft):
        prompt_text = clarify.build_clarification_prompt(draft)
        sent = await ctx.send(prompt_text)
        draft.clarification_prompt_id = sent.id
        workflow_store.save(draft)
        return

    workflow_store.save(draft)
    result = await simboard_client.create_card(draft)
    await ctx.send(f"Card created: {result['card_id']}")
