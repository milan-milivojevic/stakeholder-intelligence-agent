"""Cryptographic bearer-token primitive tests."""

from base64 import urlsafe_b64decode

from pydantic import SecretStr

from stakeholder_intelligence_agent.access import (
    TOKEN_ENTROPY_BYTES,
    generate_bearer_token,
    token_digest,
)


def test_generated_tokens_are_unique_redacted_and_have_256_bits_of_entropy() -> None:
    tokens = tuple(generate_bearer_token() for _ in range(256))
    raw_values = tuple(token.get_secret_value() for token in tokens)

    assert TOKEN_ENTROPY_BYTES == 32
    assert len(set(raw_values)) == len(raw_values)
    assert all(raw not in repr(token) for raw, token in zip(raw_values, tokens, strict=True))
    for raw in raw_values:
        padding = "=" * (-len(raw) % 4)
        assert len(urlsafe_b64decode(raw + padding)) == TOKEN_ENTROPY_BYTES


def test_token_digest_is_fixed_keyed_and_pepper_specific() -> None:
    synthetic_value = "synthetic-bearer-secret"
    first = token_digest(synthetic_value, SecretStr("a" * 32))
    second = token_digest(synthetic_value, SecretStr("b" * 32))

    assert len(first) == 64
    assert first != second
    assert first == token_digest(SecretStr(synthetic_value), SecretStr("a" * 32))
    assert synthetic_value not in first
