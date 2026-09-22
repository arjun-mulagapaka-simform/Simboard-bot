"""Unit tests for integrations.simboard_client's real HTTP methods.

Mocked at the `httpx.AsyncClient` boundary and `simboard_auth.get_access_token`
— no network call, no real login. `_get`/`_post` both route through
`_request`, which calls `client.request(method, url, ...)` — mocks target
`.request`, not `.get`/`.post` directly.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.core.exceptions import NotFoundException, UpstreamServiceException
from src.integrations import simboard_client


@pytest.fixture(autouse=True)
def _fake_token(monkeypatch):
    monkeypatch.setattr(
        simboard_client.simboard_auth, "get_access_token", AsyncMock(return_value="fake-jwt")
    )
    monkeypatch.setattr(simboard_client.settings, "simboard_base_url", "https://simboard.test")


def _mock_request(monkeypatch, *, status_code: int = 200, json_body: dict | None = None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body if json_body is not None else {}
    response.text = str(json_body)

    mock_client = AsyncMock()
    mock_client.request.return_value = response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False

    monkeypatch.setattr(simboard_client.httpx, "AsyncClient", MagicMock(return_value=mock_client))
    return mock_client


@pytest.mark.asyncio
async def test_list_projects_maps_items_to_id_name(monkeypatch):
    _mock_request(
        monkeypatch,
        json_body={"items": [{"id": "p1", "name": "onboarding", "extra": "ignored"}]},
    )

    result = await simboard_client.list_projects()

    assert result == [{"id": "p1", "name": "onboarding"}]


@pytest.mark.asyncio
async def test_list_projects_sends_authorization_header(monkeypatch):
    mock_client = _mock_request(monkeypatch, json_body={"items": []})

    await simboard_client.list_projects()

    mock_client.request.assert_awaited_once_with(
        "GET",
        "https://simboard.test/api/projects",
        json=None,
        headers={"Authorization": "Bearer fake-jwt"},
    )


@pytest.mark.asyncio
async def test_list_boards_unscoped_reads_included_boards(monkeypatch):
    _mock_request(
        monkeypatch,
        json_body={"included": {"boards": [{"id": "b1", "projectId": "p1", "name": "sprint-42"}]}},
    )

    result = await simboard_client.list_boards()

    assert result == [{"id": "b1", "project_id": "p1", "name": "sprint-42"}]


@pytest.mark.asyncio
async def test_list_boards_scoped_hits_project_show_endpoint(monkeypatch):
    mock_client = _mock_request(
        monkeypatch,
        json_body={"included": {"boards": [{"id": "b1", "projectId": "p1", "name": "sprint-42"}]}},
    )

    result = await simboard_client.list_boards("p1")

    mock_client.request.assert_awaited_once_with(
        "GET",
        "https://simboard.test/api/projects/p1",
        json=None,
        headers={"Authorization": "Bearer fake-jwt"},
    )
    assert result == [{"id": "b1", "project_id": "p1", "name": "sprint-42"}]


@pytest.mark.asyncio
async def test_list_users_reads_included_users_for_board(monkeypatch):
    mock_client = _mock_request(
        monkeypatch,
        json_body={
            "included": {
                "users": [{"id": "u1", "name": "Prerak Dave", "email": "prerak@simform.com"}]
            }
        },
    )

    result = await simboard_client.list_users("b1")

    mock_client.request.assert_awaited_once_with(
        "GET",
        "https://simboard.test/api/boards/b1",
        json=None,
        headers={"Authorization": "Bearer fake-jwt"},
    )
    assert result == [{"id": "u1", "name": "Prerak Dave", "email": "prerak@simform.com"}]


@pytest.mark.asyncio
async def test_list_users_falls_back_to_username_when_name_blank(monkeypatch):
    _mock_request(
        monkeypatch,
        json_body={"included": {"users": [{"id": "u1", "name": "", "username": "pdave"}]}},
    )

    result = await simboard_client.list_users("b1")

    assert result == [{"id": "u1", "name": "pdave", "email": None}]


@pytest.mark.asyncio
async def test_list_users_email_defaults_to_none_when_absent(monkeypatch):
    _mock_request(
        monkeypatch,
        json_body={"included": {"users": [{"id": "u1", "name": "Prerak Dave"}]}},
    )

    result = await simboard_client.list_users("b1")

    assert result == [{"id": "u1", "name": "Prerak Dave", "email": None}]


@pytest.mark.asyncio
async def test_get_raises_not_found_on_404(monkeypatch):
    _mock_request(monkeypatch, status_code=404, json_body={})

    with pytest.raises(NotFoundException):
        await simboard_client.list_boards("missing-project")


@pytest.mark.asyncio
async def test_get_raises_upstream_on_other_error_status(monkeypatch):
    _mock_request(monkeypatch, status_code=500, json_body={})

    with pytest.raises(UpstreamServiceException):
        await simboard_client.list_projects()


@pytest.mark.asyncio
async def test_get_retries_once_after_401_with_forced_relogin(monkeypatch):
    unauthorized = MagicMock(status_code=401, text="unauthorized")
    ok = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"items": [{"id": "p1", "name": "onboarding"}]}),
    )

    mock_client = AsyncMock()
    mock_client.request.side_effect = [unauthorized, ok]
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    monkeypatch.setattr(simboard_client.httpx, "AsyncClient", MagicMock(return_value=mock_client))

    token_mock = AsyncMock(return_value="fake-jwt")
    monkeypatch.setattr(simboard_client.simboard_auth, "get_access_token", token_mock)

    result = await simboard_client.list_projects()

    assert result == [{"id": "p1", "name": "onboarding"}]
    assert mock_client.request.await_count == 2
    # One of the get_access_token calls must be the forced re-login
    # triggered by the 401 (the other is the normal cache-first lookup
    # on the retried request).
    assert {"force_refresh": True} in [call.kwargs for call in token_mock.await_args_list]


@pytest.mark.asyncio
async def test_get_raises_upstream_on_second_consecutive_401(monkeypatch):
    unauthorized = MagicMock(status_code=401, text="unauthorized")

    mock_client = AsyncMock()
    mock_client.request.side_effect = [unauthorized, unauthorized]
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    monkeypatch.setattr(simboard_client.httpx, "AsyncClient", MagicMock(return_value=mock_client))
    monkeypatch.setattr(
        simboard_client.simboard_auth, "get_access_token", AsyncMock(return_value="fake-jwt")
    )

    with pytest.raises(UpstreamServiceException):
        await simboard_client.list_projects()

    assert mock_client.request.await_count == 2


@pytest.mark.asyncio
async def test_get_raises_upstream_on_network_error(monkeypatch):
    mock_client = AsyncMock()
    mock_client.request.side_effect = httpx.ConnectError("boom")
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    monkeypatch.setattr(simboard_client.httpx, "AsyncClient", MagicMock(return_value=mock_client))

    with pytest.raises(UpstreamServiceException):
        await simboard_client.list_projects()


# --- create_card_membership ------------------------------------------------


@pytest.mark.asyncio
async def test_create_card_membership_posts_user_id_and_unwraps_item(monkeypatch):
    mock_client = _mock_request(
        monkeypatch,
        json_body={"item": {"id": "cm1", "cardId": "card-1", "userId": "user-1"}},
    )

    result = await simboard_client.create_card_membership("card-1", "user-1")

    mock_client.request.assert_awaited_once_with(
        "POST",
        "https://simboard.test/api/cards/card-1/card-memberships",
        json={"userId": "user-1"},
        headers={"Authorization": "Bearer fake-jwt"},
    )
    assert result == {"id": "cm1", "card_id": "card-1", "user_id": "user-1"}


@pytest.mark.asyncio
async def test_create_card_membership_raises_not_found_on_404(monkeypatch):
    _mock_request(monkeypatch, status_code=404, json_body={})

    with pytest.raises(NotFoundException):
        await simboard_client.create_card_membership("missing-card", "user-1")


@pytest.mark.asyncio
async def test_create_card_membership_raises_upstream_on_conflict(monkeypatch):
    _mock_request(monkeypatch, status_code=409, json_body={"userAlreadyCardMember": "..."})

    with pytest.raises(UpstreamServiceException):
        await simboard_client.create_card_membership("card-1", "user-1")


# --- get_todo_list_target ---------------------------------------------------


@pytest.mark.asyncio
async def test_get_todo_list_target_matches_to_do_by_name_case_insensitive(monkeypatch):
    _mock_request(
        monkeypatch,
        json_body={
            "included": {
                "lists": [
                    {"id": "l1", "name": "Backlog", "position": 1},
                    {"id": "l2", "name": "To Do", "position": 2},
                    {"id": "l3", "name": "Done", "position": 3},
                ],
                "cards": [],
            }
        },
    )

    result = await simboard_client.get_todo_list_target("b1")

    assert result == {"list_id": "l2", "position": 65535}


@pytest.mark.asyncio
async def test_get_todo_list_target_falls_back_to_lowest_position_when_no_todo(monkeypatch):
    _mock_request(
        monkeypatch,
        json_body={
            "included": {
                "lists": [
                    {"id": "l1", "name": "In Progress", "position": 2},
                    {"id": "l2", "name": "Backlog", "position": 1},
                ],
                "cards": [],
            }
        },
    )

    result = await simboard_client.get_todo_list_target("b1")

    assert result["list_id"] == "l2"


@pytest.mark.asyncio
async def test_get_todo_list_target_fallback_tolerates_null_position(monkeypatch):
    _mock_request(
        monkeypatch,
        json_body={
            "included": {
                "lists": [
                    {"id": "l1", "name": "In Progress", "position": None},
                    {"id": "l2", "name": "Backlog", "position": 1},
                ],
                "cards": [],
            }
        },
    )

    result = await simboard_client.get_todo_list_target("b1")

    assert result["list_id"] == "l1"


@pytest.mark.asyncio
async def test_get_todo_list_target_raises_on_no_lists(monkeypatch):
    _mock_request(monkeypatch, json_body={"included": {"lists": [], "cards": []}})

    with pytest.raises(ValueError):
        await simboard_client.get_todo_list_target("b1")


@pytest.mark.asyncio
async def test_get_todo_list_target_positions_after_existing_cards_in_that_list(monkeypatch):
    _mock_request(
        monkeypatch,
        json_body={
            "included": {
                "lists": [{"id": "l1", "name": "To Do", "position": 1}],
                "cards": [
                    {"id": "c1", "listId": "l1", "position": 100},
                    {"id": "c2", "listId": "l1", "position": 200},
                    {"id": "c3", "listId": "other-list", "position": 999999},
                ],
            }
        },
    )

    result = await simboard_client.get_todo_list_target("b1")

    assert result == {"list_id": "l1", "position": 200 + 65535}


# --- create_card -------------------------------------------------------


def _draft(**overrides):
    from src.models.card_draft import CardDraft

    fields = {
        "workflow_id": "wf-1",
        "title": "Fix login bug",
        "description": "Users can't log in on mobile",
        "card_type": "project",
        "project_id": "p1",
        "board_id": "b1",
        "assignee_user_ids": [],
        "unresolved_fields": [],
    }
    fields.update(overrides)
    return CardDraft(**fields)


@pytest.mark.asyncio
async def test_create_card_posts_to_todo_list_with_title_and_description(monkeypatch):
    mock_client = AsyncMock()
    board_response = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={
                "included": {
                    "lists": [{"id": "l1", "name": "To Do"}],
                    "cards": [{"id": "c1", "listId": "l1", "position": 100}],
                }
            }
        ),
    )
    create_response = MagicMock(
        status_code=200, json=MagicMock(return_value={"item": {"id": "card-1"}})
    )
    mock_client.request.side_effect = [board_response, create_response]
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    monkeypatch.setattr(simboard_client.httpx, "AsyncClient", MagicMock(return_value=mock_client))

    result = await simboard_client.create_card(_draft())

    assert result == {"status": "created", "card_id": "card-1", "workflow_id": "wf-1"}
    mock_client.request.assert_awaited_with(
        "POST",
        "https://simboard.test/api/lists/l1/cards",
        json={
            "name": "Fix login bug",
            "description": (
                "**THIS TICKET IS CREATED BY SimBoard bot**\n\nUsers can't log in on mobile"
            ),
            "type": "project",
            "position": 100 + 65535,
        },
        headers={"Authorization": "Bearer fake-jwt"},
    )


@pytest.mark.asyncio
async def test_create_card_raises_upstream_on_error_status(monkeypatch):
    mock_client = AsyncMock()
    board_response = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={"included": {"lists": [{"id": "l1", "name": "To Do"}], "cards": []}}
        ),
    )
    error_response = MagicMock(status_code=500, text="server error")
    mock_client.request.side_effect = [board_response, error_response]
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    monkeypatch.setattr(simboard_client.httpx, "AsyncClient", MagicMock(return_value=mock_client))

    with pytest.raises(UpstreamServiceException):
        await simboard_client.create_card(_draft())


# --- _build_card_description -------------------------------------------


def test_build_card_description_prefixes_bold_attribution():
    result = simboard_client._build_card_description(_draft(description="Users can't log in"))

    assert result == "**THIS TICKET IS CREATED BY SimBoard bot**\n\nUsers can't log in"


def test_build_card_description_attribution_only_when_no_description():
    result = simboard_client._build_card_description(_draft(description=None))

    assert result == "**THIS TICKET IS CREATED BY SimBoard bot**"


def test_build_card_description_attribution_only_when_description_blank():
    result = simboard_client._build_card_description(_draft(description=""))

    assert result == "**THIS TICKET IS CREATED BY SimBoard bot**"


@pytest.mark.asyncio
async def test_create_card_with_no_description_sends_attribution_only(monkeypatch):
    mock_client = AsyncMock()
    board_response = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={
                "included": {
                    "lists": [{"id": "l1", "name": "To Do"}],
                    "cards": [{"id": "c1", "listId": "l1", "position": 100}],
                }
            }
        ),
    )
    create_response = MagicMock(
        status_code=200, json=MagicMock(return_value={"item": {"id": "card-1"}})
    )
    mock_client.request.side_effect = [board_response, create_response]
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    monkeypatch.setattr(simboard_client.httpx, "AsyncClient", MagicMock(return_value=mock_client))

    await simboard_client.create_card(_draft(description=None))

    mock_client.request.assert_awaited_with(
        "POST",
        "https://simboard.test/api/lists/l1/cards",
        json={
            "name": "Fix login bug",
            "description": "**THIS TICKET IS CREATED BY SimBoard bot**",
            "type": "project",
            "position": 100 + 65535,
        },
        headers={"Authorization": "Bearer fake-jwt"},
    )
