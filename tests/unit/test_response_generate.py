"""Comparison arithmetic and the two narrative paths."""

from __future__ import annotations

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.response_generate import ResponseGenerateNode, load_narrative_prompt

_ERROR = AgentStatus.ERROR.value
_SUCCESS = AgentStatus.SUCCESS.value


@pytest.fixture
def node_factory(kpi_thresholds, kpi_fields, narrative_prompt):
    system, template = narrative_prompt

    def _make(llm_client=None):
        return ResponseGenerateNode(
            kpi_thresholds=kpi_thresholds,
            kpi_fields=kpi_fields,
            system_prompt=system,
            user_prompt_template=template,
            llm_client=llm_client,
        )

    return _make


class TestComparisonArithmetic:
    """Direction matters: a lower unit cost is an improvement, a lower on-time
    rate is not. Reversing that is the failure this table exists to prevent."""

    @pytest.mark.parametrize(
        "field,current,prior,expected",
        [
            ("otd_pct", 0.99, 0.94, "normal"),  # rose — good for this KPI
            ("otd_pct", 0.88, 0.94, "warning"),  # fell 6pp
            ("otd_pct", 0.83, 0.94, "critical"),  # fell 11pp
            ("cost_per_kg", 120.0, 130.0, "normal"),  # fell — good for this KPI
            ("cost_per_kg", 130.15, 130.0, "warning"),
            ("cost_per_kg", 130.25, 130.0, "critical"),
            ("damage_rate", 0.0015, 0.002, "normal"),
            ("damage_rate", 0.008, 0.002, "warning"),
            ("damage_rate", 0.013, 0.002, "critical"),
        ],
    )
    def test_severity_respects_direction(
        self, node_factory, validated_state, baseline_kpis, field, current, prior, expected
    ):
        node = node_factory()
        state = validated_state()
        state["kpi_data"] = json.dumps({**baseline_kpis, field: current})
        state["baseline_data"] = json.dumps({**baseline_kpis, field: prior})
        result = node.execute(state)
        assert result["status"] == _SUCCESS
        assert json.loads(result["delta_summary"])[field]["severity"] == expected

    def test_band_boundary_is_not_lost_to_float_noise(self, node_factory, validated_state, baseline_kpis):
        node = node_factory()
        state = validated_state()
        state["kpi_data"] = json.dumps({**baseline_kpis, "otd_pct": 0.89})
        state["baseline_data"] = json.dumps({**baseline_kpis, "otd_pct": 0.94})
        summary = json.loads(node.execute(state)["delta_summary"])
        assert summary["otd_pct"]["severity"] == "warning"

    def test_absent_baseline_field_is_unknown_not_zero(self, node_factory, validated_state, baseline_kpis):
        node = node_factory()
        state = validated_state()
        state["baseline_data"] = json.dumps({k: v for k, v in baseline_kpis.items() if k != "otd_pct"})
        summary = json.loads(node.execute(state)["delta_summary"])
        assert summary["otd_pct"]["severity"] == "unknown"
        assert summary["otd_pct"]["delta"] is None

    def test_kpi_without_a_configured_band_is_unknown(self, kpi_thresholds, narrative_prompt, validated_state):
        system, template = narrative_prompt
        node = ResponseGenerateNode(
            kpi_thresholds=kpi_thresholds,
            kpi_fields=["otd_pct", "unbanded_metric"],
            system_prompt=system,
            user_prompt_template=template,
        )
        summary = json.loads(node.execute(validated_state())["delta_summary"])
        assert summary["unbanded_metric"] == {
            "direction": "unknown",
            "delta": None,
            "severity": "unknown",
        }


