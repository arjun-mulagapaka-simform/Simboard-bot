"""Unit tests for pipeline.normalize — multi-word @mention reconstruction."""

from types import SimpleNamespace

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


def test_multi_word_mention_name_is_reconstructed_in_order():
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

    message = normalize(SimpleNamespace(activity=activity))

    assert len(message.other_mentions) == 1
    assert message.other_mentions[0].name == "Prerak Dave"
    assert message.other_mentions[0].id == "29:prerak"


def test_single_word_mention_name_is_unaffected():
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

    message = normalize(SimpleNamespace(activity=activity))

    assert message.other_mentions[0].name == "Arjun"


def test_two_distinct_mentioned_users_are_not_merged():
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

    message = normalize(SimpleNamespace(activity=activity))

    names = {m.name for m in message.other_mentions}
    assert names == {"Prerak Dave", "Arjun"}
