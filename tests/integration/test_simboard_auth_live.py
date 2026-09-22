"""Live SimBoard login check for integrations.simboard_auth.

Real network call against the configured SimBoard instance — skipped
automatically when no bot service-account credentials are set (e.g. in CI),
so this only runs when `.env` (or the environment) has real
`BOT_SIMBOARD_BASE_URL`/`BOT_SIMBOARD_USERNAME`/`BOT_SIMBOARD_PASSWORD` set.
"""

import time

import pytest

from src.config import settings
from src.integrations import simboard_auth

pytestmark = pytest.mark.skipif(
    not settings.simboard_base_url
    or not settings.simboard_username
    or not settings.simboard_password,
    reason="SimBoard bot service-account credentials not configured",
)


@pytest.mark.asyncio
async def test_login_returns_a_usable_bearer_token():
    """A real login should succeed and yield a non-expired token."""
    simboard_auth._cached_token = None
    simboard_auth._cached_token_exp = 0.0

    token = await simboard_auth.get_access_token()

    assert isinstance(token, str)
    assert token.count(".") == 2  # well-formed JWT
    assert simboard_auth._cached_token_exp > time.time()


@pytest.mark.asyncio
async def test_second_call_reuses_the_cached_token():
    """A second call within the token's lifetime should not hit the network again."""
    simboard_auth._cached_token = None
    simboard_auth._cached_token_exp = 0.0

    first = await simboard_auth.get_access_token()
    second = await simboard_auth.get_access_token()

    assert first == second
