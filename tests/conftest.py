"""Shared fixtures.

Running from the repository root puts ``src`` on the path; the framework comes
from the installed wheel.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def repo_root() -> Path:
    return _ROOT


@pytest.fixture
def kpi_fields() -> list[str]:
    return [
        "otd_pct",
        "exception_rate",
        "cost_per_kg",
        "warehouse_utilization_pct",
        "damage_rate",
    ]


@pytest.fixture
def baseline_kpis() -> dict:
    return {
        "otd_pct": 0.94,
        "exception_rate": 0.018,
        "cost_per_kg": 130.0,
        "warehouse_utilization_pct": 0.80,
        "damage_rate": 0.002,
    }


@pytest.fixture
def kpi_thresholds() -> dict:
    import yaml

    with open(_ROOT / "config" / "kpi-thresholds.yaml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture
def audience_formats() -> dict:
    import yaml

    with open(_ROOT / "config" / "audience-formats.yaml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture
def narrative_prompt() -> tuple[str, str]:
    from src.nodes.response_generate import load_narrative_prompt

    return load_narrative_prompt(_ROOT / "prompts" / "narrative.md")


class RecordingLLM:
    """A model stand-in exposing the framework's client contract."""

    def __init__(self, body: str = "OTD declined; unit cost held flat.") -> None:
        self._body = body
        self.last_messages: list | None = None
        self.call_count = 0

    def complete(self, messages: list) -> dict:
        self.last_messages = messages
        self.call_count += 1
        return {"content": f"{self._body} for internal reporting purposes.", "model": "test"}


@pytest.fixture
def mock_llm() -> RecordingLLM:
    return RecordingLLM()


@pytest.fixture
def caller_context(baseline_kpis):
    """Factory for a valid ``input_context`` mapping."""

    def _make(
        *,
        kpi_changes: dict | None = None,
        baseline_changes: dict | None = None,
        audience: str | None = "executive",
        drop_baseline: bool = False,
    ) -> dict:
        context: dict = {"kpi_data": {**baseline_kpis, **(kpi_changes or {})}}
        if not drop_baseline:
            context["baseline_data"] = {**baseline_kpis, **(baseline_changes or {})}
        if audience is not None:
            context["audience_type"] = audience
        return context

    return _make


@pytest.fixture
def seeded_state(caller_context):
    """Factory for a state dict as the backbone would present it to a slot.

    ``caller_trust_level`` is seeded because the domain nodes require a verified
    caller; a full invoke gets it from the invocation context instead.
    """

    def _make(**kwargs) -> dict:
        return {
            "input_context": caller_context(**kwargs),
            "caller_trust_level": "VERIFIED_EXTERNAL",
            "node_history": [],
            "error_log": [],
            "execution_time": {},
        }

    return _make


@pytest.fixture
def validated_state(baseline_kpis, audience_formats):
    """Factory for a state as it looks after the pre_process slot has run."""

    def _make(*, kpi_changes: dict | None = None, audience: str = "executive") -> dict:
        return {
            "kpi_data": json.dumps({**baseline_kpis, **(kpi_changes or {})}, sort_keys=True),
            "baseline_data": json.dumps(baseline_kpis, sort_keys=True),
            "audience_type": audience,
            "format_params": json.dumps(audience_formats[audience], sort_keys=True),
            "caller_trust_level": "VERIFIED_EXTERNAL",
        }

    return _make
