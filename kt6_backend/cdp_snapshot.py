from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote


SCHEMA_VERSION = "kt6.cdp-multisource.v1"
MAX_NODE_COUNT = 10_000
MAX_RELATION_COUNT = 40_000

_DOM_SOURCE = "DOMSnapshot.captureSnapshot"
_AX_SOURCE = "Accessibility.getFullAXTree"
_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "gridcell",
        "link",
        "listbox",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "radio",
        "scrollbar",
        "searchbox",
        "slider",
        "spinbutton",
        "switch",
        "tab",
        "textbox",
        "treeitem",
    }
)
_NATIVE_FOCUSABLE_TAGS = frozenset({"button", "input", "select", "textarea"})


def normalize_cdp_snapshot(
    dom_snapshot: Mapping[str, Any] | None,
    ax_tree: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Normalize CDP DOM and accessibility snapshots into a safe KT6 node graph.

    DOM and AX records are joined by the composite ``(frameId, backendNodeId)``
    identity.  The result deliberately exposes only interaction *candidates*;
    snapshot evidence is never treated as permission or a live execution target.
    """

    return CDPSnapshotNormalizer().normalize(dom_snapshot, ax_tree)


class CDPSnapshotNormalizer:
    """Pure-Python normalizer for CDP DOMSnapshot and Accessibility results."""

    def normalize(
        self,
        dom_snapshot: Mapping[str, Any] | None,
        ax_tree: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    ) -> dict[str, Any]:
        dom_payload = _unwrap_mapping(dom_snapshot, "DOM snapshot")
        ax_nodes = _ax_nodes(ax_tree)
        strings = _string_table(dom_payload.get("strings", []))
        documents = dom_payload.get("documents", [])
        if not isinstance(documents, list):
            raise ValueError("DOM snapshot documents must be a list")
        _preflight_snapshot_limits(documents, ax_nodes, strings)

        nodes_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
        node_order: list[str] = []
        nodes_by_id: dict[str, dict[str, Any]] = {}
        dom_locations: dict[tuple[int, int], str] = {}
        backend_frames: dict[int, set[str]] = defaultdict(set)
        document_records: list[dict[str, Any]] = []
        pending_iframes: list[tuple[int, int, int]] = []
        relations: list[dict[str, Any]] = []
        relation_keys: set[tuple[str, str, str]] = set()

        for document_index, raw_document in enumerate(documents):
            if not isinstance(raw_document, Mapping):
                raise ValueError(f"DOM snapshot document {document_index} must be an object")
            frame_id = _decode_string(raw_document.get("frameId"), strings)
            node_tree = raw_document.get("nodes", {})
            if not isinstance(node_tree, Mapping):
                raise ValueError(
                    f"DOM snapshot document {document_index}.nodes must be an object"
                )
            node_count = _node_count(node_tree)
            parent_indexes = _integer_array(node_tree.get("parentIndex", []))
            backend_ids = _integer_array(node_tree.get("backendNodeId", []))
            node_types = _integer_array(node_tree.get("nodeType", []))
            node_names = _sequence(node_tree.get("nodeName", []))
            node_values = _sequence(node_tree.get("nodeValue", []))
            attributes = _node_attributes(node_tree.get("attributes"))
            shadow_types = _rare_values(node_tree.get("shadowRootType"))
            content_documents = _rare_values(node_tree.get("contentDocumentIndex"))
            clickable_indexes = _rare_boolean_indexes(node_tree.get("isClickable"))
            layout_bounds = _layout_bounds(raw_document.get("layout"))

            root_node_id = ""
            for node_index in range(node_count):
                backend_node_id = _positive_integer_at(backend_ids, node_index)
                key = _dom_key(frame_id, backend_node_id, document_index, node_index)
                if key in nodes_by_key:
                    raise ValueError(
                        "duplicate CDP node identity for "
                        f"frameId={frame_id!r}, backendNodeId={backend_node_id!r}"
                    )
                node_id = _node_id(
                    frame_id,
                    backend_node_id,
                    document_index=document_index,
                    node_index=node_index,
                )
                raw_attributes = attributes.get(node_index, [])
                decoded_attributes = _decode_attributes(raw_attributes, strings)
                dom_name = _decode_string(_at(node_names, node_index), strings)
                dom_value = _decode_string(_at(node_values, node_index), strings)
                shadow_root_type = _decode_string(
                    shadow_types.get(node_index), strings
                )
                bounds = layout_bounds.get(node_index)
                role = str(decoded_attributes.get("role", "")).strip()
                name = _dom_name(decoded_attributes, dom_value)
                disabled = _dom_disabled(decoded_attributes)
                focusable = _dom_focusable(dom_name, decoded_attributes, disabled)
                is_clickable = node_index in clickable_indexes
                node = {
                    "node_id": node_id,
                    "frame_id": frame_id,
                    "backend_node_id": backend_node_id,
                    "document_index": document_index,
                    "dom_node_index": node_index,
                    "ax_node_ids": [],
                    "dom_node_type": _at(node_types, node_index),
                    "dom_node_name": dom_name,
                    "dom_node_value": dom_value,
                    "attributes": decoded_attributes,
                    "shadow_root_type": shadow_root_type or None,
                    "role": role,
                    "name": name,
                    "description": "",
                    "value": "",
                    "disabled": disabled,
                    "focusable": focusable,
                    "is_clickable": is_clickable,
                    "bounds": bounds,
                    "center": _center(bounds),
                    "ignored": False,
                    "_parent_links": [],
                    "_children": [],
                    "_field_sources": {
                        "disabled": _DOM_SOURCE,
                        "focusable": _DOM_SOURCE,
                        "is_clickable": _DOM_SOURCE,
                        **({"bounds": f"{_DOM_SOURCE}.layout"} if bounds else {}),
                        **({"role": _DOM_SOURCE} if role else {}),
                        **({"name": _DOM_SOURCE} if name else {}),
                    },
                    "_source_records": [
                        {
                            "source": _DOM_SOURCE,
                            "frame_id": frame_id,
                            "backend_node_id": backend_node_id,
                            "document_index": document_index,
                            "node_index": node_index,
                        }
                    ],
                }
                nodes_by_key[key] = node
                nodes_by_id[node_id] = node
                node_order.append(node_id)
                dom_locations[(document_index, node_index)] = node_id
                if backend_node_id is not None:
                    backend_frames[backend_node_id].add(frame_id)
                parent_index = _integer_at(parent_indexes, node_index, default=-1)
                if root_node_id == "" and parent_index < 0:
                    root_node_id = node_id

            document_record = {
                "document_index": document_index,
                "frame_id": frame_id,
                "document_url": _decode_string(
                    raw_document.get("documentURL"), strings
                ),
                "title": _decode_string(raw_document.get("title"), strings),
                "root_node_id": root_node_id,
                "owner_node_id": None,
                "parent_frame_id": None,
            }
            document_records.append(document_record)

            for node_index in range(node_count):
                child_id = dom_locations[(document_index, node_index)]
                parent_index = _integer_at(parent_indexes, node_index, default=-1)
                if 0 <= parent_index < node_count:
                    parent_id = dom_locations[(document_index, parent_index)]
                    shadow_type = nodes_by_id[child_id].get("shadow_root_type")
                    relation_type = "shadow_root" if shadow_type else "dom_child"
                    details = (
                        {"shadow_root_type": shadow_type} if shadow_type else {}
                    )
                    _add_relation(
                        relations,
                        relation_keys,
                        nodes_by_id,
                        source=parent_id,
                        target=child_id,
                        relation_type=relation_type,
                        provenance=_DOM_SOURCE,
                        details=details,
                    )
                content_document_index = _as_int(content_documents.get(node_index))
                if content_document_index is not None:
                    pending_iframes.append(
                        (document_index, node_index, content_document_index)
                    )

        for owner_document, owner_node_index, child_document in pending_iframes:
            owner_id = dom_locations.get((owner_document, owner_node_index))
            if not owner_id or not 0 <= child_document < len(document_records):
                continue
            child_record = document_records[child_document]
            child_root_id = child_record.get("root_node_id")
            if not child_root_id:
                continue
            _add_relation(
                relations,
                relation_keys,
                nodes_by_id,
                source=owner_id,
                target=child_root_id,
                relation_type="iframe_document",
                provenance=_DOM_SOURCE,
                details={
                    "owner_document_index": owner_document,
                    "child_document_index": child_document,
                },
            )
            child_record["owner_node_id"] = owner_id
            child_record["parent_frame_id"] = nodes_by_id[owner_id]["frame_id"]

        ax_frames = _resolve_ax_frames(ax_nodes, backend_frames, document_records)
        ax_id_to_node_id: dict[str, str] = {}
        for ax_index, raw_ax_node in enumerate(ax_nodes):
            ax_id = str(raw_ax_node.get("nodeId", f"ax-{ax_index}")).strip()
            frame_id = ax_frames.get(ax_id, "")
            backend_node_id = _as_positive_int(raw_ax_node.get("backendDOMNodeId"))
            key = _ax_key(frame_id, backend_node_id, ax_id)
            node = nodes_by_key.get(key)
            if node is None:
                node_id = _node_id(
                    frame_id,
                    backend_node_id,
                    ax_node_id=ax_id,
                )
                node = {
                    "node_id": node_id,
                    "frame_id": frame_id,
                    "backend_node_id": backend_node_id,
                    "document_index": None,
                    "dom_node_index": None,
                    "ax_node_ids": [],
                    "dom_node_type": None,
                    "dom_node_name": "",
                    "dom_node_value": "",
                    "attributes": {},
                    "shadow_root_type": None,
                    "role": "",
                    "name": "",
                    "description": "",
                    "value": "",
                    "disabled": False,
                    "focusable": False,
                    "is_clickable": False,
                    "bounds": None,
                    "center": None,
                    "ignored": False,
                    "_parent_links": [],
                    "_children": [],
                    "_field_sources": {},
                    "_source_records": [],
                }
                nodes_by_key[key] = node
                nodes_by_id[node_id] = node
                node_order.append(node_id)
            ax_id_to_node_id[ax_id] = node["node_id"]
            if ax_id not in node["ax_node_ids"]:
                node["ax_node_ids"].append(ax_id)
            _merge_ax_node(node, raw_ax_node, ax_id)

        for ax_index, raw_ax_node in enumerate(ax_nodes):
            ax_id = str(raw_ax_node.get("nodeId", f"ax-{ax_index}")).strip()
            target_id = ax_id_to_node_id.get(ax_id)
            if not target_id:
                continue
            parent_ax_id = str(raw_ax_node.get("parentId", "")).strip()
            parent_id = ax_id_to_node_id.get(parent_ax_id)
            if parent_id and parent_id != target_id:
                _add_relation(
                    relations,
                    relation_keys,
                    nodes_by_id,
                    source=parent_id,
                    target=target_id,
                    relation_type="ax_child",
                    provenance=_AX_SOURCE,
                )
            child_ids = raw_ax_node.get("childIds", [])
            if isinstance(child_ids, list):
                for raw_child_id in child_ids:
                    child_id = ax_id_to_node_id.get(str(raw_child_id).strip())
                    if child_id and child_id != target_id:
                        _add_relation(
                            relations,
                            relation_keys,
                            nodes_by_id,
                            source=target_id,
                            target=child_id,
                            relation_type="ax_child",
                            provenance=_AX_SOURCE,
                        )

        normalized_nodes = []
        for node_id in node_order:
            node = nodes_by_id[node_id]
            normalized_nodes.append(_finalize_node(node))
        if len(normalized_nodes) > MAX_NODE_COUNT:
            raise ValueError(f"normalized CDP snapshot exceeds {MAX_NODE_COUNT} nodes")

        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "cdp_dom_ax_snapshot",
            "scene_type": "cdp_multisource_node_graph",
            "node_count": len(normalized_nodes),
            "relation_count": len(relations),
            "candidate_count": sum(
                1 for node in normalized_nodes if node["interaction_candidate"]
            ),
            "nodes": normalized_nodes,
            "relations": relations,
            "frames": document_records,
            "actionable_grounding": False,
            "safe_for_execution": False,
            "provenance": {
                "semantic_source": "cdp_dom_accessibility_snapshot",
                "sources": [_DOM_SOURCE, _AX_SOURCE],
                "join_key": ["frame_id", "backend_node_id"],
                "pixel_inference_performed": False,
                "pixel_verified": False,
                "actionable_grounding": False,
                "safe_for_execution": False,
            },
        }


def _unwrap_mapping(value: Mapping[str, Any] | None, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    result = value.get("result")
    if isinstance(result, Mapping):
        return result
    return value


def _ax_nodes(
    value: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        payload = value.get("result")
        if isinstance(payload, Mapping):
            value = payload
        raw_nodes = value.get("nodes", [])
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        raw_nodes = value
    else:
        raise ValueError("AX tree must be an object or list")
    if not isinstance(raw_nodes, list):
        raise ValueError("AX tree nodes must be a list")
    nodes = []
    for index, node in enumerate(raw_nodes):
        if not isinstance(node, Mapping):
            raise ValueError(f"AX tree node {index} must be an object")
        nodes.append(node)
    return nodes


def _string_table(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("DOM snapshot strings must be a list")
    return tuple(str(item) for item in value)


def _decode_string(value: Any, strings: tuple[str, ...]) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return strings[value] if 0 <= value < len(strings) else ""
    return str(value)


def _sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _integer_array(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return [_as_int(item) if _as_int(item) is not None else -1 for item in value]


def _node_count(nodes: Mapping[str, Any]) -> int:
    fields = ("parentIndex", "nodeType", "nodeName", "nodeValue", "backendNodeId")
    return max(
        (len(nodes.get(field, [])) for field in fields if isinstance(nodes.get(field), list)),
        default=0,
    )


def _preflight_snapshot_limits(
    documents: list[Any],
    ax_nodes: list[Mapping[str, Any]],
    strings: tuple[str, ...],
) -> None:
    if len(documents) > MAX_NODE_COUNT:
        raise ValueError(f"CDP DOM snapshot exceeds {MAX_NODE_COUNT} documents")

    dom_node_count = 0
    dom_backend_keys: set[tuple[str, int]] = set()
    backend_frames: dict[int, set[str]] = defaultdict(set)
    document_records: list[dict[str, Any]] = []
    relation_keys: set[tuple[Any, ...]] = set()
    for document_index, document in enumerate(documents):
        if not isinstance(document, Mapping):
            raise ValueError(
                f"DOM snapshot document {document_index} must be an object"
            )
        nodes = document.get("nodes", {})
        if not isinstance(nodes, Mapping):
            raise ValueError(
                f"DOM snapshot document {document_index}.nodes must be an object"
            )
        node_count = _node_count(nodes)
        dom_node_count += node_count
        if dom_node_count > MAX_NODE_COUNT:
            raise ValueError(f"CDP DOM snapshot exceeds {MAX_NODE_COUNT} nodes")

        frame_id = _decode_string(document.get("frameId"), strings)
        document_records.append({"frame_id": frame_id})
        backend_ids = nodes.get("backendNodeId", [])
        if isinstance(backend_ids, list):
            for raw_backend_id in backend_ids[:node_count]:
                backend_node_id = _as_positive_int(raw_backend_id)
                if backend_node_id is None:
                    continue
                dom_backend_keys.add((frame_id, backend_node_id))
                backend_frames[backend_node_id].add(frame_id)

        parent_indexes = nodes.get("parentIndex", [])
        if isinstance(parent_indexes, list):
            for node_index, raw_parent in enumerate(parent_indexes[:node_count]):
                parent_index = _as_int(raw_parent)
                if (
                    parent_index is not None
                    and 0 <= parent_index < node_count
                    and parent_index != node_index
                ):
                    relation_keys.add(
                        ("dom_child", document_index, parent_index, node_index)
                    )
                    _check_relation_limit(relation_keys)

        content_documents = _rare_values(nodes.get("contentDocumentIndex"))
        for node_index, child_document_index in content_documents.items():
            relation_keys.add(
                (
                    "iframe_document",
                    document_index,
                    node_index,
                    _as_int(child_document_index),
                )
            )
            _check_relation_limit(relation_keys)

    if len(ax_nodes) > MAX_NODE_COUNT:
        raise ValueError(f"CDP AX tree exceeds {MAX_NODE_COUNT} nodes")

    # DOM and AX collections each being below their individual limit is not
    # sufficient: disjoint collections can still allocate twice the permitted
    # normalized graph. Resolve AX frames exactly as normalization does, then
    # count only AX identities that cannot merge with an existing DOM identity.
    ax_frames = _resolve_ax_frames(ax_nodes, backend_frames, document_records)
    ax_only_keys: set[tuple[Any, ...]] = set()
    for ax_index, node in enumerate(ax_nodes):
        ax_id = str(node.get("nodeId", f"ax-{ax_index}")).strip()
        frame_id = ax_frames.get(ax_id, "")
        backend_node_id = _as_positive_int(node.get("backendDOMNodeId"))
        if backend_node_id is not None:
            backend_key = (frame_id, backend_node_id)
            if backend_key not in dom_backend_keys:
                ax_only_keys.add(("backend", *backend_key))
        else:
            ax_only_keys.add(("ax", frame_id, ax_id))
        if dom_node_count + len(ax_only_keys) > MAX_NODE_COUNT:
            raise ValueError(
                f"normalized CDP snapshot exceeds {MAX_NODE_COUNT} nodes"
            )

        parent_id = str(node.get("parentId", "")).strip()
        if parent_id and parent_id != ax_id:
            relation_keys.add(("ax_child", parent_id, ax_id))
            _check_relation_limit(relation_keys)
        child_ids = node.get("childIds", [])
        if not isinstance(child_ids, list):
            continue
        for raw_child_id in child_ids:
            child_id = str(raw_child_id).strip()
            if child_id and child_id != ax_id:
                relation_keys.add(("ax_child", ax_id, child_id))
                _check_relation_limit(relation_keys)


def _check_relation_limit(relation_keys: set[tuple[Any, ...]]) -> None:
    if len(relation_keys) > MAX_RELATION_COUNT:
        raise ValueError(f"CDP snapshot exceeds {MAX_RELATION_COUNT} relations")


def _node_attributes(value: Any) -> dict[int, Any]:
    # The current CDP protocol uses ArrayOfStrings[], indexed directly by
    # node. Keep RareStringData support for older clients and simple fixtures.
    if isinstance(value, list):
        return {
            index: item
            for index, item in enumerate(value)
            if isinstance(item, list) and item
        }
    return _rare_values(value)


def _rare_values(value: Any) -> dict[int, Any]:
    if not isinstance(value, Mapping):
        return {}
    indexes = value.get("index", [])
    values = value.get("value", [])
    if not isinstance(indexes, list) or not isinstance(values, list):
        return {}
    result: dict[int, Any] = {}
    for index, item in zip(indexes, values):
        numeric = _as_int(index)
        if numeric is not None and numeric >= 0:
            result[numeric] = item
    return result


def _rare_boolean_indexes(value: Any) -> set[int]:
    if not isinstance(value, Mapping) or not isinstance(value.get("index"), list):
        return set()
    return {
        index
        for raw in value["index"]
        if (index := _as_int(raw)) is not None and index >= 0
    }


def _layout_bounds(value: Any) -> dict[int, list[float]]:
    if not isinstance(value, Mapping):
        return {}
    node_indexes = value.get("nodeIndex", [])
    raw_bounds = value.get("bounds", [])
    if not isinstance(node_indexes, list) or not isinstance(raw_bounds, list):
        return {}
    result: dict[int, list[float]] = {}
    for raw_index, raw_box in zip(node_indexes, raw_bounds):
        node_index = _as_int(raw_index)
        if node_index is None or node_index < 0:
            continue
        box = _box(raw_box)
        if box is not None and node_index not in result:
            result[node_index] = box
    return result


def _box(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    result = []
    for item in value:
        if isinstance(item, bool):
            return None
        try:
            numeric = float(item)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(numeric):
            return None
        result.append(numeric)
    if result[2] < 0 or result[3] < 0:
        return None
    return result


def _center(bounds: list[float] | None) -> list[float] | None:
    if bounds is None:
        return None
    return [
        round(bounds[0] + bounds[2] / 2, 3),
        round(bounds[1] + bounds[3] / 2, 3),
    ]


def _decode_attributes(value: Any, strings: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(value, list):
        return {}
    result: dict[str, str] = {}
    for offset in range(0, len(value) - 1, 2):
        name = _decode_string(value[offset], strings).strip()
        if not name:
            continue
        result[name] = _decode_string(value[offset + 1], strings)
    return result


def _dom_name(attributes: Mapping[str, str], node_value: str) -> str:
    for key in ("aria-label", "alt", "title"):
        value = str(attributes.get(key, "")).strip()
        if value:
            return value
    return node_value.strip()


def _dom_disabled(attributes: Mapping[str, str]) -> bool:
    return "disabled" in attributes or str(attributes.get("aria-disabled", "")).casefold() == "true"


def _dom_focusable(
    dom_node_name: str, attributes: Mapping[str, str], disabled: bool
) -> bool:
    if disabled:
        return False
    raw_tabindex = attributes.get("tabindex")
    if raw_tabindex is not None:
        try:
            return int(raw_tabindex) >= 0
        except (TypeError, ValueError):
            return False
    tag = dom_node_name.casefold()
    return tag in _NATIVE_FOCUSABLE_TAGS or (
        tag == "a" and bool(str(attributes.get("href", "")).strip())
    )


def _resolve_ax_frames(
    ax_nodes: list[Mapping[str, Any]],
    backend_frames: Mapping[int, set[str]],
    documents: list[dict[str, Any]],
) -> dict[str, str]:
    raw_by_id: dict[str, Mapping[str, Any]] = {}
    frames: dict[str, str] = {}
    for index, node in enumerate(ax_nodes):
        ax_id = str(node.get("nodeId", f"ax-{index}")).strip()
        raw_by_id[ax_id] = node
        explicit = str(node.get("frameId", "")).strip()
        if explicit:
            frames[ax_id] = explicit
            continue
        backend_node_id = _as_positive_int(node.get("backendDOMNodeId"))
        candidates = backend_frames.get(backend_node_id, set()) if backend_node_id else set()
        if len(candidates) == 1:
            frames[ax_id] = next(iter(candidates))

    changed = True
    while changed:
        changed = False
        for ax_id, node in raw_by_id.items():
            if ax_id in frames:
                continue
            parent_id = str(node.get("parentId", "")).strip()
            if parent_id in frames:
                frames[ax_id] = frames[parent_id]
                changed = True

    known_document_frames = {
        str(item.get("frame_id", "")) for item in documents if item.get("frame_id")
    }
    default_frame = next(iter(known_document_frames)) if len(known_document_frames) == 1 else ""
    for ax_id in raw_by_id:
        frames.setdefault(ax_id, default_frame)
    return frames


def _merge_ax_node(
    node: dict[str, Any], raw_ax_node: Mapping[str, Any], ax_id: str
) -> None:
    properties = _ax_properties(raw_ax_node.get("properties", []))
    role = _text_ax_value(raw_ax_node.get("role"))
    name = _text_ax_value(raw_ax_node.get("name"))
    description = _text_ax_value(raw_ax_node.get("description"))
    value = _text_ax_value(raw_ax_node.get("value"))
    if role:
        node["role"] = role
        node["_field_sources"]["role"] = _AX_SOURCE
    if name:
        node["name"] = name
        node["_field_sources"]["name"] = _AX_SOURCE
    if description:
        node["description"] = description
        node["_field_sources"]["description"] = _AX_SOURCE
    if value:
        node["value"] = value
        node["_field_sources"]["value"] = _AX_SOURCE
    if "disabled" in properties:
        current_disabled = bool(node.get("disabled", False))
        ax_disabled = _bool_value(properties["disabled"])
        node["disabled"] = current_disabled or ax_disabled
        if ax_disabled and not current_disabled:
            node["_field_sources"]["disabled"] = _AX_SOURCE
        elif "disabled" not in node["_field_sources"]:
            node["_field_sources"]["disabled"] = _AX_SOURCE
    if "focusable" in properties:
        node["focusable"] = _bool_value(properties["focusable"])
        node["_field_sources"]["focusable"] = _AX_SOURCE
    if "clickable" in properties and not node["_field_sources"].get("is_clickable"):
        node["is_clickable"] = _bool_value(properties["clickable"])
        node["_field_sources"]["is_clickable"] = _AX_SOURCE
    node["ignored"] = bool(raw_ax_node.get("ignored", False))
    node["_field_sources"]["ignored"] = _AX_SOURCE
    node["_source_records"].append(
        {
            "source": _AX_SOURCE,
            "frame_id": node["frame_id"],
            "backend_node_id": node["backend_node_id"],
            "ax_node_id": ax_id,
        }
    )


def _ax_properties(value: Any) -> dict[str, Any]:
    if not isinstance(value, list):
        return {}
    result: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name", "")).strip()
        if name:
            result[name] = _ax_value(item.get("value"))
    return result


def _ax_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get("value")
    return value


def _text_ax_value(value: Any) -> str:
    raw = _ax_value(value)
    return "" if raw is None else str(raw).strip()


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _add_relation(
    relations: list[dict[str, Any]],
    keys: set[tuple[str, str, str]],
    nodes_by_id: Mapping[str, dict[str, Any]],
    *,
    source: str,
    target: str,
    relation_type: str,
    provenance: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    key = (relation_type, source, target)
    if key in keys or source not in nodes_by_id or target not in nodes_by_id:
        return
    if len(relations) >= MAX_RELATION_COUNT:
        raise ValueError(f"normalized CDP snapshot exceeds {MAX_RELATION_COUNT} relations")
    keys.add(key)
    relation = {
        "type": relation_type,
        "source": source,
        "target": target,
        "provenance": {"source": provenance},
    }
    if details:
        relation["details"] = dict(details)
    relations.append(relation)
    parent = nodes_by_id[source]
    child = nodes_by_id[target]
    if target not in parent["_children"]:
        parent["_children"].append(target)
    link = {"node_id": source, "relation": relation_type, "source": provenance}
    if link not in child["_parent_links"]:
        child["_parent_links"].append(link)


def _finalize_node(node: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in node.items() if not key.startswith("_")}
    parent_links = sorted(
        node["_parent_links"],
        key=lambda item: (
            {"iframe_document": 0, "shadow_root": 1, "dom_child": 2, "ax_child": 3}.get(
                item["relation"], 9
            ),
            item["node_id"],
        ),
    )
    parent_ids = list(dict.fromkeys(item["node_id"] for item in parent_links))
    result["parent_id"] = parent_links[0]["node_id"] if parent_links else None
    result["parent_relation"] = parent_links[0]["relation"] if parent_links else None
    result["parent_ids"] = parent_ids
    result["parent_links"] = parent_links
    result["children_ids"] = list(node["_children"])

    role = str(result.get("role", "")).casefold()
    semantic_interaction_hint = bool(
        not result["ignored"]
        and not result["disabled"]
        and (
            result["is_clickable"]
            or result["focusable"]
            or role in _INTERACTIVE_ROLES
        )
    )
    has_dom_snapshot_record = any(
        record.get("source") == _DOM_SOURCE for record in node["_source_records"]
    )
    backend_node_id = _as_positive_int(result.get("backend_node_id"))
    candidate = bool(
        semantic_interaction_hint
        and has_dom_snapshot_record
        and backend_node_id is not None
    )
    result["semantic_interaction_hint"] = semantic_interaction_hint
    result["interaction_candidate"] = candidate
    result["interaction_eligible"] = False
    result["actionable_grounding"] = False
    result["interaction"] = {
        "status": "candidate_only" if candidate else "analysis_only",
        "candidate": candidate,
        "can_click_now": False,
        "preflight_required": candidate,
        "safe_for_execution": False,
        "target_type": "cdp_backend_node_candidate" if candidate else "none",
        "target_ref": (
            {
                "frame_id": result["frame_id"],
                "backend_node_id": result["backend_node_id"],
            }
            if candidate and result["backend_node_id"] is not None
            else None
        ),
        "reason_codes": (
            ["cdp_snapshot_candidate_requires_live_revalidation"]
            if candidate
            else [
                "cdp_snapshot_semantic_hint_not_bindable"
                if semantic_interaction_hint
                else "cdp_snapshot_analysis_only"
            ]
        ),
    }
    source_names = list(
        dict.fromkeys(record["source"] for record in node["_source_records"])
    )
    semantic_source = (
        "cdp_dom_accessibility"
        if len(source_names) > 1
        else "cdp_dom_snapshot"
        if source_names == [_DOM_SOURCE]
        else "cdp_accessibility"
    )
    result["source"] = {
        "kind": "cdp",
        "semantic_source": semantic_source,
        "methods": source_names,
        "frame_id": result["frame_id"],
        "backend_node_id": result["backend_node_id"],
        "trust": "browser_observed",
    }
    result["provenance"] = {
        "sources": node["_source_records"],
        "field_sources": dict(sorted(node["_field_sources"].items())),
        "join_key": {
            "frame_id": result["frame_id"],
            "backend_node_id": result["backend_node_id"],
        },
        "actionable_grounding": False,
        "safe_for_execution": False,
    }
    return result


def _dom_key(
    frame_id: str,
    backend_node_id: int | None,
    document_index: int,
    node_index: int,
) -> tuple[Any, ...]:
    if backend_node_id is not None:
        return ("backend", frame_id, backend_node_id)
    return ("dom", document_index, node_index)


def _ax_key(
    frame_id: str, backend_node_id: int | None, ax_node_id: str
) -> tuple[Any, ...]:
    if backend_node_id is not None:
        return ("backend", frame_id, backend_node_id)
    return ("ax", frame_id, ax_node_id)


def _node_id(
    frame_id: str,
    backend_node_id: int | None,
    *,
    document_index: int | None = None,
    node_index: int | None = None,
    ax_node_id: str | None = None,
) -> str:
    frame = quote(frame_id or "unknown", safe="")
    if backend_node_id is not None:
        return f"cdp:{frame}:{backend_node_id}"
    if ax_node_id is not None:
        return f"cdp:{frame}:ax:{quote(ax_node_id, safe='')}"
    return f"cdp:{frame}:dom:{document_index}:{node_index}"


def _at(values: list[Any], index: int) -> Any:
    return values[index] if 0 <= index < len(values) else None


def _integer_at(values: list[int], index: int, *, default: int) -> int:
    value = _at(values, index)
    return value if isinstance(value, int) else default


def _positive_integer_at(values: list[int], index: int) -> int | None:
    value = _at(values, index)
    return value if isinstance(value, int) and value > 0 else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_positive_int(value: Any) -> int | None:
    result = _as_int(value)
    return result if result is not None and result > 0 else None


__all__ = [
    "CDPSnapshotNormalizer",
    "MAX_NODE_COUNT",
    "MAX_RELATION_COUNT",
    "SCHEMA_VERSION",
    "normalize_cdp_snapshot",
]
