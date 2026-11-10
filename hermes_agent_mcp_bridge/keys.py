"""RSA signing key created on first run and stored with mode 0600."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from hermes_agent_mcp_bridge.errors import ConfigError


def load_or_create(path: Path) -> tuple[rsa.RSAPrivateKey, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise ConfigError("signing key must be mode 0600")
        raw = path.read_bytes()
        loaded = serialization.load_pem_private_key(raw, password=None)
        if not isinstance(loaded, rsa.RSAPrivateKey) or loaded.key_size < 2048:
            raise ConfigError("signing key must be an RSA key of at least 2048 bits")
        key = loaded
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, pem)
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    kid = hashlib.sha256(public).hexdigest()[:16]
    return key, kid


def jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64int(numbers.n),
        "e": _b64int(numbers.e),
    }


def _b64int(value: int) -> str:
    length = max(1, (value.bit_length() + 7) // 8)
    raw = value.to_bytes(length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
