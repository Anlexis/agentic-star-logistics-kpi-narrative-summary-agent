"""Output validation — the release boundary of the pipeline.

Nothing leaves this agent without passing through here. The node enforces four
release rules and, when any of them fails, withholds the narrative rather than
returning it with an error attached:

* **No credentials in the report.** The scan is the framework's own credential
  detector rather than a private pattern list. A local list narrower than the
  framework's would be a bypass, not a shortcut: the framework re-scans every
  value this node returns and raises when it finds one the node missed, and the
  node's whole update — including the clearing below — is discarded when it does.
* **No individual driver identified.** A management report attributes movements
  to routes and depots, never to a named person or an operational reference.
* **The advisory disclaimer is present.** Injected when absent rather than
  refused: a missing disclaimer is a formatting slip, not a safety failure.
* **A refusal is a refusal.** On any violation the node returns an error status
  *and clears every field that carries answer text*, so nothing downstream can
  fall back to the ungated narrative.

Violation messages name the check that fired and the field it fired on. They
never carry the matched text: the matched text is the thing being withheld, and
a message repeating it would leak exactly what the refusal exists to contain.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar, Mapping, Optional, Pattern

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials
from framework.utils.audit_logger import emit_trace_event

DEFAULT_DISCLAIMER = "for internal reporting purposes"

# Every state field that can carry answer text or a payload. On a violation all
# of them are overwritten, so no accessor — present or future — can reach the
# withheld narrative through a field this node forgot about.
OUTPUT_BEARING_FIELDS = (
    "narrative_output",
    "formatted_output",
    "result",
    "delta_summary",
    "kpi_data",
    "baseline_data",
)

# The placeholder is deliberately non-empty. An empty string is falsy, and a
# falsy value is indistinguishable from an absent one to any caller that treats
# "no output" as "fall back to the previous field".
WITHHELD_NOTICE = "Output withheld: the generated narrative did not pass the release checks."

# An operational driver reference in the legacy fleet convention. These are
# hashed or dropped on the way in, so one appearing in the report means the
# model produced it rather than the pipeline carrying it through.
_UNHASHED_DRIVER_REF_PAT: Pattern[str] = re.compile(r"\bD-\d{3,}\b", flags=re.IGNORECASE)

# The anonymised form is a 16-character hex digest. It is safe to store but has
# no place in a narrative aimed at a human reader.
_DRIVER_REF_HASH_PAT: Pattern[str] = re.compile(r"\b[0-9a-f]{16}\b")

# A personal name attributed to a driving role. The role words are restricted to
# people — "carrier" is deliberately absent, because carriers are companies and
# naming one ("Carrier Nippon Express reported...") is ordinary, correct content
# for this report. Refusing that would block real work in the name of a rule
# about individuals.
_JA_DRIVER_NAME_PAT: Pattern[str] = re.compile(r"(?:ドライバー|運転手|乗務員)[\s:：]*[一-龥々ヵヶ]{2,4}")
# Two admitted forms, both explicit attributions: a labelled one ("driver: Smith")
# and a full name ("driver John Smith"). A single capitalised word after the role
# is deliberately NOT enough — "Driver Shortage", "Operator Coverage" and any
# title-cased heading would match, and withholding a report over a section title
# is the failure direction that stops real work.
_EN_DRIVER_NAME_PAT: Pattern[str] = re.compile(
    r"\b(?:driver|operator)\s*[:：]\s*[A-Z][a-z]{1,20}\b"
    r"|\b(?:driver|operator)\s+[A-Z][a-z]{1,20}\s+[A-Z][a-z]{1,20}\b"
)


class ReleaseCheckError(Exception):
    """A release rule failed. Node-internal control flow.

    The violation is surfaced to the backbone as an error status with the output
    fields cleared, not as a propagating exception: an exception here would be
    turned into a bare error update that clears nothing, which is the opposite of
    what a refusal needs to do.
    """


class OutputValidateNode(FunctionNode):
    """The final guard before the narrative leaves the pipeline."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, disclaimer: str = DEFAULT_DISCLAIMER) -> None:
        if not disclaimer or not disclaimer.strip():
            raise ValueError("disclaimer must be a non-empty string")
        self._disclaimer = disclaimer

    def execute(self, state: AgentState) -> dict[str, Any]:
        narrative = state.get("narrative_output")
        if not isinstance(narrative, str) or not narrative.strip():
            return self._withhold(state, reason="narrative_output is missing or empty")

        violation = self._release_violation(narrative)
        if violation is not None:
            return self._withhold(state, reason=violation)

        disclaimer_injected = False
        if self._disclaimer.lower() not in narrative.lower():
            narrative = self._inject_disclaimer(narrative)
            disclaimer_injected = True

        emit_trace_event(
            "output_validate",
            {
                "released": True,
                "disclaimer_injected": disclaimer_injected,
                "narrative_chars": len(narrative),
            },
            state,
        )

        return {
            "status": AgentStatus.SUCCESS.value,
            "blocked": False,
            "narrative_output": narrative,
            "formatted_output": narrative,
            # The caller's raw figures have served their purpose; they are not
            # part of the report and must not reach the checkpoint.
            "kpi_data": None,
            "baseline_data": None,
        }

    # ── refusal ──────────────────────────────────────────────

    def _withhold(self, state: Mapping[str, Any], *, reason: str) -> dict[str, Any]:
        """Return an error update that carries no released text.

        Every output-bearing field is overwritten — clearing only the field the
        violation was found in would leave the same text reachable through
        another one.
        """
        emit_trace_event(
            "output_validate",
            {"released": False, "reason": reason},
            state,
        )
        cleared: dict[str, Any] = {field: None for field in OUTPUT_BEARING_FIELDS}
        cleared["formatted_output"] = WITHHELD_NOTICE
        cleared["status"] = AgentStatus.ERROR.value
        cleared["blocked"] = True
        cleared["error_log"] = [f"OutputValidateNode: {reason}"]
        return cleared

    # ── release rules ────────────────────────────────────────

    @staticmethod
    def _release_violation(text: str) -> Optional[str]:
        """Return the name of the first failed release rule, or None when clean.

        The credential scan runs first and uses the framework detector, so this
        node's block set and the framework's are the same set by construction.
        Only the finding's *type* is reported — never its matched text.
        """
        findings = detect_credentials(text)
        if findings:
            return f"narrative_output carries a credential-shaped value " f"({findings[0]['type']}); output withheld"
        if _UNHASHED_DRIVER_REF_PAT.search(text):
            return "narrative_output names an operational driver reference"
        if _JA_DRIVER_NAME_PAT.search(text) or _EN_DRIVER_NAME_PAT.search(text):
            return "narrative_output attributes a movement to a named individual"
        if _DRIVER_REF_HASH_PAT.search(text):
            return "narrative_output carries an anonymised driver reference"
        return None

    def _inject_disclaimer(self, narrative: str) -> str:
        stripped = narrative.rstrip()
        return f"{stripped}\n\n— {self._disclaimer}.\n"
