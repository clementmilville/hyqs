import uuid

import pytest

from hyqs.pipeline.hosts import (
    enroll_host,
    generate_host_keypair,
    resolve_worker_host_id,
    sign_challenge,
    verify_host_signature,
)


def _unique_name() -> str:
    return f"host-{uuid.uuid4().hex[:8]}"


def test_enroll_host_is_idempotent_for_same_key(store):
    name = _unique_name()
    _, public_pem = generate_host_keypair()

    first = enroll_host(store, name, public_pem)
    second = enroll_host(store, name, public_pem)

    assert first.id == second.id
    assert store.get_host(first.id).public_key == public_pem


def test_enroll_host_rejects_name_collision_with_different_key(store):
    name = _unique_name()
    _, public_pem_a = generate_host_keypair()
    _, public_pem_b = generate_host_keypair()

    enroll_host(store, name, public_pem_a)

    with pytest.raises(ValueError):
        enroll_host(store, name, public_pem_b)


def test_sign_challenge_and_verify_host_signature_round_trip():
    private_pem, public_pem = generate_host_keypair()
    nonce = "challenge-nonce"

    signature = sign_challenge(private_pem, nonce)

    assert verify_host_signature(public_pem, nonce, signature) is True


def test_verify_host_signature_returns_false_for_tampered_nonce():
    private_pem, public_pem = generate_host_keypair()
    signature = sign_challenge(private_pem, "original-nonce")

    assert verify_host_signature(public_pem, "tampered-nonce", signature) is False


def test_verify_host_signature_returns_false_for_wrong_public_key():
    private_pem, _ = generate_host_keypair()
    _, other_public_pem = generate_host_keypair()
    nonce = "challenge-nonce"

    signature = sign_challenge(private_pem, nonce)

    assert verify_host_signature(other_public_pem, nonce, signature) is False


def test_resolve_worker_host_id_returns_none_for_empty_name(store):
    assert resolve_worker_host_id(store, "") is None


def test_resolve_worker_host_id_returns_id_for_enrolled_host(store):
    name = _unique_name()
    host = store.create_host(name)

    assert resolve_worker_host_id(store, name) == host.id


def test_resolve_worker_host_id_returns_none_for_unknown_host(store):
    assert resolve_worker_host_id(store, "unknown-host") is None
