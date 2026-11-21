"""Webhook signature checks for GitHub HMAC and Origin Ed25519 deliveries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def identify(headers: Mapping[str, str]) -> str | None:
    lowered = {key.lower(): value for key, value in headers.items()}
    if "x-hub-signature-256" in lowered or "x-github-event" in lowered:
        return "github"
    signature = lowered.get("webhook-signature", "")
    agent = lowered.get("user-agent", "")
    if signature.startswith("v1ed,") or " v1ed," in f" {signature}" or agent.startswith("Cursor-Origin-Webhook/"):
        return "origin"
    return None


def verify_github(secret: str, body: bytes, header: str) -> bool:
    if not secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    given = header.removeprefix("sha256=").strip()
    if len(given) != len(expected):
        return False
    return hmac.compare_digest(given, expected)


def verify_origin(
    body: bytes,
    headers: Mapping[str, str],
    keys: list[Ed25519PublicKey],
    *,
    now: float,
) -> bool:
    delivery = headers.get("webhook-id", "")
    timestamp = headers.get("webhook-timestamp", "")
    signature = headers.get("webhook-signature", "")
    if not delivery or not timestamp or not signature or not keys:
        return False
    if len(delivery) > 200 or any(ord(ch) < 33 or ord(ch) > 126 for ch in delivery):
        return False
    try:
        issued = int(timestamp)
    except ValueError:
        return False
    if abs(now - issued) > 300:
        return False
    digest = hashlib.sha256(f"{delivery}.{timestamp}.".encode() + body).hexdigest().encode("utf-8")
    for part in signature.split():
        if not part.startswith("v1ed,"):
            continue
        try:
            raw = base64.b64decode(part[5:], validate=True)
        except ValueError:
            continue
        for key in keys:
            try:
                key.verify(raw, digest)
            except InvalidSignature:
                continue
            else:
                return True
    return False


def keys_from_jwks(document: bytes) -> list[Ed25519PublicKey]:
    try:
        loaded = json.loads(document)
    except json.JSONDecodeError as exc:
        raise ValueError("jwks is not json") from exc
    keys = loaded.get("keys") if isinstance(loaded, dict) else None
    if not isinstance(keys, list):
        raise ValueError("jwks has no keys")
    parsed: list[Ed25519PublicKey] = []
    for item in keys:
        if not isinstance(item, dict):
            continue
        if item.get("kty") != "OKP" or item.get("crv") != "Ed25519":
            continue
        material = item.get("x")
        if not isinstance(material, str):
            continue
        pad = "=" * (-len(material) % 4)
        try:
            raw = base64.urlsafe_b64decode(material + pad)
            parsed.append(Ed25519PublicKey.from_public_bytes(raw))
        except (ValueError, TypeError):
            continue
    if not parsed:
        raise ValueError("jwks has no ed25519 keys")
    return parsed
