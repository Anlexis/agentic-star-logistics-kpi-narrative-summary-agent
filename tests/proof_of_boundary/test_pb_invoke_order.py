# PB-6 — invoke-order boundary.
#
# A full agent.invoke() must run the fixed backbone in order:
#
#     initialize -> pre_process -> main -> {route} -> post_process -> finalize
#
# The framework records every executed node in node_history, in execution order,
# so the surfaced history is the evidence. A run that ends in anything other
# than success routes straight from main to finalize and never visits
# post_process — which is itself an ordering violation this test would catch.
#
# The trust level used is the one the manifest declares. The internal shortcut
# is never used here: it would over-privilege the run and hide a regression in
# the trust gate.
#
# Deterministic: no model, no network.

from __future__ import annotations

import json
import pathlib
from typing import ClassVar

from framework.nodes.base_node import BaseNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import build_default

_DEPLOY_PAYLOAD_PATH = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "invoke_payload.json"
_DEPLOY_PAYLOAD = json.loads(_DEPLOY_PAYLOAD_PATH.read_text(encoding="utf-8"))

# The request that drives the pipeline to a released report. It must stay
# byte-equal to the deployment sign-off payload, so the run this test proves and
# the run the deployment check performs are the same run — pinned below.
_VALID_PAYLOAD = "Summarise this period's logistics KPI movements for the executive board."

_EXPECTED_ORDER = [
    "InitializeNode",
    "InputValidateNode",
    "IntentClassifyNode",
    "PreProcessSlotNode",
    "ResponseGenerateNode",
    "MainSlotNode",
    "OutputValidateNode",
    "PostProcessSlotNode",
    "FinalizeNode",
]


def _invoke(input_context, *, trust=TrustLevel.VERIFIED_EXTERNAL, user_input=_VALID_PAYLOAD):
    agent = build_default()
    agent.compile()
    ctx = InvocationContext(session_id="pb6-invoke-order", caller_trust_level=trust)
    return agent.invoke(user_input, ctx=ctx, input_context=input_context)


class TestSignOffPayload:
    def test_payload_matches_the_deployment_file(self):
        assert _DEPLOY_PAYLOAD["input"] == _VALID_PAYLOAD

    def test_the_deployment_payload_carries_the_domain_parameters(self):
        """The sign-off request has to exercise the real domain path; a request
        with no parameters would prove only that the process boots."""
        context = _DEPLOY_PAYLOAD["input_context"]
        assert set(context) >= {"kpi_data", "baseline_data", "audience_type"}


class TestBackboneOrder:
    def test_full_invoke_runs_the_backbone_in_order(self):
        result = _invoke(_DEPLOY_PAYLOAD["input_context"])
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["node_history"] == _EXPECTED_ORDER

    def test_a_refusal_short_circuits_before_the_release_slot(self):
        result = _invoke({"kpi_data": {"otd_pct": "not-a-number"}})
        assert result["status"] == AgentStatus.ERROR.value
        assert "PostProcessSlotNode" not in result["node_history"]
        assert result["node_history"][-1] == "FinalizeNode"

    def test_the_release_slot_is_the_last_domain_node(self):
        history = _invoke(_DEPLOY_PAYLOAD["input_context"])["node_history"]
        assert history.index("MainSlotNode") < history.index("PostProcessSlotNode")
        assert history.index("PostProcessSlotNode") < history.index("FinalizeNode")


class TestTrustGate:
    def test_an_unverified_caller_is_denied(self):
        result = _invoke(_DEPLOY_PAYLOAD["input_context"], trust=TrustLevel.ANONYMOUS)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["output"] is None

    def test_denial_happens_before_the_node_body_runs(self, monkeypatch):
        """An always-present privileged fixture proves the negative path without
        depending on any domain node's own refusal logic."""
        import framework.nodes.base_node as base_node_module

        events: list[str] = []
        executed: list[object] = []

        class _Privileged(BaseNode):
            required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

            def _security_gate_input(self, state):
                return state

            def execute(self, state):
                executed.append(state)
                return {"status": AgentStatus.SUCCESS.value}

            def _security_gate_output(self, result):
                return result

        monkeypatch.setattr(
            base_node_module,
            "emit_trace_event",
            lambda event_type, _payload, _state: events.append(event_type),
        )
        result = _Privileged()({"caller_trust_level": TrustLevel.ANONYMOUS.value, "correlation_id": "pb6-denial"})

        assert result["status"] == AgentStatus.ERROR.value
        assert "trust gate denied" in result["error_log"][0]
        assert events == ["s1_denied"]
        assert not executed


class TestNodeContract:
    def test_every_domain_node_declares_its_trust_level(self):
        """Inheriting the permissive default silently would let an anonymous
        caller reach a node that must not accept one."""
        import importlib
        import inspect
        import pkgutil

        package = importlib.import_module("src.nodes")
        checked = 0
        for _finder, name, _ispkg in pkgutil.walk_packages(package.__path__, prefix="src.nodes."):
            module = importlib.import_module(name)
            for obj in vars(module).values():
                if (
                    isinstance(obj, type)
                    and issubclass(obj, BaseNode)
                    and obj.__module__ == name
                    and not inspect.isabstract(obj)
                ):
                    assert "required_trust_level" in obj.__dict__, f"{obj.__name__} does not declare one"
                    assert obj.required_trust_level is TrustLevel.VERIFIED_EXTERNAL
                    checked += 1
        assert checked >= 4

    def test_the_backbone_wiring_is_not_overridden(self):
        from src.graph.graph import LogisticsKPINarrativeSummaryAgent

        assert "add_edges" not in LogisticsKPINarrativeSummaryAgent.__dict__
        assert "register_nodes" in LogisticsKPINarrativeSummaryAgent.__dict__

    def test_slot_registration_extends_the_backbone(self):
        agent = build_default()
        agent.register_nodes()
        slots = list(agent._nodes)
        assert slots[0] == "initialize" and slots[-1] == "finalize"
        assert slots.index("pre_process") < slots.index("main") < slots.index("post_process")
