"""State schema for the Logistics KPI Narrative Summary agent.

State is a flat TypedDict extending the framework ``AgentState`` so the
framework-reserved fields (``trace_id``, ``correlation_id``, ``node_history``,
...) flow through automatically. LangGraph checkpoints are msgpack-serialised,
so every field here is a primitive or a JSON string — never a Pydantic model, a
dataclass, or an arbitrary object.

A class named ``AgentState`` must not be defined in this module: it would shadow
the framework class the schema extends.
"""

from typing import Optional

from framework.schemas.agent_state import AgentState


class LogisticsKPINarrativeState(AgentState, total=False):
    """State for the logistics KPI narrative pipeline.

    ``kpi_data`` and ``baseline_data`` hold the caller's raw figures and are
    cleared by the output validator before the run finishes, so the persisted
    checkpoint never carries them.
    """

    # ── Validated caller inputs (cleared by the output validator) ──
    kpi_data: Optional[str]
    """JSON string mapping KPI field name to a finite numeric value.

    Shape: ``{"otd_pct": 0.94, "exception_rate": 0.018, ...}``. Field names are
    checked against the configured KPI field list, and every value is parsed
    through a finite, bounded numeric check before it reaches this field.
    """

    baseline_data: Optional[str]
    """JSON string of prior-period KPI values. Same shape as ``kpi_data``."""

    audience_type: Optional[str]
    """One of ``"executive"`` | ``"operations"`` | ``"customer"``.

    Chosen by the caller; an unknown value is refused.
    """

    # ── Computed during the run ──
    format_params: Optional[str]
    """JSON string resolved from ``audience_type``.

    Shape: ``{"length_tokens_max": int, "sections": [...], "tone": str}``.
    """

    delta_summary: Optional[str]
    """JSON string of the per-KPI comparison against the baseline.

    Shape: ``{"<kpi_name>": {"direction": str, "delta": float, "severity": str}}``
    where severity is one of ``normal`` | ``warning`` | ``critical`` | ``unknown``.
    """

    narrative_output: Optional[str]
    """The audience-formatted narrative, after the output validator has checked
    it and confirmed the advisory disclaimer is present."""

    blocked: Optional[bool]
    """True when the output validator refused to release the narrative."""


# Generic alias for callers that expect the conventional name; the domain
# name above stays the primary one.
State = LogisticsKPINarrativeState
