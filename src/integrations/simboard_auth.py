"""Bot service-account auth against SimBoard's access-token endpoint.

The bot authenticates as a dedicated SimBoard service account (username/
password), not via an Entra token -> `identity_provider_user` mapping (see
tasks-list.md, "Today (2026-09-14)": auth model changed). Confirmed against
`server/api/controllers/access-tokens/create.js` in simboard-repo:

    POST {base_url}/api/access-tokens
    body: {"emailOrUsername": ..., "password": ...}
    -> 200 {"item": "<jwt>"}   (never a bearer-prefixed string, just the raw JWT)
    -> 401 on bad credentials

The JWT's own `exp` claim (seconds since epoch) is the source of truth for
token lifetime -- `sails.config.custom.tokenExpiresIn` days, baked in at
issuance -- so we decode it locally rather than hardcoding a TTL that could
drift from the server's real config.
"""

import base64
import json
import logging
import time

import httpx

from src.config import settings
from src.core.exceptions import UpstreamServiceException

logger = logging.getLogger("simboard_auth")

# Re-authenticate this many seconds before the token's real `exp`, so a
# request already in flight doesn't race a token that expires mid-call.
_EXPIRY_SKEW_SECONDS = 60

_cached_token: str | None = None
_cached_token_exp: float = 0.0


def _decode_jwt_exp(token: str) -> float:
    """Read the `exp` claim out of a JWT's payload segment, without verifying it.

    We only need `exp` to know when to re-authenticate; the token's
    authenticity is SimBoard's concern (it validates the signature on every
    request we make with it), so no signature check is done here.

    Args:
        token: The raw JWT string returned by `POST /api/access-tokens`.

    Returns:
        The `exp` claim as a Unix timestamp (seconds). Falls back to `0.0`
        (i.e. "already expired") if the token isn't a well-formed 3-segment
        JWT or the payload has no `exp` -- this forces a fresh login on the
        next call rather than caching a token indefinitely.
    """
    try:
        _header, payload_segment, _signature = token.split(".")
        padding = "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_segment + padding))
        return float(payload.get("exp", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return 0.0


async def _login() -> str:
    """Exchange the bot's service-account credentials for a fresh access token.

    Side effects:
        One outbound HTTP call to SimBoard's `POST /api/access-tokens`.

    Raises:
        UpstreamServiceException: on a network failure, a non-2xx response
            (e.g. `401` for bad credentials), or a `200` response missing
            the expected `item` field.
    """
    url = f"{settings.simboard_base_url.rstrip('/')}/api/access-tokens"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                json={
                    "emailOrUsername": settings.simboard_username,
                    "password": settings.simboard_password,
                },
            )
    except httpx.HTTPError as exc:
        raise UpstreamServiceException(f"SimBoard login request failed: {exc}") from exc

    if response.status_code != 200:
        raise UpstreamServiceException(
            f"SimBoard login failed: {response.status_code} {response.text}"
        )

    token = response.json().get("item")
    if not token:
        raise UpstreamServiceException("SimBoard login response missing 'item' token")

    return token


async def get_access_token(force_refresh: bool = False) -> str:
    """Return a valid bearer token for authenticating to SimBoard, logging in if needed.

    Call this from every `simboard_client.py` function right before making a
    request, instead of logging in per call -- the token is cached
    process-wide (module-level) and reused until it's within
    `_EXPIRY_SKEW_SECONDS` of its `exp` claim.

    Args:
        force_refresh: If True, skip the cache and log in unconditionally,
            even if the cached token still looks unexpired. For the case
            where a cached token was rejected with a `401` before its own
            `exp` (revoked/rotated server-side, or an out-of-band Session
            deletion) -- see `simboard_client._get`'s retry-on-401 logic,
            the only current caller of this. Defaults to False for the
            normal (cache-first) path.

    Returns:
        The raw JWT string to send as `Authorization: Bearer <token>`.

    Raises:
        UpstreamServiceException: propagated from `_login` if a fresh login
            is needed and fails.

    Side effects:
        Mutates module-level cache state (`_cached_token`,
        `_cached_token_exp`). Makes an outbound HTTP call whenever the
        cached token is missing, expired/near-expiring, or `force_refresh`
        is True.
    """
    global _cached_token, _cached_token_exp

    if (
        not force_refresh
        and _cached_token is not None
        and time.time() < _cached_token_exp - _EXPIRY_SKEW_SECONDS
    ):
        return _cached_token

    logger.info("simboard access token missing, expiring, or force-refreshed, logging in")
    token = await _login()
    _cached_token = token
    _cached_token_exp = _decode_jwt_exp(token)
    return token
