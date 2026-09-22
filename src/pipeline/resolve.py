"""ExtractionResult -> resolved ids for project/board/assignees.

Project/board resolution is alias table -> exact (case-insensitive) name
match only — no fuzzy matching (see git history around 2026-09-18: a
fuzzy match on a short/generic hint like "project-1" silently resolved to
an unrelated real project, with no confirmation step, since a single
fuzzy survivor was treated as unambiguous — a false positive worse than
just asking the user). A hint that doesn't match anything exactly is left
in `unresolved_fields` and asked about openly, never guessed at.

Assignee resolution here matches `NormalizedMessage.other_mentions` (a
structural, deterministic signal from the Teams activity itself — no LLM
involved in detecting it) by display name against
simboard_client.list_users() (a name/email directory), NOT via Graph —
graph_client is intentionally excluded from Phase A per user decision.
Fuzzy matching is still used for an assignee named in a clarification
reply (`_apply_unresolved_field_answer`'s `"assignee:"` branch) — the
2026-09-18 false-positive concern was specific to project/board, and an
assignee mismatch there still requires an explicit yes/no confirmation
afterward regardless (`CardDraft.pending_confirmation_fields`'s
`"assignee"` entry), unlike project/board.
"""

import logging
from difflib import SequenceMatcher

from openai import OpenAIError

from src.config import settings
from src.integrations import simboard_client
from src.integrations.azure_openai_client import call_with_retry, get_client
from src.models.card_draft import CardDraft
from src.models.extraction import ExtractionResult
from src.models.multi_field_reply import MultiFieldSplitResult
from src.models.normalized_message import NormalizedMessage

logger = logging.getLogger("resolve")

_YES_NO_STRIP = " \t\n,.!?;:@"


def _normalize_yes_no(text: str) -> str:
    """Strip leading/trailing punctuation and whitespace, lowercase.

    A clarification reply that re-@mentions the bot (needed since Teams'
    reply_to_id is unreliable — see workflow.py) leaves stray punctuation
    after `activity.strip_mentions_text()` removes the mention markup
    itself, e.g. `"@SimBoard, yes"` -> `", yes"`, not `"yes"`. Confirmed
    live 2026-09-21: without this, a "yes" reply never matched the exact
    `("yes", "y", "no", "n")` check and the same confirmation question was
    re-asked forever.
    """
    return text.strip(_YES_NO_STRIP).lower()


# Hardcoded for Phase A testing — maps a hashtag/mention alias to a fixture
# name in simboard_client. Real alias management (per-org, admin-configured)
# is out of scope until the fuzzy/reranker layer is built.
_PROJECT_ALIASES = {"onboarding": "onboarding", "billing": "billing"}
_BOARD_ALIASES = {"sprint-42": "sprint-42", "q3-launch": "q3-launch"}


def _fuzzy_candidates(key: str, candidates: list[dict], floor: float) -> list[dict]:
    """Candidates whose name similarity to `key` is at/above `floor`.

    Sorted best match first. Similarity is `difflib.SequenceMatcher.ratio()`
    on lowercased names — cheap and dependency-free; swap for an embedding
    model here if precision on short/abbreviated hints becomes a problem.
    """
    scored = [
        (candidate, SequenceMatcher(None, key, candidate["name"].lower()).ratio())
        for candidate in candidates
    ]
    return [
        c for c, score in sorted(scored, key=lambda pair: pair[1], reverse=True) if score >= floor
    ]


