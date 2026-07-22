"""Bearer-token generation, keyed lookup digests, and invitation encryption."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

if TYPE_CHECKING:
    from datetime import datetime

TOKEN_ENTROPY_BYTES = 32


@dataclass(frozen=True, slots=True)
class IssuedBearerToken:
    """One-time raw session secret with redacted string representation."""

    access_session_id: str
    token: SecretStr
    expires_at: datetime


def generate_bearer_token() -> SecretStr:
    """Generate a URL-safe bearer secret with 256 bits of entropy."""
    return SecretStr(secrets.token_urlsafe(TOKEN_ENTROPY_BYTES))


def generate_opaque_id(prefix: str) -> str:
    """Generate a non-semantic, collision-resistant local identifier."""
    return f"{prefix}_{secrets.token_hex(16)}"


def token_digest(token: str | SecretStr, pepper: SecretStr) -> str:
    """Return the keyed SHA-256 digest that is safe to persist and query."""
    token_value = token.get_secret_value() if isinstance(token, SecretStr) else token
    return hmac.new(
        pepper.get_secret_value().encode("utf-8"),
        token_value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def encrypt_invitation_token(token: str | SecretStr, pepper: SecretStr) -> str:
    """Encrypt a recoverable PM copy of an invitation token at rest.

    The token remains absent from domain models and list responses. The PM link
    endpoint decrypts it only after resolving the PM's engagement scope.
    """
    token_value = token.get_secret_value() if isinstance(token, SecretStr) else token
    return _invitation_cipher(pepper).encrypt(token_value.encode("utf-8")).decode("ascii")


def decrypt_invitation_token(ciphertext: str, pepper: SecretStr) -> SecretStr:
    """Decrypt one invitation token or fail closed if its ciphertext is invalid."""
    try:
        value = _invitation_cipher(pepper).decrypt(ciphertext.encode("ascii"))
    except (InvalidToken, ValueError, UnicodeError) as error:
        raise ValueError from error
    return SecretStr(value.decode("utf-8"))


def _invitation_cipher(pepper: SecretStr) -> Fernet:
    key = urlsafe_b64encode(hashlib.sha256(pepper.get_secret_value().encode("utf-8")).digest())
    return Fernet(key)
