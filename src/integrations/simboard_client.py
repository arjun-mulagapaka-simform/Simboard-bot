"""SimBoard-facing client.

`list_projects`/`list_boards`/`list_users`/`get_todo_list_target`/
`create_card`/`create_card_membership` are all wired to real SimBoard HTTP
endpoints (confirmed against `controllers/projects/index.js`,
`controllers/projects/show.js`, `controllers/boards/show.js`, and
`controllers/card-memberships/create.js` in simboard-repo; `create_card`
confirmed live against a real board). `create_card_membership` is called
by `workflow.py` after card creation for each of `CardDraft.assignee_user_ids`
— those ids are already email-resolved and board-scoped by
`pipeline.resolve._resolve_assignees`, so there's no separate
permission-check step here (that used to be a `verify_assignee_permission`
stub, retired 2026-09-16 once resolution itself became the verification).
"""

import logging

import httpx

from src.config import settings
from src.core.exceptions import NotFoundException, UpstreamServiceException
from src.integrations import simboard_auth
from src.models.card_draft import CardDraft

logger = logging.getLogger("simboard_client")


async def _request(
    method: str,
    path: str,
    *,
    json: dict | None = None,
    ok_status: int = 200,
    _retried_after_401: bool = False,
) -> dict:
    """Issue an authenticated HTTP request against the SimBoard API.

    Internal helper shared by `_get`/`_post` — handles attaching the bot's
    bearer token and translating transport/HTTP failures into this app's
    typed exceptions.

    Args:
        method: `"GET"` or `"POST"`.
        path: The request path including the leading `/api/...` segment,
            e.g. `"/api/projects"`. Joined onto `settings.simboard_base_url`.
        json: The request body for a `POST`, or `None` for a `GET`/a `POST`
            with no body.
        ok_status: The status code treated as success. `200` for every
            `GET`; SimBoard's `create.js` controllers also return `200`
            (not `201`) on a successful `POST`, so this defaults to `200`
            rather than needing every `POST` caller to override it.
        _retried_after_401: Internal only — set on the recursive call this
            function makes after a forced re-login, so a second `401` in a
            row raises instead of retrying forever. Callers should never
            pass this.

    Returns:
        The parsed JSON response body (a dict — every SimBoard `show`/
        `index`/`create` response is `{item|items, included: {...}}`).

    Raises:
        NotFoundException: on a `404` response.
        UpstreamServiceException: on a `401` that persists after one forced
            re-login, on any other non-`ok_status` response, or on a
            network-level failure (connection error, timeout, DNS, etc.).

    Side effects:
        One outbound HTTP call (two if the first comes back `401` — the
        cached token can be rejected before its own `exp` if it was
        revoked/rotated server-side, e.g. the session was deleted). Calls
        `simboard_auth.get_access_token()` first, which may itself trigger
        a login call if no valid cached token exists.
    """
    token = await simboard_auth.get_access_token()
    url = f"{settings.simboard_base_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.request(method, url, json=json, headers=headers)
    except httpx.HTTPError as exc:
        raise UpstreamServiceException(f"SimBoard request to {path} failed: {exc}") from exc

    if response.status_code == 401:
        if _retried_after_401:
            raise UpstreamServiceException(
                f"SimBoard request to {path} failed: 401 after a forced re-login"
            )
        logger.warning("simboard request to %s got 401, forcing re-login and retrying", path)
        await simboard_auth.get_access_token(force_refresh=True)
        return await _request(method, path, json=json, ok_status=ok_status, _retried_after_401=True)

    if response.status_code == 404:
        raise NotFoundException(f"SimBoard resource not found: {path}")
    if response.status_code != ok_status:
        raise UpstreamServiceException(
            f"SimBoard request to {path} failed: {response.status_code} {response.text}"
        )

    return response.json()


async def _get(path: str) -> dict:
    """Issue an authenticated GET against the SimBoard API. See `_request`."""
    return await _request("GET", path)


async def _post(path: str, json: dict) -> dict:
    """Issue an authenticated POST against the SimBoard API. See `_request`."""
    return await _request("POST", path, json=json)


