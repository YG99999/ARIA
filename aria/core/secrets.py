"""Fernet encryption wrapper for the credential store.

Master key is generated once at setup, stored at data/.secrets_key (chmod 600).
Never stored in .env — environment variables are often logged.
"""

import os
import stat
import sys
from pathlib import Path

from cryptography.fernet import Fernet


def _key_path() -> Path:
    from core.config import settings  # late import to avoid circular

    return settings.data_dir / ".secrets_key"


def _load_or_generate_key() -> bytes:
    path = _key_path()

    if path.exists():
        return path.read_bytes().strip()

    key = Fernet.generate_key()
    path.write_bytes(key)
    if sys.platform != "win32":
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # chmod 600
    return key


def _fernet() -> Fernet:
    return Fernet(_load_or_generate_key())


def encrypt(plaintext: str) -> bytes:
    """Encrypt a string, returning ciphertext bytes suitable for BLOB storage."""
    return _fernet().encrypt(plaintext.encode())


def decrypt(ciphertext: bytes) -> str:
    """Decrypt ciphertext bytes back to a plaintext string."""
    return _fernet().decrypt(ciphertext).decode()
