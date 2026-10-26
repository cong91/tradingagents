"""Encrypt data-source credentials before they reach PostgreSQL.

A session cookie is a live bearer secret: a leaked ``reddit_session`` or X
``auth_token`` lets anyone act as the operator's browser session. PostgreSQL is
shared infrastructure here — the same database serves every control-plane table
— so what a database dump or a stray query exposes must be ciphertext, not the
cookies themselves.

The key lives in the engine state directory (``~/.tradingagents`` by default,
already gitignored and mounted as a volume) and is created on first use. It is
never stored in the database, so a database dump alone cannot decrypt anything.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def _state_dir() -> Path:
    override = os.environ.get("TRADINGAGENTS_DATA_DIR")
    return Path(override).expanduser() if override else Path.home() / ".tradingagents"


def key_path() -> Path:
    """Where the Fernet key lives; overridable for tests and containers."""
    override = os.environ.get("TRADINGAGENTS_CREDENTIAL_KEY_PATH")
    return Path(override).expanduser() if override else _state_dir() / "credential.key"


def _fernet() -> Fernet:
    path = key_path()
    if path.exists():
        return Fernet(path.read_bytes().strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # O_EXCL: two workers starting together must not each install a key that
    # then decrypts only half the rows.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return Fernet(path.read_bytes().strip())
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    return Fernet(key)


def encrypt_json(payload: dict) -> str:
    """Serialize and encrypt one credential payload."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return _fernet().encrypt(raw).decode("ascii")


def decrypt_json(token: str) -> dict:
    """Decrypt a stored payload; raises ``ValueError`` when it cannot be read.

    A rotated or missing key makes old rows unreadable. Surfacing that as a
    value error lets the API report "re-import the cookies" instead of crashing
    the whole settings screen.
    """
    try:
        raw = _fernet().decrypt(token.encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise ValueError(
            "stored credential cannot be decrypted with the current key; re-import the cookies"
        ) from exc
    return json.loads(raw.decode("utf-8"))