async def list_projects() -> list[dict]:
    """Fetch all SimBoard projects available for resolution/lookup.

    Used by `pipeline.resolve.resolve` as the candidate pool when matching
    a message's project hashtag hint to a real SimBoard `project`.

    Calls `GET /api/projects` — returns every project the bot's service
    account can see (manager/shared/board-membership visibility, same
    rules as any other SimBoard user; see `controllers/projects/index.js`).

    Args:
        None.

    Returns:
        A list of dicts, each shaped `{"id": str, "name": str}` — `id` is
        the SimBoard project id to use as `CardDraft.project_id`, `name`
        is the human-readable name to fuzzy-match hashtags against. An
        empty list means the bot account has no visible projects (not
        necessarily an error).

    Raises:
        UpstreamServiceException: on an auth failure, non-2xx response, or
            network failure — see `_get`.

    Side effects:
        One outbound HTTP call (`GET /api/projects`), plus whatever
        `simboard_auth.get_access_token()` does (see its docstring).
    """
    data = await _get("/api/projects")
    return [{"id": p["id"], "name": p["name"]} for p in data.get("items", [])]


async def list_boards(project_id: str | None = None) -> list[dict]:
    """Fetch SimBoard boards, optionally scoped to one project.

    Used by `pipeline.resolve.resolve` as the candidate pool when matching
    a message's board/"bucket" hashtag hint. Note: whether "bucket" in the
    original design actually means SimBoard `board` or `list` is still an
    open question (see ../../simboard-dev-handover.md §3) — this function
    currently assumes `board`.

    When `project_id` is given, calls `GET /api/projects/:id` and reads
    `included.boards` (scoped to that one project — see
    `controllers/projects/show.js`). When omitted, calls `GET /api/projects`
    and reads `included.boards` across every visible project (see
    `controllers/projects/index.js`).

    Args:
        project_id: If given, restrict results to boards belonging to that
            project's SimBoard id (as returned by `list_projects`). If
            `None` (default), return boards across all projects.

    Returns:
        A list of dicts shaped `{"id": str, "project_id": str, "name":
        str}`. Returns an empty list if `project_id` is given but matches
        no known/visible project — this is not treated as an error, since
        `_get` already raises `NotFoundException` for a genuinely
        nonexistent project id.

    Raises:
        NotFoundException: if `project_id` is given but doesn't exist.
        UpstreamServiceException: on an auth failure, other non-2xx
            response, or network failure — see `_get`.

    Side effects:
        One outbound HTTP call (`GET /api/projects` or
        `GET /api/projects/:id`), plus whatever
        `simboard_auth.get_access_token()` does.
    """
    if project_id is None:
        data = await _get("/api/projects")
    else:
        data = await _get(f"/api/projects/{project_id}")

    boards = data.get("included", {}).get("boards", [])
    return [{"id": b["id"], "project_id": b["projectId"], "name": b["name"]} for b in boards]


async def list_users(board_id: str) -> list[dict]:
    """Fetch the SimBoard users who are members of one board.

    Used by `pipeline.resolve.resolve` for email/display-name matching of
    @mentions, scoped to the board the card is being created on — not a
    global user directory (SimBoard's own `GET /api/users` is admin/project-
    owner-only and the bot's service account isn't assumed to hold that
    role; see ../../simboard-api-findings.md). Calls `GET /api/boards/:id`
    and reads `included.users`, which is exactly the set of users with a
    `BoardMembership` on that board (plus any card creators — see
    `controllers/boards/show.js`).

    Args:
        board_id: The SimBoard board id (as returned by `list_boards`) to
            fetch members for. Required — there is no unscoped "all users"
            call available to a non-admin account.

    Returns:
        A list of dicts shaped `{"id": str, "name": str, "email": str |
        None}` — `id` is the SimBoard user id to place in
        `CardDraft.assignee_user_ids`, `name` is matched fuzzily against
        `NormalizedMessage.other_mentions[].name` (falls back to `username`
        if `name` is blank). `email` is only populated when the bot's
        service account has `ADMIN` role (`present-one.js`'s existing
        admin-gated behavior — see ../../simboard-api-findings.md); it's
        `None` for a non-admin service account, same as before this field
        was added. A Teams user with no matching entry here should be
        treated as unresolved (added to `CardDraft.unresolved_fields`), not
        silently dropped.

    Raises:
        NotFoundException: if `board_id` doesn't exist or isn't visible to
            the bot's service account.
        UpstreamServiceException: on an auth failure, other non-2xx
            response, or network failure — see `_get`.

    Side effects:
        One outbound HTTP call (`GET /api/boards/:id`), plus whatever
        `simboard_auth.get_access_token()` does.
    """
    data = await _get(f"/api/boards/{board_id}")
    users = data.get("included", {}).get("users", [])
    return [
        {"id": u["id"], "name": u.get("name") or u.get("username"), "email": u.get("email")}
        for u in users
    ]


