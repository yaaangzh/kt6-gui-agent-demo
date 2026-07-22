from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .perception_runtime import PerceptionRuntime
from .vision_recognition import CanvasFrame, CanvasVisionAdapter


class SQLitePageCaptureStore:
    def __init__(self, db_path: Path, asset_dir: Path):
        self.db_path = db_path
        self.asset_dir = asset_dir
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _init_db(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS page_captures (
                  capture_id TEXT PRIMARY KEY,
                  url TEXT NOT NULL,
                  title TEXT NOT NULL,
                  scene_key TEXT NOT NULL,
                  scene_revision INTEGER NOT NULL,
                  template_hash TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  selected_mode TEXT NOT NULL,
                  capture_json TEXT NOT NULL,
                  result_json TEXT NOT NULL,
                  summary_json TEXT NOT NULL,
                  created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_page_captures_created
                  ON page_captures (created_at DESC);
                """
            )
            connection.commit()

    def save(self, record: dict[str, Any]) -> None:
        meta = record["result"]["meta"]
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO page_captures (
                  capture_id, url, title, scene_key, scene_revision,
                  template_hash, content_hash, selected_mode,
                  capture_json, result_json, summary_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["capture_id"],
                    record["capture"]["page"]["url"],
                    record["capture"]["page"]["title"],
                    meta["scene_key"],
                    meta["scene_revision"],
                    meta["template_hash"],
                    meta["content_hash"],
                    record["result"]["perception"]["scene"]["mode"],
                    self._json(record["capture"]),
                    self._json(record["result"]),
                    self._json(record["summary"]),
                    record["created_at"],
                ),
            )
            connection.commit()

    def get(self, capture_id: str) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT capture_id, capture_json, result_json, summary_json, created_at
                FROM page_captures
                WHERE capture_id = ?
                """,
                (capture_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "capture_id": row["capture_id"],
            "capture": json.loads(row["capture_json"]),
            "result": json.loads(row["result_json"]),
            "summary": json.loads(row["summary_json"]),
            "created_at": row["created_at"],
        }

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT capture_id, url, title, scene_key, scene_revision,
                       selected_mode, summary_json, created_at
                FROM page_captures
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "capture_id": row["capture_id"],
                "url": row["url"],
                "title": row["title"],
                "scene_key": row["scene_key"],
                "scene_revision": row["scene_revision"],
                "selected_mode": row["selected_mode"],
                "summary": json.loads(row["summary_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)


class PagePerceptionService:
    MAX_DOM_ELEMENTS = 1000
    MAX_CANVASES = 4
    MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
    MAX_TOPOLOGY_TEXT_CHARS = 100_000
    MIN_ACTIONABLE_VISION_CONFIDENCE = 0.8
    MAX_RECOGNIZED_RELATIONS = 4_000
    TOPOLOGY_TEXT_KINDS = frozenset({"user_provided_ascii", "external_ocr_transcript"})
    DATA_URL_PATTERN = re.compile(r"^data:image/(png|jpeg|webp);base64,(.+)$", re.DOTALL)

    def __init__(
        self,
        store: SQLitePageCaptureStore,
        perception_runtime: PerceptionRuntime,
        canvas_vision: CanvasVisionAdapter | None = None,
        text_recognizer: Any | None = None,
    ):
        self.store = store
        self.perception_runtime = perception_runtime
        self.canvas_vision = canvas_vision
        self.text_recognizer = text_recognizer

    def ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        capture_id = f"capture_{uuid.uuid4().hex[:12]}"
        page = self._normalize_page(payload.get("page", {}))
        dom = self._normalize_dom(payload.get("dom", {}))
        canvases = self._normalize_canvases(capture_id, payload.get("canvases", []))
        adapter_scene = self._normalize_adapter_scene(payload.get("adapter_scene"))
        topology_text = self._normalize_topology_text(payload.get("topology_text"))
        capture = {
            "page": page,
            "dom": dom,
            "canvases": canvases,
            "adapter_scene": adapter_scene,
            "topology_text": topology_text,
            "captured_at": payload.get("captured_at", time.time()),
        }

        dom_scene = self._dom_scene(dom, page)
        canvas_scene = self._canvas_scene(canvases, adapter_scene, page)
        text_scene = self._text_scene(topology_text, page, canvases)
        selected = self._select_scene(dom_scene, canvas_scene, text_scene)
        perception = {
            "strategy": "live_page_hybrid",
            "selected_mode": selected["mode"],
            "raw_scenes": {
                "dom": dom_scene["input"],
                "canvas": canvas_scene["input"],
                "text": text_scene["input"],
            },
            "candidates": {
                "dom": dom_scene,
                "canvas": canvas_scene,
                "text": text_scene,
            },
            "scene": selected,
            "business_object_bindings": selected["business_object_bindings"],
            "decision": self._decision(dom_scene, canvas_scene, text_scene, selected),
        }

        template_hash = self._template_hash(capture, selected)
        content_hash = self._content_hash(capture, selected)
        scene_key = self._scene_key(page, template_hash)
        result = self.perception_runtime.register_external(
            scene_key=scene_key,
            template_hash=template_hash,
            content_hash=content_hash,
            perception=perception,
            source="live_page_capture",
            source_revision=content_hash[:12],
        )
        summary = {
            "dom_element_count": len(dom["elements"]),
            "canvas_count": len(canvases),
            "canvas_screenshot_count": sum(1 for canvas in canvases if canvas.get("screenshot_path")),
            "adapter_scene_available": adapter_scene is not None,
            "topology_text_available": topology_text is not None,
            "selected_mode": result["perception"]["scene"]["mode"],
            "requires_vision_model": result["perception"]["scene"].get("requires_vision_model", False),
            "semantic_source": result["perception"]["scene"].get("provenance", {}).get(
                "semantic_source", "unknown"
            ),
        }
        record = {
            "capture_id": capture_id,
            "capture": capture,
            "result": result,
            "summary": summary,
            "created_at": time.time(),
        }
        self.store.save(record)
        return self._public_record(record)

    def get_capture(self, capture_id: str) -> dict[str, Any] | None:
        record = self.store.get(capture_id)
        return self._public_record(record) if record else None

    def list_captures(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.list(limit=limit)

    def get_result(self, capture_id: str) -> dict[str, Any] | None:
        record = self.store.get(capture_id)
        return copy.deepcopy(record["result"]) if record else None

    def get_topology(self, capture_id: str) -> dict[str, Any] | None:
        record = self.store.get(capture_id)
        if not record:
            return None
        capture = record["capture"]
        result = record["result"]
        adapter_scene = capture.get("adapter_scene")
        if adapter_scene:
            topology = copy.deepcopy(adapter_scene)
        else:
            topology = self._topology_from_scene(result["perception"]["scene"], capture)

        topology["page_capture"] = {
            "capture_id": capture_id,
            "url": capture["page"]["url"],
            "title": capture["page"]["title"],
            **record["summary"],
        }
        perception = result["perception"]
        topology["raw_scenes"] = perception["raw_scenes"]
        topology["ui_perception_candidates"] = perception["candidates"]
        topology["ui_perception"] = perception["scene"]
        topology["perception_decision"] = perception["decision"]
        topology["perception_meta"] = result["meta"]
        topology["topology_changes"] = result["changes"]
        return topology

    def _public_record(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "capture_id": record["capture_id"],
            "page": copy.deepcopy(record["capture"]["page"]),
            "summary": copy.deepcopy(record["summary"]),
            "perception_meta": copy.deepcopy(record["result"]["meta"]),
            "topology_changes": copy.deepcopy(record["result"]["changes"]),
            "scene": copy.deepcopy(record["result"]["perception"]["scene"]),
            "created_at": record["created_at"],
        }

    def _normalize_page(self, page: dict[str, Any]) -> dict[str, Any]:
        url = str(page.get("url", "")).strip()
        if not url:
            raise ValueError("page.url is required")
        viewport = page.get("viewport", {})
        return {
            "url": url[:2048],
            "title": str(page.get("title", ""))[:300],
            "language": str(page.get("language", ""))[:30],
            "ui_version": str(page.get("ui_version", "unknown"))[:100],
            "viewport": {
                "width": int(viewport.get("width", 0)),
                "height": int(viewport.get("height", 0)),
                "device_pixel_ratio": float(viewport.get("device_pixel_ratio", 1)),
            },
        }

    def _normalize_dom(self, dom: dict[str, Any]) -> dict[str, Any]:
        elements = []
        for index, item in enumerate(list(dom.get("elements", []))[: self.MAX_DOM_ELEMENTS]):
            bbox = item.get("bbox", [])
            if len(bbox) != 4:
                continue
            try:
                depth = max(0, int(item.get("depth", 0)))
            except (TypeError, ValueError):
                depth = 0
            try:
                document_order = max(0, int(item.get("document_order", index)))
            except (TypeError, ValueError):
                document_order = index
            elements.append(
                {
                    "ref": str(item.get("ref") or "")[:500],
                    "parent_ref": str(item.get("parent_ref") or "")[:500],
                    "depth": depth,
                    "document_order": document_order,
                    "tag": str(item.get("tag", ""))[:50].lower(),
                    "role": str(item.get("role", ""))[:100],
                    "label": str(item.get("label", ""))[:300],
                    "aria_label": str(item.get("aria_label", ""))[:300],
                    "placeholder": str(item.get("placeholder", ""))[:300],
                    "business_id": str(item.get("business_id", ""))[:200],
                    "business_type": str(item.get("business_type", ""))[:100],
                    "bbox": [round(float(value), 2) for value in bbox],
                    "disabled": bool(item.get("disabled", False)),
                    "checked": bool(item.get("checked", False)),
                }
            )
        return {"elements": elements}

    def _normalize_canvases(self, capture_id: str, canvases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = []
        for index, item in enumerate(list(canvases)[: self.MAX_CANVASES]):
            canvas = {
                "canvas_id": str(item.get("canvas_id", f"canvas_{index}"))[:200],
                "width": int(item.get("width", 0)),
                "height": int(item.get("height", 0)),
                "client_width": round(float(item.get("client_width", 0)), 2),
                "client_height": round(float(item.get("client_height", 0)), 2),
                "bbox": [round(float(value), 2) for value in item.get("bbox", [0, 0, 0, 0])],
            }
            capture_error = str(item.get("capture_error", "")).strip()
            if capture_error:
                canvas["capture_error"] = capture_error[:500]
            data_url = item.get("data_url")
            if data_url:
                try:
                    screenshot = self._store_screenshot(capture_id, index, str(data_url))
                    canvas.update(screenshot)
                except ValueError as exc:
                    canvas["capture_error"] = str(exc)
            normalized.append(canvas)
        return normalized

    def _store_screenshot(self, capture_id: str, index: int, data_url: str) -> dict[str, Any]:
        match = self.DATA_URL_PATTERN.match(data_url)
        if not match:
            raise ValueError("unsupported canvas data URL")
        extension = "jpg" if match.group(1) == "jpeg" else match.group(1)
        try:
            raw = base64.b64decode(match.group(2), validate=True)
        except ValueError as exc:
            raise ValueError("invalid canvas screenshot encoding") from exc
        if len(raw) > self.MAX_SCREENSHOT_BYTES:
            raise ValueError("canvas screenshot exceeds 5 MB")
        digest = hashlib.sha256(raw).hexdigest()
        path = self.store.asset_dir / f"{capture_id}-canvas-{index}.{extension}"
        path.write_bytes(raw)
        return {
            "screenshot_path": str(path.resolve()),
            "screenshot_sha256": digest,
            "screenshot_bytes": len(raw),
            "mime_type": f"image/{match.group(1)}",
        }

    def _normalize_adapter_scene(self, scene: Any) -> dict[str, Any] | None:
        if not isinstance(scene, dict) or not scene.get("objects"):
            return None
        return {
            "ui_version": scene.get("ui_version", "unknown"),
            "topology_revision": scene.get("topology_revision"),
            "site": scene.get("site", "unknown"),
            "floor": scene.get("floor", "unknown"),
            "scene": scene.get("scene", "live canvas scene"),
            "canvas": copy.deepcopy(scene.get("canvas", {"width": 0, "height": 0})),
            "objects": copy.deepcopy(scene.get("objects", [])),
            "links": copy.deepcopy(scene.get("links", [])),
            "co_channel_relations": copy.deepcopy(scene.get("co_channel_relations", [])),
            "visual_grounding": copy.deepcopy(scene.get("visual_grounding", {})),
            "view_transform": copy.deepcopy(scene.get("view_transform", {})),
        }

    def _normalize_topology_text(self, observation: Any) -> dict[str, str] | None:
        if observation is None:
            return None
        if not isinstance(observation, dict):
            raise ValueError("topology_text must be an object")
        allowed_fields = {"kind", "format", "source_id", "text"}
        unknown_fields = set(observation) - allowed_fields
        if unknown_fields:
            raise ValueError(f"unsupported topology_text fields: {', '.join(sorted(unknown_fields))}")

        kind = str(observation.get("kind", "")).strip()
        if kind not in self.TOPOLOGY_TEXT_KINDS:
            supported = ", ".join(sorted(self.TOPOLOGY_TEXT_KINDS))
            raise ValueError(f"topology_text.kind must be one of: {supported}")
        text_format = str(observation.get("format", "")).strip()
        source_id = str(observation.get("source_id", "")).strip()
        text = observation.get("text")
        if not text_format:
            raise ValueError("topology_text.format is required")
        if not source_id:
            raise ValueError("topology_text.source_id is required")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("topology_text.text is required")
        if len(text) > self.MAX_TOPOLOGY_TEXT_CHARS:
            raise ValueError("topology_text.text exceeds 100000 characters")
        return {
            "kind": kind,
            "format": text_format[:100],
            "source_id": source_id[:200],
            "text": text,
        }

    def _dom_scene(self, dom: dict[str, Any], page: dict[str, Any]) -> dict[str, Any]:
        elements = []
        bindings = {}
        for index, item in enumerate(dom["elements"], start=1):
            x, y, width, height = item["bbox"]
            business_id = item.get("business_id") or None
            element_id = f"live_dom_{index:04d}"
            confidence = 0.99 if business_id else 0.85
            element = {
                "element_id": element_id,
                "business_id": business_id,
                "type": item.get("business_type") or item.get("role") or item.get("tag") or "element",
                "label": item.get("aria_label") or item.get("label") or item.get("placeholder") or element_id,
                "selector": item.get("ref"),
                "parent_ref": item.get("parent_ref", ""),
                "depth": item.get("depth", 0),
                "document_order": item.get("document_order", index - 1),
                "bbox": item["bbox"],
                "center": [round(x + width / 2, 2), round(y + height / 2, 2)],
                "attributes": {
                    "tag": item.get("tag"),
                    "role": item.get("role"),
                    "disabled": item.get("disabled"),
                    "checked": item.get("checked"),
                },
                "confidence": confidence,
            }
            elements.append(element)
            if business_id:
                bindings[business_id] = {
                    "element_id": element_id,
                    "dom_ref": item.get("ref"),
                    "confidence": confidence,
                    "method": "live_dom_snapshot",
                }
        ui_tree = self._dom_ui_tree(elements)
        scene = {
            "mode": "live_dom_snapshot",
            "input": {
                "source": "live_browser_dom",
                "url": page["url"],
                "element_count": len(elements),
            },
            "scene_type": "live_dom_page",
            "object_count": len(elements),
            "elements": elements,
            "ui_tree": ui_tree,
            "business_object_bindings": bindings,
            "relations": [],
            "co_channel_relations": [],
            "relation_count": 0,
            "requires_vision_model": False,
            "limitations": [
                "DOM 元素来自浏览器实时采集",
                "Canvas 内部像素节点不属于 DOM 元素",
            ],
        }
        return self._stamp_provenance(
            scene,
            semantic_source="browser_dom",
            pixel_inference_performed=False,
            pixel_verified=False,
            actionable_grounding=bool(bindings),
        )

    def _dom_ui_tree(self, elements: list[dict[str, Any]]) -> dict[str, Any]:
        ordered = sorted(
            enumerate(elements),
            key=lambda entry: (
                int(entry[1].get("document_order", entry[0])),
                entry[0],
            ),
        )
        nodes: dict[str, dict[str, Any]] = {}
        source_ref_index: dict[str, str] = {}
        ambiguous_source_refs: set[str] = set()
        node_refs: dict[str, str] = {}
        issues: list[dict[str, Any]] = []

        def generated_ref(element_id: str) -> str:
            base = f"@dom:{element_id}"
            candidate = base
            suffix = 2
            while candidate in nodes:
                candidate = f"{base}:{suffix}"
                suffix += 1
            return candidate

        for fallback_order, element in ordered:
            element_id = str(element.get("element_id", f"live_dom_{fallback_order + 1:04d}"))
            source_ref = str(element.get("selector", "")).strip()
            document_order = int(element.get("document_order", fallback_order))
            if not source_ref:
                node_ref = generated_ref(element_id)
                issues.append(
                    {
                        "code": "dom_ref_missing",
                        "element_id": element_id,
                        "generated_ref": node_ref,
                        "document_order": document_order,
                    }
                )
            elif source_ref in source_ref_index:
                node_ref = generated_ref(element_id)
                ambiguous_source_refs.add(source_ref)
                issues.append(
                    {
                        "code": "dom_ref_duplicate",
                        "ref": source_ref,
                        "element_id": element_id,
                        "generated_ref": node_ref,
                        "document_order": document_order,
                    }
                )
            else:
                node_ref = source_ref
                source_ref_index[source_ref] = node_ref
            node_refs[element_id] = node_ref
            nodes[node_ref] = {
                "element_id": element_id,
                "role": element.get("type", "element"),
                "name": element.get("label", element_id),
                "tag": element.get("attributes", {}).get("tag", ""),
                "parent_ref": str(element.get("parent_ref", "")).strip(),
                "depth": int(element.get("depth", 0)),
                "document_order": document_order,
                "children": [],
            }

        effective_parent: dict[str, str | None] = {}
        for _, element in ordered:
            element_id = str(element.get("element_id", ""))
            node_ref = node_refs[element_id]
            parent_ref = str(element.get("parent_ref", "")).strip()
            if not parent_ref:
                effective_parent[node_ref] = None
                continue
            if parent_ref in ambiguous_source_refs:
                effective_parent[node_ref] = None
                issues.append(
                    {
                        "code": "dom_parent_ambiguous",
                        "ref": node_ref,
                        "parent_ref": parent_ref,
                        "element_id": element_id,
                    }
                )
                continue
            parent_node_ref = source_ref_index.get(parent_ref)
            if parent_node_ref is None:
                effective_parent[node_ref] = None
                issues.append(
                    {
                        "code": "dom_parent_unknown",
                        "ref": node_ref,
                        "parent_ref": parent_ref,
                        "element_id": element_id,
                    }
                )
                continue
            if parent_node_ref == node_ref:
                effective_parent[node_ref] = None
                issues.append(
                    {
                        "code": "dom_parent_cycle",
                        "ref": node_ref,
                        "parent_ref": parent_ref,
                        "element_id": element_id,
                    }
                )
                continue
            effective_parent[node_ref] = parent_node_ref

        processed: set[str] = set()
        for start_ref in nodes:
            if start_ref in processed:
                continue
            path: list[str] = []
            positions: dict[str, int] = {}
            current: str | None = start_ref
            while current is not None and current not in processed:
                if current in positions:
                    cycle = path[positions[current] :]
                    break_ref = min(
                        cycle,
                        key=lambda ref: (nodes[ref]["document_order"], ref),
                    )
                    effective_parent[break_ref] = None
                    issues.append(
                        {
                            "code": "dom_parent_cycle",
                            "ref": break_ref,
                            "cycle_refs": cycle,
                        }
                    )
                    break
                positions[current] = len(path)
                path.append(current)
                current = effective_parent.get(current)
            processed.update(path)

        roots: list[str] = []
        for node_ref in nodes:
            parent_node_ref = effective_parent.get(node_ref)
            if parent_node_ref is None:
                roots.append(node_ref)
            else:
                nodes[parent_node_ref]["children"].append(node_ref)

        order_key = lambda ref: (nodes[ref]["document_order"], ref)
        roots.sort(key=order_key)
        for node in nodes.values():
            node["children"].sort(key=order_key)

        visited: set[str] = set()
        pending = list(reversed(roots))
        while pending:
            node_ref = pending.pop()
            if node_ref in visited:
                continue
            visited.add(node_ref)
            pending.extend(reversed(nodes[node_ref]["children"]))
        missing_from_forest = sorted(set(nodes) - visited, key=order_key)
        if missing_from_forest:
            issues.append(
                {
                    "code": "dom_hierarchy_unreachable",
                    "refs": missing_from_forest,
                }
            )

        return {
            "tree_type": "browser_dom_hierarchy",
            "roots": roots,
            "nodes": nodes,
            "complete": len(nodes) == len(elements) and len(visited) == len(nodes) and not issues,
            "issues": issues,
        }

    def _stamp_provenance(
        self,
        scene: dict[str, Any],
        *,
        semantic_source: str,
        pixel_inference_performed: bool,
        pixel_verified: bool,
        actionable_grounding: bool,
        **details: Any,
    ) -> dict[str, Any]:
        provenance = {
            "semantic_source": semantic_source,
            "pixel_inference_performed": pixel_inference_performed,
            "pixel_verified": pixel_verified,
            "actionable_grounding": actionable_grounding,
            **details,
        }
        scene["provenance"] = provenance
        scene["pixel_inference_performed"] = pixel_inference_performed
        scene["pixel_verified"] = pixel_verified
        scene["actionable_grounding"] = actionable_grounding
        return scene

    def _canvas_scene(
        self,
        canvases: list[dict[str, Any]],
        adapter_scene: dict[str, Any] | None,
        page: dict[str, Any],
    ) -> dict[str, Any]:
        input_payload = {
            "source": "live_browser_canvas",
            "url": page["url"],
            "canvases": copy.deepcopy(canvases),
        }
        pixel_capture_available = any(canvas.get("screenshot_path") for canvas in canvases)
        if adapter_scene:
            return self._renderer_scene(adapter_scene, input_payload, pixel_capture_available)

        vision_error = None
        frames = self._canvas_frames(canvases)
        if frames and self.canvas_vision:
            try:
                adapter_id = str(self.canvas_vision.adapter_id).strip()
                adapter_version = str(self.canvas_vision.adapter_version).strip()
                if not adapter_id or not adapter_version:
                    raise ValueError("CanvasVisionAdapter id and version are required")
                recognized = self.canvas_vision.recognize(
                    page=copy.deepcopy(page),
                    frames=frames,
                )
                vision_scene = self._vision_scene(
                    recognized,
                    input_payload=input_payload,
                    frames=frames,
                    adapter_id=adapter_id,
                    adapter_version=adapter_version,
                    adapter_supports_actions=bool(
                        getattr(self.canvas_vision, "supports_actionable_grounding", False)
                    ),
                )
                if vision_scene:
                    return vision_scene
            except Exception as exc:  # An optional adapter must not break page capture.
                vision_error = f"{type(exc).__name__}: {exc}"[:500]

        if pixel_capture_available:
            limitations = [
                "已捕获真实 Canvas 像素，但尚未获得可用的像素语义识别结果",
                "需要 OCR、目标检测或多模态视觉模型识别节点和关系",
            ]
        elif canvases:
            limitations = [
                "检测到 Canvas，但本次像素截图不可用",
                "没有可供视觉模型识别节点和关系的截图输入",
            ]
        else:
            limitations = [
                "页面未提供 Canvas 像素或语义适配器",
            ]
        if vision_error:
            limitations.append("Canvas 视觉适配器执行失败，已安全回退到未识别截图候选")
        scene = {
            "mode": "canvas_screenshot_capture" if pixel_capture_available else "canvas_capture_unavailable",
            "input": input_payload,
            "scene_type": (
                "unrecognized_canvas_capture" if pixel_capture_available else "unavailable_canvas_capture"
            ),
            "object_count": 0,
            "elements": [],
            "business_object_bindings": {},
            "relations": [],
            "co_channel_relations": [],
            "relation_count": 0,
            "pixel_capture_available": pixel_capture_available,
            "requires_vision_model": pixel_capture_available,
            "limitations": limitations,
        }
        if vision_error:
            scene["vision_error"] = vision_error
        return self._stamp_provenance(
            scene,
            semantic_source="unrecognized_canvas_pixels" if pixel_capture_available else "none",
            pixel_inference_performed=False,
            pixel_verified=False,
            actionable_grounding=False,
        )

    def _renderer_scene(
        self,
        adapter_scene: dict[str, Any],
        input_payload: dict[str, Any],
        pixel_capture_available: bool,
    ) -> dict[str, Any]:
        elements = []
        bindings = {}
        for index, obj in enumerate(adapter_scene.get("objects", []), start=1):
            business_id = str(obj.get("business_id", ""))
            if not business_id:
                continue
            width = float(obj.get("width", 60))
            height = float(obj.get("height", 60))
            x = float(obj.get("x", 0))
            y = float(obj.get("y", 0))
            element_id = f"live_canvas_{index:04d}"
            elements.append(
                {
                    "element_id": element_id,
                    "business_id": business_id,
                    "type": obj.get("type", "object"),
                    "label": obj.get("label", business_id),
                    "bbox": [x - width / 2, y - height / 2, width, height],
                    "center": [x, y],
                    "attributes": {
                        key: value
                        for key, value in obj.items()
                        if key not in {"business_id", "type", "label", "x", "y", "width", "height"}
                    },
                    "confidence": 1.0,
                }
            )
            bindings[business_id] = {
                "element_id": element_id,
                "canvas_ref": f"renderer:{business_id}",
                "confidence": 1.0,
                "method": "canvas_renderer_adapter",
            }
        scene = {
            "mode": "canvas_renderer_adapter",
            "input": input_payload,
            "scene_type": "live_canvas_topology",
            "object_count": len(elements),
            "elements": elements,
            "business_object_bindings": bindings,
            "relations": copy.deepcopy(adapter_scene.get("links", [])),
            "co_channel_relations": copy.deepcopy(adapter_scene.get("co_channel_relations", [])),
            "relation_count": len(adapter_scene.get("links", [])),
            "coordinate_space": {
                "type": "topology_world",
                "width": adapter_scene.get("canvas", {}).get("width", 0),
                "height": adapter_scene.get("canvas", {}).get("height", 0),
                "view_transform": copy.deepcopy(adapter_scene.get("view_transform", {})),
            },
            "pixel_capture_available": pixel_capture_available,
            "requires_vision_model": False,
            "limitations": (
                [
                    "Canvas 像素来自浏览器实时截图",
                    "节点语义来自页面渲染器适配器，不是视觉模型推断",
                ]
                if pixel_capture_available
                else [
                    "本次 Canvas 像素截图不可用",
                    "节点语义来自页面渲染器适配器，不是视觉模型推断",
                ]
            ),
        }
        return self._stamp_provenance(
            scene,
            semantic_source="canvas_renderer_adapter",
            pixel_inference_performed=False,
            pixel_verified=False,
            actionable_grounding=bool(bindings),
        )

    def _canvas_frames(self, canvases: list[dict[str, Any]]) -> tuple[CanvasFrame, ...]:
        frames = []
        for canvas in canvases:
            screenshot_path = canvas.get("screenshot_path")
            if not screenshot_path:
                continue
            bbox = list(canvas.get("bbox", []))
            if len(bbox) != 4:
                bbox = [0.0, 0.0, 0.0, 0.0]
            frames.append(
                CanvasFrame(
                    canvas_id=str(canvas.get("canvas_id", "")),
                    screenshot_path=Path(str(screenshot_path)),
                    screenshot_sha256=str(canvas.get("screenshot_sha256", "")),
                    mime_type=str(canvas.get("mime_type", "")),
                    width=int(canvas.get("width", 0)),
                    height=int(canvas.get("height", 0)),
                    client_width=float(canvas.get("client_width", 0)),
                    client_height=float(canvas.get("client_height", 0)),
                    bbox=tuple(float(value) for value in bbox),
                )
            )
        return tuple(frames)

    def _vision_scene(
        self,
        recognized: Any,
        *,
        input_payload: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
        adapter_id: str,
        adapter_version: str,
        adapter_supports_actions: bool,
    ) -> dict[str, Any] | None:
        if recognized is None:
            return None
        if not isinstance(recognized, dict):
            raise ValueError("CanvasVisionAdapter result must be an object")
        objects = recognized.get("objects", [])
        if not isinstance(objects, list):
            raise ValueError("CanvasVisionAdapter objects must be a list")
        if not objects:
            return None
        if len(objects) > self.MAX_DOM_ELEMENTS:
            raise ValueError("CanvasVisionAdapter returned too many objects")

        elements = []
        bindings = {}
        object_ids = set()
        all_objects_grounded = True
        for index, obj in enumerate(objects, start=1):
            if not isinstance(obj, dict):
                raise ValueError("CanvasVisionAdapter object entries must be objects")
            business_id = str(obj.get("business_id", "")).strip()
            if not business_id:
                raise ValueError("CanvasVisionAdapter objects require business_id")
            if business_id in object_ids:
                raise ValueError(f"duplicate CanvasVisionAdapter business_id: {business_id}")
            object_ids.add(business_id)

            confidence = self._confidence(obj.get("confidence", recognized.get("confidence", 0.5)))
            frame = self._vision_frame(obj, frames)
            bbox, center, geometry_grounded = self._vision_geometry(obj, frame)
            grounded = geometry_grounded and confidence >= self.MIN_ACTIONABLE_VISION_CONFIDENCE
            all_objects_grounded = all_objects_grounded and grounded
            element_id = f"vision_canvas_{index:04d}"
            attributes = obj.get("attributes", {})
            if not isinstance(attributes, dict):
                raise ValueError("CanvasVisionAdapter object attributes must be an object")
            semantic_attributes = copy.deepcopy(attributes)
            semantic_attributes.update(
                {
                    key: copy.deepcopy(value)
                    for key, value in obj.items()
                    if key
                    not in {
                        "business_id",
                        "type",
                        "label",
                        "x",
                        "y",
                        "width",
                        "height",
                        "bbox",
                        "center",
                        "confidence",
                        "attributes",
                        "evidence",
                        "evidence_refs",
                    }
                }
            )
            elements.append(
                {
                    "element_id": element_id,
                    "business_id": business_id,
                    "type": obj.get("type", "object"),
                    "label": obj.get("label", business_id),
                    "bbox": bbox,
                    "center": center,
                    "attributes": semantic_attributes,
                    "confidence": confidence,
                }
            )
            bindings[business_id] = {
                "element_id": element_id,
                "canvas_ref": f"vision:{frame.canvas_id if frame else 'unresolved'}:{business_id}",
                "confidence": confidence,
                "method": "canvas_vision_adapter",
                "actionable": grounded and len(frames) == 1 and adapter_supports_actions,
            }

        relations = self._recognized_relations(recognized.get("links", recognized.get("relations", [])), object_ids)
        co_channel_relations = self._recognized_relations(
            recognized.get("co_channel_relations", []), object_ids
        )
        first_frame = frames[0]
        semantic_tree = self._semantic_tree(elements, relations)
        scene = {
            "mode": "canvas_vision_adapter",
            "input": input_payload,
            "scene_type": "vision_recognized_canvas_topology",
            "object_count": len(elements),
            "elements": elements,
            "business_object_bindings": bindings,
            "relations": relations,
            "co_channel_relations": co_channel_relations,
            "relation_count": len(relations),
            "semantic_tree": semantic_tree,
            "coordinate_space": {
                "type": "canvas_pixels",
                "width": first_frame.width,
                "height": first_frame.height,
                "frames": [
                    {
                        "canvas_id": frame.canvas_id,
                        "width": frame.width,
                        "height": frame.height,
                        "bbox": list(frame.bbox),
                    }
                    for frame in frames
                ],
            },
            "pixel_capture_available": True,
            "requires_vision_model": False,
            "limitations": [
                "节点和关系由配置的 CanvasVisionAdapter 从已保存截图像素识别",
                "识别置信度与业务对象标识仍需由具体适配器和测试数据校验",
                "多 Canvas 场景仅用于分析；建立统一页面坐标变换前不可执行",
                "视觉模型给出的业务 ID 默认只用于分析；需经生产资产库核验后才能执行",
            ],
        }
        fusion_summary = recognized.get("fusion_summary")
        if isinstance(fusion_summary, dict):
            scene["fusion_summary"] = copy.deepcopy(fusion_summary)
        fusion_analysis = recognized.get("fusion_analysis")
        if isinstance(fusion_analysis, dict):
            scene["fusion_analysis"] = copy.deepcopy(fusion_analysis)
        return self._stamp_provenance(
            scene,
            semantic_source="canvas_pixels",
            pixel_inference_performed=True,
            pixel_verified=True,
            actionable_grounding=(
                bool(elements)
                and all_objects_grounded
                and len(frames) == 1
                and adapter_supports_actions
            ),
            adapter_id=adapter_id[:200],
            adapter_version=adapter_version[:100],
            adapter_supports_actionable_grounding=adapter_supports_actions,
            screenshot_sha256=[frame.screenshot_sha256 for frame in frames],
        )

    def _vision_frame(
        self,
        obj: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
    ) -> CanvasFrame | None:
        canvas_id = str(obj.get("canvas_id", "")).strip()
        if canvas_id:
            return next((frame for frame in frames if frame.canvas_id == canvas_id), None)
        return frames[0] if len(frames) == 1 else None

    def _vision_geometry(
        self,
        obj: dict[str, Any],
        frame: CanvasFrame | None,
    ) -> tuple[list[float], list[float], bool]:
        bbox = obj.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            x, y, width, height = (float(value) for value in bbox)
            return self._checked_vision_geometry(x, y, width, height, frame)
        center = obj.get("center")
        if isinstance(center, (list, tuple)) and len(center) == 2:
            x, y = (float(value) for value in center)
            width = float(obj.get("width", 0))
            height = float(obj.get("height", 0))
            return self._checked_vision_geometry(x - width / 2, y - height / 2, width, height, frame)
        if "x" in obj and "y" in obj:
            x = float(obj["x"])
            y = float(obj["y"])
            width = float(obj.get("width", 0))
            height = float(obj.get("height", 0))
            return self._checked_vision_geometry(x - width / 2, y - height / 2, width, height, frame)
        return [0.0, 0.0, 0.0, 0.0], [0.0, 0.0], False

    def _checked_vision_geometry(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        frame: CanvasFrame | None,
    ) -> tuple[list[float], list[float], bool]:
        bbox = [x, y, width, height]
        center = [x + width / 2, y + height / 2]
        values_valid = all(math.isfinite(value) for value in bbox)
        within_frame = bool(
            frame
            and frame.width > 0
            and frame.height > 0
            and x >= 0
            and y >= 0
            and width > 0
            and height > 0
            and x + width <= frame.width
            and y + height <= frame.height
        )
        return bbox, center, values_valid and within_frame

    def _confidence(self, value: Any) -> float:
        try:
            return round(max(0.0, min(1.0, float(value))), 4)
        except (TypeError, ValueError):
            return 0.0

    def _recognized_relations(self, relations: Any, object_ids: set[str]) -> list[dict[str, Any]]:
        if not isinstance(relations, list):
            raise ValueError("recognized relations must be a list")
        if len(relations) > self.MAX_RECOGNIZED_RELATIONS:
            raise ValueError("recognized relations exceed 4000 entries")
        normalized = []
        for relation in relations:
            if not isinstance(relation, dict):
                raise ValueError("recognized relation entries must be objects")
            source = str(relation.get("source", "")).strip()
            target = str(relation.get("target", "")).strip()
            if not source or not target:
                raise ValueError("recognized relations require source and target")
            if source not in object_ids or target not in object_ids:
                raise ValueError("recognized relation contains a dangling endpoint")
            normalized.append(copy.deepcopy(relation))
        return normalized

    def _text_scene(
        self,
        topology_text: dict[str, str] | None,
        page: dict[str, Any],
        canvases: list[dict[str, Any]],
    ) -> dict[str, Any]:
        pixel_capture_available = any(canvas.get("screenshot_path") for canvas in canvases)
        if topology_text is None:
            return self._unavailable_text_scene(
                {
                    "source": "topology_text",
                    "url": page["url"],
                    "available": False,
                },
                "本次页面采集未提供拓扑文本观察",
            )

        text_sha256 = hashlib.sha256(topology_text["text"].encode("utf-8")).hexdigest()
        input_payload = {
            "source": "provided_topology_text",
            "url": page["url"],
            "available": True,
            "kind": topology_text["kind"],
            "format": topology_text["format"],
            "source_id": topology_text["source_id"],
            "text_sha256": text_sha256,
            "character_count": len(topology_text["text"]),
        }
        if self.text_recognizer is None:
            return self._unavailable_text_scene(
                input_payload,
                "已提供拓扑文本，但服务未配置 TopologyTextRecognizer",
            )

        try:
            recognized = self.text_recognizer.recognize(
                topology_text["text"],
                source_ref=topology_text["source_id"],
            )
            if not isinstance(recognized, dict):
                raise ValueError("TopologyTextRecognizer result must be an object")
            scene = copy.deepcopy(recognized)
            elements = scene.get("elements", [])
            if not isinstance(elements, list):
                raise ValueError("TopologyTextRecognizer elements must be a list")
            if not elements:
                return self._unavailable_text_scene(
                    input_payload,
                    "TopologyTextRecognizer 未从文本中重建出业务对象",
                )
            if len(elements) > self.MAX_DOM_ELEMENTS:
                raise ValueError("TopologyTextRecognizer returned too many elements")
            if scene.get("usable_for_analysis") is False:
                unavailable = self._unavailable_text_scene(
                    input_payload,
                    "TopologyTextRecognizer 判定输入不完整或不可靠，已拒绝部分语义结果",
                )
                unavailable["recognition_issues"] = copy.deepcopy(scene.get("issues", []))
                unavailable["recognition_metrics"] = copy.deepcopy(scene.get("metrics", {}))
                unavailable["recognition_diagnostics"] = copy.deepcopy(scene.get("diagnostics", {}))
                return unavailable
            bindings = scene.get("business_object_bindings", {})
            if not isinstance(bindings, dict):
                raise ValueError("TopologyTextRecognizer bindings must be an object")
            for binding in bindings.values():
                if isinstance(binding, dict):
                    binding["actionable"] = False
                    binding["method"] = "topology_text_reconstruction"
                    binding.pop("canvas_ref", None)
                    binding.pop("dom_ref", None)

            recognizer_scene_type = scene.get("scene_type")
            scene.update(
                {
                    "mode": "topology_text_reconstruction",
                    "input": input_payload,
                    "scene_type": "semantic_topology_without_pixel_grounding",
                    "object_count": len(elements),
                    "elements": elements,
                    "business_object_bindings": bindings,
                    "relations": copy.deepcopy(scene.get("relations", [])),
                    "co_channel_relations": copy.deepcopy(scene.get("co_channel_relations", [])),
                    "relation_count": len(scene.get("relations", [])),
                    "pixel_capture_available": pixel_capture_available,
                    "requires_vision_model": pixel_capture_available,
                    "usable_for_actions": False,
                    "actionable_grounding": False,
                    "limitations": list(scene.get("limitations", []))
                    + [
                        "节点和关系仅由提供的文本语义重建，未读取 Canvas 截图像素",
                        "文本坐标或合成布局不可用于 GUI 点击和其他界面副作用",
                    ],
                }
            )
            scene["semantic_tree"] = self._semantic_tree(elements, scene["relations"])
            recognizer_id = str(
                getattr(self.text_recognizer, "recognizer_id", type(self.text_recognizer).__name__)
            )
            recognizer_version = str(getattr(self.text_recognizer, "recognizer_version", "unknown"))
            semantic_source = (
                "provided_text"
                if topology_text["kind"] == "user_provided_ascii"
                else "external_ocr_transcript"
            )
            return self._stamp_provenance(
                scene,
                semantic_source=semantic_source,
                pixel_inference_performed=False,
                pixel_verified=False,
                actionable_grounding=False,
                text_kind=topology_text["kind"],
                text_format=topology_text["format"],
                text_source_id=topology_text["source_id"],
                text_sha256=text_sha256,
                recognizer_id=recognizer_id[:200],
                recognizer_version=recognizer_version[:100],
                recognizer_scene_type=recognizer_scene_type,
            )
        except Exception as exc:  # Keep capture evidence even when an optional parser fails.
            scene = self._unavailable_text_scene(
                input_payload,
                "TopologyTextRecognizer 执行失败，文本证据已保留但未被选为语义场景",
            )
            scene["text_recognition_error"] = f"{type(exc).__name__}: {exc}"[:500]
            return scene

    def _unavailable_text_scene(self, input_payload: dict[str, Any], limitation: str) -> dict[str, Any]:
        scene = {
            "mode": "topology_text_unavailable",
            "input": input_payload,
            "scene_type": "unavailable_topology_text",
            "object_count": 0,
            "elements": [],
            "business_object_bindings": {},
            "relations": [],
            "co_channel_relations": [],
            "relation_count": 0,
            "requires_vision_model": False,
            "limitations": [limitation],
        }
        return self._stamp_provenance(
            scene,
            semantic_source="none",
            pixel_inference_performed=False,
            pixel_verified=False,
            actionable_grounding=False,
        )

    def _select_scene(
        self,
        dom_scene: dict[str, Any],
        canvas_scene: dict[str, Any],
        text_scene: dict[str, Any],
    ) -> dict[str, Any]:
        if canvas_scene["mode"] == "canvas_renderer_adapter" and canvas_scene["object_count"]:
            return canvas_scene
        if dom_scene["business_object_bindings"]:
            return dom_scene
        if canvas_scene["mode"] == "canvas_vision_adapter" and canvas_scene["object_count"]:
            return canvas_scene
        if text_scene["mode"] == "topology_text_reconstruction" and text_scene["object_count"]:
            return text_scene
        if canvas_scene.get("requires_vision_model"):
            return canvas_scene
        if dom_scene["object_count"]:
            return dom_scene
        return canvas_scene

    def _decision(
        self,
        dom_scene: dict[str, Any],
        canvas_scene: dict[str, Any],
        text_scene: dict[str, Any],
        selected: dict[str, Any],
    ) -> dict[str, Any]:
        if selected["mode"] == "canvas_renderer_adapter":
            if selected.get("pixel_capture_available"):
                reason = "页面提供 Canvas 渲染器语义数据，同时保留真实像素截图用于校验"
            else:
                reason = "页面提供 Canvas 渲染器语义数据，但本次像素截图不可用"
        elif selected["mode"] == "canvas_vision_adapter":
            reason = "配置的 CanvasVisionAdapter 已从真实截图像素识别出节点和关系"
        elif selected["mode"] == "topology_text_reconstruction":
            reason = "使用提供的拓扑文本重建语义；该结果未读取或校验 Canvas 截图像素"
        elif selected["mode"] == "live_dom_snapshot":
            if canvas_scene["input"].get("canvases") and not canvas_scene.get("pixel_capture_available"):
                reason = "Canvas 像素截图不可用，使用浏览器实时 DOM/ARIA 元素"
            else:
                reason = "使用浏览器实时 DOM/ARIA 元素；Canvas 语义不可用时不虚构节点"
        elif selected.get("pixel_capture_available"):
            reason = "仅捕获到 Canvas 像素，等待视觉模型补充语义识别"
        else:
            reason = "检测到 Canvas，但本次像素截图不可用，无法进行视觉语义识别"
        return {
            "reason": reason,
            "dom_element_count": dom_scene["object_count"],
            "canvas_count": len(canvas_scene["input"].get("canvases", [])),
            "canvas_pixel_capture_available": canvas_scene.get("pixel_capture_available", False),
            "canvas_adapter_available": canvas_scene["mode"] == "canvas_renderer_adapter",
            "canvas_vision_available": canvas_scene["mode"] == "canvas_vision_adapter",
            "topology_text_available": text_scene["input"].get("available", False),
            "topology_text_recognized": text_scene["mode"] == "topology_text_reconstruction",
            "selected_mode": selected["mode"],
            "requires_vision_model": selected.get("requires_vision_model", False),
            "semantic_source": selected.get("provenance", {}).get("semantic_source", "unknown"),
        }

    def _template_hash(self, capture: dict[str, Any], selected: dict[str, Any]) -> str:
        page = capture["page"]
        adapter_scene = capture.get("adapter_scene")
        dom_structure = (
            []
            if adapter_scene
            else [
                {
                    "ref": item.get("ref"),
                    "parent_ref": item.get("parent_ref"),
                    "depth": item.get("depth"),
                    "document_order": item.get("document_order"),
                    "tag": item.get("tag"),
                    "role": item.get("role"),
                    "business_id": item.get("business_id"),
                    "aria_label": item.get("aria_label"),
                }
                for item in capture["dom"]["elements"]
            ]
        )
        canvas_structure = [
            {
                "canvas_id": item.get("canvas_id"),
                "width": item.get("width"),
                "height": item.get("height"),
            }
            for item in capture["canvases"]
        ]
        payload = {
            "schema": "live-page-v1",
            "route": self._page_route(page["url"]),
            "ui_version": page.get("ui_version"),
            "viewport": page.get("viewport"),
            "dom_structure": dom_structure,
            "canvas_structure": canvas_structure,
            "adapter_ui_version": (adapter_scene or {}).get("ui_version"),
        }
        if selected["mode"] in {"canvas_vision_adapter", "topology_text_reconstruction"}:
            provenance = selected.get("provenance", {})
            payload["recognition_pipeline"] = {
                "mode": selected["mode"],
                "adapter_id": provenance.get("adapter_id"),
                "adapter_version": provenance.get("adapter_version"),
                "recognizer_id": provenance.get("recognizer_id"),
                "recognizer_version": provenance.get("recognizer_version"),
                "text_kind": provenance.get("text_kind"),
                "text_format": provenance.get("text_format"),
            }
        return self._hash(payload)

    def _content_hash(self, capture: dict[str, Any], selected: dict[str, Any]) -> str:
        adapter_scene = capture.get("adapter_scene")
        if adapter_scene:
            payload = {
                "objects": sorted(
                    adapter_scene.get("objects", []), key=lambda item: str(item.get("business_id", ""))
                ),
                "links": sorted(adapter_scene.get("links", []), key=self._relation_key),
                "co_channel_relations": sorted(
                    adapter_scene.get("co_channel_relations", []), key=self._relation_key
                ),
            }
        elif selected["mode"] in {"canvas_vision_adapter", "topology_text_reconstruction"}:
            include_geometry = selected["mode"] == "canvas_vision_adapter"
            payload = {
                "mode": selected["mode"],
                "elements": sorted(
                    [
                        {
                            "business_id": item.get("business_id"),
                            "type": item.get("type"),
                            "label": item.get("label"),
                            "attributes": item.get("attributes", {}),
                            **(
                                {"bbox": item.get("bbox"), "center": item.get("center")}
                                if include_geometry
                                else {}
                            ),
                        }
                        for item in selected.get("elements", [])
                    ],
                    key=lambda item: str(item.get("business_id", "")),
                ),
                "relations": sorted(
                    [self._semantic_relation(item) for item in selected.get("relations", [])],
                    key=self._relation_key,
                ),
                "co_channel_relations": sorted(
                    [
                        self._semantic_relation(item)
                        for item in selected.get("co_channel_relations", [])
                    ],
                    key=self._relation_key,
                ),
                "visual_groups": sorted(
                    [
                        {
                            key: copy.deepcopy(group[key])
                            for key in (
                                "group_id",
                                "kind",
                                "label",
                                "is_device",
                                "anchor",
                                "resolved_members",
                                "member_edge_count",
                            )
                            if key in group
                        }
                        for group in selected.get("visual_groups", [])
                        if isinstance(group, dict)
                    ],
                    key=lambda group: str(group.get("group_id", group.get("label", ""))),
                ),
                "issues": sorted(
                    [
                        {
                            key: copy.deepcopy(issue[key])
                            for key in ("code", "severity", "business_id", "marker", "hedged")
                            if key in issue
                        }
                        for issue in selected.get("issues", [])
                        if isinstance(issue, dict)
                    ],
                    key=lambda issue: (
                        str(issue.get("code", "")),
                        str(issue.get("business_id", "")),
                    ),
                ),
                "usable_for_analysis": selected.get("usable_for_analysis"),
            }
        else:
            payload = {
                "dom": capture["dom"]["elements"],
                "canvas_hashes": [item.get("screenshot_sha256") for item in capture["canvases"]],
            }
        return self._hash(payload)

    def _scene_key(self, page: dict[str, Any], template_hash: str) -> str:
        route_hash = hashlib.sha256(self._page_route(page["url"]).encode("utf-8")).hexdigest()[:12]
        return f"live-page:{route_hash}:{template_hash[:12]}"

    def _page_route(self, url: str) -> str:
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    def _relation_key(self, relation: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(relation.get("source", "")),
            str(relation.get("target", "")),
            str(relation.get("type", relation.get("channel", ""))),
            str(relation.get("relation_id", relation.get("edge_id", relation.get("id", "")))),
        )

    def _semantic_relation(self, relation: dict[str, Any]) -> dict[str, Any]:
        return {
            key: copy.deepcopy(value)
            for key, value in relation.items()
            if key not in {"evidence", "evidence_ids", "evidence_refs"}
        }

    def _semantic_tree(
        self,
        elements: list[dict[str, Any]],
        relations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        nodes = {
            str(element["business_id"]): {
                "element_id": element.get("element_id"),
                "role": element.get("type", "object"),
                "name": element.get("label", element["business_id"]),
                "type": element.get("type", "object"),
                "bbox": copy.deepcopy(element.get("bbox", [0, 0, 0, 0])),
                "confidence": element.get("confidence", 0),
                "children": [],
            }
            for element in elements
            if element.get("business_id")
        }
        incoming = {business_id: 0 for business_id in nodes}
        degree = {business_id: 0 for business_id in nodes}
        outgoing: dict[str, list[tuple[tuple[str, str, str], dict[str, Any]]]] = {
            business_id: [] for business_id in nodes
        }
        valid_relations: list[tuple[tuple[str, str, str], dict[str, Any]]] = []
        issues: list[dict[str, Any]] = []
        for index, relation in enumerate(relations, start=1):
            source = str(relation.get("source", ""))
            target = str(relation.get("target", ""))
            relation_id = str(
                relation.get(
                    "relation_id",
                    relation.get("edge_id", relation.get("id", f"relation_{index:04d}")),
                )
            )
            relation_type = str(relation.get("type", "relation"))
            key = (source, target, relation_id)
            projection = {
                "source": source,
                "target": target,
                "relation_id": relation_id,
                "type": relation_type,
            }
            if source not in nodes or target not in nodes:
                issues.append(
                    {
                        "code": "tree_relation_endpoint_missing",
                        "relation_id": relation_id,
                    }
                )
                continue
            incoming[target] += 1
            degree[source] += 1
            degree[target] += 1
            outgoing[source].append((key, projection))
            valid_relations.append((key, projection))

        for source in outgoing:
            outgoing[source].sort(key=lambda item: item[0])
        orphans = sorted(business_id for business_id, value in degree.items() if value == 0)
        roots = sorted(
            business_id
            for business_id in nodes
            if incoming[business_id] == 0 and degree[business_id] > 0
        )
        visited: set[str] = set()
        tree_relation_keys: set[tuple[str, str, str]] = set()

        def project_component(root: str) -> None:
            queue = [root]
            visited.add(root)
            while queue:
                source = queue.pop(0)
                for key, projection in outgoing[source]:
                    target = projection["target"]
                    if target in visited:
                        continue
                    visited.add(target)
                    tree_relation_keys.add(key)
                    nodes[source]["children"].append(
                        {
                            "target": target,
                            "relation_id": projection["relation_id"],
                            "type": projection["type"],
                        }
                    )
                    queue.append(target)

        for root in list(roots):
            if root not in visited:
                project_component(root)
        remaining = sorted(set(nodes) - visited - set(orphans))
        while remaining:
            root = remaining[0]
            roots.append(root)
            issues.append(
                {
                    "code": "tree_projection_requires_synthetic_root",
                    "business_id": root,
                }
            )
            project_component(root)
            remaining = sorted(set(nodes) - visited - set(orphans))

        non_tree_relations = [
            projection
            for key, projection in valid_relations
            if key not in tree_relation_keys
        ]
        for node in nodes.values():
            node["children"].sort(
                key=lambda child: (
                    str(child["target"]),
                    str(child["relation_id"]),
                )
            )
        return {
            "tree_type": "dom_like_semantic_projection",
            "roots": roots,
            "nodes": nodes,
            "orphans": orphans,
            "non_tree_relations": non_tree_relations,
            "complete": len(visited) + len(orphans) == len(nodes) and not issues,
            "issues": issues,
        }

    def _hash(self, value: Any) -> str:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _topology_from_scene(self, scene: dict[str, Any], capture: dict[str, Any]) -> dict[str, Any]:
        objects = []
        for element in scene.get("elements", []):
            if not element.get("business_id"):
                continue
            center = element.get("center", [0, 0])
            if not isinstance(center, (list, tuple)) or len(center) != 2:
                center = [0, 0]
            objects.append(
                {
                    "business_id": element["business_id"],
                    "type": element.get("type", "object"),
                    "label": element.get("label", element["business_id"]),
                    "x": center[0],
                    "y": center[1],
                    **element.get("attributes", {}),
                }
            )
        first_canvas = capture.get("canvases", [{}])[0] if capture.get("canvases") else {}
        return {
            "ui_version": capture["page"].get("ui_version", "unknown"),
            "site": "captured-page",
            "floor": "page",
            "scene": "live page capture",
            "canvas": {
                "width": first_canvas.get("width", capture["page"]["viewport"]["width"]),
                "height": first_canvas.get("height", capture["page"]["viewport"]["height"]),
            },
            "objects": objects,
            "links": copy.deepcopy(scene.get("relations", [])),
            "co_channel_relations": copy.deepcopy(scene.get("co_channel_relations", [])),
            "visual_grounding": {},
        }
