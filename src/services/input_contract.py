"""The caller-data contract for the KPI narrative pipeline.

Everything the caller can influence arrives through the structured invocation
parameters (``input_context``) and is checked here before any of it reaches the
pipeline. Three rules cover the whole surface:

* **Numbers are finite and bounded.** ``float("nan")`` and ``float("inf")``
  parse successfully and then compare False against every threshold, so an
  unchecked non-finite value would pass a severity comparison silently instead
  of failing it. Every caller-supplied number goes through
  :func:`finite_in_range`, which rejects booleans, non-numerics, NaN, both
  infinities, and out-of-range magnitudes.
* **Strings that reach the report are inert.** A KPI field name renders into the
  narrative, so it is restricted to a short lowercase identifier; a driver
  reference is restricted to a short alphanumeric token. Free text there would
  be caller-controlled output injection.
* **The payload is screened for instruction-shaped content first**, keys
  included, before any structural check runs — so a hostile field *name* is
  refused for the right reason rather than for being an unknown key.

Rejections name the field, never the value: echoing a rejected value back into
an error log puts caller-controlled text into the audit trail.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping

# A KPI field name renders into the narrative and into audit events.
INERT_FIELD_RE = re.compile(r"^[a-z0-9_]{1,32}$")
# A driver reference is an operational token, so it may carry case and hyphens.
INERT_DRIVER_REF_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Structural caps. A KPI report has tens of fields, not thousands.
MAX_KPI_ENTRIES = 64
MAX_CONTEXT_FIELDS = 16
MAX_STRING_LEN = 256
MAX_SCAN_DEPTH = 8

# Magnitude ceiling for every caller-supplied number. KPI values are rates,
# ratios and unit costs; anything beyond this is a data error, not a KPI.
NUMERIC_ABS_MAX = 1e9


class ContractViolation(ValueError):
    """A caller-supplied value failed the contract. Carries a field reference only."""


# ── instruction-shaped content ───────────────────────────────────────────────

# Chat-template control tokens are a class, not a phrase list: a payload that
# opens a new turn or a new system block steers the model regardless of what
# words follow it. These are matched before any phrase check because the token
# forms survive a markup strip that would erase them and leave plain directive
# text behind.
_CONTROL_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"<\|[^|>]{1,64}\|>"),  # <|im_start|>, <|endoftext|>, ...
    re.compile(r"\[/?INST\]", re.IGNORECASE),  # [INST] / [/INST]
    re.compile(r"<</?SYS>>", re.IGNORECASE),  # <<SYS>> / <</SYS>>
    re.compile(r"<\|?(?:system|assistant|user)\|?>", re.IGNORECASE),
)

# Directive phrases are anchored to a full instruction shape. An unanchored
# "act as a" or a bare SQL verb matches ordinary logistics prose — "the carrier
# will act as a customs agent", "insert into the manifest" — and refusing those
# would block real reports, which is the more damaging failure direction.
_DIRECTIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|preceding)\s+"
        r"(?:instructions?|rules?|prompts?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bdisregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)\s+" r"(?:instructions?|rules?|prompts?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\byou\s+are\s+now\s+(?:a|an|the)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:reveal|print|repeat|output)\s+(?:your\s+|the\s+)?(?:system\s+)?" r"(?:prompt|instructions)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bnew\s+(?:system\s+)?instructions?\s*:", re.IGNORECASE),
)

# The strip mirrors what a naive markup sanitiser would do. Screening the
# stripped form as well catches a directive spliced with inline tags
# ("ig<b>nore all previous instructions") that only reads as an instruction once
# the tags are removed. Screening the RAW form first catches the control tokens
# that the same strip would silently delete.
_MARKUP_RE = re.compile(r"<[^>]{0,64}>")


def screen_instruction_content(value: object, *, field: str) -> str | None:
    """Return a reason when ``value`` contains instruction-shaped content.

    Walks mappings and sequences depth-first and screens KEYS as well as values:
    a field name is caller data too, and a JSON ``\\u`` escape is already decoded
    by the time the payload reaches this function, so a post-parse scan cannot be
    evaded by escaping.
    """
    return _screen(value, field=field, depth=0)


def _screen(value: object, *, field: str, depth: int) -> str | None:
    if depth > MAX_SCAN_DEPTH:
        return f"{field} is nested more deeply than the contract allows"
    if isinstance(value, str):
        if _looks_like_instruction(value):
            return f"{field} contains instruction-shaped content"
        return None
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and _looks_like_instruction(key):
                return f"{field} contains an instruction-shaped field name"
            reason = _screen(item, field=field, depth=depth + 1)
            if reason is not None:
                return reason
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            reason = _screen(item, field=field, depth=depth + 1)
            if reason is not None:
                return reason
    return None


def _looks_like_instruction(text: str) -> bool:
    stripped = _MARKUP_RE.sub("", text)
    for candidate in (text, stripped):
        for pattern in _CONTROL_TOKEN_PATTERNS:
            if pattern.search(candidate):
                return True
        for pattern in _DIRECTIVE_PATTERNS:
            if pattern.search(candidate):
                return True
    return False


# ── numbers ──────────────────────────────────────────────────────────────────


def finite_in_range(
    value: object,
    *,
    field: str,
    minimum: float = -NUMERIC_ABS_MAX,
    maximum: float = NUMERIC_ABS_MAX,
) -> float:
    """Return ``value`` as a float, or raise :class:`ContractViolation`.

    Rejects booleans (``isinstance(True, int)`` is True in Python, so a bare
    numeric check would accept ``true`` as 1), non-numeric types, NaN, both
    infinities, and magnitudes outside the configured band.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractViolation(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ContractViolation(f"{field} must be a finite number")
    if not (minimum <= number <= maximum):
        raise ContractViolation(f"{field} is outside the accepted range")
    return number


# ── strings ──────────────────────────────────────────────────────────────────


def inert_field_name(name: object, *, field: str) -> str:
    """Return ``name`` when it is a short lowercase identifier, else raise."""
    if not isinstance(name, str):
        raise ContractViolation(f"{field} must be named by a string")
    if not INERT_FIELD_RE.match(name):
        raise ContractViolation(f"{field} must be a lowercase identifier of at most 32 characters")
    return name


def inert_driver_ref(value: object, *, field: str) -> str:
    """Return ``value`` when it is a short alphanumeric token, else raise."""
    if not isinstance(value, str):
        raise ContractViolation(f"{field} must be a string reference")
    if not INERT_DRIVER_REF_RE.match(value):
        raise ContractViolation(f"{field} must be an alphanumeric reference of at most 64 characters")
    return value


def bounded_mapping(value: object, *, field: str, max_entries: int) -> Mapping[str, Any]:
    """Return ``value`` when it is a mapping within the entry cap, else raise."""
    if not isinstance(value, Mapping):
        raise ContractViolation(f"{field} must be an object")
    if len(value) > max_entries:
        raise ContractViolation(f"{field} carries more than {max_entries} entries")
    return value


def one_of(value: object, *, field: str, allowed: Iterable[str]) -> str:
    """Return ``value`` when it is one of ``allowed``, else raise."""
    permitted = sorted(allowed)
    if not isinstance(value, str) or value not in permitted:
        raise ContractViolation(f"{field} must be one of {permitted}")
    return value