def _match_with_ambiguity(
    hint: str | None, candidates: list[dict], aliases: dict[str, str]
) -> tuple[str | None, list[dict]]:
    """Resolve a free-text hint to one candidate's id, surfacing ambiguity.

    Tries, in order: the alias table, an exact (case-insensitive) name
    match, then fuzzy similarity (`settings.fuzzy_match_floor`). A fuzzy
    match only resolves the field when exactly one candidate survives the
    floor; 2+ surviving candidates are ambiguous and left unresolved here
    (see module docstring). Only used for assignee-by-name matching today
    (`_match_by_name`'s callers) — project/board use the fuzzy-free
    `_match_exact` instead, see module docstring.

    Args:
        hint: The hashtag/mention text to resolve, or `None`/empty if no
            hint was extracted.
        candidates: Candidate dicts as returned by `simboard_client`, each
            with at least `"id"` and `"name"`.
        aliases: Alias table mapping a lowercased hint to a candidate's
            `name` (also matched case-insensitively).

    Returns:
        A `(id, ambiguous_candidates)` tuple. `id` is the matching
        candidate's `"id"` via alias/exact/unambiguous-fuzzy match, or
        `None` if `hint` is empty or nothing matched. `ambiguous_candidates`
        is non-empty only when `id` is `None` because 2+ fuzzy candidates
        survived the floor — otherwise it's `[]`, and discarded by
        `_match_by_name` (the only caller) either way.
    """
    if not hint:
        return None, []

    key = hint.strip().lower()
    target_name = aliases.get(key, key)

    for candidate in candidates:
        if candidate["name"].lower() == target_name:
            return candidate["id"], []

    fuzzy = _fuzzy_candidates(key, candidates, settings.fuzzy_match_floor)
    if len(fuzzy) == 1:
        return fuzzy[0]["id"], []
    if len(fuzzy) >= 2:
        return None, fuzzy

    return None, []


def _match_by_name(hint: str | None, candidates: list[dict], aliases: dict[str, str]) -> str | None:
    """Same as `_match_with_ambiguity`, discarding the ambiguous candidates.

    For call sites that only need the resolved id (or lack of one).
    """
    matched_id, _ = _match_with_ambiguity(hint, candidates, aliases)
    return matched_id


def _match_exact(hint: str | None, candidates: list[dict], aliases: dict[str, str]) -> str | None:
    """Resolve a free-text hint to one candidate's id.

    Alias or exact (case-insensitive) name match only, no fuzzy fallback.

    Used for project/board resolution (see module docstring for why fuzzy
    was dropped there specifically). Unlike `_match_with_ambiguity`, a
    non-match is always a flat miss — there's no ambiguous-candidates case
    to report, since only an exact string can match here.

    Args:
        hint: The hashtag text to resolve, or `None`/empty if no hint was
            extracted.
        candidates: Candidate dicts as returned by `simboard_client`, each
            with at least `"id"` and `"name"`.
        aliases: Alias table mapping a lowercased hint to a candidate's
            `name` (also matched case-insensitively).

    Returns:
        The matching candidate's `"id"`, or `None` if `hint` is empty or
        matches no candidate's name exactly (after alias substitution).
    """
    if not hint:
        return None

    key = hint.strip().lower()
    target_name = aliases.get(key, key)

    for candidate in candidates:
        if candidate["name"].lower() == target_name:
            return candidate["id"]

    return None


async def _resolve_project_and_board(
    extraction: ExtractionResult,
) -> tuple[str | None, str | None, list[str]]:
    """Resolve `project_hint`/`board_hint`, board scoped to the project.

    Alias/exact match only (`_match_exact`) — see module docstring for why
    fuzzy matching was dropped here.

    Returns:
        `(project_id, board_id, unresolved)` — see `resolve`'s docstring
        for what each means.
    """
    unresolved: list[str] = []

    projects = await simboard_client.list_projects()
    project_id = _match_exact(extraction.project_hint.value, projects, _PROJECT_ALIASES)
    if project_id is None:
        unresolved.append("project_id")

    board_id = None
    if project_id is not None and extraction.board_hint.value:
        boards = await simboard_client.list_boards(project_id)
        board_id = _match_exact(extraction.board_hint.value, boards, _BOARD_ALIASES)
        if board_id is None:
            unresolved.append("board_id")

    return project_id, board_id, unresolved