_CARD_POSITION_GAP = 65535  # Planka/Trello convention — large gaps let a card be
# reordered between two others without renumbering the whole list.


async def get_todo_list_target(board_id: str) -> dict:
    """Resolve where a new card should land on a board: list id + position.

    Used by `create_card` to fill in the `listId` and `position` SimBoard's
    real card-creation endpoint both require (`POST /api/lists/:listId/
    cards` — cards belong to a list, not a board directly, and `position`
    is mandatory server-side even though the manual UI doesn't ask for it,
    confirmed live via `422 Position must be present`). A default SimBoard
    project ships 5 lists per board; new cards should land in the "To Do"
    one, matching what a human gets when creating a card manually (list
    defaults, only title required).

    Calls `GET /api/boards/:id` once and reads both `included.lists` (to
    pick the "To Do" list, case-insensitive; falls back to the
    lowest-`position` list if none is named "To Do" — better to create the
    card somewhere findable than fail outright, since list naming isn't
    guaranteed across every board) and `included.cards` (to compute a
    position that sorts after every existing card in that list).

    Args:
        board_id: The SimBoard board id (as returned by `list_boards`) to
            find the target list on.

    Returns:
        A dict shaped `{"list_id": str, "position": int}`. `position` is
        `max(existing card positions in that list) + 65535`, or `65535` if
        the list has no cards yet.

    Raises:
        NotFoundException: if `board_id` doesn't exist or isn't visible to
            the bot's service account.
        UpstreamServiceException: on an auth failure, other non-2xx
            response, or network failure — see `_get`.
        ValueError: if the board has no lists at all (shouldn't happen for
            a normally-created SimBoard board, but a caller should not
            silently create a card with no `listId`).

    Side effects:
        One outbound HTTP call (`GET /api/boards/:id`), plus whatever
        `simboard_auth.get_access_token()` does.
    """
    data = await _get(f"/api/boards/{board_id}")
    included = data.get("included", {})
    lists = included.get("lists", [])
    if not lists:
        raise ValueError(f"board {board_id} has no lists to create a card in")

    list_id = None
    for lst in lists:
        name = (lst.get("name") or "").strip().lower()
        if name == "to do":
            list_id = lst["id"]
            break
    if list_id is None:
        list_id = min(lists, key=lambda lst: lst.get("position") or 0)["id"]

    cards_in_list = [c for c in included.get("cards", []) if c.get("listId") == list_id]
    max_position = max((c.get("position") or 0 for c in cards_in_list), default=0)

    return {"list_id": list_id, "position": max_position + _CARD_POSITION_GAP}


async def create_card_membership(card_id: str, user_id: str) -> dict:
    """Assign a user to an already-created SimBoard card.

    Called by `workflow.py` after a successful `create_card`, once per id
    in `CardDraft.assignee_user_ids` (already email-resolved and
    board-scoped by `pipeline.resolve`).

    Calls `POST /api/cards/:cardId/card-memberships` (confirmed against
    `controllers/card-memberships/create.js` in simboard-repo). Assignment
    is a separate call from card creation — SimBoard has no `assigneeIds`
    field on the card itself, only a `CardMembership` join row per
    assignee.

    Args:
        card_id: The SimBoard card id (from `create_card`'s response) to
            assign to.
        user_id: The SimBoard user id (as resolved by
            `pipeline.resolve`/returned by `list_users`) to assign.

    Returns:
        A dict shaped `{"id": str, "card_id": str, "user_id": str, ...}` —
        the created `CardMembership` record (`item` unwrapped from the
        response envelope). Exact extra fields TBD until tested against a
        real instance; callers should only rely on `id` being present.

    Raises:
        NotFoundException: if `card_id` doesn't exist, or the bot's
            service account can't see it, or `user_id` doesn't exist.
        UpstreamServiceException: on a `403` (bot's service account isn't
            an `EDITOR` board member — see `controllers/card-memberships/
            create.js`), a `409` (`user_id` is already a member of the
            card, or SimBoard's Microsoft To-Do sync side-effect failed),
            an auth failure, or a network failure. All three of these
            (`403`/`409` variants) currently surface as the same generic
            `UpstreamServiceException` — `_request` doesn't yet
            distinguish response bodies beyond status code, so a caller
            that needs to tell them apart must inspect the exception
            message. Note: SimBoard also 404s (not 403) if `user_id` isn't
            a member of the card's board — same "not a board member"
            check already enforced by `list_users(board_id)` scoping
            assignee candidates, so this should only happen if the
            assignee became a non-member between resolution and this call.

    Side effects:
        One outbound HTTP call (via `_post`), creating a persistent
        `CardMembership` row and, per SimBoard's own side effects, may
        trigger a Microsoft To-Do sync.
    """
    data = await _post(f"/api/cards/{card_id}/card-memberships", json={"userId": user_id})
    membership = data["item"]
    return {
        "id": membership["id"],
        "card_id": membership.get("cardId", card_id),
        "user_id": membership.get("userId", user_id),
    }


