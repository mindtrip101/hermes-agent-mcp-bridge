"""PKCE S256 (RFC 7636). The plain method is rejected by callers."""

from __future__ import annotations

import base64
import hashlib
import hmac

_VERIFIER_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def challenge_ok(challenge: str) -> bool:
    if len(challenge) != 43 or "=" in challenge:
        return False
    return all(ch in _VERIFIER_CHARS for ch in challenge)


def verifier_matches(verifier: str, challenge: str) -> bool:
    if not 43 <= len(verifier) <= 128:
        return False
    if any(ch not in _VERIFIER_CHARS for ch in verifier):
        return False
    digest = s256(verifier)
    if len(digest) != len(challenge):
        return False
    return hmac.compare_digest(digest, challenge)
