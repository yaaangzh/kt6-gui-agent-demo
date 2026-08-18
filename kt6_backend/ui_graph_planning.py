from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from .ui_graph_reasoner import UIGraphReasoner
from .ui_operation_graph import validate


SCHEMA_VERSION = "kt6.ui-operation-planning-result.v1"
MAX_INSTRUCTION_CHARS = 8_000


class UIGraphNotFoundError(LookupError):
    """The requested page capture has no retained UI Graph."""


class UIGraphReasonerNotConfiguredError(RuntimeError):
    """Planning was requested before an internal reasoner was configured."""


MODEL_VIEW_SCHEMA_VERSION = "kt6.ui-graph-reasoning-view.v1"


class UIGraphProjectionError(ValueError):
    """A model-facing projection could not fit the configured byte budget."""


def _canonical_text(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _compact_node(node: Mapping[str, Any]) -> dict[str, Any] | None:
    node_id = node.get("id", node.get("node_id"))
    if not isinstance(node_id, str) or not node_id:
        return None
    source = node.get("source")
    source = source if isinstance(source, Mapping) else {}
    interaction = node.get("interaction")
    interaction = interaction if isinstance(interaction, Mapping) else {}
    candidate = interaction.get("candidate") is True
    interaction_status = interaction.get("status")
    if not isinstance(interaction_status, str) or not interaction_status.strip():
        interaction_status = "candidate_only" if candidate else "analysis_only"
    compact_source = {
        key: source[key]
        for key in (
            "kind",
            "source_ref",
            "frame_id",
            "document_id",
            "backend_node_id",
        )
        if key in source
    }
    result: dict[str, Any] = {
        "id": node_id,
        "kind": str(node.get("kind", "element"))[:100],
        "role": str(node.get("role", ""))[:100],
        "name": str(node.get("name", ""))[:300],
        "source": compact_source,
        "disabled": node.get("disabled") is True,
        "actionable": False,
        "safe_for_execution": False,
        "interaction": {
            "status": interaction_status[:100],
            "candidate": candidate,
            "authorized": False,
        },
    }
    for key in ("business_id", "owner_business_id", "action_id"):
        value = node.get(key)
        if isinstance(value, str) and value:
            result[key] = value[:300]
    return result


def _compact_edge(edge: Mapping[str, Any]) -> dict[str, Any] | None:
    source = edge.get("source")
    target = edge.get("target")
    edge_type = edge.get("type")
    if not all(isinstance(value, str) and value for value in (source, target, edge_type)):
        return None
    result = {
        "id": str(edge.get("id", ""))[:200],
        "type": edge_type[:100],
        "source": source[:200],
        "target": target[:200],
        "analysis_only": True,
        "safe_for_execution": False,
    }
    for key in ("relation_type", "relation_id", "action_id"):
        value = edge.get(key)
        if isinstance(value, str) and value:
            result[key] = value[:200]
    provenance = edge.get("provenance")
    if isinstance(provenance, Mapping):
        source_kind = provenance.get("source")
        if isinstance(source_kind, str) and source_kind:
            result["source_kind"] = source_kind[:100]
    return result


def _node_priority(node: Mapping[str, Any]) -> tuple[Any, ...]:
    source = node.get("source")
    source_kind = (
        str(source.get("kind", "")).casefold() if isinstance(source, Mapping) else ""
    )
    interaction = node.get("interaction")
    candidate = (
        isinstance(interaction, Mapping) and interaction.get("candidate") is True
    )
    if candidate and source_kind in {"dom", "cdp"}:
        rank = 0
    elif candidate:
        rank = 1
    elif node.get("kind") in {"business_object", "action_claim"}:
        rank = 2
    elif node.get("name"):
        rank = 3
    else:
        rank = 4
    return (rank, source_kind, str(node.get("id", "")))


def _reasoning_graph_text(
    graph: Mapping[str, Any],
    *,
    max_bytes: int,
) -> tuple[str, dict[str, Any]]:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 512:
        raise UIGraphProjectionError("reasoner graph byte budget must be at least 512")
    raw_nodes = graph.get("nodes", [])
    raw_edges = graph.get("edges", [])
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise UIGraphProjectionError("stored UI Graph nodes and edges must be arrays")

    nodes = [
        compact
        for raw in raw_nodes
        if isinstance(raw, Mapping)
        for compact in [_compact_node(raw)]
        if compact is not None
    ]
    edges = [
        compact
        for raw in raw_edges
        if isinstance(raw, Mapping)
        for compact in [_compact_edge(raw)]
        if compact is not None
    ]
    nodes.sort(key=_node_priority)
    node_by_id = {node["id"]: node for node in nodes}
    parent_edges = sorted(
        (edge for edge in edges if edge["type"] == "parent_of"),
        key=lambda edge: (edge["source"], edge["target"], edge["id"]),
    )
    optional_edges = sorted(
        (edge for edge in edges if edge["type"] != "parent_of"),
        key=lambda edge: (
            {
                "owner_of": 0,
                "supports_action": 1,
                "claims_business_object": 2,
                "semantic_relation": 3,
            }.get(edge["type"], 4),
            edge["source"],
            edge["target"],
            edge["id"],
        ),
    )
    parents_by_child: dict[str, set[str]] = {}
    for edge in parent_edges:
        parents_by_child.setdefault(edge["target"], set()).add(edge["source"])

    def selected_ids(prefix_count: int) -> set[str]:
        selected = {node["id"] for node in nodes[:prefix_count]}
        pending = list(selected)
        while pending:
            child = pending.pop()
            for parent in sorted(parents_by_child.get(child, ())):
                if parent in node_by_id and parent not in selected:
                    selected.add(parent)
                    pending.append(parent)
        return selected

    page = graph.get("page")
    page = page if isinstance(page, Mapping) else {}
    issue_codes = [
        str(issue.get("code", ""))[:100]
        for issue in graph.get("issues", [])
        if isinstance(issue, Mapping) and issue.get("code")
    ][:20]

    def payload(prefix_count: int, optional_edge_count: int) -> dict[str, Any]:
        selected = selected_ids(prefix_count)
        included_nodes = sorted(
            (node_by_id[node_id] for node_id in selected),
            key=lambda node: node["id"],
        )
        required_edges = [
            edge
            for edge in parent_edges
            if edge["source"] in selected and edge["target"] in selected
        ]
        available_optional = [
            edge
            for edge in optional_edges
            if edge["source"] in selected and edge["target"] in selected
        ]
        included_edges = required_edges + available_optional[:optional_edge_count]
        return {
            "schema_version": MODEL_VIEW_SCHEMA_VERSION,
            "full_graph_schema_version": graph.get("schema_version"),
            "graph_id": graph.get("graph_id"),
            "capture_id": graph.get("capture_id"),
            "page": {
                key: page[key]
                for key in ("url", "title", "ui_version")
                if key in page
            },
            "analysis_only": True,
            "execution_authorized": False,
            "safe_for_execution": False,
            "nodes": included_nodes,
            "edges": included_edges,
            "issue_codes": issue_codes,
            "projection": {
                "input_node_count": len(nodes),
                "included_node_count": len(included_nodes),
                "omitted_node_count": len(nodes) - len(included_nodes),
                "input_edge_count": len(edges),
                "included_edge_count": len(included_edges),
                "omitted_edge_count": len(edges) - len(included_edges),
                "candidate_count": sum(
                    1
                    for node in included_nodes
                    if node["interaction"]["candidate"] is True
                ),
                "truncated": (
                    len(included_nodes) < len(nodes) or len(included_edges) < len(edges)
                ),
                "selection": "interaction_candidates_business_nodes_then_parent_closure",
                "max_utf8_bytes": max_bytes,
            },
        }

    low, high = 0, len(nodes)
    best_prefix = -1
    while low <= high:
        middle = (low + high) // 2
        encoded = _canonical_text(payload(middle, 0)).encode("utf-8")
        if len(encoded) <= max_bytes:
            best_prefix = middle
            low = middle + 1
        else:
            high = middle - 1
    if best_prefix < 0:
        raise UIGraphProjectionError(
            "UI Graph metadata cannot fit the reasoner graph byte budget"
        )

    selected = selected_ids(best_prefix)
    available_optional_count = sum(
        1
        for edge in optional_edges
        if edge["source"] in selected and edge["target"] in selected
    )
    low, high = 0, available_optional_count
    best_payload = payload(best_prefix, 0)
    best_text = _canonical_text(best_payload)
    while low <= high:
        middle = (low + high) // 2
        candidate_payload = payload(best_prefix, middle)
        candidate_text = _canonical_text(candidate_payload)
        if len(candidate_text.encode("utf-8")) <= max_bytes:
            best_payload = candidate_payload
            best_text = candidate_text
            low = middle + 1
        else:
            high = middle - 1
    return best_text, copy.deepcopy(best_payload["projection"])


def project_ui_graph_for_reasoning(
    graph: Mapping[str, Any],
    *,
    max_bytes: int = 256 * 1024,
) -> str:
    """Return the existing bounded model view for another validated planner."""

    text, _projection = _reasoning_graph_text(graph, max_bytes=max_bytes)
    return text

class UIGraphPlanningService:
    """Turn a retained UI Graph into a validated, non-executable operation DAG."""

    def __init__(self, page_perception: Any, reasoner: UIGraphReasoner | None = None):
        self.page_perception = page_perception
        self.reasoner = reasoner

    def health(self) -> dict[str, Any]:
        reasoner = self.reasoner
        return {
            "configured": reasoner is not None,
            "reasoner_id": (
                str(getattr(reasoner, "reasoner_id", "unknown"))[:200]
                if reasoner is not None
                else None
            ),
            "reasoner_version": (
                str(getattr(reasoner, "reasoner_version", "unknown"))[:100]
                if reasoner is not None
                else None
            ),
            "mode": "validated_dry_run_proposal",
            "safe_for_execution": False,
        }

    def get_graph(self, capture_id: str) -> dict[str, Any]:
        if not isinstance(capture_id, str):
            raise ValueError("page_capture_id must be a string")
        normalized_capture_id = capture_id.strip()
        if not normalized_capture_id:
            raise ValueError("page_capture_id is required")
        graph = self.page_perception.get_ui_graph(normalized_capture_id)
        if graph is None:
            raise UIGraphNotFoundError("page capture or UI Graph not found")
        graph_capture_id = graph.get("capture_id")
        if graph_capture_id != normalized_capture_id:
            raise UIGraphNotFoundError("page capture or UI Graph not found")
        return copy.deepcopy(graph)

    def plan(self, *, capture_id: str, instruction: str) -> dict[str, Any]:
        if not isinstance(instruction, str):
            raise ValueError("instruction must be a string")
        normalized_instruction = instruction.strip()
        if not normalized_instruction:
            raise ValueError("instruction is required")
        if len(normalized_instruction) > MAX_INSTRUCTION_CHARS:
            raise ValueError("instruction exceeds the configured limit")
        graph = self.get_graph(capture_id)
        reasoner = self.reasoner
        if reasoner is None:
            raise UIGraphReasonerNotConfiguredError(
                "internal UI Graph reasoner is not configured"
            )

        graph_id = str(graph.get("graph_id", "")).strip()
        if not graph_id:
            raise ValueError("stored UI Graph is missing graph_id")
        graph_stats = graph.get("stats")
        if isinstance(graph_stats, Mapping) and graph_stats.get("truncated") is True:
            raise UIGraphProjectionError(
                "stored UI Graph is truncated and cannot be planned safely"
            )
        try:
            graph_byte_budget = int(
                getattr(reasoner, "MAX_GRAPH_TEXT_BYTES", 512 * 1024)
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise UIGraphProjectionError("reasoner graph byte budget is invalid") from exc
        graph_text, model_projection = _reasoning_graph_text(
            graph, max_bytes=graph_byte_budget
        )
        model_output = reasoner.plan(
            instruction=normalized_instruction, ui_graph_text=graph_text, graph_id=graph_id
        )
        # Bind the proposal to the exact bounded view that the reasoner saw.
        # Validating against the retained full graph would let a model reference
        # a node omitted from this request's projection (for example via stale
        # context or a guessed stable id).
        projected_graph = json.loads(graph_text)
        validation = validate(
            projected_graph,
            model_output,
            instruction=normalized_instruction,
            require_graph_id=True,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "planned" if validation["valid"] else "rejected",
            "page_capture_id": str(capture_id).strip(),
            "graph_id": graph_id,
            "reasoner": {
                "reasoner_id": str(getattr(reasoner, "reasoner_id", "unknown"))[:200],
                "reasoner_version": str(
                    getattr(reasoner, "reasoner_version", "unknown")
                )[:100],
            },
            "model_projection": model_projection,
            "dry_run_only": True,
            "safe_for_execution": False,
            "validation": validation,
        }


__all__ = [
    "SCHEMA_VERSION",
    "UIGraphProjectionError",
    "UIGraphNotFoundError",
    "UIGraphPlanningService",
    "UIGraphReasonerNotConfiguredError",
    "project_ui_graph_for_reasoning",
]
