"""Live SimBoard reads/writes for integrations.simboard_client.

Real network calls against the configured SimBoard instance — skipped
automatically when no bot service-account credentials are set, same pattern
as `test_simboard_auth_live.py`.

Targets a fixed project/board ("Apollo Web Revamp" / "Sprint Board") rather
than `list_projects()[0]`/`list_boards()[0]` — now that the bot's service
account is `ADMIN` (needed so `list_users` returns email), it can see every
project/board in the instance, not just ones intentionally set up for bot
testing, so indexing `[0]` is no longer a stable target.

Note: `test_create_card_creates_a_real_card_in_the_todo_list` and
`test_create_card_and_assign_raj_kalpesh_trivedi` each create a real,
persistent card on the Sprint Board — tagged with a distinctive title so
they're easy to spot/delete afterward, not auto-cleaned up (no
`delete_card` client method exists yet).
"""

import uuid

import pytest

from src.config import settings
from src.integrations import simboard_client

pytestmark = pytest.mark.skipif(
    not settings.simboard_base_url
    or not settings.simboard_username
    or not settings.simboard_password,
    reason="SimBoard bot service-account credentials not configured",
)

_TEST_PROJECT_NAME = "Apollo Web Revamp"
_TEST_BOARD_NAME = "Sprint Board"


async def _get_test_project() -> dict | None:
    """The fixed project used for live testing, or None if not visible yet."""
    projects = await simboard_client.list_projects()
    return next((p for p in projects if p["name"] == _TEST_PROJECT_NAME), None)


async def _get_test_board(project_id: str) -> dict | None:
    """The fixed board used for live testing, or None if not visible yet."""
    boards = await simboard_client.list_boards(project_id)
    return next((b for b in boards if b["name"] == _TEST_BOARD_NAME), None)


@pytest.mark.asyncio
async def test_list_projects_returns_at_least_one_project():
    """The bot account should see at least the project(s) it's a member of."""
    projects = await simboard_client.list_projects()

    if not projects:
        pytest.skip("bot account not yet added to any project")

    assert all("id" in p and "name" in p for p in projects)


@pytest.mark.asyncio
async def test_list_boards_for_test_project_returns_shape():
    """Boards for the test project should map to {id, project_id, name}."""
    project = await _get_test_project()
    if project is None:
        pytest.skip(f"project {_TEST_PROJECT_NAME!r} not visible to the bot account")

    boards = await simboard_client.list_boards(project["id"])

    if not boards:
        pytest.skip("project has no boards yet")

    assert all({"id", "project_id", "name"} <= board.keys() for board in boards)


@pytest.mark.asyncio
async def test_list_users_for_test_board_returns_shape():
    """Users for the test board should map to {id, name, email}, board-scoped."""
    project = await _get_test_project()
    if project is None:
        pytest.skip(f"project {_TEST_PROJECT_NAME!r} not visible to the bot account")

    board = await _get_test_board(project["id"])
    if board is None:
        pytest.skip(f"board {_TEST_BOARD_NAME!r} not found in test project")

    users = await simboard_client.list_users(board["id"])

    if not users:
        pytest.skip("board has no members yet")

    assert all({"id", "name", "email"} <= user.keys() for user in users)


@pytest.mark.asyncio
async def test_list_users_returns_real_emails_now_that_bot_is_admin():
    """Confirms email is actually populated, not just present as a None key.

    `present-one.js` only fills `email` when the acting account is ADMIN —
    this checks the bot's service account really has that role live,
    rather than assuming it from provisioning notes.
    """
    project = await _get_test_project()
    if project is None:
        pytest.skip(f"project {_TEST_PROJECT_NAME!r} not visible to the bot account")

    board = await _get_test_board(project["id"])
    if board is None:
        pytest.skip(f"board {_TEST_BOARD_NAME!r} not found in test project")

    users = await simboard_client.list_users(board["id"])
    if not users:
        pytest.skip("board has no members yet")

    assert any(u.get("email") for u in users)


@pytest.mark.asyncio
async def test_get_todo_list_target_for_test_board_resolves_a_real_list():
    """The Todo list on the test board should resolve to a real list id + position."""
    project = await _get_test_project()
    if project is None:
        pytest.skip(f"project {_TEST_PROJECT_NAME!r} not visible to the bot account")

    board = await _get_test_board(project["id"])
    if board is None:
        pytest.skip(f"board {_TEST_BOARD_NAME!r} not found in test project")

    target = await simboard_client.get_todo_list_target(board["id"])

    assert isinstance(target["list_id"], str) and target["list_id"]
    assert isinstance(target["position"], int) and target["position"] > 0


@pytest.mark.asyncio
async def test_create_card_creates_a_real_card_in_the_todo_list():
    """A CardDraft with just title/description should create a real card."""
    from src.models.card_draft import CardDraft

    project = await _get_test_project()
    if project is None:
        pytest.skip(f"project {_TEST_PROJECT_NAME!r} not visible to the bot account")

    board = await _get_test_board(project["id"])
    if board is None:
        pytest.skip(f"board {_TEST_BOARD_NAME!r} not found in test project")

    marker = uuid.uuid4().hex[:8]
    draft = CardDraft(
        workflow_id=f"wf-live-{marker}",
        title=f"[integration-test] {marker}",
        description="Created by test_simboard_client_live.py — safe to delete.",
        card_type="project",
        project_id=project["id"],
        board_id=board["id"],
        assignee_user_ids=[],
        unresolved_fields=[],
    )

    result = await simboard_client.create_card(draft)

    assert result["status"] == "created"
    assert result["card_id"]
    assert result["workflow_id"] == draft.workflow_id


@pytest.mark.asyncio
async def test_create_card_and_assign_raj_kalpesh_trivedi():
    """A real card should be assignable to a real board member end-to-end.

    Exercises create_card -> create_card_membership, the same sequence
    `workflow.py` runs after a resolved draft clears the confidence gate.
    Skips if the named user isn't a member of the test board.
    """
    from src.models.card_draft import CardDraft

    project = await _get_test_project()
    if project is None:
        pytest.skip(f"project {_TEST_PROJECT_NAME!r} not visible to the bot account")

    board = await _get_test_board(project["id"])
    if board is None:
        pytest.skip(f"board {_TEST_BOARD_NAME!r} not found in test project")

    board_id = board["id"]
    users = await simboard_client.list_users(board_id)
    raj = next((u for u in users if u["name"] == "Raj Kalpesh Trivedi"), None)
    if raj is None:
        pytest.skip("Raj Kalpesh Trivedi is not a member of the test board")

    marker = uuid.uuid4().hex[:8]
    draft = CardDraft(
        workflow_id=f"wf-live-assign-{marker}",
        title=f"[integration-test] assign {marker}",
        description="Created by test_simboard_client_live.py — safe to delete.",
        card_type="project",
        project_id=project["id"],
        board_id=board_id,
        assignee_user_ids=[raj["id"]],
        unresolved_fields=[],
    )

    card_result = await simboard_client.create_card(draft)
    assert card_result["status"] == "created"

    membership = await simboard_client.create_card_membership(card_result["card_id"], raj["id"])

    assert membership["card_id"] == card_result["card_id"]
    assert membership["user_id"] == raj["id"]
