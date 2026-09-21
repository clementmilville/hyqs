import pytest
from cryptography.fernet import Fernet

from hyqs.pipeline import crypto


def test_encrypt_str_then_decrypt_str_returns_original_plaintext(monkeypatch):
    monkeypatch.setenv("HYQS_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    ciphertext = crypto.encrypt_str("hello world")
    assert crypto.decrypt_str(ciphertext) == "hello world"


def test_decrypt_str_raises_on_tampered_ciphertext(monkeypatch):
    monkeypatch.setenv("HYQS_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    ciphertext = crypto.encrypt_str("hello world")
    tampered = ciphertext[:-1] + ("A" if ciphertext[-1] != "A" else "B")
    with pytest.raises(Exception):
        crypto.decrypt_str(tampered)


def test_encrypt_str_raises_runtime_error_when_key_unset(monkeypatch):
    monkeypatch.delenv("HYQS_CREDENTIAL_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="HYQS_CREDENTIAL_ENCRYPTION_KEY"):
        crypto.encrypt_str("hello world")


def test_decrypt_str_raises_runtime_error_when_key_unset(monkeypatch):
    monkeypatch.delenv("HYQS_CREDENTIAL_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="HYQS_CREDENTIAL_ENCRYPTION_KEY"):
        crypto.decrypt_str("some-ciphertext")


def test_import_succeeds_with_no_env_var_set(monkeypatch):
    monkeypatch.delenv("HYQS_CREDENTIAL_ENCRYPTION_KEY", raising=False)
    import importlib

    importlib.reload(crypto)
