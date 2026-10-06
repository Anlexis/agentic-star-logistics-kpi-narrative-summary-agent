"""Audience resolution."""

from __future__ import annotations

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.intent_classify import IntentClassifyNode

_ERROR = AgentStatus.ERROR.value
_SUCCESS = AgentStatus.SUCCESS.value


@pytest.fixture
def node(audience_formats):
    return IntentClassifyNode(audience_formats)


class TestResolution:
    @pytest.mark.parametrize(
        "audience,tone,length",
        [
            ("executive", "concise_board_brief", 350),
            ("operations", "operational_detail", 900),
            ("customer", "client_facing_polite", 500),
        ],
    )
    def test_each_audience_resolves(self, node, audience, tone, length):
        result = node.execute({"audience_type": audience})
        assert result["status"] == _SUCCESS
        bundle = json.loads(result["format_params"])
        assert bundle["tone"] == tone
        assert bundle["length_tokens_max"] == length
        assert bundle["sections"]

    def test_format_params_is_a_json_string(self, node):
        result = node.execute({"audience_type": "executive"})
        assert isinstance(result["format_params"], str)

    def test_serialisation_is_stable(self, node):
        first = node.execute({"audience_type": "operations"})["format_params"]
        second = node.execute({"audience_type": "operations"})["format_params"]
        assert first == second


class TestRefusals:
    @pytest.mark.parametrize("audience", [None, 7, "vendor"])
    def test_unresolvable_audience_refuses(self, node, audience):
        result = node.execute({"audience_type": audience} if audience is not None else {})
        assert result["status"] == _ERROR
        assert result["error_log"]


class TestConstruction:
    def test_empty_bundle_map_is_rejected(self):
        with pytest.raises(ValueError, match="non-empty mapping"):
            IntentClassifyNode({})

    @pytest.mark.parametrize(
        "bundle,match",
        [
            ({"sections": ["a"], "tone": "t"}, "length_tokens_max"),
            ({"length_tokens_max": 0, "sections": ["a"], "tone": "t"}, "positive int"),
            ({"length_tokens_max": 10, "sections": [], "tone": "t"}, "non-empty list"),
            ({"length_tokens_max": 10, "sections": ["a"], "tone": ""}, "non-empty string"),
        ],
    )
    def test_malformed_bundle_is_rejected_at_construction(self, bundle, match):
        with pytest.raises(ValueError, match=match):
            IntentClassifyNode({"executive": bundle})
