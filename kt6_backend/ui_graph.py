from __future__ import annotations

"""Deterministic, analysis-only UI graph projection.

The graph is deliberately evidence, not an execution contract.  In particular,
``interaction.candidate`` preserves a collector's static observation while the
authorization-shaped fields on every node remain false.  JSON/JSONL are the
canonical model-facing representations; visual renderers such as Mermaid must
never be used as the execution truth.
"""

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal


SCHEMA_VERSION = "kt6.ui-graph.v1"
MAX_GRAPH_NODES = 2_000
MAX_GRAPH_EDGES = 8_000

_SOURCE_ORDER = {"dom": 0, "cdp": 1, "page_api": 2, "vision": 3, "text": 4}
_PARENT_RELATIONS = frozenset(
    {"dom_child", "ax_child", "shadow_root", "iframe_document", "parent_of"}
)
_AUTHORIZATION_KEYS = frozenset(
    {
        "actionable",
        "actionable_grounding",
        "authorized",
        "can_click_now",
        "execution_authorized",
        "interaction_eligible",
        "safe_for_execution",
        "usable_for_actions",
    }
)

__all__ = [
    "MAX_GRAPH_EDGES",
    "MAX_GRAPH_NODES",
    "SCHEMA_VERSION",
    "build_ui_graph",
    "serialize_ui_graph",
]


@dataclass
class _Observation:
    modality: str
    source_path: str
    source_index: int
    node: dict[str, Any]
    aliases: tuple[str, ...]
    scope: tuple[str, str, str]
    parent_ref: str
    parent_relation: str
    business_id: str
    owner_business_id: str
    action_id: str
    document_order: int
    source_rank: int = 0


@dataclass
class _SceneSpec:
    modality: str
    path: str
    scene: Mapping[str, Any]
    elements: list[tuple[int, Mapping[str, Any]]]
    relations: list[tuple[str, int, Mapping[str, Any]]]
    raw_adapter: bool = False


