"""Cognito access-token checks and the /api prefix, with locally signed RS256 tokens."""

import time
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI

from kairos_api.auth import Authenticator, StripPrefix

ISSUER = "https://cognito-idp.ca-central-1.amazonaws.com/ca-central-1_TEST"
CLIENT = "client-123"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


async def public_key(token: str) -> Any:
    return KEY.public_key()


def token(*, key: Any = KEY, **overrides: Any) -> str:
    claims = {
        "iss": ISSUER,
        "client_id": CLIENT,
        "token_use": "access",
        "sub": "owner",
        "exp": int(time.time()) + 600,
    } | overrides
    return jwt.encode(claims, key, algorithm="RS256")


def app() -> FastAPI:
    auth = Authenticator(issuer=ISSUER, client_id=CLIENT, keys=public_key)
    api = FastAPI(dependencies=[Depends(auth)])

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/threads")
    async def threads() -> list[str]:
        return []

    api.add_middleware(StripPrefix, prefix="/api")
    return api


async def get(path: str, bearer: str | None = None) -> httpx2.Response:
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    transport = httpx2.ASGITransport(app=app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://kairos.test") as client:
        return await client.get(path, headers=headers)


async def test_a_valid_access_token_is_accepted_under_the_prefix() -> None:
    assert (await get("/api/threads", token())).status_code == 200
    assert (await get("/threads", token())).status_code == 200  # prefix optional


async def test_public_paths_need_no_token() -> None:
    assert (await get("/api/health")).status_code == 200


@pytest.mark.parametrize(
    ("bearer", "detail"),
    [
        (None, "sign in required"),
        (token(client_id="someone-else"), "token not issued for this app"),
        (token(token_use="id"), "token not issued for this app"),  # noqa: S106  # a claim, not a secret
        (token(exp=int(time.time()) - 5), "invalid or expired token"),
        (token(iss="https://evil.example"), "invalid or expired token"),
        (token(key=OTHER_KEY), "invalid or expired token"),
        ("not-a-jwt", "invalid or expired token"),
    ],
)
async def test_bad_or_missing_tokens_are_refused(bearer: str | None, detail: str) -> None:
    response = await get("/api/threads", bearer)
    assert response.status_code == 401
    assert response.json()["detail"] == detail
