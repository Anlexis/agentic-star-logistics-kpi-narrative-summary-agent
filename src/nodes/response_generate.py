"""Narrative generation — the main step of the pipeline.

The comparison against the baseline is computed here, deterministically, before
any model is called: whether a movement is an improvement or a deterioration
depends on the KPI, and a lower cost per kilogram is good news while a lower
on-time-delivery rate is not. Resolving direction and severity in code means the
model is asked to phrase a conclusion, never to reach one.

Two output paths, both producing a real report from the caller's data:

* With a language model configured, the resolved comparison is rendered into
  prose in the requested tone and length.
* Without one, the same comparison is composed into a structured report by the
  deterministic writer below. A deployment with no model key configured is a
  supported deployment, so this path is a first-class output, not a placeholder.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar, Iterable, Mapping, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.utils.audit_logger import emit_trace_event

from src.services.input_contract import ContractViolation, finite_in_range

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "normal": 2, "unknown": 3}

# How a configured section name maps onto the content this writer can produce.
# An unrecognised name falls back to the per-KPI movement list, which is the
# safe default: it is the section every audience bundle wants at least once.
_SECTION_KINDS: dict[str, str] = {
    "headline": "headline",
    "summary": "headline",
    "sla_summary": "headline",
    "top_movers": "movements",
    "delta_table": "movements",
    "deviation_context": "movements",
    "exception_top3": "exceptions",
    "exceptions": "exceptions",
    "exception_analysis": "exceptions",
    "root_cause": "exceptions",
    "issues": "exceptions",
    "board_action": "actions",
    "action_items": "actions",
    "actions": "actions",
    "remediation": "actions",
    "recommendations": "actions",
    "next_steps": "actions",
}
_TOP_EXCEPTIONS = 3
# Guards against a model that answers with a wall of text; the audience bundle's
# own token ceiling is advisory to the model, this is the hard cap.
_MAX_NARRATIVE_CHARS = 20_000


class ResponseGenerateError(ValueError):
    """The narrative could not be produced. Node-internal control flow."""


class ResponseGenerateNode(FunctionNode):
    """Computes the per-KPI comparison and produces the narrative.

    Constructor inputs:
        kpi_thresholds: per-KPI direction and warning/critical bands
        kpi_fields: the declared KPI field names
        system_prompt / user_prompt_template: the model-facing prompt
        llm_client: optional; an object exposing ``complete(messages) -> dict``

    The advisory disclaimer is not enforced here — the output validator owns
    that check, so it holds on both output paths.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        kpi_thresholds: Mapping[str, Mapping[str, Any]],
        kpi_fields: Iterable[str],
        system_prompt: str,
        user_prompt_template: str,
        llm_client: Optional[Any] = None,
    ) -> None:
        if not kpi_thresholds:
            raise ValueError("kpi_thresholds must be a non-empty mapping")
        if llm_client is not None and not hasattr(llm_client, "complete"):
            raise TypeError("llm_client must expose a .complete(messages) method")

        kpi_fields = list(kpi_fields)
        if not kpi_fields:
            raise ValueError("kpi_fields must declare at least one field")
        if not system_prompt or not user_prompt_template:
            raise ValueError("system_prompt and user_prompt_template must be non-empty")

        for field, spec in kpi_thresholds.items():
            if not isinstance(spec, Mapping):
                raise ValueError(f"kpi_thresholds[{field!r}] must be a mapping")
            direction = spec.get("direction")
            if direction not in ("higher_is_better", "lower_is_better"):
                raise ValueError(
                    f"kpi_thresholds[{field!r}].direction must be "
                    f"'higher_is_better' or 'lower_is_better', got {direction!r}"
                )
            # The threshold table is configuration, but it is numeric input all
            # the same: a non-finite band would make every severity comparison
            # return False and silently classify a breach as normal.
            for key in ("warning_delta", "critical_delta"):
                try:
                    finite_in_range(spec.get(key), field=f"kpi_thresholds[{field!r}].{key}")
                except ContractViolation as exc:
                    raise ValueError(str(exc)) from exc

        self._thresholds = {k: dict(v) for k, v in kpi_thresholds.items()}
        self._llm = llm_client
        self._kpi_fields = kpi_fields
        self._system_prompt = system_prompt
        self._user_prompt_template = user_prompt_template

    # ── execute ──────────────────────────────────────────────

    def execute(self, state: AgentState) -> dict[str, Any]:
        try:
            return self._generate(state)
        except ResponseGenerateError as exc:
            emit_trace_event(
                "response_generate",
                {"produced": False, "reason": str(exc)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"ResponseGenerateNode: {exc}"],
            }

    def _generate(self, state: Mapping[str, Any]) -> dict[str, Any]:
        kpi_raw = state.get("kpi_data")
        if not kpi_raw:
            raise ResponseGenerateError("kpi_data is required")
        baseline_raw = state.get("baseline_data") or "{}"
        format_raw = state.get("format_params")
        if not format_raw:
            raise ResponseGenerateError("format_params is required")

        kpi = json.loads(kpi_raw)
        baseline = json.loads(baseline_raw)
        format_params = json.loads(format_raw)
        audience = state.get("audience_type") or "executive"

        delta_summary = self._compute_delta_summary(kpi, baseline)

        if self._llm is None:
            narrative = self._compose_narrative(
                audience=audience,
                kpi=kpi,
                baseline=baseline,
                delta_summary=delta_summary,
                format_params=format_params,
            )
            generated_by = "deterministic"
        else:
            narrative = self._call_model(
                audience=audience,
                kpi_data=kpi_raw,
                baseline_data=baseline_raw,
                delta_summary=delta_summary,
                format_params=format_params,
            )
            generated_by = "model"

        emit_trace_event(
            "response_generate",
            {
                "produced": True,
                "generated_by": generated_by,
                "audience_type": audience,
                "narrative_chars": len(narrative),
                "kpi_fields": len(self._kpi_fields),
            },
            state,
        )

        return {
            "status": AgentStatus.SUCCESS.value,
            "delta_summary": json.dumps(delta_summary, sort_keys=True),
            "narrative_output": narrative,
        }

    # ── model path ───────────────────────────────────────────

    def _call_model(
        self,
        *,
        audience: str,
        kpi_data: str,
        baseline_data: str,
        delta_summary: Mapping[str, Any],
        format_params: Mapping[str, Any],
    ) -> str:
        prompt = self._render_user_prompt(
            audience_type=audience,
            kpi_data=kpi_data,
            baseline_data=baseline_data,
            delta_summary=delta_summary,
            format_params=format_params,
        )
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": prompt},
        ]
        client = self._llm
        if client is None:  # pragma: no cover — the caller checks this first
            raise ResponseGenerateError("no model is configured")
        response = client.complete(messages)
        if not isinstance(response, Mapping):
            raise ResponseGenerateError("the model returned an unrecognised response shape")
        narrative = response.get("content")
        if not isinstance(narrative, str) or not narrative.strip():
            raise ResponseGenerateError("the model returned an empty narrative")
        if len(narrative) > _MAX_NARRATIVE_CHARS:
            raise ResponseGenerateError("the model returned a narrative beyond the length ceiling")
        return narrative

    def _render_user_prompt(
        self,
        *,
        audience_type: str,
        kpi_data: str,
        baseline_data: str,
        delta_summary: Mapping[str, Any],
        format_params: Mapping[str, Any],
    ) -> str:
        return self._user_prompt_template.format(
            audience_type=audience_type,
            tone=format_params.get("tone", "neutral"),
            length_tokens_max=format_params.get("length_tokens_max", 600),
            sections=", ".join(str(s) for s in format_params.get("sections", [])),
            kpi_data=kpi_data,
            baseline_data=baseline_data,
            delta_summary=json.dumps(delta_summary, sort_keys=True),
        )

    # ── deterministic path ───────────────────────────────────

    def _compose_narrative(
        self,
        *,
        audience: str,
        kpi: Mapping[str, Any],
        baseline: Mapping[str, Any],
        delta_summary: Mapping[str, Any],
        format_params: Mapping[str, Any],
    ) -> str:
        """Compose the report from the resolved comparison, without a model."""
        sections = [str(s) for s in format_params.get("sections", [])] or ["summary"]
        exceptions = self._rank_exceptions(delta_summary)
        lines: list[str] = [f"Logistics KPI summary — {audience} view", ""]

        for section in sections:
            key = section.strip().lower().replace(" ", "_").replace("-", "_")
            lines.append(f"## {section}")
            kind = _SECTION_KINDS.get(key, "movements")
            if kind == "headline":
                lines.extend(self._headline_lines(delta_summary, exceptions))
            elif kind == "exceptions":
                lines.extend(self._exception_lines(exceptions))
            elif kind == "actions":
                lines.extend(self._recommendation_lines(exceptions))
            else:
                lines.extend(self._movement_lines(kpi, baseline, delta_summary))
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _headline_lines(
        delta_summary: Mapping[str, Any],
        exceptions: list[tuple[str, Mapping[str, Any]]],
    ) -> list[str]:
        critical = sum(1 for e in delta_summary.values() if e.get("severity") == "critical")
        warning = sum(1 for e in delta_summary.values() if e.get("severity") == "warning")
        if not exceptions:
            return ["- All reported KPIs held within their configured bands this period."]
        worst = exceptions[0][0]
        return [
            f"- {critical} critical and {warning} warning band breach(es) this period.",
            f"- {worst} moved furthest against its target and leads the exception list.",
        ]

    def _movement_lines(
        self,
        kpi: Mapping[str, Any],
        baseline: Mapping[str, Any],
        delta_summary: Mapping[str, Any],
    ) -> list[str]:
        lines: list[str] = []
        for field in self._kpi_fields:
            entry = delta_summary.get(field, {})
            current = kpi.get(field)
            prior = baseline.get(field)
            delta = entry.get("delta")
            if current is None:
                lines.append(f"- {field}: not reported this period")
                continue
            if delta is None:
                lines.append(f"- {field}: {current:g} (no comparable prior period)")
                continue
            movement = "up" if delta > 0 else ("down" if delta < 0 else "unchanged")
            lines.append(
                f"- {field}: {current:g} vs {prior:g} ({movement} {abs(delta):g}) "
                f"— {entry.get('severity', 'unknown')}"
            )
        return lines

    @staticmethod
    def _exception_lines(exceptions: list[tuple[str, Mapping[str, Any]]]) -> list[str]:
        if not exceptions:
            return ["- No KPI breached its warning band this period."]
        return [
            f"- {field}: {entry['severity']} — moved {entry['delta']:+g} against "
            f"a {entry['direction'].replace('_', ' ')} target"
            for field, entry in exceptions
        ]

    @staticmethod
    def _recommendation_lines(exceptions: list[tuple[str, Mapping[str, Any]]]) -> list[str]:
        if not exceptions:
            return ["- Hold the current operating plan; no KPI requires intervention."]
        lines = [f"1. Address {exceptions[0][0]} first — it is the most severe exception this period."]
        lines.extend(
            f"{index}. Then review {field}, which also breached its band."
            for index, (field, _entry) in enumerate(exceptions[1:], start=2)
        )
        return lines

    def _rank_exceptions(self, delta_summary: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
        """Return the most severe breaches first, capped at three."""
        breaches = [
            (field, entry)
            for field, entry in delta_summary.items()
            if entry.get("severity") in ("warning", "critical") and isinstance(entry.get("delta"), (int, float))
        ]
        breaches.sort(
            key=lambda item: (
                _SEVERITY_ORDER.get(str(item[1].get("severity")), 9),
                -abs(float(item[1]["delta"])),
                item[0],
            )
        )
        return breaches[:_TOP_EXCEPTIONS]

    # ── comparison ───────────────────────────────────────────

    def _compute_delta_summary(
        self,
        kpi: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return ``{field: {direction, delta, severity}}`` for every declared KPI."""
        summary: dict[str, Any] = {}
        for field in self._kpi_fields:
            if field not in self._thresholds:
                # Declared as a KPI but with no band configured — say so rather
                # than guess a direction.
                summary[field] = {"direction": "unknown", "delta": None, "severity": "unknown"}
                continue

            spec = self._thresholds[field]
            current = kpi.get(field)
            prior = baseline.get(field)
            if current is None or prior is None:
                summary[field] = {
                    "direction": spec["direction"],
                    "delta": None,
                    "severity": "unknown",
                }
                continue

            delta = float(current) - float(prior)
            summary[field] = {
                "direction": spec["direction"],
                "delta": delta,
                "severity": self._classify_severity(delta, spec),
            }
        return summary

    @staticmethod
    def _classify_severity(delta: float, spec: Mapping[str, Any]) -> str:
        """Return ``normal`` | ``warning`` | ``critical`` for ``delta``.

        A movement in the good direction is always normal; only the bad
        direction is compared against the bands. The epsilon absorbs the
        floating-point noise of subtracting two near-equal ratios, so a value
        sitting exactly on a band does not fall to the wrong side of it.
        """
        warn = abs(float(spec["warning_delta"]))
        crit = abs(float(spec["critical_delta"]))
        bad = delta < 0 if spec["direction"] == "higher_is_better" else delta > 0
        if not bad:
            return "normal"

        epsilon = 1e-9
        abs_delta = abs(delta)
        if abs_delta + epsilon >= crit:
            return "critical"
        if abs_delta + epsilon >= warn:
            return "warning"
        return "normal"


# ── prompt loader ────────────────────────────────────────────


def load_narrative_prompt(prompt_path: Optional[Path] = None) -> tuple[str, str]:
    """Read the narrative prompt file and return ``(system_prompt, user_template)``.

    The file uses two headings, ``## System`` and ``## User template``; the user
    template is the first fenced code block under the second heading.
    """
    if prompt_path is None:
        prompt_path = Path(__file__).resolve().parents[2] / "prompts" / "narrative.md"

    text = Path(prompt_path).read_text(encoding="utf-8")
    system = _extract_section(text, "## System")
    user_section = _extract_section(text, "## User template")
    user_template = _extract_first_code_block(user_section)

    if not system or not user_template:
        raise ResponseGenerateError(f"prompt file {prompt_path} is missing System and/or User template sections")
    return system, user_template


def _extract_section(text: str, heading: str) -> str:
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.strip().startswith("## "):
            if line.strip() == heading:
                inside = True
                continue
            if inside:
                break
        elif inside:
            out.append(line)
    return "\n".join(out).strip()


def _extract_first_code_block(section: str) -> str:
    inside = False
    out: list[str] = []
    for line in section.splitlines():
        if line.startswith("```"):
            if inside:
                break
            inside = True
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()