def build_ui_graph(capture: Mapping[str, Any]) -> dict[str, Any]:
    """Build a bounded UI evidence graph from a stored or public page capture.

    Supported inputs are the SQLite record shape (``capture`` + ``result``), the
    public PagePerceptionService shape, and a raw capture payload.  The optional
    ``cdp_perception`` may live at any corresponding public/stored perception
    location.
    """

    if not isinstance(capture, Mapping):
        raise TypeError("capture must be a mapping")

    issues: list[dict[str, Any]] = []
    projection = {
        "input_element_count": 0,
        "projected_out_count": 0,
        "limit_truncated": 0,
    }
    specs = _scene_specs(capture, issues=issues, projection=projection)
    observations: list[_Observation] = []
    relation_claims: list[tuple[str, str, int, Mapping[str, Any]]] = []
    for spec in specs:
        has_explicit_parent_relations = any(
            str(relation.get("type", "")) in _PARENT_RELATIONS
            for _, _, relation in spec.relations
        )
        for source_rank, (source_index, item) in enumerate(spec.elements):
            observations.append(
                _observation(
                    spec,
                    item,
                    source_index=source_index,
                    source_rank=source_rank,
                    suppress_parent=(
                        spec.modality == "cdp" and has_explicit_parent_relations
                    ),
                )
            )
        for relation_set, relation_index, relation in spec.relations:
            relation_claims.append(
                (spec.modality, f"{spec.path}.{relation_set}", relation_index, relation)
            )

    _assign_observation_ids(observations)
    observations.sort(key=_selection_key)
    input_observation_count = len(observations)
    if len(observations) > MAX_GRAPH_NODES:
        observations = observations[:MAX_GRAPH_NODES]
        issues.append(
            {
                "code": "graph_node_limit",
                "message": f"semantic observations exceed {MAX_GRAPH_NODES} nodes",
                "omitted_count": input_observation_count - len(observations),
            }
        )
    observation_ids = {observation.node["id"] for observation in observations}

    alias_index: dict[tuple[str, tuple[str, str, str], str], list[str]] = defaultdict(list)
    modality_alias_index: dict[tuple[str, str], list[str]] = defaultdict(list)
    for observation in observations:
        for alias in observation.aliases:
            alias_index[(observation.modality, observation.scope, alias)].append(
                observation.node["id"]
            )
            modality_alias_index[(observation.modality, alias)].append(
                observation.node["id"]
            )

    _apply_dom_action_bindings(
        capture,
        observations=observations,
        alias_index=alias_index,
        modality_alias_index=modality_alias_index,
        issues=issues,
    )

    nodes = [observation.node for observation in observations]
    business_claims: dict[str, list[_Observation]] = defaultdict(list)
    referenced_business_ids: set[str] = set()
    action_claims: dict[str, list[_Observation]] = defaultdict(list)
    for observation in observations:
        if observation.business_id:
            business_claims[observation.business_id].append(observation)
            referenced_business_ids.add(observation.business_id)
        if observation.owner_business_id:
            referenced_business_ids.add(observation.owner_business_id)
        if observation.action_id:
            action_claims[observation.action_id].append(observation)
    for modality, _, _, relation in relation_claims:
        if modality in {"dom", "cdp"}:
            continue
        source = _text(relation.get("source"), 300)
        target = _text(relation.get("target"), 300)
        if source:
            referenced_business_ids.add(source)
        if target:
            referenced_business_ids.add(target)

    business_nodes: dict[str, str] = {}
    action_nodes: dict[str, str] = {}
    derived_omitted = 0
    for business_id in sorted(referenced_business_ids):
        if len(nodes) >= MAX_GRAPH_NODES:
            derived_omitted += 1
            continue
        evidence_ids = sorted(
            observation.node["id"] for observation in business_claims.get(business_id, [])
        )
        node = _derived_node(
            node_id=_stable_id("business", business_id),
            kind="business_object",
            name=business_id,
            source_kind="identity_claim",
            source_path="derived.business_id",
            evidence_node_ids=evidence_ids,
        )
        node["business_id"] = business_id
        business_nodes[business_id] = node["id"]
        nodes.append(node)
    for action_id in sorted(action_claims):
        if len(nodes) >= MAX_GRAPH_NODES:
            derived_omitted += 1
            continue
        evidence_ids = sorted(
            observation.node["id"] for observation in action_claims[action_id]
        )
        node = _derived_node(
            node_id=_stable_id("action", action_id),
            kind="action_claim",
            name=action_id,
            source_kind="action_claim",
            source_path="derived.action_id",
            evidence_node_ids=evidence_ids,
        )
        node["action_id"] = action_id
        action_nodes[action_id] = node["id"]
        nodes.append(node)
    if derived_omitted:
        issues.append(
            {
                "code": "derived_node_limit",
                "message": "business/action claim nodes omitted at graph node limit",
                "omitted_count": derived_omitted,
            }
        )

    edges: list[dict[str, Any]] = []
    for observation in observations:
        node_id = observation.node["id"]
        if observation.parent_ref:
            candidates = alias_index.get(
                (observation.modality, observation.scope, observation.parent_ref), []
            )
            if not candidates:
                candidates = modality_alias_index.get(
                    (observation.modality, observation.parent_ref), []
                )
            candidates = sorted(set(candidates) - {node_id})
            if len(candidates) == 1:
                edges.append(
                    _edge(
                        "parent_of",
                        candidates[0],
                        node_id,
                        observation.modality,
                        observation.source_path,
                        relation_type=observation.parent_relation or "parent",
                    )
                )
            else:
                issues.append(
                    {
                        "code": (
                            "parent_ref_unresolved" if not candidates else "parent_ref_ambiguous"
                        ),
                        "node_id": node_id,
                        "parent_ref": observation.parent_ref,
                        "source": observation.modality,
                    }
                )
        business_node = business_nodes.get(observation.business_id)
        if business_node:
            edges.append(
                _edge(
                    "claims_business_object",
                    node_id,
                    business_node,
                    observation.modality,
                    observation.source_path,
                )
            )
        owner_node = business_nodes.get(observation.owner_business_id)
        if owner_node:
            edges.append(
                _edge(
                    "owner_of",
                    owner_node,
                    node_id,
                    observation.modality,
                    observation.source_path,
                )
            )
        action_node = action_nodes.get(observation.action_id)
        if action_node:
            edges.append(
                _edge(
                    "supports_action",
                    node_id,
                    action_node,
                    observation.modality,
                    observation.source_path,
                    action_id=observation.action_id,
                )
            )

    for modality, path, relation_index, relation in relation_claims:
        raw_source = _text(relation.get("source"), 300)
        raw_target = _text(relation.get("target"), 300)
        source_id = _relation_endpoint(
            raw_source, modality, business_nodes, modality_alias_index
        )
        target_id = _relation_endpoint(
            raw_target, modality, business_nodes, modality_alias_index
        )
        node_id_set = {node["id"] for node in nodes}
        if (
            not source_id
            or not target_id
            or source_id not in node_id_set
            or target_id not in node_id_set
        ):
            issues.append(
                {
                    "code": "relation_endpoint_unresolved",
                    "source": modality,
                    "relation_index": relation_index,
                    "source_ref": raw_source,
                    "target_ref": raw_target,
                }
            )
            continue
        raw_type = _text(
            relation.get("type", relation.get("channel", "relation")), 100
        ) or "relation"
        edge_type = "parent_of" if raw_type in _PARENT_RELATIONS else "semantic_relation"
        relation_id = _text(
            relation.get("relation_id", relation.get("edge_id", relation.get("id"))),
            200,
        )
        edges.append(
            _edge(
                edge_type,
                source_id,
                target_id,
                modality,
                f"{path}[{relation_index}]",
                relation_type=raw_type,
                relation_id=relation_id,
                metadata=_relation_metadata(relation),
            )
        )

    nodes.sort(key=lambda node: node["id"])
    edges = _finalize_edges(edges)
    input_edge_count = len(edges)
    if len(edges) > MAX_GRAPH_EDGES:
        edges = edges[:MAX_GRAPH_EDGES]
        issues.append(
            {
                "code": "graph_edge_limit",
                "message": f"graph exceeds {MAX_GRAPH_EDGES} edges",
                "omitted_count": input_edge_count - len(edges),
            }
        )
    issues = _sorted_issues(issues)

    source_counts = Counter(
        node.get("source", {}).get("kind", "unknown") for node in nodes
    )
    edge_counts = Counter(edge["type"] for edge in edges)
    truncated = bool(
        projection["limit_truncated"]
        or input_observation_count > len(observations)
        or derived_omitted
        or input_edge_count > len(edges)
    )
    stats = {
        "input_element_count": projection["input_element_count"],
        "projected_element_count": input_observation_count,
        "projected_out_count": projection["projected_out_count"],
        "node_count": len(nodes),
        "edge_count": len(edges),
        "source_counts": dict(sorted(source_counts.items())),
        "edge_counts": dict(sorted(edge_counts.items())),
        "issue_count": len(issues),
        "truncated": truncated,
        "max_nodes": MAX_GRAPH_NODES,
        "max_edges": MAX_GRAPH_EDGES,
    }
    capture_id = _text(capture.get("capture_id"), 200)
    page = _page(capture)
    graph_core = {
        "capture_id": capture_id,
        "nodes": nodes,
        "edges": edges,
        "issues": issues,
    }
    graph_id = "uig:" + hashlib.sha256(_canonical_bytes(graph_core)).hexdigest()[:24]
    return {
        "schema_version": SCHEMA_VERSION,
        "graph_id": graph_id,
        "capture_id": capture_id,
        "page": page,
        "analysis_only": True,
        "execution_authorized": False,
        "safe_for_execution": False,
        "nodes": nodes,
        "edges": edges,
        "issues": issues,
        "stats": stats,
    }