class TestDeterministicPath:
    def test_report_is_produced_without_a_model(self, node_factory, validated_state):
        result = node_factory().execute(validated_state(kpi_changes={"otd_pct": 0.83}))
        assert result["status"] == _SUCCESS
        assert "otd_pct" in result["narrative_output"]

    def test_report_reflects_the_caller_figures(self, node_factory, validated_state):
        result = node_factory().execute(validated_state(kpi_changes={"cost_per_kg": 999.5}))
        assert "999.5" in result["narrative_output"]

    def test_sections_follow_the_audience_bundle(self, node_factory, validated_state, audience_formats):
        for audience in ("executive", "operations", "customer"):
            narrative = node_factory().execute(validated_state(audience=audience))["narrative_output"]
            for section in audience_formats[audience]["sections"]:
                assert f"## {section}" in narrative

    def test_clean_period_says_so_rather_than_inventing_an_exception(self, node_factory, validated_state):
        narrative = node_factory().execute(validated_state())["narrative_output"]
        assert "held within their configured bands" in narrative

    def test_exceptions_are_ranked_most_severe_first(self, node_factory, validated_state):
        narrative = node_factory().execute(validated_state(kpi_changes={"otd_pct": 0.83, "damage_rate": 0.008}))[
            "narrative_output"
        ]
        assert narrative.index("Address otd_pct") < narrative.index("Then review damage_rate")


class TestModelPath:
    def test_model_is_called_once_with_both_roles(self, node_factory, validated_state, mock_llm):
        result = node_factory(mock_llm).execute(validated_state())
        assert result["status"] == _SUCCESS
        assert mock_llm.call_count == 1
        roles = [m["role"] for m in mock_llm.last_messages]
        assert roles == ["system", "user"]

    def test_prompt_carries_the_resolved_comparison(self, node_factory, validated_state, mock_llm):
        node_factory(mock_llm).execute(validated_state(audience="operations"))
        user = mock_llm.last_messages[1]["content"]
        assert "Audience: operations" in user
        assert "Current-period KPIs" in user
        assert "delta_summary" in user

    @pytest.mark.parametrize("response", [{"content": ""}, {"content": "   "}, {"content": None}, {}, "plain string"])
    def test_unusable_model_response_refuses(self, node_factory, validated_state, response):
        class _Client:
            @staticmethod
            def complete(messages):
                return response

        result = node_factory(_Client()).execute(validated_state())
        assert result["status"] == _ERROR

    def test_oversized_model_response_refuses(self, node_factory, validated_state):
        class _Client:
            @staticmethod
            def complete(messages):
                return {"content": "x" * 20_001}

        assert node_factory(_Client()).execute(validated_state())["status"] == _ERROR

    def test_model_is_not_called_when_inputs_are_missing(self, node_factory, mock_llm):
        result = node_factory(mock_llm).execute({"caller_trust_level": "VERIFIED_EXTERNAL"})
        assert result["status"] == _ERROR
        assert mock_llm.call_count == 0


class TestConstruction:
    def test_client_without_the_expected_method_is_rejected(self, kpi_thresholds, kpi_fields, narrative_prompt):
        system, template = narrative_prompt
        with pytest.raises(TypeError, match="complete"):
            ResponseGenerateNode(
                kpi_thresholds=kpi_thresholds,
                kpi_fields=kpi_fields,
                system_prompt=system,
                user_prompt_template=template,
                llm_client=object(),
            )

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), "0.05", True, None])
    def test_non_finite_threshold_is_rejected_at_construction(self, kpi_fields, narrative_prompt, bad):
        """The threshold table is configuration, but it is numeric input all the
        same: a non-finite band makes every comparison return False and reports a
        breach as normal."""
        system, template = narrative_prompt
        with pytest.raises(ValueError):
            ResponseGenerateNode(
                kpi_thresholds={
                    "otd_pct": {"direction": "higher_is_better", "warning_delta": bad, "critical_delta": -0.1}
                },
                kpi_fields=kpi_fields,
                system_prompt=system,
                user_prompt_template=template,
            )

    def test_unknown_direction_is_rejected_at_construction(self, kpi_fields, narrative_prompt):
        system, template = narrative_prompt
        with pytest.raises(ValueError, match="direction"):
            ResponseGenerateNode(
                kpi_thresholds={"otd_pct": {"direction": "sideways", "warning_delta": -0.05, "critical_delta": -0.1}},
                kpi_fields=kpi_fields,
                system_prompt=system,
                user_prompt_template=template,
            )


class TestPromptFile:
    def test_both_sections_are_read(self, repo_root):
        system, template = load_narrative_prompt(repo_root / "prompts" / "narrative.md")
        assert "logistics analyst" in system
        assert "{kpi_data}" in template

    def test_missing_sections_raise(self, tmp_path):
        path = tmp_path / "narrative.md"
        path.write_text("# nothing here\n", encoding="utf-8")
        with pytest.raises(Exception):
            load_narrative_prompt(path)
