"""Short-lived RS256 access tokens. Embedded keys and remote jku headers are ignored."""

from __future__ import annotations

import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from hermes_agent_mcp_bridge import SCOPE

_FORBIDDEN_HEADER = frozenset({"jku", "jwk", "x5u", "x5c", "x5t", "x5t#s256", "crit", "enc"})


def encode_access_token(
    *,
    key: rsa.RSAPrivateKey,
    kid: str,
    issuer: str,
    subject: str,
    resource: str,
    jti: str,
    client_id: str,
    ttl: int,
    scope: str = SCOPE,
    now: int | None = None,
) -> str:
    issued = int(time.time()) if now is None else now
    claims = {
        "iss": issuer,
        "sub": subject,
        "aud": resource,
        "iat": issued,
        "nbf": issued,
        "exp": issued + ttl,
        "jti": jti,
        "scope": scope,
        "client_id": client_id,
    }
    encoded = jwt.encode(
        claims,
        key,
        algorithm="RS256",
        headers={"typ": "at+jwt", "kid": kid},
    )
    if isinstance(encoded, bytes):
        return encoded.decode("ascii")
    return encoded


def header_kid(token: str, *, expected_kid: str) -> None:
    header = jwt.get_unverified_header(token)
    if not isinstance(header, dict):
        raise jwt.InvalidTokenError("header")
    if header.get("alg") != "RS256":
        raise jwt.InvalidTokenError("alg")
    if _FORBIDDEN_HEADER.intersection(header):
        raise jwt.InvalidTokenError("header")
    typ = header.get("typ")
    if typ not in {None, "JWT", "at+jwt"}:
        raise jwt.InvalidTokenError("typ")
    if header.get("kid") != expected_kid:
        raise jwt.InvalidTokenError("kid")


def decode_access_token(
    token: str,
    *,
    key: rsa.RSAPrivateKey,
    kid: str,
    issuer: str,
    resource: str,
    clock_skew: int,
    max_ttl: int,
) -> dict[str, Any]:
    if len(token) > 8192:
        raise jwt.InvalidTokenError("size")
    header_kid(token, expected_kid=kid)
    claims = jwt.decode(
        token,
        key.public_key(),
        algorithms=["RS256"],
        audience=resource,
        issuer=issuer,
        leeway=clock_skew,
        options={
            "require": ["exp", "iat", "nbf", "iss", "aud", "sub", "jti", "scope", "client_id"],
            "verify_aud": True,
            "verify_iss": True,
            "verify_exp": True,
            "verify_nbf": True,
            "verify_iat": True,
        },
    )
    iat = _number(claims.get("iat"))
    exp = _number(claims.get("exp"))
    if iat is None or exp is None or exp <= iat or exp - iat > max_ttl + clock_skew:
        raise jwt.InvalidTokenError("ttl")
    scope = claims.get("scope")
    if not isinstance(scope, str) or SCOPE not in scope.split(" "):
        raise jwt.InvalidTokenError("scope")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti or len(jti) > 128:
        raise jwt.InvalidTokenError("jti")
    return claims


def _number(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)