def serialize_ui_graph(
    graph: Mapping[str, Any], *, format: Literal["json", "jsonl"] = "json"
) -> str:
    """Serialize a UI graph as canonical compact JSON or deterministic JSONL."""

    if not isinstance(graph, Mapping):
        raise TypeError("graph must be a mapping")
    if format == "json":
        return _canonical_bytes(graph).decode("utf-8")
    if format != "jsonl":
        raise ValueError("format must be 'json' or 'jsonl'")

    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    issues = graph.get("issues", [])
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
        raise ValueError("graph.nodes must be an array")
    if not isinstance(edges, Sequence) or isinstance(edges, (str, bytes)):
        raise ValueError("graph.edges must be an array")
    if not isinstance(issues, Sequence) or isinstance(issues, (str, bytes)):
        raise ValueError("graph.issues must be an array")
    meta = {
        key: value
        for key, value in graph.items()
        if key not in {"nodes", "edges", "issues", "stats"}
    }
    records: list[dict[str, Any]] = [{"record": "graph", **meta}]
    records.extend(
        {"record": "node", **dict(node)}
        for node in sorted(nodes, key=lambda item: str(item.get("id", "")))
        if isinstance(node, Mapping)
    )
    records.extend(
        {"record": "edge", **dict(edge)}
        for edge in sorted(edges, key=lambda item: str(item.get("id", "")))
        if isinstance(edge, Mapping)
    )
    records.extend(
        {"record": "issue", **dict(issue)}
        for issue in _sorted_issues(
            [dict(issue) for issue in issues if isinstance(issue, Mapping)]
        )
    )
    records.append({"record": "stats", "value": graph.get("stats", {})})
    return "\n".join(_canonical_bytes(record).decode("utf-8") for record in records)


