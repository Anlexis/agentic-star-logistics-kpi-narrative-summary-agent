"""The Marketplace runner constructs the graph with NO config — `agent_cls()` — and
`config/config.yaml` is never loaded on that path. A constructor that only
reads injected config raised ValueError on every Marketplace pod. These construct the graph
exactly that way and run its own sign-off payload through it.
"""
import importlib
import json
import re
from pathlib import Path

from framework.schemas import InvocationContext
from framework.schemas.trust_level import TrustLevel

_REPO = Path(__file__).resolve().parents[2]
_PAYLOAD = json.loads((_REPO / "deploy" / "invoke_payload.json").read_text(encoding="utf-8"))
_CLASS = re.search(r'^class:\s*"?([\w.]+)"?', (_REPO / "config" / "agent.yaml").read_text(encoding="utf-8"), re.M).group(1)
_MODULE, _NAME = _CLASS.rsplit(".", 1)


def _graph():
    return getattr(importlib.import_module(_MODULE), _NAME)()      # no config injected


def _status(result):
    status = result.get("status")
    return getattr(status, "value", status)


def test_the_graph_constructs_with_no_injected_config():
    graph = _graph()
    assert graph.config.get("kpi_fields"), "config/config.yaml was not self-loaded"


def test_own_payload_succeeds_on_the_marketplace_shaped_call():
    result = _graph().invoke(
        _PAYLOAD["input"],
        session_id="",
        ctx=InvocationContext(caller_id="test", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        input_context={**_PAYLOAD.get("input_context", {}), "conversation_history": []},
    )
    assert _status(result) == "success"
