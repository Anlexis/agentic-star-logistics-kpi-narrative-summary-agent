"""Standalone HTTP entry point.

An adapter only: authentication, size and shape limits, and the credential
screen below. Every domain rule lives in the graph, so a caller reaching the
agent through the hosted platform instead of this server gets the same
behaviour.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.security.credential_detector import detect_credentials_in_value
from framework.utils.audit_logger import emit_trace_event
from shared.secrets import factory as secrets_factory

from src.graph.graph import LogisticsKPINarrativeSummaryAgent, runtime_config

app = FastAPI(title="Logistics KPI Narrative Summary")

_config = runtime_config()
_secrets_provider = secrets_factory(namespace="log", agent_name="LogisticsKPINarrativeSummaryAgent")

# get(), not require(): a deployment without a model key is supported — the
# agent composes the report deterministically — so a missing key must degrade
# rather than stop the process from starting.
_model_key = _secrets_provider.get("OPENAI_API_KEY")
_llm: Any = None
if _model_key:
    # Imported inside the branch: the provider client pulls in a heavy
    # dependency tree that the no-key path has no use for.
    from shared.services.llm.openai_client import OpenAIClient

    _llm_settings = dict(_config.get("llm") or {})
    _llm_settings["api_key"] = _model_key
    _llm = OpenAIClient(config=_llm_settings)
else:
    logging.getLogger(__name__).info("No model API key provisioned — reports will be composed deterministically.")

agent = LogisticsKPINarrativeSummaryAgent(config=_config, llm_client=_llm)

# Mirror the registry's conditional checkpointer: without one, memory and
# human-in-the-loop silently do nothing on this path.
_needs_checkpointer = bool(_config.get("memory_enabled") or _config.get("hitl", {}).get("enabled", False))
agent.compile(checkpointer=MemorySaver() if _needs_checkpointer else None)
agent.provision_secrets(_secrets_provider)

# Coarse ceiling on the serialised parameters. The graph enforces per-field
# bounds — inert names, finite values, entry caps; this stops an oversized
# payload from reaching the graph at all.
_MAX_INPUT_CONTEXT_BYTES = 262_144

# ── Structured-parameter credential screen ───────────────────────────────────
# Why this runs here rather than inside a node:
#
# The framework's mandatory output gate scans every value of every node result
# for credential patterns, and the backbone's first node returns the structured
# parameters verbatim in its own result. A credential-shaped string anywhere in
# them therefore fails the FIRST node of the graph, before any of this
# template's code runs. The caller gets an error with no indication of which
# field caused it, and on a hosted conversation the same parameters are replayed
# every turn, so the session never recovers on its own.
#
# The request cannot succeed either way. Screening here does not change what is
# accepted; it turns an opaque failure into one the caller can act on.
#
# The screen calls the SAME detector the gate calls, on the SAME object, so what
# this adapter refuses and what the gate blocks are one set by construction —
# there is no local pattern list that could drift from it. Scanning field by
# field composes exactly to scanning the whole mapping, because the detector on
# a mapping is the union over its values; that identity is what lets the refusal
# name the offending field without widening or narrowing the match.
#
# Field names are caller data too, so a name is repeated back only when it is
# short, inert, and not itself credential-shaped. The rejected value and the
# matched text are never echoed — not in the response, not in the audit record.
_SAFE_FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _field_reference(name: object, index: int) -> str:
    """Render a caller-supplied field name into something safe to put in a message."""
    if isinstance(name, str) and _SAFE_FIELD_NAME_RE.match(name) and not detect_credentials_in_value(name):
        return f"input_context.{name}"
    return f"input_context field #{index}"


def screen_input_context(input_context: dict[str, Any]) -> str | None:
    """Return a reference to the first credential-bearing field, else None."""
    for index, (name, value) in enumerate(input_context.items(), start=1):
        if detect_credentials_in_value(value):
            return _field_reference(name, index)
    return None


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Structured invocation parameters: kpi_data, baseline_data, audience_type —
    # validated field by field inside the graph.
    input_context: dict[str, Any] | None = None


def _bearer_matches(supplied: str, expected: str) -> bool:
    """Constant-time bearer comparison that is safe for non-ASCII header input."""
    # Compare bytes: compare_digest raises TypeError on non-ASCII str input
    # (headers decode as latin-1), which would surface as a 500 rather than a 401.
    return secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode())


def _resolve_standalone_trust(
    current: TrustLevel,
    authorization: str,
    invoke_auth_token: str | None,
    internal_runner_token: str | None,
) -> TrustLevel:
    """Authenticate standalone callers without letting an external token elevate.

    Required here specifically: every domain node declares VERIFIED_EXTERNAL and
    nothing else sets a trust level in a standalone deployment, so without this
    boundary every request would arrive anonymous and be denied at the trust gate.
    Trust established upstream is never demoted.
    """
    if current is not TrustLevel.ANONYMOUS:
        return current
    if internal_runner_token and _bearer_matches(authorization, internal_runner_token):
        return TrustLevel.INTERNAL
    if invoke_auth_token and _bearer_matches(authorization, invoke_auth_token):
        return TrustLevel.VERIFIED_EXTERNAL
    if internal_runner_token or invoke_auth_token:
        # Generic body on purpose — do not reveal whether the token was absent,
        # malformed, or simply wrong.
        raise HTTPException(status_code=401, detail="Token is invalid or expired.")
    return TrustLevel.ANONYMOUS


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Any:
    # This adapter is the entry-point auth boundary. Both tokens are
    # deployment-level caller credentials, not agent secrets: no invocation
    # context exists before this point, so the secret provider does not apply.
    trust = _resolve_standalone_trust(
        getattr(request.state, "trust_level", TrustLevel.ANONYMOUS),
        request.headers.get("authorization", ""),
        os.environ.get("INVOKE_AUTH_TOKEN"),
        os.environ.get("STG_INTERNAL_RUNNER_TOKEN"),
    )

    input_context = req.input_context or {}
    if input_context and len(json.dumps(input_context, default=str)) > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(status_code=413, detail="input_context exceeds the maximum allowed size.")

    offending_field = screen_input_context(input_context)
    if offending_field is not None:
        emit_trace_event(
            "input_context_credential_refused",
            {"field": offending_field},
            {"session_id": req.session_id},
        )
        # 400, not 422: pydantic owns 422 and answers it with a list of error
        # objects, so reusing it would make client handling ambiguous.
        raise HTTPException(
            status_code=400,
            detail=(
                f"{offending_field} contains a credential-shaped value. Remove API keys, "
                "tokens and connection strings from input_context and retry."
            ),
        )

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return agent.invoke(req.input, ctx=ctx, input_context=input_context)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "LogisticsKPINarrativeSummaryAgent"}
