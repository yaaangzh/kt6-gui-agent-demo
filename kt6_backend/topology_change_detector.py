from __future__ import annotations

import json
import math
from typing import Any


class TopologyChangeDetector:
    def __init__(self, movement_threshold: float = 1.0):
        self.movement_threshold = movement_threshold

    def diff(self, previous_scene: dict[str, Any], current_scene: dict[str, Any]) -> dict[str, Any]:
        previous_nodes = self._node_map(previous_scene)
        current_nodes = self._node_map(current_scene)
        previous_ids = set(previous_nodes)
        current_ids = set(current_nodes)

        added_nodes = [self._node_summary(current_nodes[node_id]) for node_id in sorted(current_ids - previous_ids)]
        removed_nodes = [self._node_summary(previous_nodes[node_id]) for node_id in sorted(previous_ids - current_ids)]
        moved_nodes: list[dict[str, Any]] = []
        attribute_changes: list[dict[str, Any]] = []

        for node_id in sorted(previous_ids & current_ids):
            previous = previous_nodes[node_id]
            current = current_nodes[node_id]
            previous_center = previous.get("center", [])
            current_center = current.get("center", [])
            if self._moved(previous_center, current_center):
                moved_nodes.append(
                    {
                        "business_id": node_id,
                        "from": previous_center,
                        "to": current_center,
                    }
                )

            changed_attributes = self._attribute_diff(previous, current)
            if changed_attributes:
                attribute_changes.append(
                    {
                        "business_id": node_id,
                        "changes": changed_attributes,
                    }
                )

        previous_edges = self._edge_map(previous_scene)
        current_edges = self._edge_map(current_scene)
        previous_edge_ids = set(previous_edges)
        current_edge_ids = set(current_edges)
        added_edges = [current_edges[edge_id] for edge_id in sorted(current_edge_ids - previous_edge_ids)]
        removed_edges = [previous_edges[edge_id] for edge_id in sorted(previous_edge_ids - current_edge_ids)]

        affected = set()
        blocking = set()
        rebind = set()
        for node in added_nodes + removed_nodes:
            affected.add(node["business_id"])
        blocking.update(node["business_id"] for node in removed_nodes)
        for node in moved_nodes:
            affected.add(node["business_id"])
            rebind.add(node["business_id"])
        for node in attribute_changes:
            affected.add(node["business_id"])
            blocking.add(node["business_id"])
        for edge in added_edges + removed_edges:
            affected.update((edge["source"], edge["target"]))
            blocking.update((edge["source"], edge["target"]))

        changes = {
            "added_nodes": added_nodes,
            "removed_nodes": removed_nodes,
            "moved_nodes": moved_nodes,
            "attribute_changes": attribute_changes,
            "added_edges": added_edges,
            "removed_edges": removed_edges,
            "affected_business_ids": sorted(affected),
            "blocking_business_ids": sorted(blocking),
            "rebind_business_ids": sorted(rebind - blocking),
        }
        changes["is_empty"] = not any(
            changes[key]
            for key in (
                "added_nodes",
                "removed_nodes",
                "moved_nodes",
                "attribute_changes",
                "added_edges",
                "removed_edges",
            )
        )
        changes["summary"] = self._summary(changes)
        return changes

    def merge(self, change_sets: list[dict[str, Any]]) -> dict[str, Any]:
        if not change_sets:
            return self.empty()

        list_fields = (
            "added_nodes",
            "removed_nodes",
            "moved_nodes",
            "attribute_changes",
            "added_edges",
            "removed_edges",
        )
        merged = {field: [] for field in list_fields}
        for field in list_fields:
            seen: set[str] = set()
            for changes in change_sets:
                for item in changes.get(field, []):
                    signature = json.dumps(item, ensure_ascii=False, sort_keys=True)
                    if signature not in seen:
                        seen.add(signature)
                        merged[field].append(item)

        for field in ("affected_business_ids", "blocking_business_ids", "rebind_business_ids"):
            merged[field] = sorted(
                {
                    business_id
                    for changes in change_sets
                    for business_id in changes.get(field, [])
                }
            )
        merged["rebind_business_ids"] = sorted(
            set(merged["rebind_business_ids"]) - set(merged["blocking_business_ids"])
        )
        merged["is_empty"] = not any(merged[field] for field in list_fields)
        merged["summary"] = self._summary(merged)
        return merged

    def empty(self) -> dict[str, Any]:
        changes = {
            "added_nodes": [],
            "removed_nodes": [],
            "moved_nodes": [],
            "attribute_changes": [],
            "added_edges": [],
            "removed_edges": [],
            "affected_business_ids": [],
            "blocking_business_ids": [],
            "rebind_business_ids": [],
            "is_empty": True,
        }
        changes["summary"] = self._summary(changes)
        return changes

    def _node_map(self, scene: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            element["business_id"]: element
            for element in scene.get("elements", [])
            if element.get("business_id")
        }

    def _edge_map(self, scene: dict[str, Any]) -> dict[str, dict[str, Any]]:
        edges: dict[str, dict[str, Any]] = {}
        relations = list(scene.get("relations", []))
        relations.extend(
            {
                **relation,
                "type": "co_channel",
            }
            for relation in scene.get("co_channel_relations", [])
        )
        for relation in relations:
            source = relation.get("source")
            target = relation.get("target")
            relation_type = relation.get("type", "unknown")
            if not source or not target:
                continue
            if relation_type == "co_channel":
                source, target = sorted((source, target))
            normalized = {**relation, "source": source, "target": target, "type": relation_type}
            edge_id = f"{source}|{target}|{relation_type}"
            edges[edge_id] = normalized
        return edges

    def _node_summary(self, node: dict[str, Any]) -> dict[str, Any]:
        return {
            "business_id": node["business_id"],
            "type": node.get("type"),
            "label": node.get("label"),
            "center": node.get("center", []),
        }

    def _moved(self, previous: list[Any], current: list[Any]) -> bool:
        if len(previous) != 2 or len(current) != 2:
            return previous != current
        return math.dist(previous, current) > self.movement_threshold

    def _attribute_diff(self, previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        previous_attributes = {
            "type": previous.get("type"),
            "label": previous.get("label"),
            **previous.get("attributes", {}),
        }
        current_attributes = {
            "type": current.get("type"),
            "label": current.get("label"),
            **current.get("attributes", {}),
        }
        changes = {}
        for key in sorted(set(previous_attributes) | set(current_attributes)):
            if previous_attributes.get(key) != current_attributes.get(key):
                changes[key] = {
                    "from": previous_attributes.get(key),
                    "to": current_attributes.get(key),
                }
        return changes

    def _summary(self, changes: dict[str, Any]) -> str:
        if changes.get("is_empty"):
            return "拓扑结构未变化"
        parts = []
        labels = (
            ("added_nodes", "新增节点"),
            ("removed_nodes", "删除节点"),
            ("moved_nodes", "移动节点"),
            ("attribute_changes", "状态变化"),
            ("added_edges", "新增链路"),
            ("removed_edges", "删除链路"),
        )
        for field, label in labels:
            count = len(changes.get(field, []))
            if count:
                parts.append(f"{label} {count}")
        return "，".join(parts)