def _scene_specs(
    root: Mapping[str, Any],
    *,
    issues: list[dict[str, Any]],
    projection: dict[str, int],
) -> list[_SceneSpec]:
    stored = _mapping(root.get("capture"))
    result = _mapping(root.get("result"))
    perception = _mapping(result.get("perception"))
    if not perception:
        perception = _mapping(root.get("perception"))
    candidates = _mapping(perception.get("candidates"))
    specs: list[_SceneSpec] = []

    dom_candidates = (
        (perception.get("dom_perception"), "result.perception.dom_perception"),
        (root.get("dom_perception"), "dom_perception"),
        (candidates.get("dom"), "result.perception.candidates.dom"),
        (stored.get("dom"), "capture.dom"),
        (root.get("dom"), "dom"),
    )
    dom_scene, dom_path = _first_scene(dom_candidates, keys=("elements",))
    if dom_scene is not None:
        specs.append(_make_spec("dom", dom_path, dom_scene))

    cdp_candidates = (
        (root.get("cdp_perception"), "cdp_perception"),
        (stored.get("cdp_perception"), "capture.cdp_perception"),
        (perception.get("cdp_perception"), "result.perception.cdp_perception"),
        (candidates.get("cdp"), "result.perception.candidates.cdp"),
        (root.get("cdp"), "cdp"),
    )
    cdp_scene, cdp_path = _first_scene(cdp_candidates, keys=("nodes",))
    if cdp_scene is not None:
        specs.append(
            _projected_cdp_spec(
                cdp_path, cdp_scene, issues=issues, projection=projection
            )
        )

    page_api_candidates = (
        (root.get("page_api_perception"), "page_api_perception"),
        (
            perception.get("page_api_perception"),
            "result.perception.page_api_perception",
        ),
        (candidates.get("page_api"), "result.perception.candidates.page_api"),
    )
    page_scene, page_path = _first_scene(page_api_candidates, keys=("elements",))
    if page_scene is not None:
        specs.append(_make_spec("page_api", page_path, page_scene))
    else:
        adapter = stored.get("adapter_scene", root.get("adapter_scene"))
        if isinstance(adapter, Mapping) and isinstance(adapter.get("objects"), list):
            adapter_path = "capture.adapter_scene" if stored else "adapter_scene"
            specs.append(_make_spec("page_api", adapter_path, adapter, raw_adapter=True))

    canvas_candidates = (
        (root.get("canvas_perception"), "canvas_perception"),
        (
            perception.get("canvas_perception"),
            "result.perception.canvas_perception",
        ),
        (candidates.get("canvas"), "result.perception.candidates.canvas"),
        (root.get("vision_perception"), "vision_perception"),
        (root.get("vision"), "vision"),
    )
    for value, path in canvas_candidates:
        if isinstance(value, Mapping) and _is_vision_scene(value):
            specs.append(_make_spec("vision", path, value))
            break

    selected = _mapping(root.get("scene")) or _mapping(perception.get("scene"))
    text_candidates = (
        (root.get("text_perception"), "text_perception"),
        (perception.get("text_perception"), "result.perception.text_perception"),
        (candidates.get("text"), "result.perception.candidates.text"),
        (root.get("text"), "text"),
        (selected if _is_text_scene(selected) else None, "scene"),
    )
    text_scene, text_path = _first_scene(text_candidates, keys=("elements",))
    if text_scene is not None and _is_text_scene(text_scene):
        specs.append(_make_spec("text", text_path, text_scene))

    for spec in specs:
        if spec.modality != "cdp":
            projection["input_element_count"] += len(spec.elements)
    return specs


def _projected_cdp_spec(
    path: str,
    scene: Mapping[str, Any],
    *,
    issues: list[dict[str, Any]],
    projection: dict[str, int],
) -> _SceneSpec:
    raw_nodes = scene.get("nodes", [])
    if not isinstance(raw_nodes, list):
        raw_nodes = []
    indexed = [
        (index, item) for index, item in enumerate(raw_nodes) if isinstance(item, Mapping)
    ]
    projection["input_element_count"] += len(indexed)
    node_by_ref = {
        _text(item.get("node_id", item.get("id")), 300): (index, item)
        for index, item in indexed
        if _text(item.get("node_id", item.get("id")), 300)
    }
    parents: dict[str, list[str]] = defaultdict(list)
    for _, item in indexed:
        node_ref = _text(item.get("node_id", item.get("id")), 300)
        raw_parents = item.get("parent_ids")
        if isinstance(raw_parents, list):
            parents[node_ref].extend(_text(value, 300) for value in raw_parents)
        else:
            parent = _text(item.get("parent_id"), 300)
            if parent:
                parents[node_ref].append(parent)
    relations = _relation_entries(scene)
    for _, _, relation in relations:
        if _text(relation.get("type"), 100) in _PARENT_RELATIONS:
            source = _text(relation.get("source"), 300)
            target = _text(relation.get("target"), 300)
            if source and target:
                parents[target].append(source)

    seeds = [
        (index, item)
        for index, item in indexed
        if _cdp_semantically_relevant(item)
    ]
    seeds.sort(key=lambda entry: _cdp_priority(entry[1], entry[0]))
    selected: set[str] = set()
    selected_indexes: set[int] = set()
    hit_limit = False
    for index, item in seeds:
        node_ref = _text(item.get("node_id", item.get("id")), 300)
        pending = [(node_ref, index)]
        seen_chain: set[str] = set()
        while pending:
            ref, fallback_index = pending.pop(0)
            if not ref or ref in seen_chain:
                continue
            seen_chain.add(ref)
            found = node_by_ref.get(ref)
            if found is None:
                continue
            found_index, _ = found
            if ref not in selected:
                if len(selected) >= MAX_GRAPH_NODES:
                    hit_limit = True
                    break
                selected.add(ref)
                selected_indexes.add(found_index)
            for parent in parents.get(ref, []):
                if parent and parent not in seen_chain:
                    pending.append((parent, fallback_index))
        if hit_limit:
            break

    projected = [entry for entry in indexed if entry[0] in selected_indexes]
    omitted = len(indexed) - len(projected)
    projection["projected_out_count"] += omitted
    if hit_limit:
        projection["limit_truncated"] += omitted
    if omitted:
        issues.append(
            {
                "code": "cdp_semantic_projection",
                "message": "non-semantic or over-limit CDP nodes were projected out",
                "input_count": len(indexed),
                "retained_count": len(projected),
                "limit_reached": hit_limit,
            }
        )
    filtered_relations = [
        entry
        for entry in relations
        if _text(entry[2].get("source"), 300) in selected
        and _text(entry[2].get("target"), 300) in selected
    ]
    return _SceneSpec("cdp", path, scene, projected, filtered_relations)


