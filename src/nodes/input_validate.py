"""Input validation — first step of the narrative pipeline.

Reads the caller's structured invocation parameters, checks every field against
explicit bounds, normalises what survives into state, and refuses everything
else. Nothing downstream re-checks these values, so this node is the whole
boundary between caller data and the pipeline.

Three things happen here that are worth stating plainly:

* The raw parameters are screened for instruction-shaped content before any
  structural check, so a hostile field name is refused as an injection attempt
  rather than as an unknown key.
* A driver reference is replaced by a salted digest, or dropped when no salt is
  provisioned. It is never carried forward in the clear.
* The raw parameter mapping is cleared from state once the validated values have
  been extracted, so the caller's original payload does not reach the
  checkpoint, later nodes, or the response envelope.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Iterable, Mapping

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.utils.audit_logger import emit_trace_event

from src.services.anonymization import hash_driver_ref, resolve_salt
from src.services.input_contract import (
    MAX_CONTEXT_FIELDS,
    MAX_KPI_ENTRIES,
    ContractViolation,
    bounded_mapping,
    finite_in_range,
    inert_driver_ref,
    inert_field_name,
    one_of,
    screen_instruction_content,
)

AUDIENCE_VALUES = frozenset({"executive", "operations", "customer"})
DEFAULT_AUDIENCE = "executive"

_DRIVER_REF_MARKER = "driver_ref"
_ACCEPTED_FIELDS = frozenset({"kpi_data", "baseline_data", "audience_type"})

# Keys the platform itself puts into input_context, not the caller. The Marketplace
# runner invokes every agent as
#     agent.invoke(message, ctx=ctx, input_context={"conversation_history": history})
# (agenticstar-agentcore, shared/bootstrap/marketplace_app.py), whatever the user typed.
# Refusing it as an unknown field refused every chat request before the question was
# read. Discarded, not validated: nothing in this pipeline reads prior turns, and
# screening a transcript would let one earlier message refuse every later one. Discarding
# adds no exposure — the backbone's first node has already copied the raw input_context
# into state before this contract runs.
PLATFORM_RESERVED_KEYS = frozenset({"conversation_history"})


class ValidationError(ValueError):
    """A caller payload failed validation. Node-internal control flow."""


class InputValidateNode(FunctionNode):
    """Validates and normalises the caller-supplied KPI payloads."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, kpi_fields: Iterable[str]) -> None:
        self.kpi_fields = list(kpi_fields)
        if not self.kpi_fields:
            raise ValueError("kpi_fields must declare at least one field")

    def execute(self, state: AgentState) -> dict[str, Any]:
        try:
            result = self._validate(state)
        except (ValidationError, ContractViolation) as exc:
            emit_trace_event(
                "input_validate",
                {"accepted": False, "reason": str(exc)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"InputValidateNode: {exc}"],
                # The rejected payload never travels further, not even on the
                # refusal path.
                "input_context": {},
                "kpi_data": None,
                "baseline_data": None,
            }

        emit_trace_event(
            "input_validate",
            {
                "accepted": True,
                "fields_validated": len(self.kpi_fields),
                "has_baseline": result.get("baseline_data") is not None,
                "audience_type": result.get("audience_type"),
            },
            state,
        )
        return result

    # ── validation ───────────────────────────────────────────

    def _validate(self, state: Mapping[str, Any]) -> dict[str, Any]:
        context = state.get("input_context")
        if context is None:
            context = {}
        context = bounded_mapping(context, field="input_context", max_entries=MAX_CONTEXT_FIELDS)
        context = {k: v for k, v in context.items() if k not in PLATFORM_RESERVED_KEYS}

        reason = screen_instruction_content(context, field="input_context")
        if reason is not None:
            raise ValidationError(reason)

        unknown = sorted(set(context) - _ACCEPTED_FIELDS)
        if unknown:
            # Naming the count rather than the names: an unrecognised key is
            # caller-controlled text and must not be echoed.
            raise ValidationError(
                f"input_context carries {len(unknown)} field(s) that are not part of "
                f"the contract; accepted fields are {sorted(_ACCEPTED_FIELDS)}"
            )

        salt = resolve_salt()

        kpi_raw = context.get("kpi_data")
        if kpi_raw is None:
            raise ValidationError("input_context.kpi_data is required")
        kpi_parsed = self._parse_payload(kpi_raw, name="input_context.kpi_data")
        self._assert_required_fields(kpi_parsed)
        kpi_normalised = self._normalise(kpi_parsed, salt=salt, field="input_context.kpi_data")

        result: dict[str, Any] = {
            "status": AgentStatus.SUCCESS.value,
            "kpi_data": json.dumps(kpi_normalised, sort_keys=True),
            "baseline_data": None,
            # The validated values now live in their own state fields; the raw
            # caller mapping is not needed again and must not persist.
            "input_context": {},
        }

        baseline_raw = context.get("baseline_data")
        if baseline_raw is not None:
            baseline_parsed = self._parse_payload(baseline_raw, name="input_context.baseline_data")
            baseline_normalised = self._normalise(baseline_parsed, salt=salt, field="input_context.baseline_data")
            result["baseline_data"] = json.dumps(baseline_normalised, sort_keys=True)

        audience = context.get("audience_type")
        result["audience_type"] = (
            DEFAULT_AUDIENCE
            if audience is None
            else one_of(audience, field="input_context.audience_type", allowed=AUDIENCE_VALUES)
        )
        return result

    # ── helpers ──────────────────────────────────────────────

    def _parse_payload(self, raw: object, *, name: str) -> Mapping[str, Any]:
        """Accept either a JSON object or a JSON string encoding one."""
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"{name} is not valid JSON: {exc.msg}") from exc
        return bounded_mapping(raw, field=name, max_entries=MAX_KPI_ENTRIES)

    def _assert_required_fields(self, parsed: Mapping[str, Any]) -> None:
        missing = [field for field in self.kpi_fields if field not in parsed]
        if missing:
            # These names come from configuration, not from the caller.
            raise ValidationError(f"input_context.kpi_data is missing {missing}")

    def _normalise(self, parsed: Mapping[str, Any], *, salt: str | None, field: str) -> dict[str, Any]:
        """Return the payload with every value checked and driver refs anonymised.

        Every entry is checked, not only the declared KPI fields: a field the
        configuration does not declare is still caller data that renders into the
        prompt, so it goes through the same name and value rules.
        """
        normalised: dict[str, Any] = {}
        for key, value in parsed.items():
            name = inert_field_name(key, field=f"{field} field")
            if _DRIVER_REF_MARKER in name:
                reference = inert_driver_ref(value, field=f"{field}.{name}")
                if salt is None:
                    # No salt provisioned — drop rather than pass through.
                    continue
                normalised[name] = hash_driver_ref(reference, salt)
                continue
            normalised[name] = finite_in_range(value, field=f"{field}.{name}")
        return normalised
