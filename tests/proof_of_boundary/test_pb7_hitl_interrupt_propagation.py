# PB-7: human-in-the-loop interrupt propagation.
#
# PB-7 verifies that an interrupt raised inside the graph propagates out to the
# invoking caller, so an orchestrator can pause a run, collect a human decision
# and resume it. That behaviour only exists for templates that opt into it.
#
# This template does not: no node calls interrupt(), and config/config.yaml
# enables neither human review nor memory, so the compiled graph has no
# checkpointer and there is no suspension to observe. PB-7 therefore ships as a
# real, importable module that skips with a stated reason and carries the
# condition under which it would start running — rather than asserting something
# trivially true.

from __future__ import annotations

import pathlib
import warnings

import pytest

_CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _human_review_enabled() -> bool:
    """True when the runtime config turns human review on.

    An absent or unreadable config warns rather than skipping quietly: "never
    shipped" and "genuinely not applicable" look identical from here, and only
    one of them is fine.
    """
    if not _CONFIG_PATH.exists():
        warnings.warn(
            f"{_CONFIG_PATH.name} not found — PB-7 skipped without verifying the setting.",
            stacklevel=2,
        )
        return False
    try:
        import yaml

        data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 — any read failure is the same signal here
        warnings.warn(f"{_CONFIG_PATH.name} could not be read as YAML ({exc}) — PB-7 skipped.", stacklevel=2)
        return False
    block = data.get("hitl") if isinstance(data, dict) else None
    return bool(block.get("enabled", False)) if isinstance(block, dict) else False


_ENABLED = _human_review_enabled()
_SKIP_REASON = (
    "this template does not enable human review — no node suspends the graph, "
    "so there is no interrupt propagation to assert"
)


@pytest.mark.skipif(not _ENABLED, reason=_SKIP_REASON)
class TestHumanReviewPropagation:
    def test_interrupt_reaches_the_caller(self):
        # Reached only once human review is switched on in the runtime config.
        raise AssertionError("human review is enabled but the propagation assertion has not been written")