def _match_by_email(email: str | None, candidates: list[dict]) -> str | None:
    """Resolve a mention to a candidate's id by exact email match.

    Preferred over name matching whenever both sides actually have an
    email to compare — it's an exact key, not a fuzzy heuristic, so a hit
    here is unambiguous by construction (unlike `_match_with_ambiguity`,
    which can return 2+ surviving candidates for a name).

    Args:
        email: `MentionedUser.email` from the Teams mention — `None` today
            for every real Teams message (`Account` has no email field;
            see `pipeline.normalize`), until RSC/`TeamsChannelAccount`
            lands. Callers should always fall back to name matching when
            this is `None`, not treat it as "no assignee".
        candidates: Candidate dicts as returned by
            `simboard_client.list_users`, each with `"id"` and (now that
            the bot's service account is `ADMIN`) `"email"` — `None` for a
            non-admin service account or a user with no email on file.

    Returns:
        The matching candidate's `"id"`, or `None` if `email` is `None` or
        matches no candidate's `"email"` (case-insensitive comparison).
    """
    if not email:
        return None
    key = email.strip().lower()
    for candidate in candidates:
        candidate_email = candidate.get("email")
        if candidate_email and candidate_email.strip().lower() == key:
            return candidate["id"]
    return None


async def _resolve_assignees(
    message: NormalizedMessage, board_id: str | None
) -> tuple[list[str], list[str]]:
    """Resolve `message.other_mentions` against the SimBoard user directory.

    `simboard_client.list_users` requires a board id (SimBoard has no
    unscoped "all users" call for a non-admin account — see
    `simboard_client.list_users`'s docstring), so every mention is left
    unresolved when `board_id` is `None` (board not yet resolved) — there's
    no directory to match names against yet.

    Email-only (`_match_by_email`), by decision (2026-09-16): fuzzy/LLM
    name matching was found unreliable in practice (a real assignment
    attempt against a real board member failed to match by display name),
    so it's no longer used as a fallback here — a mention with no email or
    no email match is left unresolved (asked about via the clarify loop)
    rather than silently guessed at by name. This makes assignee
    resolution currently non-functional against real Teams messages,
    since Teams sends no mention email yet (`MentionedUser.email` is
    `None` until RSC/Track 2 lands — see tasks-list.md) — that's an
    accepted, intentional tradeoff, not a bug. Test via the Emulator by
    hand-injecting an `email` field into the mention payload (`Account`'s
    pydantic config allows extra fields) until Track 2 supplies it for
    real.

    Args:
        message: Supplies `message.other_mentions`, the assignee
            candidates.
        board_id: The already-resolved board id to scope the user
            directory to, or `None` if board resolution hasn't succeeded.

    Returns:
        `(assignee_user_ids, unresolved)` — see `resolve`'s docstring for
        what each means.
    """
    assignee_user_ids: list[str] = []
    unresolved: list[str] = []

    if board_id is None:
        return (
            assignee_user_ids,
            [f"assignee:{mention.name}" for mention in message.other_mentions],
        )

    users = await simboard_client.list_users(board_id)

    for mention in message.other_mentions:
        user_id = _match_by_email(mention.email, users)
        if user_id is not None:
            assignee_user_ids.append(user_id)
        else:
            unresolved.append(f"assignee:{mention.name}")

    return assignee_user_ids, unresolved


async def resolve_sender(message: NormalizedMessage, board_id: str) -> str | None:
    """Resolve the message sender to a SimBoard user id, by email only.

    Same standard as assignee resolution (2026-09-16 decision): both the
    person creating the card and the person it's assigned to are verified
    against SimBoard by email, not by trusting Teams display names. Used
    by `workflow.py` as a fail-closed gate before `create_card` — a sender
    who doesn't resolve is not a board member (or isn't verifiable yet)
    and the card is not created.

    Args:
        message: Supplies `message.sender.email`.
        board_id: The already-resolved board id to scope the user
            directory to. Unlike `_resolve_assignees`, callers must not
            call this before a board is resolved — there's no card to
            gate without one.

    Returns:
        The sender's SimBoard user id, or `None` if `message.sender.email`
        is unset or matches no board member. `None` today for every real
        Teams message — Teams sends no sender email yet (see
        `MentionedUser.email`'s docstring) — until RSC/Track 2 lands.

    Side effects:
        Calls `simboard_client.list_users(board_id)`.
    """
    users = await simboard_client.list_users(board_id)
    return _match_by_email(message.sender.email, users)


