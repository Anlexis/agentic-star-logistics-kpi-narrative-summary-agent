"""The release boundary: what is let through, what is withheld, and how.

A refusal here has to do two things — report the error, and make the refused
text unreachable. The second is the part that is easy to get wrong: the
framework's own output accessor falls back to the last value written to the
result fields regardless of status, so returning an error without clearing them
ships the refused narrative inside the error envelope.
"""

from __future__ import annotations

import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.output_validate import (
    OUTPUT_BEARING_FIELDS,
    WITHHELD_NOTICE,
    OutputValidateNode,
)

_ERROR = AgentStatus.ERROR.value
_SUCCESS = AgentStatus.SUCCESS.value

_CLEAN = "On-time delivery fell 5 points against the prior period. for internal reporting purposes."

# Every shape the framework's own detector recognises. The node reuses that
# detector rather than a local list, so this table is the framework's block set,
# not an approximation of it.
_CREDENTIAL_SHAPES = {
    "stripe": "sk_live_" + "a" * 20,
    "openai": "sk-" + "b" * 24,
    "jwt": "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12,
    "aws": "AKIA" + "C" * 16,
    "bearer": "Bearer " + "d" * 20,
    "postgres": "postgresql://user:example-password@db.internal:5432/logistics",
    "redis": "redis://user:example-password@cache.internal:6379/0",
}


@pytest.fixture
def node():
    return OutputValidateNode()


def _state(narrative: str) -> dict:
    return {
        "narrative_output": narrative,
        "result": narrative,
        "delta_summary": '{"otd_pct": {"severity": "warning"}}',
        "kpi_data": '{"otd_pct": 0.89}',
        "baseline_data": '{"otd_pct": 0.94}',
    }


class TestRelease:
    def test_clean_narrative_is_released(self, node):
        result = node.execute(_state(_CLEAN))
        assert result["status"] == _SUCCESS
        assert result["blocked"] is False
        assert result["narrative_output"] == _CLEAN
        assert result["formatted_output"] == _CLEAN

    def test_caller_figures_are_cleared_on_release(self, node):
        result = node.execute(_state(_CLEAN))
        assert result["kpi_data"] is None
        assert result["baseline_data"] is None

    def test_missing_disclaimer_is_appended_not_refused(self, node):
        result = node.execute(_state("On-time delivery fell 5 points."))
        assert result["status"] == _SUCCESS
        assert "for internal reporting purposes" in result["narrative_output"]

    def test_disclaimer_match_is_case_insensitive(self, node):
        text = "Summary. FOR INTERNAL REPORTING PURPOSES."
        assert node.execute(_state(text))["narrative_output"] == text

    def test_configured_disclaimer_is_used(self):
        node = OutputValidateNode(disclaimer="advisory only")
        assert "advisory only" in node.execute(_state("Summary."))["narrative_output"]

    def test_empty_disclaimer_is_rejected_at_construction(self):
        with pytest.raises(ValueError, match="non-empty"):
            OutputValidateNode(disclaimer="   ")


class TestRefusals:
    @pytest.mark.parametrize("shape", sorted(_CREDENTIAL_SHAPES), ids=sorted(_CREDENTIAL_SHAPES))
    def test_every_framework_recognised_credential_is_withheld(self, node, shape):
        """A pattern the framework catches and this node misses is a bypass, not a
        gap: the framework then raises inside the wrapper, which discards this
        node's update — including the clearing below."""
        secret = _CREDENTIAL_SHAPES[shape]
        result = node.execute(_state(f"Depot access note: {secret}. {_CLEAN}"))
        assert result["status"] == _ERROR
        assert result["blocked"] is True
        assert secret not in str(result)

    @pytest.mark.parametrize(
        "narrative,reason",
        [
            ("Route delay attributed to D-9001. " + _CLEAN, "operational driver reference"),
            ("遅延はドライバー山田太郎に起因。" + _CLEAN, "named individual"),
            ("Delay attributed to driver John Smith. " + _CLEAN, "named individual"),
            ("driver: Smith raised the exception. " + _CLEAN, "named individual"),
            ("Reference 0a1b2c3d4e5f6a7b noted. " + _CLEAN, "anonymised driver reference"),
        ],
        ids=["reference", "ja-name", "en-name", "en-labelled", "digest"],
    )
    def test_individual_attribution_is_withheld(self, node, narrative, reason):
        result = node.execute(_state(narrative))
        assert result["status"] == _ERROR
        assert reason in result["error_log"][0]

    @pytest.mark.parametrize("narrative", [None, "", "   ", 7])
    def test_absent_narrative_is_withheld(self, node, narrative):
        assert node.execute(_state(narrative))["status"] == _ERROR

    @pytest.mark.parametrize(
        "sentence",
        [
            "Carrier Nippon Express absorbed the surcharge on this lane.",
            "Depot D-4 handled the overflow; see the operations annex.",
            "The operator network held throughput steady across all lanes.",
            "Driver shortage remains the dominant cost pressure this quarter.",
            "Driver Shortage And Operator Coverage — Quarterly View",
            "Operator Network Utilisation improved across every lane.",
        ],
    )
    def test_ordinary_report_language_is_released(self, node, sentence):
        """Carriers are companies and naming one is correct content for this
        report. A rule about individuals that blocks company names blocks real
        work, which is the more damaging failure direction."""
        assert node.execute(_state(f"{sentence} {_CLEAN}"))["status"] == _SUCCESS


class TestContainment:
    """A violation must leave nothing reachable, not merely flag itself."""

    @pytest.mark.parametrize("field", OUTPUT_BEARING_FIELDS)
    def test_every_output_bearing_field_is_overwritten(self, node, field):
        secret = _CREDENTIAL_SHAPES["jwt"]
        result = node.execute(_state(f"Note {secret}. {_CLEAN}"))
        assert field in result, f"{field} was not addressed by the refusal"
        assert result[field] in (None, WITHHELD_NOTICE)

    def test_the_placeholder_is_truthy(self):
        """An empty string is falsy, and a falsy value re-activates the very
        fallback the refusal exists to prevent."""
        assert bool(WITHHELD_NOTICE)

    def test_refused_text_appears_nowhere_in_the_update(self, node):
        secret = _CREDENTIAL_SHAPES["aws"]
        narrative = f"Depot key {secret}. {_CLEAN}"
        rendered = str(node.execute(_state(narrative)))
        assert secret not in rendered
        assert "On-time delivery fell 5 points" not in rendered

    def test_the_matched_value_is_not_named_in_the_error(self, node):
        secret = _CREDENTIAL_SHAPES["openai"]
        result = node.execute(_state(f"key {secret}. {_CLEAN}"))
        message = result["error_log"][0]
        assert secret not in message
        assert "credential-shaped" in message

    def test_the_refusal_does_not_raise(self, node):
        """Raising would hand control to the framework wrapper, which returns a
        bare error update that clears nothing — the opposite of containment."""
        result = node.execute(_state(_CREDENTIAL_SHAPES["bearer"]))
        assert result["status"] == _ERROR

    def test_the_inventory_covers_every_field_the_node_can_write(self, node):
        """Guards against a future output field quietly joining the state without
        joining the clearing list."""
        written = set(node.execute(_state(_CLEAN))) - {"status", "blocked"}
        assert written <= set(OUTPUT_BEARING_FIELDS)