def _make_spec(
    modality: str,
    path: str,
    scene: Mapping[str, Any],
    *,
    raw_adapter: bool = False,
) -> _SceneSpec:
    key = "objects" if raw_adapter else "elements"
    raw_elements = scene.get(key, [])
    elements = [
        (index, item)
        for index, item in enumerate(raw_elements if isinstance(raw_elements, list) else [])
        if isinstance(item, Mapping)
    ]
    return _SceneSpec(
        modality, path, scene, elements, _relation_entries(scene), raw_adapter
    )


def _relation_entries(scene: Mapping[str, Any]) -> list[tuple[str, int, Mapping[str, Any]]]:
    entries: list[tuple[str, int, Mapping[str, Any]]] = []
    primary_key = "relations" if isinstance(scene.get("relations"), list) else "links"
    for key in (primary_key, "co_channel_relations"):
        values = scene.get(key, [])
        if not isinstance(values, list):
            continue
        entries.extend(
            (key, index, value)
            for index, value in enumerate(values)
            if isinstance(value, Mapping)
        )
    return entries


def _observation(
    spec: _SceneSpec,
    item: Mapping[str, Any],
    *,
    source_index: int,
    source_rank: int,
    suppress_parent: bool,
) -> _Observation:
    declared_source = _mapping(item.get("source"))
    scene_provenance = _mapping(spec.scene.get("provenance"))
    attributes = _mapping(item.get("attributes"))
    business_id = _first_text(
        item.get("business_id"),
        attributes.get("business_id"),
        attributes.get("data-business-id"),
    )
    owner_business_id = _first_text(
        item.get("owner_business_id"),
        attributes.get("owner_business_id"),
        attributes.get("data-owner-business-id"),
    )
    action_id = _first_text(
        item.get("action_id"),
        attributes.get("action_id"),
        attributes.get("data-action-id"),
    )
    role = _first_text(
        item.get("role"), item.get("type"), attributes.get("role"), item.get("tag")
    ) or "element"
    name = _first_text(
        item.get("aria_label"),
        item.get("name"),
        item.get("label"),
        item.get("description"),
        item.get("placeholder"),
        business_id,
    )
    interaction_claim = _mapping(item.get("interaction"))
    if spec.modality == "dom":
        source_ref = _first_stable_dom_ref(
            item.get("ref"),
            item.get("source_ref"),
            declared_source.get("source_ref"),
            item.get("selector"),
            interaction_claim.get("target_ref"),
        )
    else:
        source_ref = _first_text(
            item.get("node_id"),
            item.get("ref"),
            item.get("source_ref"),
            declared_source.get("source_ref"),
            item.get("element_id"),
            item.get("selector"),
        )
    frame_id = _first_text(item.get("frame_id"), declared_source.get("frame_id"))
    frame_record = (
        _cdp_frame_record(spec.scene, frame_id)
        if spec.modality == "cdp"
        else {}
    )
    frame_url = _first_text(
        item.get("frame_url"),
        declared_source.get("frame_url"),
        frame_record.get("document_url"),
        frame_record.get("url"),
    )
    document_id = _first_text(
        item.get("document_id"), declared_source.get("document_id")
    )
    candidate_claim = bool(
        item.get("interaction_candidate") is True
        or item.get("interaction_eligible") is True
        or interaction_claim.get("candidate") is True
    )
    disabled = bool(item.get("disabled", attributes.get("disabled", False)))
    candidate = bool(
        candidate_claim
        and not disabled
        and (spec.modality != "dom" or bool(source_ref))
    )
    reason_codes = ["analysis_only_ui_graph"]
    if candidate:
        reason_codes.append(f"{spec.modality}_interaction_candidate_observed")
    if disabled:
        reason_codes.append("source_reports_disabled")
    if spec.modality == "dom" and candidate_claim and not source_ref:
        reason_codes.append("dom_stable_rebind_unavailable")
    source = {
        "kind": spec.modality,
        "path": f"{spec.path}.{'objects' if spec.raw_adapter else ('nodes' if spec.modality == 'cdp' else 'elements')}[{source_index}]",
        "semantic_source": _first_text(
            declared_source.get("semantic_source"),
            scene_provenance.get("semantic_source"),
            {
                "dom": "browser_dom",
                "cdp": "cdp_dom_accessibility_snapshot",
                "page_api": "page_api_adapter",
                "vision": "canvas_pixels",
                "text": "topology_text",
            }.get(spec.modality),
        ),
        "method": _first_text(
            declared_source.get("method"),
            "cdp_snapshot" if spec.modality == "cdp" else spec.scene.get("mode"),
        ),
        "trust": _first_text(declared_source.get("trust"), "observed_claim"),
    }
    if source_ref:
        source["source_ref"] = source_ref
    if frame_id:
        source["frame_id"] = frame_id
    if document_id:
        source["document_id"] = document_id
    if frame_url:
        source["frame_url"] = frame_url
    if spec.modality == "cdp":
        parent_frame_id = _first_text(frame_record.get("parent_frame_id"))
        source["parent_frame_id"] = parent_frame_id
    if declared_source.get("kind") and declared_source.get("kind") != spec.modality:
        source["declared_kind"] = _text(declared_source.get("kind"), 100)
    for key in ("backend_node_id", "canvas_id", "producer_id", "producer_version"):
        value = item.get(key, declared_source.get(key))
        if value is not None and value != "":
            source[key] = _bounded(value)

    bbox = _bbox(item, raw_adapter=spec.raw_adapter)
    node: dict[str, Any] = {
        "id": "",
        "kind": "element",
        "role": _text(role, 100),
        "name": _text(name, 300),
        "source": source,
        "actionable": False,
        "can_click_now": False,
        "safe_for_execution": False,
        "disabled": disabled,
        "interaction": {
            "status": "candidate_only" if candidate else "analysis_only",
            "candidate": candidate,
            "authorized": False,
            "preflight_required": candidate,
            "reason_codes": sorted(set(reason_codes)),
        },
    }
    if business_id:
        node["business_id"] = business_id
    if owner_business_id:
        node["owner_business_id"] = owner_business_id
    if action_id:
        node["action_id"] = action_id
    if bbox is not None:
        node["bbox"] = bbox
    confidence = _finite_number(item.get("confidence"))
    if confidence is not None:
        node["confidence"] = max(0.0, min(1.0, confidence))
    compact_attributes = _compact_attributes(item, attributes)
    if compact_attributes:
        node["attributes"] = compact_attributes
    aliases = tuple(
        sorted(
            {
                value
                for value in (
                    source_ref,
                    _text(item.get("node_id"), 300),
                    _text(item.get("id"), 300),
                    _text(item.get("element_id"), 300),
                    _text(item.get("ref"), 300),
                    _text(item.get("source_ref"), 300),
                    _text(item.get("selector"), 500),
                )
                if value
            }
        )
    )
    parent_ref = "" if suppress_parent else _first_text(
        item.get("parent_ref"), item.get("parent_id"), attributes.get("parent_ref")
    )
    try:
        document_order = int(item.get("document_order", source_index))
    except (TypeError, ValueError, OverflowError):
        document_order = source_index
    return _Observation(
        modality=spec.modality,
        source_path=source["path"],
        source_index=source_index,
        node=node,
        aliases=aliases,
        scope=(frame_id, document_id, frame_url),
        parent_ref=parent_ref,
        parent_relation=_first_text(item.get("parent_relation"), "parent"),
        business_id=business_id,
        owner_business_id=owner_business_id,
        action_id=action_id,
        document_order=document_order,
        source_rank=source_rank,
    )


