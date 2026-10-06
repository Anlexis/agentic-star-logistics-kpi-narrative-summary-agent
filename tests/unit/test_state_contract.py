"""The state schema contract."""

from __future__ import annotations

import ast

import pytest


@pytest.fixture
def state_source(repo_root) -> str:
    return (repo_root / "src" / "schemas" / "state.py").read_text(encoding="utf-8")


class TestStateModule:
    def test_extends_the_framework_state(self, state_source):
        tree = ast.parse(state_source)
        imported = any(
            isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("framework.schemas.agent_state")
            and any(alias.name == "AgentState" for alias in node.names)
            for node in ast.walk(tree)
        )
        assert imported

    def test_does_not_shadow_the_framework_class(self, state_source):
        tree = ast.parse(state_source)
        classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        assert "AgentState" not in classes
        assert "LogisticsKPINarrativeState" in classes

    def test_declared_base_is_the_framework_state(self, state_source):
        tree = ast.parse(state_source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "LogisticsKPINarrativeState":
                bases = [b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in node.bases]
                assert "AgentState" in bases
                return
        pytest.fail("LogisticsKPINarrativeState not found")


class TestGraphUsesIt:
    def test_the_graph_declares_this_schema(self):
        """The compiled graph derives its channels from the declared schema, so a
        graph still declaring the framework base drops every domain field between
        nodes and produces nothing."""
        from src.graph.graph import LogisticsKPINarrativeSummaryAgent
        from src.schemas.state import LogisticsKPINarrativeState

        agent = LogisticsKPINarrativeSummaryAgent.__new__(LogisticsKPINarrativeSummaryAgent)
        assert agent.state_schema is LogisticsKPINarrativeState

    def test_every_field_the_pipeline_writes_is_declared(self):
        """A field written by a node but absent from the schema is silently
        dropped by the graph runtime."""
        from src.nodes.output_validate import OUTPUT_BEARING_FIELDS
        from src.schemas.state import LogisticsKPINarrativeState

        declared = set(LogisticsKPINarrativeState.__annotations__)
        for ancestor in LogisticsKPINarrativeState.__mro__[1:]:
            declared |= set(getattr(ancestor, "__annotations__", {}))
        for field in ("audience_type", "format_params", "blocked", *OUTPUT_BEARING_FIELDS):
            assert field in declared, f"{field} is written by a node but not declared in the state"
