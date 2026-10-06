"""Graph composition for the Logistics KPI Narrative Summary agent.

The agent inherits the framework's base graph directly and fills the three
domain slots of the fixed backbone:

    initialize -> pre_process -> main -> {route} -> post_process -> finalize

    pre_process   InputValidate -> IntentClassify
    main          ResponseGenerate
    post_process  OutputValidate

``add_edges()`` is not overridden — the backbone owns the wiring. Each slot is a
small orchestrator that runs its ordered sub-nodes in turn, merging the partial
updates and stopping at the first refusal so an error status reaches ``route()``
intact. A slot that receives a state already carrying an error passes it through
untouched: the backbone always visits every slot, so a later slot must not
overwrite an earlier refusal.

Runtime configuration reaches the graph through the constructor, exactly as the
registry supplies it, and the standalone entry point loads the same file the
same way. Reading it from the manifest instead would return nothing: the
manifest carries identity and compile-time requirements only, and a reader
pointed at it degrades to defaults without saying so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Mapping, Optional

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.utils.audit_logger import emit_trace_event
from framework.utils.config_loader import load_agent_config

from src.nodes.input_validate import InputValidateNode
from src.nodes.intent_classify import IntentClassifyNode
from src.nodes.output_validate import DEFAULT_DISCLAIMER, WITHHELD_NOTICE, OutputValidateNode
from src.nodes.response_generate import ResponseGenerateNode, load_narrative_prompt
from src.schemas.state import LogisticsKPINarrativeState

# src/graph/graph.py -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_PATHS = {
    "kpi_thresholds_path": "config/kpi-thresholds.yaml",
    "audience_formats_path": "config/audience-formats.yaml",
    "narrative_prompt_path": "prompts/narrative.md",
}


def runtime_config(project_root: Optional[Path] = None) -> dict[str, Any]:
    """Load ``config/config.yaml`` — the live runtime parameters."""
    root = Path(project_root) if project_root is not None else _REPO_ROOT
    loaded = load_agent_config(root)
    return dict(loaded) if isinstance(loaded, dict) else {}


def _is_error(result: Mapping[str, Any]) -> bool:
    status = result.get("status")
    # The status may be the enum or its value depending on who wrote it.
    return status in (AgentStatus.ERROR, AgentStatus.ERROR.value)


class _SequentialSlotNode(FunctionNode):
    """Runs an ordered list of sub-nodes inline, merging their partial updates."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    _sub_nodes: tuple[Any, ...] = ()

    def execute(self, state: AgentState) -> dict[str, Any]:
        slot_name = self.__class__.__name__
        if _is_error(state):
            emit_trace_event(
                "slot_skipped",
                {"slot": slot_name, "reason": "upstream refusal"},
                state,
            )
            return {"status": state.get("status")}

        merged = dict(state)
        accumulated: dict[str, Any] = {}
        history: list[str] = []
        short_circuited = False
        for node in self._sub_nodes:
            result = node(merged) or {}
            merged = {**merged, **result}
            history.extend(result.get("node_history", []))
            accumulated = {**accumulated, **result}
            if _is_error(result):
                short_circuited = True
                break

        emit_trace_event(
            "slot_complete",
            {
                "slot": slot_name,
                "sub_nodes_run": len(history),
                "short_circuited": short_circuited,
            },
            state,
        )

        # Sub-node names stay in the history so the audit trail records the real
        # execution order. Per-node timings do not survive the merge — the
        # framework rebuilds that field from the incoming state — so they are
        # dropped here rather than shipped half-populated.
        accumulated["node_history"] = history
        accumulated.pop("execution_time", None)
        return accumulated


class PreProcessSlotNode(_SequentialSlotNode):
    """pre_process: validate the caller payload, then resolve the audience format."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, input_validate: InputValidateNode, intent_classify: IntentClassifyNode) -> None:
        self._sub_nodes = (input_validate, intent_classify)


class MainSlotNode(_SequentialSlotNode):
    """main: produce the narrative."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, response_generate: ResponseGenerateNode) -> None:
        self._sub_nodes = (response_generate,)


