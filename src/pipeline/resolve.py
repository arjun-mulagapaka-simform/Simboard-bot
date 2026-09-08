"""ExtractionResult -> resolved ids for project/board/assignees.

Resolution is layered per ../../bot-docs/01-research-and-requirements.md
§4.2 / ../../bot-docs/06-agent-workflow.md §Step 4: alias table -> exact
(case-insensitive) name match -> fuzzy similarity (floor
`settings.fuzzy_match_floor`) -> LLM re-rank of the surviving fuzzy
candidates. A single fuzzy candidate at/above the floor resolves the field
outright; 2+ surviving candidates are handed to one LLM call
(`_llm_rerank_candidate`) that picks a winner if confident
(`settings.rerank_confidence_floor`) using the original message text as
context, otherwise the field stays unresolved and its names are surfaced
via `CardDraft.ambiguous_candidates` so
`pipeline.clarify.build_clarification_prompt` can ask a specific
"did you mean X or Y?" question instead.

Assignee resolution here matches `NormalizedMessage.other_mentions` (a
structural, deterministic signal from the Teams activity itself — no LLM
involved in detecting it) by display name against
simboard_client.list_users() (a name/email directory), NOT via Graph —
graph_client is intentionally excluded from Phase A per user decision.
"""

import logging
from difflib import SequenceMatcher

from openai import OpenAIError

from src.config import settings
from src.integrations import simboard_client
from src.integrations.azure_openai_client import call_with_retry, get_client
from src.models.card_draft import CardDraft
from src.models.extraction import ExtractionResult
from src.models.normalized_message import NormalizedMessage
from src.models.rerank import RerankResult

logger = logging.getLogger("resolve")

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
    (see module docstring) — the caller is expected to try
    `_llm_rerank_candidate` on them before giving up.

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
        survived the floor — otherwise it's `[]`.
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


_RERANK_SYSTEM_PROMPT = """A user referred to something by a short name in a \
Teams message, and more than one real record matched it closely enough to \
be ambiguous. First find "evidence": an exact word or phrase copied from \
the message, OTHER THAN the short name itself, that names or clearly \
points to one specific candidate over the others. The short name that \
caused the ambiguity is never valid evidence for resolving it — it matched \
multiple candidates precisely because it doesn't distinguish between them; \
restating it, or a substring of it, is not a new signal. If you cannot \
find a genuinely separate phrase like that, set "evidence" to an empty \
string and "chosen_name" to null — do not guess based on which candidate \
seems more common, more recent, or more likely in general. Only when \
"evidence" is a real phrase distinct from the short name should \
"chosen_name" name the candidate it points to. A wrong guess is worse \
than asking the user directly."""


