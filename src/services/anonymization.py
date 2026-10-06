"""Driver-reference anonymisation.

A KPI payload may carry a driver reference. The narrative is a management
report, so the reference is never useful in the output and is never persisted
in the clear: it is replaced by a salted digest, and when no salt is available
it is dropped entirely rather than passed through.

The salt is a deployment secret read through the bound secret provider. It is
read with ``get()`` rather than ``require()``: a deployment that has not
provisioned one is a valid deployment — it simply drops driver references
instead of hashing them — and requiring the key would make the agent fail to
load instead.
"""

from __future__ import annotations

import hashlib

from framework.secrets.context import current_secrets

_HASH_PREFIX_LEN = 16
_SALT_KEY = "DRIVER_REF_SALT"
_MIN_SALT_LEN = 16


def resolve_salt() -> str | None:
    """Return the configured anonymisation salt, or None when unavailable.

    A salt shorter than 16 characters is treated as unavailable: a short salt is
    brute-forceable against a small driver population, which would defeat the
    purpose of hashing at all. Dropping the reference is the safer outcome, so
    this fails closed rather than hashing weakly.
    """
    value = current_secrets().get(_SALT_KEY)
    if not isinstance(value, str) or len(value) < _MIN_SALT_LEN:
        return None
    return value


def hash_driver_ref(driver_id: str, salt: str) -> str:
    """Return a salted, truncated digest of a driver reference.

    SHA-256 over ``"{salt}:{driver_id}"``, truncated to 16 hex characters. The
    digest exists so two records about the same driver can be correlated within
    one report; re-identification is not a supported use.

    Args:
        driver_id: The raw driver reference from the caller's KPI payload.
        salt: The deployment salt from :func:`resolve_salt`.

    Returns:
        A 16-character lowercase hex digest.

    Raises:
        TypeError: ``driver_id`` is not a string.
        ValueError: ``salt`` is empty or not a string.
    """
    if not isinstance(driver_id, str):
        raise TypeError(f"driver_id must be str, got {type(driver_id).__name__}")
    if not isinstance(salt, str) or not salt:
        raise ValueError("salt must be a non-empty string")

    return hashlib.sha256(f"{salt}:{driver_id}".encode()).hexdigest()[:_HASH_PREFIX_LEN]
