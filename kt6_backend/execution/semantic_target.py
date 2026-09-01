from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..asset_inventory import compact_text, identity_key, strong_identity_key


_SELECTED_VALUES = frozenset({"true", "1", "selected", "checked", "active"})
_GENERIC_TEXTBOX_QUERIES = frozenset(
    {
        identity_key("搜索框"),
        identity_key("搜索输入框"),
        identity_key("search box"),
        identity_key("search input"),
    }
)
_TEXTBOX_ROLES = frozenset({"textbox", "searchbox"})
_SEMANTIC_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "link",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "radio",
        "tab",
    }
)
_GENERIC_INTERACTIVE_ROLES = frozenset({"", "generic", "none"})
_SEMANTIC_PARENT_DEPTH = 3


def matching_nodes(
    target: Mapping[str, Any],
    graph: Mapping[str, Any],
    *,
    source_kinds: frozenset[str] | None = None,
) -> list[Mapping[str, Any]]:
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return []
    scored: dict[int, int] = {}
    indexed_nodes: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            continue
        node_id = compact_text(node.get("id"), 300)
        if node_id:
            indexed_nodes[node_id] = (index, node)
        source = _mapping(node.get("source"))
        if source_kinds is not None and source.get("kind") not in source_kinds:
            continue
        score = _score(target, node)
        if score > 0:
            scored[index] = score

    # Custom controls often expose their visible text on a StaticText descendant
    # while the clickable ancestor has no accessible name and only role=generic.
    # Keep the stable clickable ancestor as the execution target, but inherit the
    # exact semantic label through the captured parent_of evidence.
    parents_by_child: dict[str, set[str]] = {}
    edges = graph.get("edges")
    if isinstance(edges, list):
        for edge in edges:
            if not isinstance(edge, Mapping) or edge.get("type") != "parent_of":
                continue
            parent_id = compact_text(edge.get("source"), 300)
            child_id = compact_text(edge.get("target"), 300)
            if parent_id and child_id:
                parents_by_child.setdefault(child_id, set()).add(parent_id)
    for child_id, (_child_index, child) in indexed_nodes.items():
        text_score = _score({"query": target.get("query")}, child)
        if text_score <= 0:
            continue
        child_source = _mapping(child.get("source"))
        frontier = {child_id}
        visited = {child_id}
        for _depth in range(_SEMANTIC_PARENT_DEPTH):
            parent_ids = {
                parent_id
                for current_id in frontier
                for parent_id in parents_by_child.get(current_id, ())
                if parent_id not in visited
            }
            if not parent_ids:
                break
            visited.update(parent_ids)
            frontier = parent_ids
            for parent_id in parent_ids:
                parent_entry = indexed_nodes.get(parent_id)
                if parent_entry is None:
                    continue
                parent_index, parent = parent_entry
                parent_source = _mapping(parent.get("source"))
                if (
                    source_kinds is not None
                    and parent_source.get("kind") not in source_kinds
                ):
                    continue
                if not _same_semantic_source(child_source, parent_source):
                    continue
                inherited_score = _inherited_label_score(
                    target,
                    parent,
                    text_score,
                    ancestor_depth=_depth + 1,
                )
                if inherited_score > scored.get(parent_index, 0):
                    scored[parent_index] = inherited_score
    if not scored:
        return []
    highest = max(scored.values())
    return [
        node
        for index, node in enumerate(nodes)
        if isinstance(node, Mapping) and scored.get(index) == highest
    ]


def node_selected(node: Mapping[str, Any]) -> bool:
    attributes = _mapping(node.get("attributes"))
    for key in (
        "selected",
        "checked",
        "aria-selected",
        "aria-checked",
        "data-selected",
        "data-state",
    ):
        if compact_text(attributes.get(key), 100).casefold() in _SELECTED_VALUES:
            return True
    classes = set(re.split(r"\s+", compact_text(attributes.get("class"), 500).casefold()))
    return bool(classes.intersection({"selected", "checked", "active"}))


def _score(target: Mapping[str, Any], node: Mapping[str, Any]) -> int:
    attributes = _mapping(node.get("attributes"))
    query = identity_key(target.get("query"))
    if not query:
        return 0
    terms = {
        identity_key(value)
        for value in (
            node.get("name"),
            node.get("business_id"),
            node.get("owner_business_id"),
            node.get("action_id"),
            attributes.get("id"),
            attributes.get("aria-label"),
            attributes.get("title"),
            attributes.get("data-testid"),
            attributes.get("data-business-id"),
            attributes.get("data-asset-id"),
        )
        if identity_key(value)
    }
    exact_query = query in terms
    contains_query = len(query) >= 3 and any(query in term for term in terms)
    requested_role = compact_text(target.get("role"), 100).casefold()
    observed_role = compact_text(
        node.get("role") or attributes.get("role"), 100
    ).casefold()
    generic_textbox_query = (
        query in _GENERIC_TEXTBOX_QUERIES
        and requested_role in _TEXTBOX_ROLES
        and observed_role in _TEXTBOX_ROLES
    )
    if not exact_query and not contains_query and not generic_textbox_query:
        return 0
    score = 20 if exact_query else (5 if contains_query else 4)

    asset_id = compact_text(target.get("asset_id"), 200)
    if asset_id:
        candidates = (
            node.get("business_id"),
            node.get("owner_business_id"),
            attributes.get("data-business-id"),
            attributes.get("data-asset-id"),
            attributes.get("data-owner-business-id"),
        )
        if not any(
            compact_text(value, 200)
            and strong_identity_key("asset_id", value)
            == strong_identity_key("asset_id", asset_id)
            for value in candidates
        ):
            return 0
        score += 10

    action = compact_text(target.get("action"), 200).casefold()
    if action:
        observed = compact_text(
            node.get("action_id") or attributes.get("data-action-id"), 200
        ).casefold()
        if observed != action:
            return 0
        score += 8

    if requested_role:
        if observed_role != requested_role and not generic_textbox_query:
            return 0
        score += 4
    return score


def _inherited_label_score(
    target: Mapping[str, Any],
    node: Mapping[str, Any],
    text_score: int,
    *,
    ancestor_depth: int,
) -> int:
    if compact_text(target.get("asset_id"), 200) or compact_text(
        target.get("action"), 200
    ):
        return 0
    interaction = _mapping(node.get("interaction"))
    if interaction.get("candidate") is not True:
        return 0
    attributes = _mapping(node.get("attributes"))
    requested_role = compact_text(target.get("role"), 100).casefold()
    observed_role = compact_text(
        node.get("role") or attributes.get("role"), 100
    ).casefold()
    if observed_role not in (
        _SEMANTIC_INTERACTIVE_ROLES | _GENERIC_INTERACTIVE_ROLES
    ):
        return 0
    if requested_role and requested_role != observed_role:
        if not (
            requested_role in _SEMANTIC_INTERACTIVE_ROLES
            and observed_role in _GENERIC_INTERACTIVE_ROLES
        ):
            return 0
    proximity = max(0, _SEMANTIC_PARENT_DEPTH - ancestor_depth)
    return (30 if text_score >= 20 else 10) + proximity + (
        4 if requested_role else 0
    )


def _same_semantic_source(
    first: Mapping[str, Any], second: Mapping[str, Any]
) -> bool:
    if first.get("kind") != second.get("kind"):
        return False
    first_frame = compact_text(first.get("frame_id"), 200)
    second_frame = compact_text(second.get("frame_id"), 200)
    return not first_frame or not second_frame or first_frame == second_frame


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


__all__ = ["matching_nodes", "node_selected"]
