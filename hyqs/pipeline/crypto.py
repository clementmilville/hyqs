import os

from cryptography.fernet import Fernet

_ENV_VAR = "HYQS_CREDENTIAL_ENCRYPTION_KEY"


def _load_fernet() -> Fernet:
    key = os.environ.get(_ENV_VAR, "")
    if not key:
        raise RuntimeError(
            f"{_ENV_VAR} is not set. Set it to a valid Fernet key "
            "(e.g. Fernet.generate_key()) before encrypting or decrypting credentials."
        )
    return Fernet(key.encode())


def encrypt_str(plaintext: str) -> str:
    return _load_fernet().encrypt(plaintext.encode()).decode()


def decrypt_str(ciphertext: str) -> str:
    return _load_fernet().decrypt(ciphertext.encode()).decode()
