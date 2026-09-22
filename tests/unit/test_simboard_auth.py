"""Unit tests for integrations.simboard_auth — mocked HTTP, no network call."""

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.core.exceptions import UpstreamServiceException
from src.integrations import simboard_auth


def _jwt_with_exp(exp: float) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.fake-signature"


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.setattr(simboard_auth, "_cached_token", None)
    monkeypatch.setattr(simboard_auth, "_cached_token_exp", 0.0)
    monkeypatch.setattr(simboard_auth.settings, "simboard_base_url", "https://simboard.test")
    monkeypatch.setattr(simboard_auth.settings, "simboard_username", "bot")
    monkeypatch.setattr(simboard_auth.settings, "simboard_password", "secret")


def _mock_client(monkeypatch, *, status_code: int = 200, json_body: dict | None = None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body if json_body is not None else {}
    response.text = json.dumps(json_body) if json_body is not None else ""

    mock_client = AsyncMock()
    mock_client.post.return_value = response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False

    monkeypatch.setattr(simboard_auth.httpx, "AsyncClient", MagicMock(return_value=mock_client))
    return mock_client


@pytest.mark.asyncio
async def test_get_access_token_logs_in_and_returns_token(monkeypatch):
    token = _jwt_with_exp(time.time() + 3600)
    mock_client = _mock_client(monkeypatch, json_body={"item": token})

    result = await simboard_auth.get_access_token()

    assert result == token
    mock_client.post.assert_awaited_once_with(
        "https://simboard.test/api/access-tokens",
        json={"emailOrUsername": "bot", "password": "secret"},
    )


@pytest.mark.asyncio
async def test_get_access_token_caches_and_skips_second_login(monkeypatch):
    token = _jwt_with_exp(time.time() + 3600)
    mock_client = _mock_client(monkeypatch, json_body={"item": token})

    first = await simboard_auth.get_access_token()
    second = await simboard_auth.get_access_token()

    assert first == second == token
    mock_client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_access_token_reauthenticates_when_near_expiry(monkeypatch):
    almost_expired = _jwt_with_exp(time.time() + 10)  # within the 60s skew window
    fresh = _jwt_with_exp(time.time() + 3600)

    response1 = MagicMock(status_code=200, json=MagicMock(return_value={"item": almost_expired}))
    response2 = MagicMock(status_code=200, json=MagicMock(return_value={"item": fresh}))

    mock_client = AsyncMock()
    mock_client.post.side_effect = [response1, response2]
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    monkeypatch.setattr(simboard_auth.httpx, "AsyncClient", MagicMock(return_value=mock_client))

    first = await simboard_auth.get_access_token()
    second = await simboard_auth.get_access_token()

    assert first == almost_expired
    assert second == fresh
    assert mock_client.post.await_count == 2


@pytest.mark.asyncio
async def test_get_access_token_raises_on_bad_credentials(monkeypatch):
    _mock_client(
        monkeypatch, status_code=401, json_body={"invalidCredentials": "Invalid credentials"}
    )

    with pytest.raises(UpstreamServiceException):
        await simboard_auth.get_access_token()


@pytest.mark.asyncio
async def test_get_access_token_raises_on_missing_item_field(monkeypatch):
    _mock_client(monkeypatch, status_code=200, json_body={})

    with pytest.raises(UpstreamServiceException):
        await simboard_auth.get_access_token()


@pytest.mark.asyncio
async def test_get_access_token_raises_on_network_error(monkeypatch):
    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ConnectError("boom")
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    monkeypatch.setattr(simboard_auth.httpx, "AsyncClient", MagicMock(return_value=mock_client))

    with pytest.raises(UpstreamServiceException):
        await simboard_auth.get_access_token()