def _build_card_description(draft: CardDraft) -> str:
    """Prefix the card body with a bold bot-attribution line.

    Replaces the originally-planned separate `POST /api/comments` call for
    attribution (see ../../tasks-list.md) — baked directly into the card's
    own `description` at creation time instead, so it's visible immediately
    with no second API call and no risk of the comment succeeding/failing
    independently of card creation. SimBoard descriptions render as
    Markdown (Planka lineage), so `**...**` renders bold in the UI.

    Args:
        draft: Supplies `draft.description`, the AI-extracted/user-provided
            body text, or `None`/empty if there wasn't one.

    Returns:
        `"**THIS TICKET IS CREATED BY SimBoard bot**"` on its own, or with
        `draft.description` appended after a blank line when present.
    """
    attribution = "**THIS TICKET IS CREATED BY SimBoard bot**"
    if not draft.description:
        return attribution
    return f"{attribution}\n\n{draft.description}"


async def create_card(draft: CardDraft) -> dict:
    """Create a card in SimBoard from a fully-resolved draft.

    Call this only after `pipeline.confidence.needs_clarification(draft)`
    is False — i.e. `draft.unresolved_fields` is empty. Calling it with
    unresolved fields is a caller error; this function does not validate
    that itself.

    Resolves the board's "To Do" list and next `position` via
    `get_todo_list_target` (SimBoard cards belong to a list, not a board
    directly), then calls `POST /api/lists/:listId/cards` with
    `name`/`description`/`type`/`position`. `type` and `position` are
    both required by the real API despite neither being asked for in the
    manual creation form — confirmed live: omitting `type` returns
    `400 E_MISSING_OR_INVALID_PARAMS`, omitting `position` returns
    `422 Position must be present`. `type` is always sent as `"project"`,
    ignoring `draft.card_type` — the bot only creates one kind of card for
    now. `position` is computed to sort after every existing card in the
    list (see `get_todo_list_target`).

    `description` is always prefixed with a bold attribution line (see
    `_build_card_description`) — this is the bot's on-behalf-of attribution
    mechanism, replacing the separate `POST /api/comments` call originally
    planned for it.

    Args:
        draft: A `CardDraft` with `board_id`, `title`, and (optionally)
            `description` resolved. `project_id`/`card_type`/
            `assignee_user_ids` are not used by this call directly.

    Returns:
        A dict shaped `{"status": "created", "card_id": str,
        "workflow_id": str}`. `card_id` is the newly created SimBoard
        card's id; `workflow_id` echoes `draft.workflow_id` so the caller
        can correlate the response back to the originating Teams message.

    Raises:
        NotFoundException: if `draft.board_id` doesn't exist or isn't
            visible to the bot's service account.
        UpstreamServiceException: on an auth failure, other non-2xx
            response, or network failure — see `_post`.
        ValueError: if `draft.board_id` has no lists at all — see
            `get_todo_list_target`.

    Side effects:
        Two outbound HTTP calls (`GET /api/boards/:id` via
        `get_todo_list_target`, then `POST /api/lists/:listId/cards`), plus
        whatever `simboard_auth.get_access_token()` does. Creates a
        persistent SimBoard card and may trigger SimBoard-side
        notifications/webhooks/activity log entries. Idempotency/retry-
        safety on the create call itself is not yet handled here — callers
        should check `state.idempotency_store` first (see `workflow.py`).
    """
    target = await get_todo_list_target(draft.board_id)
    data = await _post(
        f"/api/lists/{target['list_id']}/cards",
        json={
            "name": draft.title,
            "description": _build_card_description(draft),
            "type": "project",
            "position": target["position"],
        },
    )
    card_id = data["item"]["id"]
    return {"status": "created", "card_id": card_id, "workflow_id": draft.workflow_id}