def _cdp_frame_record(
    scene: Mapping[str, Any],
    frame_id: str,
) -> Mapping[str, Any]:
    frames = scene.get("frames")
    if not frame_id or not isinstance(frames, list):
        return {}
    matches = [
        frame
        for frame in frames
        if isinstance(frame, Mapping)
        and _text(frame.get("frame_id"), 200) == frame_id
    ]
    return matches[0] if len(matches) == 1 else {}


def _assign_observation_ids(observations: list[_Observation]) -> None:
    observations.sort(key=_identity_key)
    used: Counter[str] = Counter()
    for observation in observations:
        source = observation.node["source"]
        source_ref = source.get("source_ref", "")
        if source_ref:
            identity = {
                "modality": observation.modality,
                "scope": observation.scope,
                "source_ref": source_ref,
            }
        elif observation.business_id:
            identity = {
                "modality": observation.modality,
                "scope": observation.scope,
                "business_id": observation.business_id,
            }
        else:
            identity = {
                "modality": observation.modality,
                "scope": observation.scope,
                "role": observation.node.get("role", ""),
                "name": observation.node.get("name", ""),
                "bbox": observation.node.get("bbox"),
            }
        base = _stable_id(observation.modality, identity)
        used[base] += 1
        observation.node["id"] = base if used[base] == 1 else f"{base}:{used[base]}"


