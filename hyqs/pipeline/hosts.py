"""Control-plane primitives for Epic 81 host enrollment (deterministic, no AI)."""

import base64
import logging

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)

from .models import Host
from .store import JobStore

log = logging.getLogger("hyqs.hosts")


def enroll_host(store: JobStore, name: str, public_key: str) -> Host:
    """Idempotently register a host's public key. Never rotates a key silently."""
    existing = store.get_host_by_name(name)
    if existing is not None:
        if existing.public_key == public_key:
            return existing
        raise ValueError(
            f"host {name!r} is already enrolled with a different public key; "
            "key rotation must go through a separate explicit path, not enroll_host()"
        )
    return store.create_host(name, public_key, status="active")


def generate_host_keypair() -> tuple[str, str]:
    """Generate a fresh ed25519 keypair. The private key is never persisted server-side."""
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


def sign_challenge(private_pem: str, nonce: str) -> str:
    """Sign ``nonce`` with a PEM-encoded ed25519 private key, base64-encoded."""
    private_key = load_pem_private_key(private_pem.encode(), password=None)
    signature = private_key.sign(nonce.encode())
    return base64.b64encode(signature).decode()


def resolve_worker_host_id(store: JobStore, host_name: str) -> int | None:
    """This worker's enrolled ``hosts.id``, or None for the local/default host.

    Unset ``host_name`` (today's single-host default) always resolves to None —
    unpinned, run-anywhere behavior. A non-empty name that isn't enrolled yet
    also resolves to None (logged) rather than raising, so a misconfigured or
    not-yet-enrolled worker degrades to "claims nothing host-pinned" instead of
    crashing the runner.
    """
    if not host_name:
        return None
    host = store.get_host_by_name(host_name)
    if host is None:
        log.warning("HYQS_HOST_NAME=%r is not an enrolled host; treating as unpinned", host_name)
        return None
    return host.id


def verify_host_signature(public_pem: str, nonce: str, signature: str) -> bool:
    """True if ``signature`` proves possession of the private key for ``public_pem``."""
    public_key = load_pem_public_key(public_pem.encode())
    try:
        public_key.verify(base64.b64decode(signature), nonce.encode())
    except InvalidSignature:
        return False
    return True
