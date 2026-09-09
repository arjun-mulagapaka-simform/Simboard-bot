"""Unit tests for Step 1 (Validate Event) — no network/Emulator needed.

Builds MessageActivity fixtures directly rather than going through the
Teams SDK's HTTP adapter, so these run offline and fast.
"""

from types import SimpleNamespace

import pytest
from microsoft_teams.api import ConversationAccount, MentionEntity, MessageActivity
from microsoft_teams.api.models.account import Account

import src.pipeline.validate_event as validate_event_module
from src.models.card_draft import CardDraft
from src.pipeline.validate_event import ValidationOutcome, validate
from src.state.seen_activities import SeenActivityStore
from src.state.workflow_store import WorkflowStore

_BOT_ID = "28:bot-id"
_USER_ID = "29:user-id"


def _account(id_: str, name: str = "Some User") -> Account:
    return Account(id=id_, name=name, aad_object_id=None, type="person")


def _mention_entity(mentioned_id: str, text: str = "<at>Bot</at>") -> MentionEntity:
    return MentionEntity(type="mention", mentioned=_account(mentioned_id, "Bot"), text=text)


def _activity(
    *,
    id_: str = "activity-1",
    type_: str = "message",
    mention_bot: bool = True,
    with_conversation: bool = True,
    with_from: bool = True,
    conversation_id: str = "conv-1",
    reply_to_id: str | None = None,
) -> MessageActivity:
    """Build a MessageActivity fixture, well-formed by default.

    `conversation`/`from_`/`type` are non-optional, correctly-typed fields
    on the SDK's own `MessageActivity` model (verified: `from_` and
    `conversation` are required `Account`/`ConversationAccount` instances,
    `type` is `Literal["message"]`) — so the SDK itself will not hand
    `on_message` an activity missing them or with the wrong `type`. To
    exercise `_is_well_formed`'s defensive branches anyway (belt-and-braces
    against a future SDK change or a hand-rolled test harness), this uses
    `model_construct()` to bypass that validation when asked to omit a
    field.
    """
    if with_conversation and with_from and type_ == "message":
        return MessageActivity(
            id=id_,
            type=type_,
            text="hello",
            conversation=ConversationAccount(id=conversation_id),
            from_=_account(_USER_ID, "Some User"),
            recipient=_account(_BOT_ID, "Bot"),
            entities=[_mention_entity(_BOT_ID)] if mention_bot else [],
            reply_to_id=reply_to_id,
        )

    return MessageActivity.model_construct(
        id=id_,
        type=type_,
        text="hello",
        conversation=ConversationAccount(id=conversation_id) if with_conversation else None,
        from_=_account(_USER_ID, "Some User") if with_from else None,
        recipient=_account(_BOT_ID, "Bot"),
        entities=[_mention_entity(_BOT_ID)] if mention_bot else [],
        reply_to_id=reply_to_id,
    )


def _ctx(activity: MessageActivity) -> SimpleNamespace:
    """A stand-in for ActivityContext — validate() only reads `.activity`."""
    return SimpleNamespace(activity=activity)


@pytest.fixture(autouse=True)
def _isolated_seen_store(monkeypatch):
    """Give every test its own SeenActivityStore.

    So claims don't leak across tests (the real module uses a module-level
    singleton).
    """
    store = SeenActivityStore(ttl_seconds=300.0)
    monkeypatch.setattr(validate_event_module, "seen_activities", store)
    return store


@pytest.fixture(autouse=True)
def _isolated_workflow_store(monkeypatch):
    """Give every test its own WorkflowStore, same reasoning as above."""
    store = WorkflowStore()
    monkeypatch.setattr(validate_event_module, "workflow_store", store)
    return store


def _draft(workflow_id: str, clarification_prompt_id: str | None) -> CardDraft:
    return CardDraft(
        workflow_id=workflow_id,
        title="Fix login bug",
        description=None,
        card_type="story",
        project_id="proj-1",
        board_id=None,
        assignee_user_ids=[],
        unresolved_fields=[],
        pending_confirmation_fields=["assignee"],
        clarification_prompt_id=clarification_prompt_id,
    )


def test_accepts_well_formed_mentioned_activity():
    assert validate(_ctx(_activity())) is ValidationOutcome.ACCEPT


def test_not_mentioned_is_dropped_without_claiming_dedup_slot(_isolated_seen_store):
    activity = _activity(mention_bot=False)

    assert validate(_ctx(activity)) is ValidationOutcome.NOT_MENTIONED
    # Confirms NOT_MENTIONED doesn't consume a dedup slot: a later,
    # mentioned activity with the same id must still be accepted.
    assert _isolated_seen_store.claim(activity.id) is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"with_conversation": False},
        {"with_from": False},
        {"type_": "conversationUpdate"},
    ],
)
def test_malformed_activity_is_dropped_before_mention_check(kwargs):
    activity = _activity(mention_bot=False, **kwargs)
    assert validate(_ctx(activity)) is ValidationOutcome.MALFORMED


def test_missing_id_is_malformed():
    activity = _activity(id_="", mention_bot=False)
    assert validate(_ctx(activity)) is ValidationOutcome.MALFORMED


def test_unmentioned_reply_to_pending_clarification_is_accepted(_isolated_workflow_store):
    _isolated_workflow_store.save(_draft("conv-1", clarification_prompt_id="bot-question-1"))
    activity = _activity(mention_bot=False, reply_to_id="bot-question-1")

    assert validate(_ctx(activity)) is ValidationOutcome.ACCEPT


def test_unmentioned_reply_to_a_different_message_is_not_accepted(_isolated_workflow_store):
    _isolated_workflow_store.save(_draft("conv-1", clarification_prompt_id="bot-question-1"))
    activity = _activity(mention_bot=False, reply_to_id="some-other-message")

    assert validate(_ctx(activity)) is ValidationOutcome.NOT_MENTIONED


def test_unmentioned_reply_with_no_pending_draft_is_not_accepted(_isolated_workflow_store):
    activity = _activity(mention_bot=False, reply_to_id="bot-question-1")
    assert validate(_ctx(activity)) is ValidationOutcome.NOT_MENTIONED


def test_unmentioned_non_reply_is_still_not_accepted(_isolated_workflow_store):
    _isolated_workflow_store.save(_draft("conv-1", clarification_prompt_id="bot-question-1"))
    activity = _activity(mention_bot=False, reply_to_id=None)

    assert validate(_ctx(activity)) is ValidationOutcome.NOT_MENTIONED


def test_duplicate_delivery_is_dropped_on_second_claim():
    activity = _activity(id_="dup-1")

    assert validate(_ctx(activity)) is ValidationOutcome.ACCEPT
    assert validate(_ctx(activity)) is ValidationOutcome.DUPLICATE


def test_different_activity_ids_do_not_collide():
    assert validate(_ctx(_activity(id_="a"))) is ValidationOutcome.ACCEPT
    assert validate(_ctx(_activity(id_="b"))) is ValidationOutcome.ACCEPT