def _apply_dom_action_bindings(
    root: Mapping[str, Any],
    *,
    observations: list[_Observation],
    alias_index: Mapping[tuple[str, tuple[str, str, str], str], list[str]],
    modality_alias_index: Mapping[tuple[str, str], list[str]],
    issues: list[dict[str, Any]],
) -> None:
    del alias_index  # Exact scope is already encoded by the binding's element record.
    stored_result = _mapping(_mapping(root.get("result")).get("perception"))
    raw = root.get("dom_action_bindings", stored_result.get("dom_action_bindings", {}))
    if not isinstance(raw, Mapping):
        return
    by_id = {observation.node["id"]: observation for observation in observations}
    for binding_ref, value in sorted(raw.items(), key=lambda entry: str(entry[0])):
        if not isinstance(value, Mapping):
            continue
        binding_dom_ref = _first_stable_dom_ref(
            value.get("dom_ref"),
            value.get("selector"),
            value.get("source_ref"),
        )
        if not binding_dom_ref or value.get("disabled") is True:
            issues.append(
                {
                    "code": "dom_action_binding_unusable",
                    "binding_ref": _text(binding_ref, 500),
                }
            )
            continue
        aliases = {
            _text(binding_ref, 500),
            _text(value.get("element_id"), 300),
            _text(value.get("dom_ref"), 500),
        }
        candidates: set[str] = set()
        for alias in aliases:
            if alias:
                candidates.update(modality_alias_index.get(("dom", alias), []))
        if len(candidates) != 1:
            issues.append(
                {
                    "code": "dom_action_binding_unresolved",
                    "binding_ref": _text(binding_ref, 500),
                    "candidate_count": len(candidates),
                }
            )
            continue
        observation = by_id[next(iter(candidates))]
        if observation.node.get("disabled") is True:
            issues.append(
                {
                    "code": "dom_action_binding_disabled",
                    "binding_ref": _text(binding_ref, 500),
                }
            )
            continue
        observation.action_id = observation.action_id or _text(value.get("action_id"), 200)
        observation.owner_business_id = observation.owner_business_id or _text(
            value.get("owner_business_id"), 200
        )
        observation.parent_ref = observation.parent_ref or _text(
            value.get("parent_ref"), 500
        )
        observation.node["interaction"]["candidate"] = True
        observation.node["interaction"]["status"] = "candidate_only"
        observation.node["interaction"]["preflight_required"] = True
        observation.node["interaction"]["reason_codes"] = sorted(
            set(observation.node["interaction"]["reason_codes"])
            | {"dom_action_binding_observed"}
        )
        if observation.action_id:
            observation.node["action_id"] = observation.action_id
        if observation.owner_business_id:
            observation.node["owner_business_id"] = observation.owner_business_id


def _derived_node(
    *,
    node_id: str,
    kind: str,
    name: str,
    source_kind: str,
    source_path: str,
    evidence_node_ids: list[str],
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "role": kind,
        "name": _text(name, 300),
        "source": {
            "kind": source_kind,
            "path": source_path,
            "trust": "derived_from_explicit_claims",
            "evidence_node_ids": evidence_node_ids[:100],
        },
        "actionable": False,
        "can_click_now": False,
        "safe_for_execution": False,
        "disabled": False,
        "interaction": {
            "status": "analysis_only",
            "candidate": False,
            "authorized": False,
            "preflight_required": False,
            "reason_codes": ["derived_claim_analysis_only"],
        },
    }


def _edge(
    edge_type: str,
    source: str,
    target: str,
    modality: str,
    path: str,
    **details: Any,
) -> dict[str, Any]:
    edge = {
        "type": edge_type,
        "source": source,
        "target": target,
        "analysis_only": True,
        "execution_authorized": False,
        "safe_for_execution": False,
        "provenance": {"source": modality, "path": path},
    }
    edge.update({key: value for key, value in details.items() if value not in (None, "", {})})
    return edge


def _finalize_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for edge in edges:
        key = _canonical_bytes(edge).decode("utf-8")
        unique.setdefault(key, edge)
    result = list(unique.values())
    result.sort(
        key=lambda edge: (
            edge["type"], edge["source"], edge["target"], edge.get("relation_type", ""),
            edge["provenance"].get("path", ""),
        )
    )
    used: Counter[str] = Counter()
    for edge in result:
        base = _stable_id("edge", edge)
        used[base] += 1
        edge["id"] = base if used[base] == 1 else f"{base}:{used[base]}"
    return result


def _relation_endpoint(
    value: str,
    modality: str,
    business_nodes: Mapping[str, str],
    aliases: Mapping[tuple[str, str], list[str]],
) -> str | None:
    candidates = sorted(set(aliases.get((modality, value), [])))
    if modality in {"dom", "cdp"} and len(candidates) == 1:
        return candidates[0]
    if value in business_nodes:
        return business_nodes[value]
    return candidates[0] if len(candidates) == 1 else None


def _relation_metadata(relation: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        str(key)[:100]: _bounded(value)
        for key, value in sorted(relation.items(), key=lambda item: str(item[0]))
        if str(key).casefold()
        not in _AUTHORIZATION_KEYS | {"source", "target", "type", "id", "edge_id", "relation_id"}
    }
    return result