class PostProcessSlotNode(_SequentialSlotNode):
    """post_process: run the release checks before anything leaves the agent."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, output_validate: OutputValidateNode) -> None:
        self._sub_nodes = (output_validate,)


class LogisticsKPINarrativeSummaryAgent(AgentBaseGraph):
    """Turns a period's logistics KPI figures into an audience-shaped narrative."""

    def __init__(
        self,
        config: Optional[Mapping[str, Any]] = None,
        *,
        project_root: Optional[Path] = None,
        llm_client: Optional[Any] = None,
    ) -> None:
        root = Path(project_root) if project_root is not None else _REPO_ROOT
        if not config:
            # The Marketplace runner constructs the graph with NO config — config/config.yaml
            # is never loaded on that path — so a constructor that only
            # reads injected config raised ValueError on every Marketplace pod. Load the
            # manifest's own config/config.yaml here; an injected config still wins.
            config = load_agent_config(root)
        super().__init__(config=dict(config) if config else {})

        security = dict(self.config.get("security") or {"s3_gate_enabled": True})
        if security.get("s3_gate_enabled") is False:
            raise ValueError("security.s3_gate_enabled must be true — the output gate is mandatory")
        self._security_config = security

        kpi_fields = self.config.get("kpi_fields")
        if not kpi_fields:
            raise ValueError("config/config.yaml must declare a non-empty kpi_fields list")

        kpi_thresholds = _load_yaml(root / self._path("kpi_thresholds_path"))
        audience_formats = _load_yaml(root / self._path("audience_formats_path"))
        system_prompt, user_template = load_narrative_prompt(root / self._path("narrative_prompt_path"))

        self._input_validate = InputValidateNode(kpi_fields=list(kpi_fields))
        self._intent_classify = IntentClassifyNode(audience_formats)
        self._response_generate = ResponseGenerateNode(
            kpi_thresholds=kpi_thresholds,
            kpi_fields=list(kpi_fields),
            system_prompt=system_prompt,
            user_prompt_template=user_template,
            llm_client=llm_client,
        )
        self._output_validate = OutputValidateNode(
            disclaimer=str(self.config.get("output_disclaimer") or DEFAULT_DISCLAIMER)
        )

    def _path(self, key: str) -> str:
        value = self.config.get(key)
        return str(value) if value else _DEFAULT_PATHS[key]

    @property
    def name(self) -> str:
        return "LogisticsKPINarrativeSummaryAgent"

    @property
    def state_schema(self) -> type:
        """The graph's own state, not the framework base.

        The compiled graph derives its channels from this schema, so any field
        it does not declare is dropped between nodes. Leaving the framework base
        in place here means every domain field written by one node is invisible
        to the next — the pipeline runs end to end and produces nothing.
        """
        return LogisticsKPINarrativeState

    # ── backbone wiring ──────────────────────────────────────

    def register_nodes(self) -> None:
        super().register_nodes()  # fills initialize + finalize
        self._nodes["pre_process"] = PreProcessSlotNode(
            input_validate=self._input_validate,
            intent_classify=self._intent_classify,
        )
        self._nodes["main"] = MainSlotNode(response_generate=self._response_generate)
        self._nodes["post_process"] = PostProcessSlotNode(output_validate=self._output_validate)

    def get_output(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Surface the narrative only on a successful, released run.

        The inherited accessor resolves the output as ``formatted_output or
        result`` with no status check, so on any non-success it hands back
        whatever the pipeline last put in those fields — including a narrative
        the release checks refused. Anything other than success therefore returns
        the withheld notice and nothing else.
        """
        status = state.get("status")
        released = status in (AgentStatus.SUCCESS, AgentStatus.SUCCESS.value)
        return {
            "output": state.get("narrative_output") if released else self._withheld_output(state),
            "status": status,
            "blocked": bool(state.get("blocked", False)),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }

    @staticmethod
    def _withheld_output(state: Mapping[str, Any]) -> Optional[str]:
        """Return the refusal notice, never the text the refusal withheld.

        Only the node's own placeholder is passed through. Reading
        ``formatted_output`` unconditionally would surface a stale successful
        value when the run failed after that field was written.
        """
        notice = state.get("formatted_output")
        return WITHHELD_NOTICE if notice == WITHHELD_NOTICE else None


# Entry points and the manifest refer to the class by name; this alias keeps the
# generic name working for callers that expect it.
Graph = LogisticsKPINarrativeSummaryAgent


def build_default(
    project_root: Optional[Path] = None,
    *,
    llm_client: Optional[Any] = None,
) -> LogisticsKPINarrativeSummaryAgent:
    """Construct the agent from the bundled runtime configuration.

    Reads ``config/config.yaml`` for the KPI field list, the side-car paths and
    the disclaimer, then the side-car files themselves. Pass ``llm_client`` to
    supply a model; without one the agent composes the report deterministically.
    """
    root = Path(project_root) if project_root is not None else _REPO_ROOT
    return LogisticsKPINarrativeSummaryAgent(config=runtime_config(root), project_root=root, llm_client=llm_client)


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    return data