def _fallback_title(message: NormalizedMessage) -> str:
    """Synthesize a title when the LLM violates the "always return a title" contract.

    See `pipeline.extract`'s system prompt.

    Belt-and-suspenders only — the prompt already instructs the LLM to
    never leave title unset. Deliberately not routed through
    `unresolved_fields`/clarification: title should never actually be
    missing, so a fallback here avoids an unnecessary round-trip to the
    user for something that's supposed to be guaranteed upstream.

    Returns:
        The message text truncated to ~50 chars (plus an ellipsis if cut),
        or a generic placeholder if the message has no text at all.
    """
    text = message.text.strip()
    if not text:
        return "Untitled card"
    if len(text) <= 50:
        return text
    return f"{text[:50].rstrip()}…"


async def resolve(extraction: ExtractionResult, message: NormalizedMessage) -> CardDraft:
    """Turn an extraction result's free-text hints into resolved SimBoard ids.

    Covers project/board hashtags (from `extraction`) and assignees (from
    `message.other_mentions`, a structural signal, not LLM-derived — see
    module docstring). Project/board resolution is alias/exact match only
    (see module docstring for why fuzzy was dropped there).

    A board only exists inside a project, so board resolution is only
    attempted once a project has actually been resolved — an unresolved
    or missing project always blocks board lookup rather than searching
    boards across every project.

    Args:
        extraction: The `ExtractionResult` from `pipeline.extract.extract`.
        message: The originating `NormalizedMessage` — supplies
            `workflow_id` (stamped onto the returned `CardDraft`) and
            `other_mentions` (the assignee candidates).

    Returns:
        A `CardDraft` with `project_id`/`board_id`/`assignee_user_ids`
        populated wherever resolution succeeded uniquely. Any field that
        had no match — including a project hint that was never given at
        all — is instead listed by name in `unresolved_fields` (its
        resolved-id field is left `None`/empty), which
        `pipeline.confidence.needs_clarification` checks.

    Side effects:
        Calls `simboard_client.list_projects`/`list_boards`/`list_users`
        (real SimBoard HTTP calls, confirmed live).
    """
    project_id, board_id, project_board_unresolved = await _resolve_project_and_board(extraction)
    assignee_user_ids, assignee_unresolved = await _resolve_assignees(message, board_id)

    unresolved = project_board_unresolved + assignee_unresolved

    pending_confirmation: list[str] = []
    if assignee_user_ids:
        # Always require confirmation once an assignee resolves, regardless
        # of match confidence — a defense against prompt injection, not a
        # low-confidence heuristic (bot-docs/05 §6.4).
        pending_confirmation.append("assignee")

    field_confidences: dict[str, float] = {}
    if extraction.title.value:
        field_confidences["title"] = extraction.title.confidence
    if project_id is not None:
        field_confidences["project_id"] = extraction.project_hint.confidence
    if board_id is not None:
        field_confidences["board_id"] = extraction.board_hint.confidence

    return CardDraft(
        workflow_id=message.workflow_id,
        original_text=message.text,
        title=extraction.title.value or _fallback_title(message),
        description=extraction.description.value,
        card_type=extraction.card_type.value or "story",
        project_id=project_id,
        board_id=board_id,
        assignee_user_ids=assignee_user_ids,
        unresolved_fields=unresolved,
        pending_confirmation_fields=pending_confirmation,
        field_confidences=field_confidences,
    )


_MULTI_FIELD_SPLIT_SYSTEM_PROMPT = """A Teams bot asked the user to clarify \
several pending details about a task in one message (a numbered/bulleted \
list of questions). The user replied with a single free-text message that \
may answer some or all of them, in any order, possibly combined in one \
sentence. Given the pending field keys (each with a short description of \
what it's asking), the original task request for background context, and \
the user's reply, extract one entry per pending field the reply CLEARLY \
answers: the field key copied verbatim from the given list, and the exact \
snippet of the REPLY (not the original request) that answers it. Omit any \
field the reply doesn't address — do not guess, and never invent a field \
key that wasn't given to you."""

_FIELD_DESCRIPTIONS = {
    "project_id": "which project the task belongs to",
    "board_id": "which board the task belongs to",
    "title": "what the task's title should be",
    "assignee": "yes/no — is the already-matched assignee correct",
}


