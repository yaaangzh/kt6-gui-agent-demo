from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..asset_inventory import compact_text, identity_key, strong_identity_key


_SELECTED_VALUES = frozenset({"true", "1", "selected", "checked", "active"})


def matching_nodes(
    target: Mapping[str, Any],
    graph: Mapping[str, Any],
    *,
    source_kinds: frozenset[str] | None = None,
) -> list[Mapping[str, Any]]:
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return []
    scored: list[tuple[int, Mapping[str, Any]]] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        source = _mapping(node.get("source"))
        if source_kinds is not None and source.get("kind") not in source_kinds:
            continue
        score = _score(target, node)
        if score > 0:
            scored.append((score, node))
    if not scored:
        return []
    highest = max(score for score, _node in scored)
    return [node for score, node in scored if score == highest]


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
    if not exact_query and not contains_query:
        return 0
    score = 20 if exact_query else 5

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

    role = compact_text(target.get("role"), 100).casefold()
    if role:
        observed_role = compact_text(
            node.get("role") or attributes.get("role"), 100
        ).casefold()
        if observed_role != role:
            return 0
        score += 4
    return score


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


__all__ = ["matching_nodes", "node_selected"]
