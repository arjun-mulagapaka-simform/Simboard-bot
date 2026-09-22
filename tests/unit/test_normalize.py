"""Unit tests for pipeline.normalize — multi-word @mention reconstruction."""

from types import SimpleNamespace

import pytest
from microsoft_teams.api import ConversationAccount, MentionEntity, MessageActivity
from microsoft_teams.api.models.account import Account

from src.pipeline.normalize import normalize

_BOT_ID = "28:bot-id"


def _account(id_: str, name: str) -> Account:
    return Account(id=id_, name=name, aad_object_id=None, type="person")


def _split_mention(mentioned_id: str, word: str) -> MentionEntity:
    """One entity for one word of a multi-word mention.

    Mirrors what real Teams sends: `.mentioned.name` and `.text` both
    carry only that word.
    """
    return MentionEntity(
        type="mention", mentioned=_account(mentioned_id, word), text=f"<at>{word}</at>"
    )


@pytest.mark.asyncio
async def test_multi_word_mention_name_is_reconstructed_in_order():
    activity = MessageActivity(
        id="a1",
        type="message",
        text="<at>Bot</at> please do <at>Prerak</at> <at>Dave</at> thing",
        conversation=ConversationAccount(id="conv-1"),
        from_=_account("29:sender", "Sender"),
        recipient=_account(_BOT_ID, "Bot"),
        entities=[
            MentionEntity(type="mention", mentioned=_account(_BOT_ID, "Bot"), text="<at>Bot</at>"),
            _split_mention("29:prerak", "Prerak"),
            _split_mention("29:prerak", "Dave"),
        ],
    )

    message = await normalize(SimpleNamespace(activity=activity))

    assert len(message.other_mentions) == 1
    assert message.other_mentions[0].name == "Prerak Dave"
    assert message.other_mentions[0].id == "29:prerak"


@pytest.mark.asyncio
async def test_single_word_mention_name_is_unaffected():
    activity = MessageActivity(
        id="a2",
        type="message",
        text="<at>Bot</at> ping <at>Arjun</at>",
        conversation=ConversationAccount(id="conv-1"),
        from_=_account("29:sender", "Sender"),
        recipient=_account(_BOT_ID, "Bot"),
        entities=[
            MentionEntity(type="mention", mentioned=_account(_BOT_ID, "Bot"), text="<at>Bot</at>"),
            _split_mention("29:arjun", "Arjun"),
        ],
    )

    message = await normalize(SimpleNamespace(activity=activity))

    assert message.other_mentions[0].name == "Arjun"


@pytest.mark.asyncio
async def test_multi_word_bot_self_mention_is_not_leaked_as_other_mention():
    """Regression test for the bot-self-mention-leak fix.

    A two-word bot display name ("Bot POC") previously leaked its second
    split entity through as a bogus other_mention, since the old exclusion
    check compared entities by object identity against
    `get_account_mention()`'s single returned entity rather than by id.
    """
    activity = MessageActivity(
        id="a4",
        type="message",
        text="<at>Bot</at> <at>POC</at> please do the thing",
        conversation=ConversationAccount(id="conv-1"),
        from_=_account("29:sender", "Sender"),
        recipient=_account(_BOT_ID, "Bot POC"),
        entities=[
            _split_mention(_BOT_ID, "Bot"),
            _split_mention(_BOT_ID, "POC"),
        ],
    )

    message = await normalize(SimpleNamespace(activity=activity))

    assert message.other_mentions == []


@pytest.mark.asyncio
async def test_two_distinct_mentioned_users_are_not_merged():
    activity = MessageActivity(
        id="a3",
        type="message",
        text="<at>Bot</at> <at>Prerak</at> <at>Dave</at> and <at>Arjun</at>",
        conversation=ConversationAccount(id="conv-1"),
        from_=_account("29:sender", "Sender"),
        recipient=_account(_BOT_ID, "Bot"),
        entities=[
            MentionEntity(type="mention", mentioned=_account(_BOT_ID, "Bot"), text="<at>Bot</at>"),
            _split_mention("29:prerak", "Prerak"),
            _split_mention("29:prerak", "Dave"),
            _split_mention("29:arjun", "Arjun"),
        ],
    )

    message = await normalize(SimpleNamespace(activity=activity))

    names = {m.name for m in message.other_mentions}
    assert names == {"Prerak Dave", "Arjun"}


def _ctx_with_roster(activity, member_by_id: dict[str, Account]) -> SimpleNamespace:
    """A fake `ActivityContext` for roster-backfill tests.

    Its `api.conversations.get_member_by_id` looks up `member_by_id` by
    member id, raising `KeyError` (caught by `_backfill_from_roster`)
    for an id not present in the map.
    """

    async def get_member_by_id(conversation_id: str, member_id: str) -> Account:
        return member_by_id[member_id]

    return SimpleNamespace(
        activity=activity,
        api=SimpleNamespace(conversations=SimpleNamespace(get_member_by_id=get_member_by_id)),
    )


@pytest.mark.asyncio
async def test_mention_is_backfilled_with_aad_object_id_and_email_from_roster():
    activity = MessageActivity(
        id="a5",
        type="message",
        text="<at>Bot</at> ping <at>Prerak</at>",
        conversation=ConversationAccount(id="conv-1"),
        from_=_account("29:sender", "Sender"),
        recipient=_account(_BOT_ID, "Bot"),
        entities=[
            MentionEntity(type="mention", mentioned=_account(_BOT_ID, "Bot"), text="<at>Bot</at>"),
            _split_mention("29:prerak", "Prerak"),
        ],
    )
    roster_account = Account(
        id="29:prerak",
        name="Prerak Dave",
        aad_object_id="aad-prerak",
        type="person",
    )
    roster_account.email = "prerak.dave@simformsolutions.com"

    message = await normalize(_ctx_with_roster(activity, {"29:prerak": roster_account}))

    assert message.other_mentions[0].aad_object_id == "aad-prerak"
    assert message.other_mentions[0].email == "prerak.dave@simformsolutions.com"
    assert message.other_mentions[0].name == "Prerak"  # unchanged by the backfill


@pytest.mark.asyncio
async def test_mention_stays_unbackfilled_when_roster_lookup_fails():
    activity = MessageActivity(
        id="a6",
        type="message",
        text="<at>Bot</at> ping <at>Prerak</at>",
        conversation=ConversationAccount(id="conv-1"),
        from_=_account("29:sender", "Sender"),
        recipient=_account(_BOT_ID, "Bot"),
        entities=[
            MentionEntity(type="mention", mentioned=_account(_BOT_ID, "Bot"), text="<at>Bot</at>"),
            _split_mention("29:prerak", "Prerak"),
        ],
    )

    message = await normalize(_ctx_with_roster(activity, {}))

    assert message.other_mentions[0].aad_object_id is None
    assert message.other_mentions[0].email is None
