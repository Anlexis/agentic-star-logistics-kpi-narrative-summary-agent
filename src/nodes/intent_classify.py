"""Audience resolution — second step of the narrative pipeline.

Maps the validated ``audience_type`` onto the format bundle that shapes the
report: how long it may be, which sections it carries, and what tone it uses.
The bundles are loaded from configuration at construction time and validated
there, so a misconfigured deployment fails at start-up rather than on the first
request.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Mapping

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.utils.audit_logger import emit_trace_event


class IntentClassifyError(ValueError):
    """The audience could not be resolved. Node-internal control flow."""


class IntentClassifyNode(FunctionNode):
    """Resolves the active audience's format bundle.

    Reads ``audience_type`` from state — already checked against the allowed set
    by the input validator — and writes the resolved bundle to ``format_params``
    as a JSON string, so the checkpointed state holds no Python dictionaries.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, audience_formats: Mapping[str, Mapping[str, Any]]) -> None:
        if not audience_formats:
            raise ValueError("audience_formats must be a non-empty mapping")

        for audience, bundle in audience_formats.items():
            if not isinstance(bundle, Mapping):
                raise ValueError(f"audience_formats[{audience!r}] must be a mapping, got {type(bundle).__name__}")
            for key in ("length_tokens_max", "sections", "tone"):
                if key not in bundle:
                    raise ValueError(f"audience_formats[{audience!r}] missing required key {key!r}")
            if not isinstance(bundle["length_tokens_max"], int) or bundle["length_tokens_max"] <= 0:
                raise ValueError(f"audience_formats[{audience!r}].length_tokens_max must be a positive int")
            if not isinstance(bundle["sections"], list) or not bundle["sections"]:
                raise ValueError(f"audience_formats[{audience!r}].sections must be a non-empty list")
            if not isinstance(bundle["tone"], str) or not bundle["tone"]:
                raise ValueError(f"audience_formats[{audience!r}].tone must be a non-empty string")

        self._audience_formats = {k: dict(v) for k, v in audience_formats.items()}

    def execute(self, state: AgentState) -> dict[str, Any]:
        try:
            bundle = self._resolve(state)
        except IntentClassifyError as exc:
            emit_trace_event(
                "intent_classify",
                {"resolved": False, "reason": str(exc)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"IntentClassifyNode: {exc}"],
            }

        emit_trace_event(
            "intent_classify",
            {"resolved": True, "audience_type": state.get("audience_type")},
            state,
        )

        # Sorted keys keep the serialised form stable across runs.
        return {
            "status": AgentStatus.SUCCESS.value,
            "format_params": json.dumps(bundle, sort_keys=True),
        }

    def _resolve(self, state: Mapping[str, Any]) -> dict[str, Any]:
        audience = state.get("audience_type")
        if audience is None:
            raise IntentClassifyError("audience_type is required")
        if not isinstance(audience, str):
            raise IntentClassifyError(f"audience_type must be a string, got {type(audience).__name__}")
        if audience not in self._audience_formats:
            known = sorted(self._audience_formats.keys())
            raise IntentClassifyError(f"audience_type must be one of {known}")
        return self._audience_formats[audience]
