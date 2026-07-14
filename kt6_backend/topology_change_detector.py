from __future__ import annotations

import json
import math
from typing import Any


class TopologyChangeDetector:
    STABLE_EDGE_ID_FIELDS = ("relation_id", "edge_id", "id")
    EDGE_MATCH_ATTRIBUTES = (
        "source_port",
        "target_port",
        "port",
        "channel",
        "band",
        "vlan",
        "vlan_id",
        "ssid",
        "protocol",
        "medium",
    )

    # Only stable, business-relevant edge properties participate in action
    # invalidation. Renderer geometry and styling are intentionally excluded so
    # camera movement, animation and repainting cannot invalidate a solution.
    SEMANTIC_EDGE_ATTRIBUTES = frozenset(
        {
            "status",
            "state",
            "health",
            "admin_status",
            "oper_status",
            "link_status",
            "poe_status",
            "enabled",
            "connected",
            "active",
            "channel",
            "band",
            "vlan",
            "vlan_id",
            "ssid",
            "role",
            "mode",
            "direction",
            "duplex",
            "speed",
            "capacity",
            "protocol",
            "medium",
            "port",
            "source_port",
            "target_port",
        }
    )

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

        matched_edges, added_edges, removed_edges = self._match_edges(previous_scene, current_scene)
        edge_attribute_changes: list[dict[str, Any]] = []
        for previous, current in matched_edges:
            changed_attributes = self._edge_attribute_diff(previous, current)
            if changed_attributes:
                change = {
                    "source": current["source"],
                    "target": current["target"],
                    "type": current["type"],
                    "changes": changed_attributes,
                }
                edge_identity = self._edge_identity_payload(current)
                if edge_identity:
                    change["edge_identity"] = edge_identity
                edge_attribute_changes.append(change)

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
        for edge in edge_attribute_changes:
            affected.update((edge["source"], edge["target"]))
            blocking.update((edge["source"], edge["target"]))

        changes = {
            "added_nodes": added_nodes,
            "removed_nodes": removed_nodes,
            "moved_nodes": moved_nodes,
            "attribute_changes": attribute_changes,
            "added_edges": added_edges,
            "removed_edges": removed_edges,
            "edge_attribute_changes": edge_attribute_changes,
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
                "edge_attribute_changes",
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
            "edge_attribute_changes",
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
            "edge_attribute_changes": [],
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

    def _edge_groups(self, scene: dict[str, Any]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
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
                source, target = sorted((source, target), key=str)
            normalized = {**relation, "source": source, "target": target, "type": relation_type}
            structural_key = (str(source), str(target), str(relation_type))
            groups.setdefault(structural_key, []).append(normalized)
        return groups

    def _match_edges(
        self,
        previous_scene: dict[str, Any],
        current_scene: dict[str, Any],
    ) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
        previous_groups = self._edge_groups(previous_scene)
        current_groups = self._edge_groups(current_scene)
        matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
        added: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []

        for structural_key in sorted(set(previous_groups) | set(current_groups)):
            group_matches, group_added, group_removed = self._match_edge_group(
                previous_groups.get(structural_key, []),
                current_groups.get(structural_key, []),
            )
            matched.extend(group_matches)
            added.extend(group_added)
            removed.extend(group_removed)
        return matched, added, removed

    def _match_edge_group(
        self,
        previous: list[dict[str, Any]],
        current: list[dict[str, Any]],
    ) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
        previous_remaining = set(range(len(previous)))
        current_remaining = set(range(len(current)))
        matched = self._consume_edge_matches(
            previous,
            current,
            previous_remaining,
            current_remaining,
            self._stable_edge_id,
        )

        matched.extend(
            self._consume_edge_matches(
                previous,
                current,
                previous_remaining,
                current_remaining,
                lambda edge: self._edge_match_hint(edge) if self._stable_edge_id(edge) is None else None,
            )
        )

        # Edges without a stable id or unchanged port-like hint are inherently
        # ambiguous. Preserve their deterministic occurrence order so a changed
        # attribute is still reported as a change instead of an add/remove pair.
        previous_idless = [
            index
            for index in sorted(previous_remaining)
            if self._stable_edge_id(previous[index]) is None
        ]
        current_idless = [
            index
            for index in sorted(current_remaining)
            if self._stable_edge_id(current[index]) is None
        ]
        for previous_index, current_index in zip(previous_idless, current_idless):
            previous_remaining.remove(previous_index)
            current_remaining.remove(current_index)
            matched.append((previous[previous_index], current[current_index]))

        added = [current[index] for index in sorted(current_remaining)]
        removed = [previous[index] for index in sorted(previous_remaining)]
        return matched, added, removed

    def _consume_edge_matches(
        self,
        previous: list[dict[str, Any]],
        current: list[dict[str, Any]],
        previous_remaining: set[int],
        current_remaining: set[int],
        identity,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        previous_by_identity: dict[str, list[int]] = {}
        current_by_identity: dict[str, list[int]] = {}
        for index in sorted(previous_remaining):
            key = identity(previous[index])
            if key is not None:
                previous_by_identity.setdefault(key, []).append(index)
        for index in sorted(current_remaining):
            key = identity(current[index])
            if key is not None:
                current_by_identity.setdefault(key, []).append(index)

        matched = []
        for key in sorted(set(previous_by_identity) & set(current_by_identity)):
            for previous_index, current_index in zip(
                previous_by_identity[key],
                current_by_identity[key],
            ):
                previous_remaining.remove(previous_index)
                current_remaining.remove(current_index)
                matched.append((previous[previous_index], current[current_index]))
        return matched

    def _stable_edge_id(self, edge: dict[str, Any]) -> str | None:
        identity = self._stable_edge_identity(edge)
        if identity is None:
            return None
        _, value = identity
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _stable_edge_identity(self, edge: dict[str, Any]) -> tuple[str, Any] | None:
        nested = edge.get("attributes", {})
        nested = nested if isinstance(nested, dict) else {}
        for field in self.STABLE_EDGE_ID_FIELDS:
            value = edge.get(field)
            if value in (None, ""):
                value = nested.get(field)
            if value not in (None, ""):
                return field, value
        return None

    def _edge_match_hint(self, edge: dict[str, Any]) -> str | None:
        semantic = self._semantic_edge_attributes(edge)
        hint = {
            field: semantic[field]
            for field in self.EDGE_MATCH_ATTRIBUTES
            if field in semantic
        }
        if not hint:
            return None
        return json.dumps(hint, ensure_ascii=False, sort_keys=True)

    def _edge_identity_payload(self, edge: dict[str, Any]) -> dict[str, Any]:
        stable_identity = self._stable_edge_identity(edge)
        if stable_identity is not None:
            field, value = stable_identity
            return {field: value}
        semantic = self._semantic_edge_attributes(edge)
        return {
            field: semantic[field]
            for field in self.EDGE_MATCH_ATTRIBUTES
            if field in semantic
        }

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

    def _edge_attribute_diff(self, previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        previous_attributes = self._semantic_edge_attributes(previous)
        current_attributes = self._semantic_edge_attributes(current)
        changes = {}
        for key in sorted(set(previous_attributes) | set(current_attributes)):
            if previous_attributes.get(key) != current_attributes.get(key):
                changes[key] = {
                    "from": previous_attributes.get(key),
                    "to": current_attributes.get(key),
                }
        return changes

    def _semantic_edge_attributes(self, edge: dict[str, Any]) -> dict[str, Any]:
        attributes = edge.get("attributes", {})
        nested = attributes if isinstance(attributes, dict) else {}
        semantic = {
            key: nested[key]
            for key in self.SEMANTIC_EDGE_ATTRIBUTES
            if key in nested
        }
        semantic.update(
            {
                key: edge[key]
                for key in self.SEMANTIC_EDGE_ATTRIBUTES
                if key in edge
            }
        )
        return semantic

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
            ("edge_attribute_changes", "链路属性变化"),
        )
        for field, label in labels:
            count = len(changes.get(field, []))
            if count:
                parts.append(f"{label} {count}")
        return "，".join(parts)