async def _llm_rerank_candidate(hint: str, candidates: list[dict], context_text: str) -> str | None:
    """Ask the LLM to pick the intended candidate among 2+ fuzzy matches.

    Called only when 2+ candidates survive the fuzzy floor
    (bot-docs/06-agent-workflow.md §Step 4, resolution order (d)) — never
    on zero candidates, and never in place of an exact/alias match.

    Args:
        hint: The original free-text hint (hashtag or mention name) that
            produced these candidates.
        candidates: The 2+ fuzzy-surviving candidate dicts (`"id"`/`"name"`).
        context_text: The full original message text, given to the LLM as
            the only disambiguating context (no conversation history, per
            bot-docs §06 Step 3's prompt-contract precedent).

    Returns:
        The winning candidate's `"id"` if the LLM names one of the given
        candidates with confidence at/above `settings.rerank_confidence_floor`,
        else `None` — meaning the field stays ambiguous and should fall
        back to asking the user (see `pipeline.clarify`).

    Side effects:
        Makes an outbound LLM API call. Failures (timeout, rate limit,
        auth) are logged and treated as "couldn't decide" rather than
        raised — bot-docs §06 Step 4's failure-condition guidance is to
        not block the workflow on a slow/unavailable lookup, so this falls
        back to ambiguous instead of failing the whole resolve() call.
    """
    names = [c["name"] for c in candidates]
    user_content = (
        f'Message: "{context_text}"\n'
        f'The user referred to something like "{hint}".\n'
        f"Candidates: {', '.join(names)}"
    )

    try:
        completion = await call_with_retry(
            lambda: get_client().beta.chat.completions.parse(
                model=settings.azure_openai_deployment,
                messages=[
                    {"role": "system", "content": _RERANK_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                response_format=RerankResult,
                # gpt-5-mini is a reasoning model — hidden reasoning tokens draw
                # from this same budget before any visible output; 400 was
                # observed live to be exhausted by reasoning alone, returning
                # an empty/unparseable response (same failure mode fixed in
                # clarify.py's candidate-question call).
                max_completion_tokens=800,
            )
        )
    except OpenAIError as exc:
        logger.warning("rerank call failed, leaving field ambiguous: %s", exc)
        return None

    parsed = completion.choices[0].message.parsed
    if parsed is None or parsed.chosen_name is None or not parsed.evidence.strip():
        return None
    if parsed.confidence < settings.rerank_confidence_floor:
        return None

    # The ambiguous hint itself is never valid evidence — it matched every
    # surviving candidate, so restating it (or a shorter substring of it)
    # proves nothing about which one the user meant. Observed live: the
    # LLM cited evidence="billing-le" (the hint verbatim) to justify
    # picking "billing-legacy" with high confidence, with no other signal
    # in the message. Guard against this in code, not just the prompt.
    # (Evidence that's a *superset* of the hint, e.g. "sprint-43" for hint
    # "sprint-4", is a legitimate, more-specific signal and stays valid.)
    evidence_key = parsed.evidence.strip().lower()
    hint_key = hint.strip().lower()
    if evidence_key in hint_key:
        logger.warning(
            "rerank evidence %r is just the ambiguous hint %r restated, discarding pick",
            parsed.evidence,
            hint,
        )
        return None

    for candidate in candidates:
        if candidate["name"] == parsed.chosen_name:
            logger.info(
                "rerank chose %r (id=%s) from %s on evidence %r, confidence=%.2f",
                candidate["name"],
                candidate["id"],
                names,
                parsed.evidence,
                parsed.confidence,
            )
            return candidate["id"]
    return None


async def _resolve_with_rerank(
    hint: str | None, candidates: list[dict], aliases: dict[str, str], context_text: str
) -> tuple[str | None, list[str]]:
    """`_match_with_ambiguity` plus the LLM re-rank fallback on ambiguity.

    Returns:
        A `(id, ambiguous_names)` tuple — same shape as
        `_match_with_ambiguity`, except `ambiguous_names` (plain names, for
        `CardDraft.ambiguous_candidates`) is only non-empty when both the
        fuzzy layer AND the LLM re-rank failed to settle on one candidate.
    """
    matched_id, ambiguous = _match_with_ambiguity(hint, candidates, aliases)
    if matched_id is not None or not ambiguous:
        return matched_id, []

    reranked_id = await _llm_rerank_candidate(hint, ambiguous, context_text)
    if reranked_id is not None:
        return reranked_id, []

    return None, [c["name"] for c in ambiguous]


async def _resolve_project_and_board(
    extraction: ExtractionResult, message: NormalizedMessage
) -> tuple[str | None, str | None, list[str], dict[str, list[str]]]:
    """Resolve `project_hint`/`board_hint`, board scoped to the project.

    Returns:
        `(project_id, board_id, unresolved, ambiguous_candidates)` — see
        `resolve`'s docstring for what each means.
    """
    unresolved: list[str] = []
    ambiguous: dict[str, list[str]] = {}

    projects = await simboard_client.list_projects()
    project_id, project_candidates = await _resolve_with_rerank(
        extraction.project_hint.value, projects, _PROJECT_ALIASES, message.text
    )
    if project_id is None:
        unresolved.append("project_id")
        if project_candidates:
            ambiguous["project_id"] = project_candidates

    board_id = None
    if project_id is not None and extraction.board_hint.value:
        boards = await simboard_client.list_boards(project_id)
        board_id, board_candidates = await _resolve_with_rerank(
            extraction.board_hint.value, boards, _BOARD_ALIASES, message.text
        )
        if board_id is None:
            unresolved.append("board_id")
            if board_candidates:
                ambiguous["board_id"] = board_candidates

    return project_id, board_id, unresolved, ambiguous


async def _resolve_assignees(
    message: NormalizedMessage,
) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """Resolve `message.other_mentions` against the SimBoard user directory.

    Returns:
        `(assignee_user_ids, unresolved, ambiguous_candidates)` — see
        `resolve`'s docstring for what each means.
    """
    users = await simboard_client.list_users()
    assignee_user_ids: list[str] = []
    unresolved: list[str] = []
    ambiguous: dict[str, list[str]] = {}

    for mention in message.other_mentions:
        user_id, user_candidates = await _resolve_with_rerank(mention.name, users, {}, message.text)
        if user_id is None:
            field = f"assignee:{mention.name}"
            unresolved.append(field)
            if user_candidates:
                ambiguous[field] = user_candidates
        else:
            assignee_user_ids.append(user_id)

    return assignee_user_ids, unresolved, ambiguous


async def resolve(extraction: ExtractionResult, message: NormalizedMessage) -> CardDraft:
    """Turn an extraction result's free-text hints into resolved SimBoard ids.

    Covers project/board hashtags (from `extraction`) and assignees (from
    `message.other_mentions`, a structural signal, not LLM-derived — see
    module docstring). Resolution is layered per the module docstring:
    alias/exact match, then fuzzy similarity, then an LLM re-rank of 2+
    surviving fuzzy candidates.

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
        (stubbed with fixture data in Phase A).
    """
    project_id, board_id, project_board_unresolved, project_board_ambiguous = (
        await _resolve_project_and_board(extraction, message)
    )
    assignee_user_ids, assignee_unresolved, assignee_ambiguous = await _resolve_assignees(message)

    unresolved = project_board_unresolved + assignee_unresolved
    ambiguous_candidates = {**project_board_ambiguous, **assignee_ambiguous}

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
        title=extraction.title.value or "",
        description=extraction.description.value,
        card_type=extraction.card_type.value or "story",
        project_id=project_id,
        board_id=board_id,
        assignee_user_ids=assignee_user_ids,
        unresolved_fields=unresolved,
        pending_confirmation_fields=pending_confirmation,
        field_confidences=field_confidences,
        ambiguous_candidates=ambiguous_candidates,
    )


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


async def _apply_unresolved_answer(
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
            match = _match_by_name(reply.text, projects, _PROJECT_ALIASES)
            if match:
                draft.project_id = match
                resolved_one = True
                continue
        elif field == "board_id":
            boards = await simboard_client.list_boards(draft.project_id)
            match = _match_by_name(reply.text, boards, _BOARD_ALIASES)
            if match:
                draft.board_id = match
                resolved_one = True
                continue
        elif field == "title":
            draft.title = reply.text
            resolved_one = True
            continue
        elif field.startswith("assignee:"):
            users = await simboard_client.list_users()
            match = _match_by_name(reply.text, users, {})
            if match:
                draft.assignee_user_ids.append(match)
                resolved_one = True
                continue

        remaining.append(field)

    return resolved_one, remaining


async def apply_clarification(draft: CardDraft, reply: NormalizedMessage) -> CardDraft:
    """Re-attempt resolution of a pending draft's unresolved fields.

    Uses the user's clarification reply, in the same conversation as the
    original request. Phase A: treats the entire reply text as a single answer and tries it
    against every currently-unresolved field's alias table in turn; the
    first field it matches is resolved and removed from
    `unresolved_fields`, the rest remain pending. Multi-field clarification
    replies (answering more than one unresolved field in one message) are
    not yet supported.

    Also handles a yes/no answer to one of `draft.pending_confirmation_fields`
    (see `pipeline.confidence.needs_clarification` for how those get set) —
    a confirmation is checked first, ahead of `unresolved_fields`, since
    only one field total (confirmation or clarification) is resolved per
    reply, matching the existing single-field-per-reply model.

    Args:
        draft: The previously saved `CardDraft` for this workflow, with
            one or more entries in `unresolved_fields` and/or
            `pending_confirmation_fields`.
        reply: The new `NormalizedMessage` — the user's free-text answer
            to the clarification question built by
            `pipeline.clarify.build_clarification_prompt`.

    Returns:
        An updated `CardDraft` for the same `workflow_id`. At most one
        pending item (a confirmation or an unresolved field) is resolved
        by this reply; everything else stays pending. A "no" answer to a
        confirmation clears that field's resolved value and moves it into
        `unresolved_fields` instead, so the next reply re-asks it as an
        open question.

    Side effects:
        Calls `simboard_client.list_projects`/`list_boards`/`list_users`
        (stubbed with fixture data in Phase A).
    """
    answer = reply.text.strip().lower()

    resolved_one, remaining_confirmations, newly_unresolved = _apply_confirmation_answer(
        draft, answer
    )
    _, remaining_unresolved = await _apply_unresolved_answer(draft, reply, resolved_one)

    draft.pending_confirmation_fields = remaining_confirmations
    draft.unresolved_fields = newly_unresolved + remaining_unresolved
    return draft
