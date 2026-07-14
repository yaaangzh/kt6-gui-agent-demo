from __future__ import annotations

import base64
import copy
import hashlib
import json
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
    DATA_URL_PATTERN = re.compile(r"^data:image/(png|jpeg|webp);base64,(.+)$", re.DOTALL)

    def __init__(self, store: SQLitePageCaptureStore, perception_runtime: PerceptionRuntime):
        self.store = store
        self.perception_runtime = perception_runtime

    def ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        capture_id = f"capture_{uuid.uuid4().hex[:12]}"
        page = self._normalize_page(payload.get("page", {}))
        dom = self._normalize_dom(payload.get("dom", {}))
        canvases = self._normalize_canvases(capture_id, payload.get("canvases", []))
        adapter_scene = self._normalize_adapter_scene(payload.get("adapter_scene"))
        capture = {
            "page": page,
            "dom": dom,
            "canvases": canvases,
            "adapter_scene": adapter_scene,
            "captured_at": payload.get("captured_at", time.time()),
        }

        dom_scene = self._dom_scene(dom, page)
        canvas_scene = self._canvas_scene(canvases, adapter_scene, page)
        selected = self._select_scene(dom_scene, canvas_scene)
        perception = {
            "strategy": "live_page_hybrid",
            "selected_mode": selected["mode"],
            "raw_scenes": {
                "dom": dom_scene["input"],
                "canvas": canvas_scene["input"],
            },
            "candidates": {
                "dom": dom_scene,
                "canvas": canvas_scene,
            },
            "scene": selected,
            "business_object_bindings": selected["business_object_bindings"],
            "decision": self._decision(dom_scene, canvas_scene, selected),
        }

        template_hash = self._template_hash(capture)
        content_hash = self._content_hash(capture)
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
            "selected_mode": result["perception"]["scene"]["mode"],
            "requires_vision_model": result["perception"]["scene"].get("requires_vision_model", False),
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
        for item in list(dom.get("elements", []))[: self.MAX_DOM_ELEMENTS]:
            bbox = item.get("bbox", [])
            if len(bbox) != 4:
                continue
            elements.append(
                {
                    "ref": str(item.get("ref", ""))[:500],
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
        return {
            "mode": "live_dom_snapshot",
            "input": {
                "source": "live_browser_dom",
                "url": page["url"],
                "element_count": len(elements),
            },
            "scene_type": "live_dom_page",
            "object_count": len(elements),
            "elements": elements,
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
        if not adapter_scene:
            if pixel_capture_available:
                limitations = [
                    "已捕获真实 Canvas 像素，但页面未提供语义适配器",
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
            return {
                "mode": "canvas_screenshot_capture" if pixel_capture_available else "canvas_capture_unavailable",
                "input": input_payload,
                "scene_type": (
                    "unrecognized_canvas_capture"
                    if pixel_capture_available
                    else "unavailable_canvas_capture"
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
        return {
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

    def _select_scene(self, dom_scene: dict[str, Any], canvas_scene: dict[str, Any]) -> dict[str, Any]:
        if canvas_scene["mode"] == "canvas_renderer_adapter" and canvas_scene["object_count"]:
            return canvas_scene
        if dom_scene["business_object_bindings"]:
            return dom_scene
        if canvas_scene.get("requires_vision_model"):
            return canvas_scene
        if dom_scene["object_count"]:
            return dom_scene
        return canvas_scene

    def _decision(
        self,
        dom_scene: dict[str, Any],
        canvas_scene: dict[str, Any],
        selected: dict[str, Any],
    ) -> dict[str, Any]:
        if selected["mode"] == "canvas_renderer_adapter":
            if selected.get("pixel_capture_available"):
                reason = "页面提供 Canvas 渲染器语义数据，同时保留真实像素截图用于校验"
            else:
                reason = "页面提供 Canvas 渲染器语义数据，但本次像素截图不可用"
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
            "selected_mode": selected["mode"],
            "requires_vision_model": selected.get("requires_vision_model", False),
        }

    def _template_hash(self, capture: dict[str, Any]) -> str:
        page = capture["page"]
        adapter_scene = capture.get("adapter_scene")
        dom_structure = (
            []
            if adapter_scene
            else [
                {
                    "ref": item.get("ref"),
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
        return self._hash(payload)

    def _content_hash(self, capture: dict[str, Any]) -> str:
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

    def _relation_key(self, relation: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(relation.get("source", "")),
            str(relation.get("target", "")),
            str(relation.get("type", relation.get("channel", ""))),
        )

    def _hash(self, value: Any) -> str:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _topology_from_scene(self, scene: dict[str, Any], capture: dict[str, Any]) -> dict[str, Any]:
        objects = []
        for element in scene.get("elements", []):
            if not element.get("business_id"):
                continue
            center = element.get("center", [0, 0])
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
