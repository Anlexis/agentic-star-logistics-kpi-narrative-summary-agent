# PB-2 / PB-5 — state and checkpoint safety.
#
# Graph checkpoints are serialised with msgpack, so the state may hold only
# plain values. Beyond that, the state is the persistence surface: anything that
# survives the run survives into storage, which is why the caller's raw figures
# and the raw parameter mapping are cleared rather than left behind.
#
# PB-5's ingress assertion runs against the real pipeline state, not a state
# assembled by hand. A fixture built by hand can validate a shape production
# never produces.

from __future__ import annotations

import ast
import json
import pathlib
import re
from typing import Any, Iterator

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import build_default

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_STATE_FILE = _ROOT / "src" / "schemas" / "state.py"

CREDENTIAL_FIELD_PATTERNS = re.compile(
    r"(jwt|token|api_key|secret|password|credential|connection_string)", re.IGNORECASE
)
PROHIBITED_TYPE_ANNOTATIONS = ("BaseModel", "InvocationContext")

_DRIVER_REFERENCE = re.compile(r"\bD-\d{3,}\b", re.IGNORECASE)


def _final_state(input_context: dict) -> dict:
    """Drive the real pipeline and return the state the run leaves behind."""
    agent = build_default()
    agent.compile()
    ctx = InvocationContext(session_id="pb5", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    initial = {
        "user_input": "Summarise the period.",
        "input_context": input_context,
        "session_id": ctx.session_id,
        "correlation_id": ctx.correlation_id,
        "trace_id": "pb5-trace",
        "thread_id": ctx.thread_id,
        "caller_trust_level": ctx.caller_trust_level.value,
        "caller_id": "",
        "status": AgentStatus.PENDING.value,
        "retry_count": 0,
        "hitl_count": 0,
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    return agent._compiled.invoke(initial)


def _walk(value: Any, path: str = "state") -> Iterator[tuple[str, Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(key, f"{path}.<key>")
            yield from _walk(child, f"{path}[{key!r}]")
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


class TestStateModuleShape:
    def test_no_credential_shaped_field_names_or_object_types(self):
        tree = ast.parse(_STATE_FILE.read_text(encoding="utf-8"))
        violations: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    name = item.target.id
                    if CREDENTIAL_FIELD_PATTERNS.search(name):
                        violations.append(f"{item.lineno}: credential-shaped field name {name}")
                    dumped = ast.dump(item.annotation) if item.annotation else ""
                    for prohibited in PROHIBITED_TYPE_ANNOTATIONS:
                        if prohibited in dumped:
                            violations.append(f"{item.lineno}: prohibited type {prohibited}")
        assert violations == [], "\n".join(violations)


class TestPersistedState:
    def test_every_persisted_value_is_a_plain_type(self, caller_context):
        for path, value in _walk(_final_state(caller_context())):
            assert isinstance(
                value, (str, int, float, bool, type(None), dict, list, tuple)
            ), f"{path}: {type(value).__name__} is not msgpack-safe"

    def test_state_survives_a_json_round_trip(self, caller_context):
        state = _final_state(caller_context())
        restored = json.loads(json.dumps(state, default=str))
        assert restored["narrative_output"]
        assert restored["delta_summary"]

    def test_caller_figures_do_not_survive_the_run(self, caller_context):
        state = _final_state(caller_context())
        assert state["kpi_data"] is None
        assert state["baseline_data"] is None

    def test_the_raw_parameter_mapping_does_not_survive_the_run(self, caller_context):
        """The validated values live in their own fields; the caller's original
        mapping has no further use and would otherwise reach storage intact."""
        assert _final_state(caller_context())["input_context"] == {}

    def test_a_driver_reference_does_not_reach_the_persisted_state(self, caller_context):
        context = caller_context()
        context["kpi_data"]["driver_ref"] = "D-9001"
        serialised = json.dumps(_final_state(context), default=str)
        assert not _DRIVER_REFERENCE.search(serialised)

    def test_no_credential_shape_reaches_the_persisted_state(self, caller_context):
        from framework.security.credential_detector import detect_credentials

        serialised = json.dumps(_final_state(caller_context()), default=str)
        assert detect_credentials(serialised) == []


class TestRefusedRunLeavesNothingBehind:
    def test_a_withheld_report_is_not_persisted(self, caller_context, monkeypatch):
        """The release checks refuse the narrative; the state they leave must not
        still carry it."""
        released = "Delay attributed to driver John Smith. for internal reporting purposes."

        def _leaky(self, state):
            return {
                "status": AgentStatus.SUCCESS.value,
                "delta_summary": "{}",
                "narrative_output": released,
            }

        monkeypatch.setattr("src.nodes.response_generate.ResponseGenerateNode.execute", _leaky)
        state = _final_state(caller_context())
        assert state["status"] == AgentStatus.ERROR.value
        serialised = json.dumps(state, default=str)
        assert "John Smith" not in serialised


@pytest.mark.skipif(True, reason="checkpointing is not enabled in the runtime config")
def test_pb5_precheckpoint_ingress_not_raw() -> None:
    """PB-5's stored-checkpoint assertion.

    The runtime config enables neither memory nor human review, so the compiled
    graph has no checkpointer and there is no stored checkpoint to inspect. The
    equivalent surface — the state the run leaves behind — is asserted above.
    """
