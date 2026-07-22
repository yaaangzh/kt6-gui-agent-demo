from __future__ import annotations

import copy
import math
import re
import unicodedata
from collections import deque
from collections.abc import Callable, Mapping
from statistics import median
from typing import Any

from .topology_vision_contract import RESPONSE_SCHEMA_VERSION, TopologyVisionContract


FUSION_SCHEMA_VERSION = "kt6.topology-fusion.v2"
DEFAULT_MODEL_CONFIDENCE = 0.5

_RESERVED_ATTRIBUTE_KEYS = frozenset(
    {
        "actionable",
        "actionability",
        "actionable_grounding",
        "pixel_inference_performed",
        "pixel_verified",
        "provenance",
        "semantic_source",
    }
)
_GENERIC_OBJECT_TYPES = frozenset({"", "object", "device", "network_device", "unknown"})
_LINE_STYLES = frozenset({"solid", "dashed", "dotted", "dash", "dot"})


class TopologyFusionError(ValueError):
    """Raised when an offline CV or model result cannot be normalized safely."""


def fuse_topology_payloads(
    cv_payload: Mapping[str, Any],
    model_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Fuse grounded CV geometry with model-proposed topology semantics.

    CV-grounded objects and model objects carrying contract-validated pixel boxes
    are placed in ``result.objects``. Model-only objects without observed geometry
    and links with ungrounded endpoints are retained separately so inferred layout
    cannot be mistaken for actionable pixel coordinates.
    """

    cv_objects, cv_links, canvas_id = _normalize_cv_payload(cv_payload)
    (
        model_nodes,
        model_links,
        model_metadata,
        structure_templates,
        negative_evidence,
    ) = _normalize_model_payload(model_payload)
    cv_index = _CVIdentifierIndex(cv_objects)

    model_matches: dict[str, tuple[str, dict[str, Any]]] = {}
    model_only_objects: list[dict[str, Any]] = []
    for model_id, model_node in model_nodes.items():
        cv_id = cv_index.resolve(model_id)
        if cv_id is None:
            model_only_objects.append(_unlocated_object(model_id, model_node))
            continue
        existing = model_matches.get(cv_id)
        if existing is None:
            model_matches[cv_id] = (model_id, copy.deepcopy(model_node))
        else:
            existing[1].update(copy.deepcopy(model_node))

    fused_objects = [
        _fuse_object(cv_object, model_matches.get(str(cv_object["business_id"])), canvas_id)
        for cv_object in cv_objects
    ]
    model_grounded_objects = [
        item
        for item in model_only_objects
        if item.get("attributes", {}).get("geometry_status") == "model_pixel_grounded"
    ]
    unlocated_objects = [
        item
        for item in model_only_objects
        if item.get("attributes", {}).get("geometry_status") != "model_pixel_grounded"
    ]
    model_grounded_ids = {
        str(item["business_id"]) for item in model_grounded_objects
    }

    def resolve_endpoint(value: str) -> str | None:
        resolved = cv_index.resolve(value)
        if resolved is not None:
            return resolved
        return value if value in model_grounded_ids else None

    normalized_model_links: list[dict[str, Any]] = []
    semantic_model_links: list[dict[str, Any]] = []
    unresolved_links: list[dict[str, Any]] = []
    for model_link in model_links:
        source = resolve_endpoint(str(model_link["source"]))
        target = resolve_endpoint(str(model_link["target"]))
        semantic_link = copy.deepcopy(model_link)
        semantic_link["source"] = source or str(model_link["source"])
        semantic_link["target"] = target or str(model_link["target"])
        semantic_model_links.append(semantic_link)
        if source is None or target is None:
            confidence = (
                float(model_link["confidence"])
                if model_link.get("confidence") is not None
                else DEFAULT_MODEL_CONFIDENCE
            )
            attributes = _safe_attributes(model_link.get("attributes", {}))
            unresolved_status = (
                "structurally_derived"
                if attributes.get("derivation_status") == "structurally_derived"
                else "model_only"
            )
            attributes.update(
                {
                    "fusion_status": unresolved_status,
                    "evidence_sources": ["multimodal_model"],
                    "geometry_status": "unresolved_endpoint",
                    "resolution_reason": "model_endpoint_has_no_cv_geometry",
                    "confidence_basis": (
                        "model_reported"
                        if model_link.get("confidence") is not None
                        else "uncalibrated_default"
                    ),
                }
            )
            unresolved_links.append(
                {
                    "relation_id": (
                        f"fusion-unresolved:{source or model_link['source']}:"
                        f"{target or model_link['target']}"
                    ),
                    "source": semantic_link["source"],
                    "target": semantic_link["target"],
                    "type": str(model_link.get("type", "topology_link")),
                    "confidence": round(confidence, 4),
                    "attributes": attributes,
                }
            )
            continue
        if source == target:
            continue
        item = copy.deepcopy(model_link)
        item["source"] = source
        item["target"] = target
        normalized_model_links.append(item)

    resolved_structure_templates = _resolve_structure_templates(
        structure_templates, resolve_endpoint=resolve_endpoint
    )
    resolved_negative_evidence = _resolve_negative_evidence(
        negative_evidence, resolve_endpoint=resolve_endpoint
    )
    model_nodes_by_semantic_id = {
        resolve_endpoint(model_id) or model_id: attributes
        for model_id, attributes in model_nodes.items()
    }

    model_layers_by_cv_id = {
        resolved_id: str(model_node.get("layer", "")).strip()
        for model_id, model_node in model_nodes.items()
        if (resolved_id := resolve_endpoint(model_id)) is not None
        and str(model_node.get("layer", "")).strip()
    }
    result_objects = fused_objects + model_grounded_objects
    fused_links, rejected_links = _fuse_links(
        cv_links,
        normalized_model_links,
        semantic_model_links=semantic_model_links,
        model_nodes_by_semantic_id=model_nodes_by_semantic_id,
        negative_evidence=resolved_negative_evidence,
        matched_cv_ids={str(item["business_id"]) for item in result_objects},
        model_layers_by_cv_id=model_layers_by_cv_id,
    )
    unlocated_objects = _infer_unlocated_geometry(
        unlocated_objects,
        fused_objects=result_objects,
        structure_templates=resolved_structure_templates,
        semantic_model_links=semantic_model_links,
        canvas_id=canvas_id,
    )
    confidence_values = [float(item["confidence"]) for item in result_objects]
    confidence_values.extend(float(item["confidence"]) for item in fused_links)
    global_confidence = (
        round(sum(confidence_values) / len(confidence_values), 4)
        if confidence_values
        else 0.0
    )

    object_statuses = _status_counts(result_objects)
    link_statuses = _status_counts(fused_links)
    unresolved_statuses = _status_counts(unresolved_links)
    geometry_statuses = _geometry_status_counts(unlocated_objects)
    result = {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "confidence": global_confidence,
        "objects": result_objects,
        "links": fused_links,
        "co_channel_relations": [],
    }
    semantic_nodes = copy.deepcopy(result_objects) + copy.deepcopy(unlocated_objects)
    semantic_links = copy.deepcopy(fused_links) + copy.deepcopy(unresolved_links)
    return {
        "schema_version": FUSION_SCHEMA_VERSION,
        "summary": {
            "cv_object_count": len(cv_objects),
            "model_object_count": len(model_nodes),
            "fused_object_count": len(result_objects),
            "confirmed_object_count": object_statuses.get("confirmed", 0),
            "cv_only_object_count": object_statuses.get("cv_only", 0),
            "model_grounded_object_count": len(model_grounded_objects),
            "unlocated_model_object_count": len(unlocated_objects),
            "cv_link_count": len(cv_links),
            "model_link_count": len(model_links),
            "fused_link_count": len(fused_links),
            "confirmed_link_count": link_statuses.get("confirmed", 0),
            "cv_only_link_count": link_statuses.get("cv_only", 0),
            "model_only_link_count": link_statuses.get("model_only", 0),
            "path_equivalent_link_count": link_statuses.get("path_equivalent", 0),
            "structurally_derived_link_count": link_statuses.get(
                "structurally_derived", 0
            )
            + unresolved_statuses.get("structurally_derived", 0),
            "conflict_link_count": link_statuses.get("conflict", 0),
            "llm_rejected_link_count": len(rejected_links),
            "unresolved_model_link_count": len(unresolved_links),
            "spatially_inferred_object_count": geometry_statuses.get(
                "spatially_inferred", 0
            ),
            "remaining_unlocated_object_count": geometry_statuses.get("unlocated", 0),
        },
        "model_metadata": model_metadata,
        "structure_templates": resolved_structure_templates,
        "result": result,
        "semantic_graph": {
            "nodes": semantic_nodes,
            "links": semantic_links,
            "structure_templates": copy.deepcopy(resolved_structure_templates),
        },
        "unlocated_objects": sorted(
            unlocated_objects, key=lambda item: _identifier_sort_key(item["business_id"])
        ),
        "unresolved_links": sorted(
            unresolved_links,
            key=lambda item: (
                _identifier_sort_key(item["source"]),
                _identifier_sort_key(item["target"]),
            ),
        ),
        "rejected_links": sorted(
            rejected_links,
            key=lambda item: (
                _identifier_sort_key(item["source"]),
                _identifier_sort_key(item["target"]),
            ),
        ),
    }


class _CVIdentifierIndex:
    def __init__(self, objects: list[dict[str, Any]]) -> None:
        self._exact: dict[str, str] = {}
        compact_candidates: dict[str, list[str]] = {}
        for item in objects:
            business_id = str(item["business_id"])
            exact = _exact_identifier_key(business_id)
            if exact in self._exact:
                raise TopologyFusionError(f"duplicate CV business_id: {business_id}")
            self._exact[exact] = business_id
            compact_candidates.setdefault(_compact_identifier_key(business_id), []).append(
                business_id
            )
        self._compact = {
            key: values[0] for key, values in compact_candidates.items() if len(values) == 1
        }

    def resolve(self, value: str) -> str | None:
        exact = self._exact.get(_exact_identifier_key(value))
        if exact is not None:
            return exact
        return self._compact.get(_compact_identifier_key(value))


def _resolve_structure_templates(
    templates: list[dict[str, Any]],
    *,
    resolve_endpoint: Callable[[str], str | None],
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for template in templates:
        item = copy.deepcopy(template)
        if item.get("type") == "star":
            center = str(item.get("center", ""))
            item["center"] = resolve_endpoint(center) or center
            leaves = item.get("leaves", [])
            if isinstance(leaves, list):
                item["leaves"] = [
                    resolve_endpoint(str(leaf)) or str(leaf) for leaf in leaves
                ]
        layers = item.get("layers")
        if isinstance(layers, list):
            for layer in layers:
                if not isinstance(layer, dict):
                    continue
                members = layer.get("members", [])
                if isinstance(members, list):
                    layer["members"] = [
                        resolve_endpoint(str(member)) or str(member)
                        for member in members
                    ]
        resolved.append(item)
    return resolved


def _resolve_negative_evidence(
    evidence: Mapping[str, Any],
    *,
    resolve_endpoint: Callable[[str], str | None],
) -> dict[str, Any]:
    pairs: list[dict[str, Any]] = []
    for raw_pair in evidence.get("pairs", []):
        if not isinstance(raw_pair, Mapping):
            continue
        source_text = str(raw_pair.get("source", ""))
        target_text = str(raw_pair.get("target", ""))
        pairs.append(
            {
                "source": resolve_endpoint(source_text) or source_text,
                "target": resolve_endpoint(target_text) or target_text,
                "reason": _text(
                    raw_pair.get("reason"), "explicit_model_rejection", 500
                ),
            }
        )
    isolated_nodes = [
        resolve_endpoint(str(node_id)) or str(node_id)
        for node_id in evidence.get("isolated_nodes", [])
    ]
    return {
        "reject_all": evidence.get("reject_all") is True,
        "pairs": pairs,
        "isolated_nodes": isolated_nodes,
    }


def _normalize_cv_payload(
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    if not isinstance(payload, Mapping):
        raise TopologyFusionError("CV JSON root must be an object")
    scene = payload.get("scene")
    source: Mapping[str, Any]
    if isinstance(scene, Mapping):
        source = scene
    else:
        source = payload

    raw_objects = source.get("objects")
    if not isinstance(raw_objects, list):
        raw_objects = source.get("elements")
    if not isinstance(raw_objects, list):
        raise TopologyFusionError("CV JSON must contain objects or elements")
    if len(raw_objects) > TopologyVisionContract.MAX_OBJECTS:
        raise TopologyFusionError("CV JSON contains too many objects")

    canvas_id = _infer_canvas_id(source)
    objects: list[dict[str, Any]] = []
    object_ids: set[str] = set()
    for index, raw_object in enumerate(raw_objects):
        if not isinstance(raw_object, Mapping):
            raise TopologyFusionError(f"CV object {index} must be an object")
        business_id = _required_identifier(
            raw_object.get("business_id", raw_object.get("id")), f"CV object {index}"
        )
        exact_key = _exact_identifier_key(business_id)
        if exact_key in object_ids:
            raise TopologyFusionError(f"duplicate CV business_id: {business_id}")
        object_ids.add(exact_key)
        bbox = _bbox(raw_object.get("bbox"), f"CV object {business_id}")
        confidence = _confidence(raw_object.get("confidence"), default=0.5)
        attributes = _safe_attributes(raw_object.get("attributes", {}))
        objects.append(
            {
                "business_id": business_id,
                "type": _text(raw_object.get("type"), "network_device", 100),
                "label": _text(raw_object.get("label"), business_id, 500),
                "canvas_id": _text(raw_object.get("canvas_id"), canvas_id, 200),
                "bbox": bbox,
                "confidence": confidence,
                "attributes": attributes,
            }
        )

    raw_links = source.get("links")
    if not isinstance(raw_links, list):
        raw_links = source.get("relations", [])
    if not isinstance(raw_links, list):
        raise TopologyFusionError("CV links or relations must be a list")
    if len(raw_links) > TopologyVisionContract.MAX_RELATIONS:
        raise TopologyFusionError("CV JSON contains too many links")
    canonical_ids = {
        _exact_identifier_key(item["business_id"]): item["business_id"] for item in objects
    }
    links: list[dict[str, Any]] = []
    for index, raw_link in enumerate(raw_links):
        if not isinstance(raw_link, Mapping):
            raise TopologyFusionError(f"CV link {index} must be an object")
        source_id = _required_identifier(raw_link.get("source"), f"CV link {index}.source")
        target_id = _required_identifier(raw_link.get("target"), f"CV link {index}.target")
        source_id = canonical_ids.get(_exact_identifier_key(source_id), source_id)
        target_id = canonical_ids.get(_exact_identifier_key(target_id), target_id)
        if source_id not in canonical_ids.values() or target_id not in canonical_ids.values():
            raise TopologyFusionError(f"CV link {source_id}->{target_id} has a dangling endpoint")
        links.append(
            {
                "source": source_id,
                "target": target_id,
                "type": _text(raw_link.get("type"), "topology_link", 100),
                "confidence": _confidence(raw_link.get("confidence"), default=0.5),
                "attributes": _safe_attributes(raw_link.get("attributes", {})),
            }
        )
    return objects, links, canvas_id


def _normalize_model_payload(
    payload: Mapping[str, Any],
) -> tuple[
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
]:
    if not isinstance(payload, Mapping):
        raise TopologyFusionError("model JSON root must be an object")
    topology = payload.get("topology", payload)
    if not isinstance(topology, Mapping):
        raise TopologyFusionError("model JSON topology must be an object")

    nodes: dict[str, dict[str, Any]] = {}
    node_ids_by_key: dict[str, str] = {}
    links: list[dict[str, Any]] = []
    structure_templates: list[dict[str, Any]] = []

    def add_node(raw_node: Mapping[str, Any], *, layer: str | None = None) -> str:
        node_id = _required_identifier(
            raw_node.get("id", raw_node.get("business_id")), "model node"
        )
        key = _compact_identifier_key(node_id)
        canonical_model_id = node_ids_by_key.get(key, node_id)
        node_ids_by_key.setdefault(key, canonical_model_id)
        attributes = _safe_attributes(raw_node.get("attributes", {}))
        attributes.update(
            {
                str(name): _safe_json(value)
                for name, value in raw_node.items()
                if name
                not in {
                    "id",
                    "business_id",
                    "connections",
                    "attributes",
                    "bbox",
                    "canvas_id",
                }
                and str(name).strip().lower() not in _RESERVED_ATTRIBUTE_KEYS
            }
        )
        raw_bbox = raw_node.get("bbox")
        raw_canvas_id = raw_node.get("canvas_id")
        if isinstance(raw_bbox, list) and len(raw_bbox) == 4 and raw_canvas_id is not None:
            try:
                model_bbox = _bbox(raw_bbox, f"model node {node_id}")
            except TopologyFusionError:
                model_bbox = None
            if model_bbox is not None:
                attributes["model_geometry"] = {
                    "bbox": model_bbox,
                    "canvas_id": _text(raw_canvas_id, "uploaded_topology", 200),
                    "confidence": _confidence(
                        raw_node.get("confidence"), DEFAULT_MODEL_CONFIDENCE
                    ),
                }
        if layer:
            attributes["layer"] = layer
        nodes.setdefault(canonical_model_id, {}).update(attributes)
        return canonical_model_id

    raw_nodes = topology.get("nodes", topology.get("objects", []))
    if raw_nodes is not None and not isinstance(raw_nodes, list):
        raise TopologyFusionError("model topology.nodes or objects must be a list")
    connected_nodes: list[tuple[str, Mapping[str, Any]]] = []
    for raw_node in raw_nodes or []:
        if not isinstance(raw_node, Mapping):
            raise TopologyFusionError("model topology.nodes entries must be objects")
        connected_nodes.append((add_node(raw_node), raw_node))

    raw_layers = topology.get("layers", [])
    if raw_layers is not None and not isinstance(raw_layers, list):
        raise TopologyFusionError("model topology.layers must be a list")
    normalized_layers: list[dict[str, Any]] = []
    for layer in raw_layers or []:
        if not isinstance(layer, Mapping):
            raise TopologyFusionError("model topology.layers entries must be objects")
        layer_name = _text(layer.get("name"), "", 200)
        devices = layer.get("devices", [])
        if not isinstance(devices, list):
            raise TopologyFusionError("model layer devices must be a list")
        layer_members: list[str] = []
        for device in devices:
            if not isinstance(device, Mapping):
                raise TopologyFusionError("model layer devices entries must be objects")
            model_id = add_node(device, layer=layer_name or None)
            connected_nodes.append((model_id, device))
            layer_members.append(model_id)
        normalized_layers.append(
            {
                "name": layer_name or f"layer-{len(normalized_layers) + 1}",
                "members": layer_members,
            }
        )
    if normalized_layers:
        structure_templates.append(
            {
                "template_id": "model:layers:1",
                "type": "layered",
                "layers": normalized_layers,
                "source": "multimodal_model",
            }
        )

    raw_structure_templates = topology.get("structure_templates", [])
    if raw_structure_templates is not None and not isinstance(
        raw_structure_templates, list
    ):
        raise TopologyFusionError("model topology.structure_templates must be a list")
    for index, raw_template in enumerate(raw_structure_templates or []):
        if not isinstance(raw_template, Mapping):
            raise TopologyFusionError(
                f"model structure_templates[{index}] must be an object"
            )
        template_type = _text(raw_template.get("type"), "", 30).casefold()
        template_id = _text(
            raw_template.get("template_id"), f"model:structure:{index + 1}", 200
        )
        template: dict[str, Any] = {
            "template_id": template_id,
            "type": template_type,
            "source": "multimodal_model",
        }
        if raw_template.get("confidence") is not None:
            template["confidence"] = _confidence(
                raw_template.get("confidence"), DEFAULT_MODEL_CONFIDENCE
            )
        if isinstance(raw_template.get("attributes"), Mapping):
            template["attributes"] = _safe_attributes(raw_template.get("attributes"))
        if template_type == "star":
            center = _required_identifier(
                raw_template.get("center"), f"model structure {template_id}.center"
            )
            center = node_ids_by_key.get(_compact_identifier_key(center), center)
            raw_leaves = raw_template.get("leaves", [])
            if not isinstance(raw_leaves, list) or not raw_leaves:
                raise TopologyFusionError(
                    f"model structure {template_id}.leaves must be a non-empty list"
                )
            leaves = []
            for raw_leaf in raw_leaves:
                leaf = _required_identifier(
                    raw_leaf, f"model structure {template_id}.leaf"
                )
                leaves.append(
                    node_ids_by_key.get(_compact_identifier_key(leaf), leaf)
                )
            template.update({"center": center, "leaves": list(dict.fromkeys(leaves))})
        elif template_type == "layered":
            raw_template_layers = raw_template.get("layers", [])
            if not isinstance(raw_template_layers, list) or not raw_template_layers:
                raise TopologyFusionError(
                    f"model structure {template_id}.layers must be a non-empty list"
                )
            template_layers: list[dict[str, Any]] = []
            for layer_index, raw_layer in enumerate(raw_template_layers):
                if not isinstance(raw_layer, Mapping):
                    raise TopologyFusionError(
                        f"model structure {template_id} layer {layer_index} is invalid"
                    )
                raw_members = raw_layer.get("members", [])
                if not isinstance(raw_members, list) or not raw_members:
                    raise TopologyFusionError(
                        f"model structure {template_id} layer members are invalid"
                    )
                layer_name = _text(
                    raw_layer.get("name"), f"layer-{layer_index + 1}", 200
                )
                members: list[str] = []
                for raw_member in raw_members:
                    member = _required_identifier(
                        raw_member, f"model structure {template_id} member"
                    )
                    member = node_ids_by_key.get(_compact_identifier_key(member), member)
                    members.append(member)
                    if member in nodes:
                        nodes[member]["layer"] = layer_name
                template_layers.append(
                    {"name": layer_name, "members": list(dict.fromkeys(members))}
                )
            template["layers"] = template_layers
        else:
            raise TopologyFusionError(
                f"model structure {template_id} type must be star or layered"
            )
        structure_templates.append(template)

    for model_id, device in connected_nodes:
        connections = device.get("connections", {})
        if connections is None:
            continue
        if isinstance(connections, Mapping):
            connection_groups = list(connections.items())
        elif isinstance(connections, list):
            connection_groups = [("related", connections)]
        else:
            raise TopologyFusionError(
                f"model node {model_id} connections must be an object or list"
            )
        for direction, raw_targets in connection_groups:
            targets = raw_targets if isinstance(raw_targets, list) else [raw_targets]
            for raw_target in targets:
                connection_attributes: dict[str, Any] = {
                    "connection_direction": str(direction)
                }
                explicit_source: str | None = None
                if isinstance(raw_target, Mapping):
                    target_value = raw_target.get(
                        "target", raw_target.get("id", raw_target.get("nodeId"))
                    )
                    if raw_target.get("source") is not None:
                        explicit_source = _required_identifier(
                            raw_target.get("source"),
                            f"model node {model_id} connection source",
                        )
                    connection_attributes.update(
                        {
                            str(name): _safe_json(value)
                            for name, value in raw_target.items()
                            if name not in {"source", "target", "id", "nodeId"}
                            and str(name).strip().lower() not in _RESERVED_ATTRIBUTE_KEYS
                        }
                    )
                else:
                    target_value = raw_target
                target_id = _required_identifier(
                    target_value, f"model node {model_id} connection"
                )
                target_key = _compact_identifier_key(target_id)
                canonical_target = node_ids_by_key.get(target_key, target_id)
                if target_key not in node_ids_by_key:
                    node_ids_by_key[target_key] = canonical_target
                    nodes[canonical_target] = {"implicit": True}
                if explicit_source is not None:
                    source_key = _compact_identifier_key(explicit_source)
                    source = node_ids_by_key.get(source_key, explicit_source)
                    if source_key not in node_ids_by_key:
                        node_ids_by_key[source_key] = source
                        nodes[source] = {"implicit": True}
                    target = canonical_target
                elif str(direction).strip().lower() == "up":
                    source, target = canonical_target, model_id
                else:
                    source, target = model_id, canonical_target
                links.append(
                    {
                        "source": source,
                        "target": target,
                        "type": "topology_link",
                        "confidence": None,
                        "attributes": connection_attributes,
                    }
                )

    raw_edges = topology.get("edges", [])
    raw_topology_links = topology.get("links", [])
    if raw_edges is not None and not isinstance(raw_edges, list):
        raise TopologyFusionError("model topology.edges must be a list")
    if raw_topology_links is not None and not isinstance(raw_topology_links, list):
        raise TopologyFusionError("model topology.links must be a list")
    all_raw_edges = list(raw_edges or []) + list(raw_topology_links or [])
    for index, raw_edge in enumerate(all_raw_edges):
        if not isinstance(raw_edge, Mapping):
            raise TopologyFusionError("model topology.edges entries must be objects")
        source = _required_identifier(raw_edge.get("source"), f"model edge {index}.source")
        target = _required_identifier(raw_edge.get("target"), f"model edge {index}.target")
        source = node_ids_by_key.get(_compact_identifier_key(source), source)
        target = node_ids_by_key.get(_compact_identifier_key(target), target)
        for endpoint in (source, target):
            key = _compact_identifier_key(endpoint)
            if key not in node_ids_by_key:
                node_ids_by_key[key] = endpoint
                nodes[endpoint] = {"implicit": True}
        raw_type = _text(raw_edge.get("type"), "topology_link", 100)
        attributes = _safe_attributes(raw_edge.get("attributes", {}))
        attributes.update(
            {
                str(name): _safe_json(value)
                for name, value in raw_edge.items()
                if name not in {"source", "target", "type", "confidence", "attributes"}
                and str(name).strip().lower() not in _RESERVED_ATTRIBUTE_KEYS
            }
        )
        relation_type = raw_type
        if raw_type.lower() in _LINE_STYLES:
            relation_type = "topology_link"
            attributes["line_style"] = raw_type
        links.append(
            {
                "source": source,
                "target": target,
                "type": relation_type,
                "confidence": _optional_confidence(raw_edge.get("confidence")),
                "attributes": attributes,
            }
        )

    raw_alarms = topology.get("alarms", [])
    if raw_alarms is not None and not isinstance(raw_alarms, list):
        raise TopologyFusionError("model topology.alarms must be a list")
    for raw_alarm in raw_alarms or []:
        if not isinstance(raw_alarm, Mapping):
            continue
        alarm_node_id = raw_alarm.get(
            "nodeId", raw_alarm.get("business_id", raw_alarm.get("id"))
        )
        if alarm_node_id is None:
            continue
        alarm_key = _compact_identifier_key(str(alarm_node_id))
        canonical_alarm_node = node_ids_by_key.get(alarm_key)
        if canonical_alarm_node is None:
            continue
        alarm_attributes = {
            str(name): _safe_json(value)
            for name, value in raw_alarm.items()
            if name not in {"nodeId", "business_id", "id"}
            and str(name).strip().lower() not in _RESERVED_ATTRIBUTE_KEYS
        }
        nodes[canonical_alarm_node].setdefault("alarms", []).append(alarm_attributes)

    layout = str(topology.get("layout", "")).strip().casefold()
    raw_center = topology.get("centerNode", topology.get("center_node"))
    if layout == "star" and raw_center is not None:
        center_id = _required_identifier(raw_center, "model topology centerNode")
        center_id = node_ids_by_key.get(_compact_identifier_key(center_id), center_id)
        if center_id in nodes:
            leaves = [node_id for node_id in nodes if node_id != center_id]
            already_declared = any(
                template.get("type") == "star"
                and template.get("center") == center_id
                for template in structure_templates
            )
            if not already_declared:
                structure_templates.append(
                    {
                        "template_id": "model:star:1",
                        "type": "star",
                        "center": center_id,
                        "leaves": leaves,
                        "source": "multimodal_model",
                    }
                )

    existing_by_pair = {
        _undirected_pair(str(link["source"]), str(link["target"])): link
        for link in links
    }
    for template in structure_templates:
        if template.get("type") != "star":
            continue
        template_id = str(template.get("template_id", "model:star"))
        center_id = str(template.get("center", ""))
        if center_id not in nodes:
            continue
        for leaf_id in (str(item) for item in template.get("leaves", [])):
            if leaf_id not in nodes or leaf_id == center_id:
                continue
            pair = _undirected_pair(center_id, leaf_id)
            existing_link = existing_by_pair.get(pair)
            if existing_link is not None:
                existing_link.setdefault("attributes", {})[
                    "structure_template_id"
                ] = template_id
                continue
            derived_link = {
                "source": center_id,
                "target": leaf_id,
                "type": "topology_link",
                "confidence": template.get("confidence"),
                "attributes": {
                    "derivation_status": "structurally_derived",
                    "structure_template_id": template_id,
                    "structure_type": "star",
                },
            }
            links.append(derived_link)
            existing_by_pair[pair] = derived_link

    negative_evidence = _normalize_negative_evidence(
        topology,
        node_ids_by_key=node_ids_by_key,
        nodes=nodes,
        connected_nodes=connected_nodes,
    )

    if len(nodes) > TopologyVisionContract.MAX_OBJECTS:
        raise TopologyFusionError("model JSON contains too many nodes")
    if len(links) > TopologyVisionContract.MAX_RELATIONS:
        raise TopologyFusionError("model JSON contains too many links")

    metadata = {
        str(name): _safe_json(value)
        for name, value in topology.items()
        if name
        not in {
            "nodes",
            "objects",
            "edges",
            "links",
            "layers",
            "structure_templates",
            "negative_edges",
            "rejected_edges",
            "disconnected_pairs",
        }
        and str(name).strip().lower() not in _RESERVED_ATTRIBUTE_KEYS
    }
    return (
        nodes,
        _deduplicate_model_links(links),
        metadata,
        structure_templates,
        negative_evidence,
    )


def _normalize_negative_evidence(
    topology: Mapping[str, Any],
    *,
    node_ids_by_key: Mapping[str, str],
    nodes: Mapping[str, dict[str, Any]],
    connected_nodes: list[tuple[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    pairs: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for field_name in ("negative_edges", "rejected_edges", "disconnected_pairs"):
        raw_items = topology.get(field_name, [])
        if raw_items is None:
            continue
        if not isinstance(raw_items, list):
            raise TopologyFusionError(f"model topology.{field_name} must be a list")
        for index, raw_item in enumerate(raw_items):
            reason = "explicit_model_rejection"
            if isinstance(raw_item, Mapping):
                raw_source = raw_item.get("source")
                raw_target = raw_item.get("target")
                if raw_item.get("reason") is not None:
                    reason = _text(raw_item.get("reason"), reason, 500)
            elif isinstance(raw_item, (list, tuple)) and len(raw_item) == 2:
                raw_source, raw_target = raw_item
            else:
                raise TopologyFusionError(
                    f"model topology.{field_name}[{index}] is invalid"
                )
            source = _required_identifier(
                raw_source, f"model topology.{field_name}[{index}].source"
            )
            target = _required_identifier(
                raw_target, f"model topology.{field_name}[{index}].target"
            )
            source = node_ids_by_key.get(_compact_identifier_key(source), source)
            target = node_ids_by_key.get(_compact_identifier_key(target), target)
            if source == target:
                continue
            pair = _undirected_pair(source, target)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            pairs.append({"source": source, "target": target, "reason": reason})

    isolated_nodes: set[str] = set()
    for node_id, raw_node in connected_nodes:
        if raw_node.get("connections_complete") is not True:
            continue
        connections = raw_node.get("connections")
        if connections == [] or connections == {}:
            isolated_nodes.add(node_id)
    for node_id, attributes in nodes.items():
        if attributes.get("connections_complete") is True and attributes.get(
            "no_connections"
        ) is True:
            isolated_nodes.add(node_id)

    return {
        "reject_all": topology.get("no_connections") is True,
        "pairs": pairs,
        "isolated_nodes": sorted(isolated_nodes, key=_identifier_sort_key),
    }


def _fuse_object(
    cv_object: dict[str, Any],
    model_match: tuple[str, dict[str, Any]] | None,
    default_canvas_id: str,
) -> dict[str, Any]:
    result = copy.deepcopy(cv_object)
    result["canvas_id"] = _text(result.get("canvas_id"), default_canvas_id, 200)
    attributes = _safe_attributes(result.get("attributes", {}))
    attributes["cv_confidence"] = result["confidence"]
    attributes["geometry_status"] = "cv_grounded"
    if model_match is None:
        attributes["fusion_status"] = "cv_only"
        attributes["evidence_sources"] = ["local_cv"]
    else:
        model_id, model_node = model_match
        model_semantics = _safe_attributes(model_node)
        model_type = _text(model_semantics.get("type"), "", 100)
        if model_type and str(result.get("type", "")).lower() in _GENERIC_OBJECT_TYPES:
            result["type"] = model_type
        model_label = _text(model_semantics.get("label"), "", 500)
        if model_label and not str(result.get("label", "")).strip():
            result["label"] = model_label
        attributes["fusion_status"] = "confirmed"
        attributes["evidence_sources"] = ["local_cv", "multimodal_model"]
        attributes["model_business_id"] = model_id
        attributes["model_semantics"] = model_semantics
    result["attributes"] = attributes
    return result


def _fuse_links(
    cv_links: list[dict[str, Any]],
    model_links: list[dict[str, Any]],
    *,
    semantic_model_links: list[dict[str, Any]],
    model_nodes_by_semantic_id: Mapping[str, Mapping[str, Any]],
    negative_evidence: Mapping[str, Any],
    matched_cv_ids: set[str],
    model_layers_by_cv_id: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cv_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for link in cv_links:
        key = _undirected_pair(link["source"], link["target"])
        previous = cv_by_pair.get(key)
        if previous is None or float(link["confidence"]) > float(previous["confidence"]):
            cv_by_pair[key] = link

    model_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for link in model_links:
        key = _undirected_pair(link["source"], link["target"])
        model_by_pair.setdefault(key, link)

    negative_reasons = {
        _undirected_pair(str(item["source"]), str(item["target"])): str(
            item.get("reason", "explicit_model_rejection")
        )
        for item in negative_evidence.get("pairs", [])
        if isinstance(item, Mapping) and item.get("source") and item.get("target")
    }
    isolated_nodes = {
        str(node_id) for node_id in negative_evidence.get("isolated_nodes", [])
    }
    reject_all = negative_evidence.get("reject_all") is True

    def rejection_reason(pair: tuple[str, str]) -> str | None:
        explicit_reason = negative_reasons.get(pair)
        if explicit_reason is not None:
            return explicit_reason
        if pair[0] in isolated_nodes or pair[1] in isolated_nodes:
            return "model_declares_endpoint_has_no_connections"
        if reject_all and pair[0] in matched_cv_ids and pair[1] in matched_cv_ids:
            return "model_explicitly_declares_no_connections"
        return None

    active_cv_links = [
        link
        for pair, link in cv_by_pair.items()
        if rejection_reason(pair) is None
    ]
    cv_adjacency = _graph_adjacency(active_cv_links)
    model_adjacency = _graph_adjacency(semantic_model_links)

    fused: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for key in sorted(set(cv_by_pair) | set(model_by_pair)):
        cv_link = cv_by_pair.get(key)
        model_link = model_by_pair.get(key)
        rejected_reason = rejection_reason(key) if cv_link is not None else None
        if cv_link is not None and rejected_reason is not None and model_link is None:
            confidence = float(cv_link["confidence"])
            attributes = _safe_attributes(cv_link.get("attributes", {}))
            attributes.update(
                {
                    "fusion_status": "llm_rejected",
                    "evidence_sources": ["local_cv", "multimodal_model"],
                    "cv_confidence": confidence,
                    "rejection_reason": rejected_reason,
                    "relation_state": "rejected",
                }
            )
            rejected.append(
                {
                    "relation_id": f"fusion-rejected:{cv_link['source']}:{cv_link['target']}",
                    "source": str(cv_link["source"]),
                    "target": str(cv_link["target"]),
                    "type": str(cv_link["type"]),
                    "confidence": round(confidence, 4),
                    "attributes": attributes,
                }
            )
            continue
        if cv_link is not None and model_link is not None:
            source = str(model_link["source"])
            target = str(model_link["target"])
            relation_type = _preferred_relation_type(cv_link, model_link)
            confidence = float(cv_link["confidence"])
            attributes = _safe_attributes(cv_link.get("attributes", {}))
            attributes.update(
                {
                    "fusion_status": "conflict" if rejected_reason else "confirmed",
                    "evidence_sources": ["local_cv", "multimodal_model"],
                    "cv_confidence": confidence,
                    "model_attributes": _safe_attributes(
                        model_link.get("attributes", {})
                    ),
                    "derivation_status": str(
                        model_link.get("attributes", {}).get(
                            "derivation_status", "model_reported"
                        )
                    ),
                }
            )
            if rejected_reason:
                attributes.update(
                    {
                        "conflict_reason": "model_contains_positive_and_negative_edge_evidence",
                        "negative_evidence_reason": rejected_reason,
                    }
                )
            if model_link.get("confidence") is not None:
                attributes["model_confidence"] = model_link["confidence"]
        elif cv_link is not None:
            source = str(cv_link["source"])
            target = str(cv_link["target"])
            relation_type = str(cv_link["type"])
            confidence = float(cv_link["confidence"])
            attributes = _safe_attributes(cv_link.get("attributes", {}))
            model_path = _find_graph_path(
                model_adjacency,
                source,
                target,
                max_hops=4,
                allow_internal=lambda node_id: _model_path_internal_allowed(
                    node_id, model_nodes_by_semantic_id
                ),
            )
            source_layer = model_layers_by_cv_id.get(source)
            target_layer = model_layers_by_cv_id.get(target)
            same_model_layer = bool(source_layer and source_layer == target_layer)
            fusion_status = "path_equivalent" if model_path is not None else "cv_only"
            attributes.update(
                {
                    "fusion_status": fusion_status,
                    "evidence_sources": (
                        ["local_cv", "multimodal_model"]
                        if model_path is not None
                        else ["local_cv"]
                    ),
                    "cv_confidence": confidence,
                }
            )
            if model_path is not None:
                attributes.update(
                    {
                        "equivalence_kind": "cv_direct_model_logical_path",
                        "equivalent_model_path": model_path,
                    }
                )
            elif same_model_layer:
                attributes["model_layer_context"] = source_layer
        else:
            assert model_link is not None
            source = str(model_link["source"])
            target = str(model_link["target"])
            relation_type = str(model_link["type"])
            confidence = (
                float(model_link["confidence"])
                if model_link.get("confidence") is not None
                else DEFAULT_MODEL_CONFIDENCE
            )
            attributes = _safe_attributes(model_link.get("attributes", {}))
            cv_path = _find_graph_path(
                cv_adjacency,
                source,
                target,
                max_hops=4,
            )
            structurally_derived = (
                attributes.get("derivation_status") == "structurally_derived"
            )
            fusion_status = (
                "path_equivalent"
                if cv_path is not None
                else "structurally_derived"
                if structurally_derived
                else "model_only"
            )
            attributes.update(
                {
                    "fusion_status": fusion_status,
                    "evidence_sources": (
                        ["local_cv", "multimodal_model"]
                        if cv_path is not None
                        else ["multimodal_model"]
                    ),
                    "visual_evidence_status": "not_confirmed_by_local_cv",
                    "confidence_basis": (
                        "model_reported"
                        if model_link.get("confidence") is not None
                        else "uncalibrated_default"
                    ),
                }
            )
            if cv_path is not None:
                attributes.update(
                    {
                        "equivalence_kind": "model_direct_cv_pixel_path",
                        "equivalent_cv_path": cv_path,
                    }
                )
        fused.append(
            {
                "relation_id": f"fusion:{source}:{target}",
                "source": source,
                "target": target,
                "type": relation_type,
                "confidence": round(confidence, 4),
                "attributes": attributes,
            }
        )
    return fused, rejected


def _graph_adjacency(links: list[dict[str, Any]]) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {}
    for link in links:
        source = str(link.get("source", "")).strip()
        target = str(link.get("target", "")).strip()
        if not source or not target or source == target:
            continue
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set()).add(source)
    return adjacency


def _find_graph_path(
    adjacency: Mapping[str, set[str]],
    source: str,
    target: str,
    *,
    max_hops: int,
    allow_internal: Callable[[str], bool] | None = None,
) -> list[str] | None:
    if source == target or source not in adjacency or target not in adjacency:
        return None
    queue: deque[list[str]] = deque([[source]])
    seen = {source}
    while queue:
        path = queue.popleft()
        if len(path) - 1 >= max_hops:
            continue
        current = path[-1]
        for neighbor in sorted(adjacency.get(current, ()), key=_identifier_sort_key):
            if neighbor in seen:
                continue
            candidate = path + [neighbor]
            if neighbor == target:
                return candidate if len(candidate) > 2 else None
            if allow_internal is not None and not allow_internal(neighbor):
                continue
            seen.add(neighbor)
            queue.append(candidate)
    return None


def _model_path_internal_allowed(
    node_id: str,
    nodes: Mapping[str, Mapping[str, Any]],
) -> bool:
    attributes = nodes.get(node_id, {})
    if attributes.get("virtual") is True or attributes.get("implicit") is True:
        return True
    semantic_type = " ".join(
        str(attributes.get(name, "")) for name in ("type", "role", "kind")
    ).casefold()
    return any(
        marker in semantic_type
        for marker in ("bus", "virtual", "logical", "junction", "connector")
    )


def _deduplicate_model_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
    for link in links:
        if link["source"] == link["target"]:
            continue
        key = _undirected_pair(link["source"], link["target"])
        existing = deduplicated.get(key)
        if existing is None:
            deduplicated[key] = copy.deepcopy(link)
            continue
        existing_attributes = existing.setdefault("attributes", {})
        existing_attributes.update(_safe_attributes(link.get("attributes", {})))
        if existing.get("confidence") is None and link.get("confidence") is not None:
            existing["confidence"] = link["confidence"]
    return list(deduplicated.values())


def _unlocated_object(model_id: str, model_node: Mapping[str, Any]) -> dict[str, Any]:
    attributes = _safe_attributes(model_node)
    model_geometry = attributes.pop("model_geometry", None)
    node_type = _text(model_node.get("type"), "network_device", 100)
    is_virtual = bool(model_node.get("virtual")) or "bus" in node_type.casefold()
    geometry_status = "unlocated"
    result: dict[str, Any] = {
        "business_id": model_id,
        "type": node_type,
        "label": _text(model_node.get("label"), model_id, 500),
        "confidence": _confidence(model_node.get("confidence"), DEFAULT_MODEL_CONFIDENCE),
    }
    if isinstance(model_geometry, Mapping):
        raw_bbox = model_geometry.get("bbox")
        raw_canvas_id = model_geometry.get("canvas_id")
        if isinstance(raw_bbox, list) and len(raw_bbox) == 4 and raw_canvas_id:
            bbox = _bbox(raw_bbox, f"model node {model_id}")
            result.update(
                {
                    "canvas_id": _text(raw_canvas_id, "uploaded_topology", 200),
                    "bbox": bbox,
                    "confidence": _confidence(
                        model_geometry.get("confidence"), result["confidence"]
                    ),
                }
            )
            geometry_status = "model_pixel_grounded"
    attributes.update(
        {
            "fusion_status": "model_only",
            "evidence_sources": ["multimodal_model"],
            "geometry_status": geometry_status,
            "virtual": is_virtual,
            "interaction_eligible": False,
        }
    )
    result["attributes"] = attributes
    return result


def _infer_unlocated_geometry(
    unlocated_objects: list[dict[str, Any]],
    *,
    fused_objects: list[dict[str, Any]],
    structure_templates: list[dict[str, Any]],
    semantic_model_links: list[dict[str, Any]],
    canvas_id: str,
) -> list[dict[str, Any]]:
    inferred_objects = copy.deepcopy(unlocated_objects)
    if not inferred_objects or not fused_objects:
        return inferred_objects

    geometry: dict[str, list[float]] = {
        str(item["business_id"]): [float(value) for value in item["bbox"]]
        for item in fused_objects
        if isinstance(item.get("bbox"), list) and len(item["bbox"]) == 4
    }
    widths = [bbox[2] for bbox in geometry.values()]
    heights = [bbox[3] for bbox in geometry.values()]
    default_width = float(median(widths)) if widths else 60.0
    default_height = float(median(heights)) if heights else 24.0
    all_centers_x = sorted(_box_center(bbox)[0] for bbox in geometry.values())
    positive_gaps = [
        right - left
        for left, right in zip(all_centers_x, all_centers_x[1:])
        if right - left > default_width * 0.5
    ]
    default_gap = (
        max(default_width * 2.0, float(median(positive_gaps)))
        if positive_gaps
        else default_width * 2.5
    )
    objects_by_id = {
        str(item["business_id"]): item for item in inferred_objects
    }

    def assign(node_id: str, center: tuple[float, float], method: str) -> None:
        item = objects_by_id.get(node_id)
        if item is None or node_id in geometry:
            return
        width = default_width
        height = default_height
        x = max(0.0, center[0] - width / 2.0)
        y = max(0.0, center[1] - height / 2.0)
        bbox = [round(x, 3), round(y, 3), round(width, 3), round(height, 3)]
        item["canvas_id"] = canvas_id
        item["bbox"] = bbox
        rendered_center = _box_center(bbox)
        item["center"] = [
            round(rendered_center[0], 3),
            round(rendered_center[1], 3),
        ]
        attributes = item.setdefault("attributes", {})
        attributes.update(
            {
                "geometry_status": "spatially_inferred",
                "geometry_method": method,
                "rendering_only": True,
                "interaction_eligible": False,
            }
        )
        geometry[node_id] = bbox

    for template in structure_templates:
        if template.get("type") != "layered":
            continue
        layers = template.get("layers", [])
        if not isinstance(layers, list):
            continue
        for layer in layers:
            if not isinstance(layer, Mapping):
                continue
            members = [str(member) for member in layer.get("members", [])]
            located = [
                (index, _box_center(geometry[node_id]))
                for index, node_id in enumerate(members)
                if node_id in geometry
            ]
            if not located:
                continue
            layer_y = float(median(center[1] for _index, center in located))
            for index, node_id in enumerate(members):
                if node_id in geometry or node_id not in objects_by_id:
                    continue
                left = [item for item in located if item[0] < index]
                right = [item for item in located if item[0] > index]
                if left and right:
                    left_index, left_center = left[-1]
                    right_index, right_center = right[0]
                    ratio = (index - left_index) / (right_index - left_index)
                    center_x = left_center[0] + ratio * (
                        right_center[0] - left_center[0]
                    )
                elif left:
                    left_index, left_center = left[-1]
                    center_x = left_center[0] + default_gap * (index - left_index)
                else:
                    right_index, right_center = right[0]
                    center_x = right_center[0] - default_gap * (right_index - index)
                assign(node_id, (center_x, layer_y), "same_layer_interpolation")

    for template in structure_templates:
        if template.get("type") != "star":
            continue
        center_id = str(template.get("center", ""))
        leaves = [str(leaf) for leaf in template.get("leaves", [])]
        if center_id not in geometry or not leaves:
            continue
        center = _box_center(geometry[center_id])
        located_radii = [
            math.dist(center, _box_center(geometry[leaf]))
            for leaf in leaves
            if leaf in geometry
        ]
        radius = (
            float(median(located_radii))
            if located_radii
            else max(default_gap * 2.0, 120.0)
        )
        radius = max(radius, default_gap * 1.5)
        for index, leaf_id in enumerate(leaves):
            if leaf_id in geometry or leaf_id not in objects_by_id:
                continue
            angle = -math.pi / 2.0 + 2.0 * math.pi * index / len(leaves)
            assign(
                leaf_id,
                (
                    center[0] + radius * math.cos(angle),
                    center[1] + radius * math.sin(angle),
                ),
                "star_template_projection",
            )

    adjacency = _graph_adjacency(semantic_model_links)
    for _iteration in range(3):
        progress = False
        for node_id, item in objects_by_id.items():
            if node_id in geometry:
                continue
            neighbor_centers = [
                _box_center(geometry[neighbor])
                for neighbor in adjacency.get(node_id, ())
                if neighbor in geometry
            ]
            if len(neighbor_centers) < 2:
                continue
            assign(
                node_id,
                (
                    sum(center[0] for center in neighbor_centers)
                    / len(neighbor_centers),
                    sum(center[1] for center in neighbor_centers)
                    / len(neighbor_centers),
                ),
                "connected_neighbor_centroid",
            )
            progress = progress or item.get("attributes", {}).get(
                "geometry_status"
            ) == "spatially_inferred"
        if not progress:
            break
    return inferred_objects


def _box_center(bbox: list[float]) -> tuple[float, float]:
    return bbox[0] + bbox[2] / 2.0, bbox[1] + bbox[3] / 2.0


def _geometry_status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        attributes = item.get("attributes", {})
        status = str(attributes.get("geometry_status", "unlocated"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _infer_canvas_id(source: Mapping[str, Any]) -> str:
    coordinate_space = source.get("coordinate_space")
    if isinstance(coordinate_space, Mapping):
        frames = coordinate_space.get("frames")
        if isinstance(frames, list) and frames and isinstance(frames[0], Mapping):
            value = str(frames[0].get("canvas_id", "")).strip()
            if value:
                return value[:200]
    input_payload = source.get("input")
    if isinstance(input_payload, Mapping):
        canvases = input_payload.get("canvases")
        if isinstance(canvases, list) and canvases and isinstance(canvases[0], Mapping):
            value = str(canvases[0].get("canvas_id", "")).strip()
            if value:
                return value[:200]
    return "uploaded_topology"


def _preferred_relation_type(
    cv_link: Mapping[str, Any], model_link: Mapping[str, Any]
) -> str:
    cv_type = str(cv_link.get("type", "topology_link"))
    model_type = str(model_link.get("type", "topology_link"))
    if cv_type in {"", "relation", "topology_link"} and model_type:
        return model_type
    return cv_type


def _status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        attributes = item.get("attributes", {})
        status = str(attributes.get("fusion_status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _bbox(value: Any, context: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise TopologyFusionError(f"{context} requires bbox=[x,y,width,height]")
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise TopologyFusionError(f"{context} bbox must contain finite numbers")
        numeric = float(item)
        if not math.isfinite(numeric):
            raise TopologyFusionError(f"{context} bbox must contain finite numbers")
        result.append(numeric)
    if result[0] < 0 or result[1] < 0 or result[2] <= 0 or result[3] <= 0:
        raise TopologyFusionError(f"{context} bbox is invalid")
    return result


def _confidence(value: Any, default: float) -> float:
    if value is None:
        return round(default, 4)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return round(default, 4)
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0 or numeric > 1:
        return round(default, 4)
    return round(numeric, 4)


def _optional_confidence(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0 or numeric > 1:
        return None
    return round(numeric, 4)


def _required_identifier(value: Any, context: str) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise TopologyFusionError(f"{context} requires an identifier")
    result = unicodedata.normalize("NFKC", str(value)).strip()
    if not result or len(result) > 200 or any(ord(char) < 32 for char in result):
        raise TopologyFusionError(f"{context} identifier is invalid")
    return result


def _text(value: Any, default: str, maximum: int) -> str:
    if value is None:
        return default
    result = str(value).strip()
    return result[:maximum] if result else default


def _exact_identifier_key(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def _compact_identifier_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).upper()
    compact = re.sub(r"[^0-9A-Z\u4e00-\u9fff]+", "", normalized)
    return compact or normalized.strip()


def _identifier_sort_key(value: str) -> tuple[str, str]:
    return _compact_identifier_key(value), _exact_identifier_key(value)


def _undirected_pair(source: str, target: str) -> tuple[str, str]:
    ordered = sorted((str(source), str(target)), key=_identifier_sort_key)
    return ordered[0], ordered[1]


def _safe_attributes(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): _safe_json(item)
        for key, item in value.items()
        if str(key).strip().lower() not in _RESERVED_ATTRIBUTE_KEYS
    }


def _safe_json(value: Any, depth: int = 0) -> Any:
    if depth >= 7:
        return str(value)[:1000]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {
            str(key): _safe_json(item, depth + 1)
            for key, item in value.items()
            if str(key).strip().lower() not in _RESERVED_ATTRIBUTE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_safe_json(item, depth + 1) for item in value]
    return str(value)


__all__ = [
    "DEFAULT_MODEL_CONFIDENCE",
    "FUSION_SCHEMA_VERSION",
    "TopologyFusionError",
    "fuse_topology_payloads",
]
