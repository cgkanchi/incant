"""Password hashing for human accounts (§11 Users).

scrypt from the standard library — memory-hard (unlike PBKDF2), no native
dependency (unlike argon2/bcrypt), and OWASP-acceptable at these parameters.
Every hash is self-describing (``scrypt$N$r$p$salt$digest``, base64url fields), so
the work factor can be raised later and old hashes upgraded transparently on the
next successful sign-in (`needs_rehash`), the same in-place upgrade pattern the
API-key pepper uses.

Deliberately NOT used for API keys or invite/reset tokens: those are high-entropy
random strings, where plain/peppered SHA-256 (auth.hash_key) is both sufficient
and constant-cost. Password hashing exists to slow down guessing of LOW-entropy
human secrets — that's the only place it belongs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# Work factor: N=2^15, r=8 → 32 MiB per verification, ~40ms on current hardware.
# Deliberately below OWASP's first-choice 2^17 (128 MiB): sign-in shares a container
# with the serving path, and a login stampede must not OOM it. The parameters ride
# in every hash, so raising them later upgrades users one sign-in at a time.
_N, _R, _P = 2**15, 8, 1
_SALT_BYTES = 16
_DKLEN = 32

# NIST-style policy: length is the only rule (no composition theater). The maximum
# guards the hash function from megabyte "passwords", nothing else.
MIN_PASSWORD_LEN = 10
MAX_PASSWORD_LEN = 128


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def validate_password(password: str) -> str | None:
    """The policy violation as a human sentence, or None when acceptable."""
    if len(password) < MIN_PASSWORD_LEN:
        return f"password must be at least {MIN_PASSWORD_LEN} characters"
    if len(password) > MAX_PASSWORD_LEN:
        return f"password must be at most {MAX_PASSWORD_LEN} characters"
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P,
                            maxmem=64 * 1024 * 1024, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification against a self-describing stored hash. Any
    malformed/foreign hash verifies False — never raises on untrusted input."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _b64d(digest)
        actual = hashlib.scrypt(password.encode(), salt=_b64d(salt),
                                n=int(n), r=int(r), p=int(p),
                                maxmem=256 * 1024 * 1024, dklen=len(expected))
    except Exception:
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    """True when the stored hash uses weaker parameters than current policy —
    re-hash with the (already verified) password on successful sign-in."""
    try:
        scheme, n, r, p, _salt, _digest = stored.split("$")
        return scheme != "scrypt" or (int(n), int(r), int(p)) < (_N, _R, _P)
    except Exception:
        return True
