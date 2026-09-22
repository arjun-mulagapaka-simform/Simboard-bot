"""Orchestrator (Task 8).

normalize -> extract -> resolve -> confidence gate -> clarify (loop) or
create. bot.py should call handle() and send back whatever text it
returns.
"""

from microsoft_teams.api import MessageActivity
from microsoft_teams.apps import ActivityContext

from src.config import settings
from src.integrations import simboard_client
from src.models.audit_event import AuditEventType
from src.pipeline import clarify, confidence, extract, normalize, resolve
from src.state.audit_store import audit_store
from src.state.idempotency_store import idempotency_store
from src.state.workflow_store import workflow_store


async def handle(ctx: ActivityContext[MessageActivity]) -> None:  # noqa: C901
    """Run the full Phase A pipeline for one inbound @mention.

    Sends the resulting reply back on the same conversation. Two paths,
    chosen by whether a pending, still-unresolved draft exists for this
    conversation (`message.workflow_id`, i.e. `activity.conversation.id`):
    - A pending draft exists (`unresolved_fields` or
      `pending_confirmation_fields` non-empty): treat this message as the
      user's answer to it. normalize -> apply_clarification (merges the
      reply into the existing draft instead of starting over).
    - Otherwise: always a fresh request. normalize -> extract -> resolve.

    Conversation-id-scoped, not `reply_to_id`-matched, as of 2026-09-18 —
    an earlier design required `message.reply_to_id` to match the pending
    draft's `clarification_prompt_id` (see item 7a in ../../todo.md for
    why conversation id alone was originally deemed insufficient — risk of
    misattributing a genuinely new, unrelated message as answering a
    pending clarification). That was dropped after a live test showed
    Teams' `replyToId` going unset even on a genuine threaded Reply —
    confirmed against Microsoft's own Bot Framework Activity spec, which
    documents `replyToId` as optional. See tasks-list.md's "Known
    limitation" entry for the accepted tradeoff this reintroduces.

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

    Side effects:
        Saves/overwrites the resolved `CardDraft` in `workflow_store` keyed
        by `message.workflow_id`. Sends exactly one message back on the
        conversation via `ctx.send()`. When confidence is sufficient,
        calls `simboard_client.create_card`, which makes a real SimBoard
        write (confirmed live).

        Marks `message.workflow_id` in progress via `workflow_store.try_start`
        for the duration of this call, so a second concurrent message in the
        same conversation is turned away instead of racing this one's
        read-modify-write of `workflow_store` — see item 7a in ../../todo.md.

        Records an `AuditEvent` (see `state/audit_store.py`) at each real
        pipeline transition — received, extracted, resolved, gated,
        rejected, ticket_created, error — all centralized here rather than
        scattered across `pipeline/*.py`, so the audit trail always
        reflects what `handle()` actually decided, not what an individual
        step merely computed. `actor` is `message.sender.id` (not a name
        or email — unreliable, see Track 2 in tasks-list.md).
        `payload_redacted` is structured facts only (ids, field names,
        counts, error types) — never raw message text, per
        bot-docs/05-permissions-and-security.md §5.

        Before calling `simboard_client.create_card`, resolves the sender
        to a SimBoard user id via `resolve.resolve_sender` (email-only,
        against the resolved board's member list) and fails closed — no
        card is created, and the user is told they couldn't be verified —
        if the sender doesn't resolve. This is unconditional today (not
        feature-flagged), so in practice every real Teams message fails
        this gate until Teams supplies sender email (see tasks-list.md's
        Track 2 entry) — an accepted tradeoff, not a bug. Then checks
        `idempotency_store` for a card already created for this
        `workflow_id` and skips creation if one exists (see
        `state/idempotency_store.py` for what this does and doesn't
        cover). Records `workflow_id -> card_id` in `idempotency_store`
        immediately after a successful creation.

        After creation, attaches each id in `draft.assignee_user_ids`
        (already email-resolved by `resolve.py`, so no separate permission
        check is needed here) via `simboard_client.create_card_membership`.
        A failed assignment does not fail the whole card — it's reported
        back in the reply text ("couldn't assign: ...") instead, since the
        card itself was already created successfully.
    """
    message = await normalize.normalize(ctx)
    correlation_id = ctx.activity.id

    audit_store.record(
        correlation_id=correlation_id,
        workflow_id=message.workflow_id,
        event_type=AuditEventType.RECEIVED,
        actor=message.sender.id,
    )

    if not workflow_store.try_start(message.workflow_id):
        await ctx.send(
            "Still working on your previous request in this conversation — " "give me a moment."
        )
        return

    try:
        pending_draft = workflow_store.get(message.workflow_id)

        # Conversation-id-scoped, not reply_to_id-matched (2026-09-18):
        # Teams' replyToId was found unreliable live — a genuine threaded
        # Reply to the bot's own clarification question still arrived with
        # reply_to_id unset (confirmed against Microsoft's own Bot
        # Framework Activity spec: replyToId is documented as optional, a
        # channel "MAY omit" it). So any @mentioned message in a
        # conversation with a pending, still-unresolved draft is now
        # treated as the answer to it — see tasks-list.md's "Known
        # limitation" entry for the accepted tradeoff (a genuinely new,
        # unrelated request sent while a clarification is pending would be
        # misread as an answer instead).
        is_clarification_reply = pending_draft is not None and (
            pending_draft.unresolved_fields or pending_draft.pending_confirmation_fields
        )

        if is_clarification_reply:
            turn = workflow_store.increment_turn(message.workflow_id)
            if turn > settings.max_clarification_turns:
                workflow_store.clear(message.workflow_id)
                await ctx.send(
                    "I still couldn't pin down all the details after a few tries — "
                    "please resend your request with the full details (project, "
                    "assignee, etc.) in one message."
                )
                return
            draft = await resolve.apply_clarification(pending_draft, message)
        else:
            extraction = await extract.extract(message)
            audit_store.record(
                correlation_id=correlation_id,
                workflow_id=message.workflow_id,
                event_type=AuditEventType.EXTRACTED,
                actor=message.sender.id,
                payload_redacted={
                    "fields_set": [
                        name
                        for name in (
                            "title",
                            "description",
                            "card_type",
                            "project_hint",
                            "board_hint",
                        )
                        if getattr(extraction, name).value is not None
                    ]
                },
            )
            draft = await resolve.resolve(extraction, message)

        audit_store.record(
            correlation_id=correlation_id,
            workflow_id=message.workflow_id,
            event_type=AuditEventType.RESOLVED,
            actor=message.sender.id,
            payload_redacted={
                "project_id": draft.project_id,
                "board_id": draft.board_id,
                "assignee_count": len(draft.assignee_user_ids),
                "unresolved_fields": draft.unresolved_fields,
            },
        )

        if confidence.needs_clarification(draft):
            audit_store.record(
                correlation_id=correlation_id,
                workflow_id=message.workflow_id,
                event_type=AuditEventType.GATED,
                actor=message.sender.id,
                payload_redacted={
                    "unresolved_fields": draft.unresolved_fields,
                    "pending_confirmation_fields": draft.pending_confirmation_fields,
                },
            )
            prompt_text = await clarify.build_clarification_prompt(draft)
            sent = await ctx.send(prompt_text)
            draft.clarification_prompt_id = sent.id
            workflow_store.save(draft)
            return

        workflow_store.save(draft)

        sender_user_id = await resolve.resolve_sender(message, draft.board_id)
        if sender_user_id is None:
            audit_store.record(
                correlation_id=correlation_id,
                workflow_id=message.workflow_id,
                event_type=AuditEventType.REJECTED,
                actor=message.sender.id,
                payload_redacted={"reason": "sender_not_verified"},
            )
            await ctx.send(
                "I couldn't verify you as a member of this board, so I can't "
                "create this card. Ask a board admin to add you, then try again."
            )
            return

        existing_card_id = idempotency_store.get_card_id(message.workflow_id)
        if existing_card_id is not None:
            # Already created on a prior run for this workflow_id (e.g. a
            # retried/re-delivered message) — don't create a second card.
            await ctx.send(f"Card already created: {existing_card_id}")
            return

        result = await simboard_client.create_card(draft)
        idempotency_store.record(message.workflow_id, result["card_id"])
        audit_store.record(
            correlation_id=correlation_id,
            workflow_id=message.workflow_id,
            event_type=AuditEventType.TICKET_CREATED,
            actor=message.sender.id,
            payload_redacted={"card_id": result["card_id"]},
        )

        unassigned = []
        for user_id in draft.assignee_user_ids:
            try:
                await simboard_client.create_card_membership(result["card_id"], user_id)
            except Exception:
                unassigned.append(user_id)

        reply = f"Card created: {result['card_id']}"
        if unassigned:
            reply += f" (couldn't assign: {', '.join(unassigned)})"
        await ctx.send(reply)
    except Exception as exc:
        audit_store.record(
            correlation_id=correlation_id,
            workflow_id=message.workflow_id,
            event_type=AuditEventType.ERROR,
            actor=message.sender.id,
            payload_redacted={"error_type": type(exc).__name__},
        )
        raise
    finally:
        workflow_store.finish(message.workflow_id)
