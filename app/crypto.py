"""Encrypt/decrypt Google OAuth tokens before they touch the database."""
from cryptography.fernet import Fernet
from . import config

_fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        if not config.FERNET_KEY:
            raise RuntimeError(
                "FERNET_KEY is not set. Generate one with:\n"
                '  python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        _fernet = Fernet(config.FERNET_KEY.encode())
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()