def _compact_attributes(
    item: Mapping[str, Any], attributes: Mapping[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in sorted(attributes.items(), key=lambda entry: str(entry[0]))[:40]:
        normalized_key = str(key)[:100]
        if normalized_key.casefold() not in _AUTHORIZATION_KEYS:
            result[normalized_key] = _bounded(value)
    for key in (
        "tag",
        "selector",
        "checked",
        "focusable",
        "is_clickable",
        "ignored",
        "asset_id",
        "business_type",
        "management_ip",
        "serial_number",
        "site_id",
        "backend_node_id",
        "shadow_root_type",
        "description",
        "value",
        "depth",
        "document_order",
    ):
        if key in item and key.casefold() not in _AUTHORIZATION_KEYS:
            result[key] = _bounded(item[key])
    return {key: value for key, value in result.items() if value not in (None, "")}


def _bbox(item: Mapping[str, Any], *, raw_adapter: bool) -> list[float] | None:
    value = item.get("bbox", item.get("bounds"))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 4:
        numbers = [_finite_number(part) for part in value]
        if all(part is not None for part in numbers):
            return [round(float(part), 3) for part in numbers]
    if raw_adapter:
        x = _finite_number(item.get("x"))
        y = _finite_number(item.get("y"))
        width = _finite_number(item.get("width"))
        height = _finite_number(item.get("height"))
        if None not in (x, y, width, height):
            return [
                round(float(x) - float(width) / 2, 3),
                round(float(y) - float(height) / 2, 3),
                round(float(width), 3),
                round(float(height), 3),
            ]
    return None


def _page(root: Mapping[str, Any]) -> dict[str, Any]:
    stored = _mapping(root.get("capture"))
    value = _mapping(stored.get("page")) or _mapping(root.get("page"))
    return {
        key: _bounded(value[key])
        for key in ("url", "title", "ui_version", "viewport")
        if key in value
    }


def _cdp_semantically_relevant(item: Mapping[str, Any]) -> bool:
    attributes = _mapping(item.get("attributes"))
    return bool(
        _first_text(item.get("name"), item.get("role"), item.get("business_id"))
        or _first_text(
            attributes.get("business_id"),
            attributes.get("data-business-id"),
            attributes.get("action_id"),
            attributes.get("data-action-id"),
        )
        or item.get("interaction_candidate") is True
        or _mapping(item.get("interaction")).get("candidate") is True
    )


def _cdp_priority(item: Mapping[str, Any], index: int) -> tuple[Any, ...]:
    attributes = _mapping(item.get("attributes"))
    candidate = bool(
        item.get("interaction_candidate") is True
        or _mapping(item.get("interaction")).get("candidate") is True
    )
    business = bool(
        _first_text(item.get("business_id"), attributes.get("business_id"), attributes.get("data-business-id"))
    )
    return (
        not candidate,
        not business,
        not bool(_text(item.get("name"), 300)),
        not bool(_text(item.get("role"), 100)),
        _text(item.get("node_id", item.get("id")), 300),
        index,
    )


def _identity_key(observation: _Observation) -> tuple[Any, ...]:
    return (
        _SOURCE_ORDER.get(observation.modality, 99),
        observation.scope,
        observation.node["source"].get("source_ref", ""),
        observation.business_id,
        observation.document_order,
        observation.source_path,
    )


def _selection_key(observation: _Observation) -> tuple[Any, ...]:
    interaction = observation.node["interaction"]
    return (
        not bool(interaction["candidate"]),
        not bool(observation.action_id or observation.owner_business_id),
        not bool(observation.business_id),
        not bool(observation.node.get("name")),
        not bool(observation.node.get("role")),
        _SOURCE_ORDER.get(observation.modality, 99),
        observation.source_rank,
        observation.node["id"],
    )


def _is_vision_scene(scene: Mapping[str, Any]) -> bool:
    if _text(scene.get("mode"), 100) == "canvas_vision_adapter":
        return True
    if _text(_mapping(scene.get("provenance")).get("semantic_source"), 100) == "canvas_pixels":
        return True
    elements = scene.get("elements", [])
    return bool(
        isinstance(elements, list)
        and any(
            isinstance(item, Mapping)
            and _text(_mapping(item.get("source")).get("kind"), 100) == "vision"
            for item in elements[:10]
        )
    )


def _is_text_scene(scene: Mapping[str, Any]) -> bool:
    return bool(
        _text(scene.get("mode"), 100) == "topology_text_reconstruction"
        or _text(_mapping(scene.get("provenance")).get("semantic_source"), 100)
        in {"provided_text", "external_ocr_transcript", "topology_text"}
    )


def _first_scene(
    candidates: Sequence[tuple[Any, str]], *, keys: tuple[str, ...]
) -> tuple[Mapping[str, Any] | None, str]:
    for value, path in candidates:
        if isinstance(value, Mapping) and any(isinstance(value.get(key), list) for key in keys):
            return value, path
    return None, ""


def _first_stable_dom_ref(*values: Any) -> str:
    for value in values:
        candidate = _text(value, 500)
        if candidate and "@capture:" not in candidate.casefold():
            return candidate
    return ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        result = _text(value, 500)
        if result:
            return result
    return ""


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _bounded(value: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return _text(value, 300)
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, Mapping):
        return {
            str(key)[:100]: _bounded(item, depth + 1)
            for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))[:40]
            if str(key).casefold() not in _AUTHORIZATION_KEYS
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_bounded(item, depth + 1) for item in list(value)[:40]]
    return _text(value, 300)


def _stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical_bytes(value)).hexdigest()[:20]
    return f"{prefix}:{digest}"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Make already-bounded graph data JSON-safe without dropping graph records."""

    if depth >= 20:
        return _text(value, 500)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item, depth + 1)
            for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_safe(item, depth + 1) for item in value]
    return _text(value, 500)



def _sorted_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (_bounded(issue) for issue in issues),
        key=lambda issue: (
            str(issue.get("code", "")),
            str(issue.get("node_id", "")),
            _canonical_bytes(issue),
        ),
    )
