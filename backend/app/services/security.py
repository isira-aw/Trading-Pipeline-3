"""PIN hashing for the stage-switch gate (§7, §10).

The stage PIN is a 6-digit code, so the keyspace is only 10^6 — a bare
digest would be trivially reversible with a rainbow table. We use salted
PBKDF2-HMAC-SHA256 from the standard library (no extra dependency) and a
constant-time comparison on verify.

Stored format: ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``
"""

import hashlib
import hmac
import os

ALGORITHM = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 240_000
SALT_BYTES = 16
DEFAULT_PIN = "000000"


def hash_pin(pin: str, iterations: int = DEFAULT_ITERATIONS) -> str:
    """Hash a PIN into the storable ``pbkdf2_sha256$...`` format."""
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)
    return f"{ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    """Check a PIN against a stored hash. Returns False on any malformed input."""
    if not stored:
        return False
    try:
        algorithm, iterations_str, salt_hex, hash_hex = stored.split("$")
        if algorithm != ALGORITHM:
            return False
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False

    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def is_default_pin(stored: str) -> bool:
    """True if the stage PIN is still the factory default.

    §10 requires the UI to warn when the default PIN is still in place and
    the user is trying to reach the live stage.
    """
    return verify_pin(DEFAULT_PIN, stored)