def _describe_field(field: str) -> str:
    """Short human-readable description of a pending field key, for the splitter LLM prompt.

    Handles the dynamic `"assignee:{name}"` form the same way
    `clarify.build_clarification_prompt` does.
    """
    if field.startswith("assignee:"):
        name = field.split(":", 1)[1]
        return f'who should be assigned (no SimBoard user matched "{name}")'
    return _FIELD_DESCRIPTIONS.get(field, field)


async def _split_multi_field_reply(
    pending_fields: list[str], reply_text: str, original_text: str
) -> dict[str, str]:
    """Split one clarification reply into per-field answer snippets via an LLM call.

    Called only when 2+ items are pending (see `apply_clarification`) —
    never for a single pending field, where the whole reply is already
    unambiguously "the answer" and no split is needed.

    Args:
        pending_fields: `draft.pending_confirmation_fields +
            draft.unresolved_fields` — the field keys the reply might
            answer. Each is described to the LLM via `_describe_field`.
        reply_text: The user's clarification reply (`NormalizedMessage.text`).
        original_text: The originating request's text
            (`CardDraft.original_text`), given as background context only —
            the LLM is told to pull answer snippets from `reply_text`, not
            this.

    Returns:
        A `{field: answer_snippet}` dict, containing only the subset of
        `pending_fields` the LLM found a clear answer for in `reply_text` —
        empty if it found none, or if the call failed. A key not in
        `pending_fields` is dropped defensively (the LLM is instructed not
        to invent one, but callers shouldn't trust that blindly).

    Side effects:
        Makes an outbound LLM API call. Failures (timeout, rate limit,
        auth, empty parse) are logged and treated as "couldn't split" —
        `apply_clarification` falls back to the single-field whole-text
        path rather than blocking the workflow on this call.
    """
    field_list = "\n".join(f"- {field}: {_describe_field(field)}" for field in pending_fields)
    user_content = (
        f'Original request: "{original_text}"\n'
        f"Pending fields:\n{field_list}\n"
        f'User\'s reply: "{reply_text}"'
    )

    try:
        completion = await call_with_retry(
            lambda: get_client().beta.chat.completions.parse(
                model=settings.azure_openai_deployment,
                messages=[
                    {"role": "system", "content": _MULTI_FIELD_SPLIT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                response_format=MultiFieldSplitResult,
                max_completion_tokens=800,
            )
        )
    except OpenAIError as exc:
        logger.warning("multi-field split call failed, falling back to single-field: %s", exc)
        return {}

    parsed = completion.choices[0].message.parsed
    if parsed is None:
        return {}

    pending_set = set(pending_fields)
    result = {a.field: a.answer for a in parsed.answers if a.field in pending_set}
    # Debug-only: dropped/kept split answers — settles whether a
    # multi-field reply silently failing is the LLM not attributing a
    # field, or the downstream exact/email match rejecting the attributed
    # snippet (see tasks-list.md's 2026-09-18 assignee/board-dependency
    # note for a known separate cause).
    logger.info(
        "split reply=%r pending=%s -> kept=%s dropped=%s",
        reply_text,
        pending_fields,
        result,
        [a.field for a in parsed.answers if a.field not in pending_set],
    )
    return result


def _apply_confirmation_answer(draft: CardDraft, answer: str) -> tuple[bool, list[str], list[str]]:
    """Consume a yes/no answer against `draft.pending_confirmation_fields`.

    At most the first confirmation field is resolved (see `apply_clarification`
    docstring for the single-item-per-reply model); the rest are carried
    forward unchanged in the returned `remaining_confirmations`.

    Returns:
        `(resolved_one, remaining_confirmations, newly_unresolved)` —
        `newly_unresolved` holds the field name if a "no" answer cleared a
        previously-resolved value, so the caller can re-ask it openly.
    """
    resolved_one = False
    remaining_confirmations: list[str] = []
    newly_unresolved: list[str] = []

    for field in draft.pending_confirmation_fields:
        if resolved_one or answer not in ("yes", "y", "no", "n"):
            remaining_confirmations.append(field)
            continue

        draft.field_confidences.pop(field, None)
        if answer in ("yes", "y"):
            resolved_one = True
            continue

        # "no": the resolved value was wrong — clear it and re-ask openly.
        if field == "assignee":
            draft.assignee_user_ids = []
        elif field == "project_id":
            draft.project_id = None
        elif field == "board_id":
            draft.board_id = None
        elif field == "title":
            draft.title = ""
        newly_unresolved.append(field)
        resolved_one = True

    return resolved_one, remaining_confirmations, newly_unresolved


async def _apply_unresolved_answer(  # noqa: C901
    draft: CardDraft, reply: NormalizedMessage, already_resolved: bool
) -> tuple[bool, list[str]]:
    """Try `reply.text` against each of `draft.unresolved_fields` in turn.

    Stops at the first match (or immediately, if `already_resolved` is
    already True from a confirmation answer this same reply) — see
    `apply_clarification` docstring for the single-item-per-reply model.

    Returns:
        `(resolved_one, remaining)` — `remaining` is the updated
        `unresolved_fields` list.
    """
    resolved_one = already_resolved
    remaining: list[str] = []

    for field in draft.unresolved_fields:
        if resolved_one:
            remaining.append(field)
            continue

        if field == "project_id":
            projects = await simboard_client.list_projects()
            match = _match_exact(reply.text, projects, _PROJECT_ALIASES)
            if match:
                draft.project_id = match
                resolved_one = True
                continue
        elif field == "board_id":
            boards = await simboard_client.list_boards(draft.project_id)
            match = _match_exact(reply.text, boards, _BOARD_ALIASES)
            if match:
                draft.board_id = match
                resolved_one = True
                continue
        elif field == "title":
            draft.title = reply.text
            resolved_one = True
            continue
        elif field.startswith("assignee:"):
            if draft.board_id is not None:
                users = await simboard_client.list_users(draft.board_id)
                match = _match_by_name(reply.text, users, {})
                if match:
                    draft.assignee_user_ids.append(match)
                    resolved_one = True
                    continue

        remaining.append(field)

    return resolved_one, remaining


def _apply_confirmation_field_answer(
    draft: CardDraft, field: str, answer: str
) -> tuple[bool, str | None]:
    """Try one yes/no answer snippet against exactly one confirmation field.

    Unlike `_apply_confirmation_answer` (which scans every pending
    confirmation field for the first one an answer resolves), this trusts
    the caller to have already matched `field` to `answer` — used by the
    multi-field path, where `_split_multi_field_reply` has already done
    that matching.

    Returns:
        `(resolved, newly_unresolved_field)` — `resolved` is False if
        `answer` isn't recognized as yes/no (field stays pending);
        `newly_unresolved_field` is `field` itself if a "no" answer
        cleared its resolved value, else `None`.
    """
    normalized = _normalize_yes_no(answer)
    if normalized not in ("yes", "y", "no", "n"):
        return False, None

    draft.field_confidences.pop(field, None)
    if normalized in ("yes", "y"):
        return True, None

    if field == "assignee":
        draft.assignee_user_ids = []
    elif field == "project_id":
        draft.project_id = None
    elif field == "board_id":
        draft.board_id = None
    elif field == "title":
        draft.title = ""
    return True, field


async def _apply_unresolved_field_answer(draft: CardDraft, field: str, answer: str) -> bool:
    """Try one answer snippet against exactly one unresolved field.

    Same per-field split as `_apply_confirmation_field_answer`, for the
    `unresolved_fields` side — used by the multi-field path once
    `_split_multi_field_reply` has already attributed `answer` to `field`.

    Returns:
        True if `field` resolved (its value is set directly on `draft`),
        False if `answer` didn't match anything for it (field stays
        pending, unchanged).
    """
    if field == "project_id":
        projects = await simboard_client.list_projects()
        match = _match_exact(answer, projects, _PROJECT_ALIASES)
        # Debug-only: an exact-only match against a free-text snippet is
        # the likeliest silent-failure point — log what it was compared
        # against.
        logger.info(
            "project_id exact-match answer=%r against names=%s -> %s",
            answer,
            [p["name"] for p in projects],
            match,
        )
        if match:
            draft.project_id = match
            return True
    elif field == "board_id":
        boards = await simboard_client.list_boards(draft.project_id)
        match = _match_exact(answer, boards, _BOARD_ALIASES)
        logger.info(
            "board_id exact-match answer=%r against names=%s -> %s",
            answer,
            [b["name"] for b in boards],
            match,
        )
        if match:
            draft.board_id = match
            return True
    elif field == "title":
        draft.title = answer
        return True
    elif field.startswith("assignee:"):
        if draft.board_id is not None:
            users = await simboard_client.list_users(draft.board_id)
            match = _match_by_name(answer, users, {})
            if match:
                draft.assignee_user_ids.append(match)
                return True

    return False


async def apply_clarification(draft: CardDraft, reply: NormalizedMessage) -> CardDraft:
    """Re-attempt resolution of a pending draft's unresolved/confirmation fields.

    Uses the user's clarification reply, in the same conversation as the
    original request. Two paths depending on how many items are pending
    (`draft.pending_confirmation_fields + draft.unresolved_fields`):

    - **Exactly one pending item**: the whole reply text is tried directly
      against it (no LLM call) — this is the original Phase A behavior,
      kept as the cheap common case.
    - **Two or more pending items**: `_split_multi_field_reply` (one LLM
      call) attempts to split the reply into a snippet per field it
      clearly answers, using `draft.original_text` as background context.
      Each returned `(field, answer)` pair is resolved independently via
      `_apply_confirmation_field_answer`/`_apply_unresolved_field_answer`,
      so a single reply can clear multiple pending items in one turn —
      whatever the split call didn't confidently attribute stays pending.
      If the split call fails or returns nothing, falls back to the
      single-item behavior (whole reply text vs. the first pending item
      only), so this path degrades gracefully rather than silently doing
      nothing.

    Args:
        draft: The previously saved `CardDraft` for this workflow, with
            one or more entries in `unresolved_fields` and/or
            `pending_confirmation_fields`.
        reply: The new `NormalizedMessage` — the user's free-text answer
            to the clarification question built by
            `pipeline.clarify.build_clarification_prompt`.

    Returns:
        An updated `CardDraft` for the same `workflow_id`. Any number of
        pending items may be resolved by this reply; everything else
        stays pending. A "no" answer to a confirmation clears that
        field's resolved value and moves it into `unresolved_fields`
        instead, so the next reply re-asks it as an open question.

    Side effects:
        Calls `simboard_client.list_projects`/`list_boards`/`list_users`
        (real SimBoard HTTP calls, confirmed live), and — only when 2+
        items are pending — one Azure OpenAI call via
        `_split_multi_field_reply`.
    """
    pending_fields = draft.pending_confirmation_fields + draft.unresolved_fields

    if len(pending_fields) <= 1:
        answer = _normalize_yes_no(reply.text)
        resolved_one, remaining_confirmations, newly_unresolved = _apply_confirmation_answer(
            draft, answer
        )
        _, remaining_unresolved = await _apply_unresolved_answer(draft, reply, resolved_one)
        draft.pending_confirmation_fields = remaining_confirmations
        draft.unresolved_fields = newly_unresolved + remaining_unresolved
        return draft

    field_answers = await _split_multi_field_reply(pending_fields, reply.text, draft.original_text)

    if not field_answers:
        answer = _normalize_yes_no(reply.text)
        resolved_one, remaining_confirmations, newly_unresolved = _apply_confirmation_answer(
            draft, answer
        )
        _, remaining_unresolved = await _apply_unresolved_answer(draft, reply, resolved_one)
        draft.pending_confirmation_fields = remaining_confirmations
        draft.unresolved_fields = newly_unresolved + remaining_unresolved
        return draft

    remaining_confirmations = list(draft.pending_confirmation_fields)
    remaining_unresolved = list(draft.unresolved_fields)
    newly_unresolved: list[str] = []

    for field, answer in field_answers.items():
        if field in remaining_confirmations:
            resolved, cleared_field = _apply_confirmation_field_answer(draft, field, answer)
            if resolved:
                remaining_confirmations.remove(field)
                if cleared_field is not None:
                    newly_unresolved.append(cleared_field)
        elif field in remaining_unresolved:
            resolved = await _apply_unresolved_field_answer(draft, field, answer)
            if resolved:
                remaining_unresolved.remove(field)

    draft.pending_confirmation_fields = remaining_confirmations
    draft.unresolved_fields = newly_unresolved + remaining_unresolved
    return draft
