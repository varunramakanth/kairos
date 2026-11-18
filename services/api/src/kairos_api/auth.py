"""Authentication for the deployed API (ADR-017).

Locally the API is open on localhost. In AWS, the owner signs in with Amazon Cognito
(Hosted UI, authorization code with PKCE) and the web app sends the access token as
``Authorization: Bearer``. Every route except ``/health`` and ``/auth/config`` then
requires a valid token: signed by the user pool's key, issued by the pool, not
expired, an *access* token (``token_use``), and issued to this app's client.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient

PUBLIC_PATHS = frozenset({"/health", "/auth/config"})

type KeyResolver = Callable[[str], Awaitable[Any]]
"""Returns the public key that verifies a given token."""


def jwks_resolver(issuer: str) -> KeyResolver:
    client = PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True, lifespan=3600)

    async def resolve(token: str) -> Any:
        # PyJWKClient fetches and caches the key set with blocking I/O.
        signing_key = await asyncio.to_thread(client.get_signing_key_from_jwt, token)
        return signing_key.key

    return resolve


class Authenticator:
    def __init__(self, *, issuer: str, client_id: str, keys: KeyResolver | None = None) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self._keys = keys or jwks_resolver(self.issuer)

    async def verify(self, token: str) -> dict[str, Any]:
        try:
            key = await self._keys(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={"require": ["exp", "iss", "token_use"], "verify_aud": False},
            )
        except (jwt.PyJWTError, KeyError) as error:
            raise HTTPException(status_code=401, detail="invalid or expired token") from error
        if claims.get("token_use") != "access" or claims.get("client_id") != self.client_id:
            raise HTTPException(status_code=401, detail="token not issued for this app")
        return claims

    async def __call__(self, request: Request) -> None:
        """FastAPI dependency for every route."""
        if request.url.path in PUBLIC_PATHS or request.method == "OPTIONS":
            return
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(
                status_code=401,
                detail="sign in required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.user = await self.verify(token)


class StripPrefix:
    """ASGI middleware: serve the API under a path prefix (``/api`` behind CloudFront)
    without changing any route."""

    def __init__(self, app: Any, prefix: str) -> None:
        self.app = app
        self.prefix = prefix.rstrip("/")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            path: str = scope["path"]
            if path == self.prefix or path.startswith(self.prefix + "/"):
                scope = {**scope, "path": path[len(self.prefix) :] or "/"}
        await self.app(scope, receive, send)
