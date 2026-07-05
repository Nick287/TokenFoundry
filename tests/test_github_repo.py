"""GitHub repo secret encryption tests — pure crypto, no network.

`encrypt_secret` is the only non-trivial pure function in app/services/
github_repo.py: it seals a secret with the repo's libsodium public key the way
GitHub's `PUT /actions/secrets/{name}` requires. We prove it round-trips (the
holder of the matching private key can recover the plaintext) and that it
produces valid base64 — the same guarantees GitHub relies on. No Azure, no
network, hermetic like test_billing.py.
"""

from __future__ import annotations

import base64

from nacl import public

from app.services.github_repo import encrypt_secret


def _keypair_pub_b64() -> tuple[public.PrivateKey, str]:
    """A fresh keypair; return (private_key, base64(public_key)) — the base64
    public key is exactly what GitHub's public-key endpoint returns."""
    sk = public.PrivateKey.generate()
    return sk, base64.b64encode(bytes(sk.public_key)).decode("utf-8")


def test_encrypt_secret_round_trips() -> None:
    sk, pk_b64 = _keypair_pub_b64()
    ciphertext_b64 = encrypt_secret(pk_b64, "s3cr3t-value")
    # The private-key holder (GitHub's server side) can decrypt it back.
    plaintext = public.SealedBox(sk).decrypt(base64.b64decode(ciphertext_b64))
    assert plaintext == b"s3cr3t-value"


def test_encrypt_secret_output_is_base64() -> None:
    _sk, pk_b64 = _keypair_pub_b64()
    out = encrypt_secret(pk_b64, "anything")
    # Valid base64 that decodes without error (GitHub rejects non-base64).
    assert base64.b64encode(base64.b64decode(out)).decode() == out


def test_encrypt_secret_is_nondeterministic() -> None:
    # Sealed boxes use an ephemeral keypair per call, so the same plaintext
    # encrypts to different ciphertext each time — a sanity check that we're
    # actually sealing, not just encoding.
    _sk, pk_b64 = _keypair_pub_b64()
    assert encrypt_secret(pk_b64, "same") != encrypt_secret(pk_b64, "same")


def test_encrypt_secret_handles_unicode() -> None:
    sk, pk_b64 = _keypair_pub_b64()
    value = "pÿ-nacl-✓-值"
    ciphertext_b64 = encrypt_secret(pk_b64, value)
    plaintext = public.SealedBox(sk).decrypt(base64.b64decode(ciphertext_b64))
    assert plaintext.decode("utf-8") == value
