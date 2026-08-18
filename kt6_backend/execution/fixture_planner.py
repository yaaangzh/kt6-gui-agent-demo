from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..asset_inventory import compact_text, strong_identity_key
from ..ui_graph import SCHEMA_VERSION as UI_GRAPH_SCHEMA_VERSION


class FixturePlanningError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class FixturePlanner:
    """Deterministic decision fixture over a freshly generated UI Graph."""

    GOALS = {
        "open_asset_details": {
            "action": "open_asset_details",
            "observed_action_id": "device.details",
        }
    }

    def plan(
        self,
        graph: Mapping[str, Any],
        intent: Mapping[str, Any],
    ) -> dict[str, Any]:
        if graph.get("schema_version") != UI_GRAPH_SCHEMA_VERSION:
            raise FixturePlanningError("fixture_ui_graph_schema_mismatch")
        goal = compact_text(intent.get("goal"), 100)
        asset_id = compact_text(intent.get("asset_id"), 200)
        goal_spec = self.GOALS.get(goal)
        if goal_spec is None or not asset_id:
            raise FixturePlanningError("fixture_intent_unsupported")
        nodes = graph.get("nodes")
        if not isinstance(nodes, list):
            raise FixturePlanningError("fixture_ui_graph_nodes_missing")

        matches = []
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            source = node.get("source")
            interaction = node.get("interaction")
            if not isinstance(source, Mapping) or not isinstance(
                interaction, Mapping
            ):
                continue
            owner = compact_text(node.get("owner_business_id"), 200)
            if not owner or strong_identity_key(
                "asset_id", owner
            ) != strong_identity_key("asset_id", asset_id):
                continue
            if (
                source.get("kind") == "cdp"
                and compact_text(node.get("action_id"), 200).casefold()
                == goal_spec["observed_action_id"]
                and interaction.get("candidate") is True
                and interaction.get("status") == "candidate_only"
                and node.get("disabled") is not True
                and node.get("can_click_now") is False
                and node.get("safe_for_execution") is False
            ):
                matches.append(node)

        if len(matches) != 1:
            raise FixturePlanningError(
                "fixture_target_missing" if not matches else "fixture_target_ambiguous"
            )
        target = matches[0]
        return {
            "planner": "fixture_ui_graph",
            "goal": goal,
            "asset_id": asset_id,
            "action": goal_spec["action"],
            "capture_id": compact_text(graph.get("capture_id"), 200),
            "graph_id": compact_text(graph.get("graph_id"), 300),
            "op": "click",
            "target_node_id": compact_text(target.get("id"), 300),
            "safe_for_execution": False,
        }


__all__ = ["FixturePlanner", "FixturePlanningError"]
