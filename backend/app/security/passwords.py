"""Password hashing.

bcrypt directly rather than through passlib: one dependency instead of two, and passlib has been
unmaintained since 2020.
"""

from __future__ import annotations

import bcrypt

#: bcrypt truncates input beyond 72 bytes. Rather than silently ignoring the tail of a long
#: passphrase, the schema layer rejects anything longer, so a user's password means what they
#: typed.
MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    """Return a bcrypt hash of ``password``."""
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password exceeds {MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check of ``password`` against ``password_hash``."""
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))
    except ValueError:
        # A malformed stored hash must not be a way in.
        return False
