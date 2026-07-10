from __future__ import annotations

from typing import Any, Protocol


def _object_attributes(obj: dict[str, Any]) -> dict[str, Any]:
    excluded = {"business_id", "type", "label", "x", "y"}
    return {key: value for key, value in obj.items() if key not in excluded}


def _scene_relations(topology: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(relation) for relation in topology.get("links", [])]


def _co_channel_relations(topology: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(relation) for relation in topology.get("co_channel_relations", [])]


class PerceptionAdapter(Protocol):
    mode: str

    def capture(self, topology: dict[str, Any], user: str) -> dict[str, Any]:
        ...

    def perceive(self, topology: dict[str, Any], raw_scene: dict[str, Any]) -> dict[str, Any]:
        ...


class DomElementPerception:
    mode = "dom_element_perception"

    def capture(self, topology: dict[str, Any], user: str) -> dict[str, Any]:
        return {
            "source": "existing_business_dom",
            "capture_mode": "mock_dom_accessibility_snapshot",
            "root_ref": "left-panel.network-topology",
            "site": topology["site"],
            "floor": topology["floor"],
            "requested_user": user,
            "raw_input": {
                "type": "dom",
                "has_dom_nodes": True,
                "has_canvas_pixels": False,
                "selectors": ["[data-business-id]", "[aria-label]", ".topology-node"],
            },
        }

    def perceive(self, topology: dict[str, Any], raw_scene: dict[str, Any]) -> dict[str, Any]:
        elements = []
        bindings = {}
        for index, obj in enumerate(topology.get("objects", []), start=1):
            business_id = obj["business_id"]
            selector = f"[data-business-id='{business_id}']"
            element_id = f"dom_element_{index:03d}"
            confidence = 0.98 if obj["type"] in {"ap", "user"} else 0.93
            elements.append(
                {
                    "element_id": element_id,
                    "business_id": business_id,
                    "type": obj["type"],
                    "label": obj["label"],
                    "selector": selector,
                    "bbox": [obj["x"] - 28, obj["y"] - 28, 56, 56],
                    "center": [obj["x"], obj["y"]],
                    "attributes": _object_attributes(obj),
                    "confidence": confidence,
                }
            )
            bindings[business_id] = {
                "element_id": element_id,
                "dom_ref": selector,
                "confidence": confidence,
                "method": self.mode,
            }

        return self._scene(topology, raw_scene, elements, bindings)

    def _scene(
        self,
        topology: dict[str, Any],
        raw_scene: dict[str, Any],
        elements: list[dict[str, Any]],
        bindings: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "input": raw_scene,
            "scene_type": "dom_topology_view",
            "object_count": len(elements),
            "elements": elements,
            "business_object_bindings": bindings,
            "relations": _scene_relations(topology),
            "co_channel_relations": _co_channel_relations(topology),
            "relation_count": len(topology.get("links", [])),
            "coordinate_space": {
                "type": "topology_world",
                "width": topology["canvas"]["width"],
                "height": topology["canvas"]["height"],
            },
            "limitations": [
                "适用于拓扑节点有 DOM、ARIA 或 data-business-id 的页面",
                "真实接入时由浏览器 DOM snapshot / accessibility tree 生成",
            ],
        }


class CanvasScreenshotPerception:
    mode = "canvas_screenshot_perception"

    def capture(self, topology: dict[str, Any], user: str) -> dict[str, Any]:
        return {
            "source": "existing_business_canvas",
            "capture_mode": "mock_canvas_screenshot_snapshot",
            "canvas_ref": "left-panel.network-topology.canvas",
            "site": topology["site"],
            "floor": topology["floor"],
            "requested_user": user,
            "raw_input": {
                "type": "canvas",
                "width": topology["canvas"]["width"],
                "height": topology["canvas"]["height"],
                "has_dom_nodes": False,
                "has_canvas_pixels": True,
            },
        }

    def perceive(self, topology: dict[str, Any], raw_scene: dict[str, Any]) -> dict[str, Any]:
        elements = []
        bindings = {}
        for index, obj in enumerate(topology.get("objects", []), start=1):
            business_id = obj["business_id"]
            element_id = f"canvas_element_{index:03d}"
            confidence = 0.96 if business_id.startswith(("ap_", "user_")) else 0.9
            elements.append(
                {
                    "element_id": element_id,
                    "business_id": business_id,
                    "type": obj["type"],
                    "label": obj["label"],
                    "bbox": [obj["x"] - 30, obj["y"] - 30, 60, 60],
                    "center": [obj["x"], obj["y"]],
                    "attributes": _object_attributes(obj),
                    "confidence": confidence,
                }
            )
            bindings[business_id] = {
                "element_id": element_id,
                "canvas_ref": f"{raw_scene['canvas_ref']}.{element_id}",
                "confidence": confidence,
                "method": self.mode,
            }

        return {
            "mode": self.mode,
            "input": raw_scene,
            "scene_type": "irregular_canvas_topology",
            "object_count": len(elements),
            "elements": elements,
            "business_object_bindings": bindings,
            "relations": _scene_relations(topology),
            "co_channel_relations": _co_channel_relations(topology),
            "relation_count": len(topology.get("links", [])),
            "coordinate_space": {
                "type": "topology_world",
                "width": topology["canvas"]["width"],
                "height": topology["canvas"]["height"],
            },
            "limitations": [
                "当前为 mock 截图识别结果，不是视觉模型真实识别",
                "真实接入时应替换为 canvas 截图 + OCR/视觉模型/图形检测",
            ],
        }


class HybridPerception:
    def __init__(self, preferred_mode: str = "hybrid"):
        self.preferred_mode = preferred_mode
        self.dom = DomElementPerception()
        self.canvas = CanvasScreenshotPerception()

    def perceive_topology(self, topology: dict[str, Any], user: str) -> dict[str, Any]:
        dom_raw = self.dom.capture(topology, user)
        canvas_raw = self.canvas.capture(topology, user)
        dom_scene = self.dom.perceive(topology, dom_raw)
        canvas_scene = self.canvas.perceive(topology, canvas_raw)
        selected = self._select_scene(dom_scene, canvas_scene)
        return {
            "strategy": self.preferred_mode,
            "selected_mode": selected["mode"],
            "raw_scenes": {
                "dom": dom_raw,
                "canvas": canvas_raw,
            },
            "candidates": {
                "dom": dom_scene,
                "canvas": canvas_scene,
            },
            "scene": selected,
            "business_object_bindings": selected["business_object_bindings"],
            "decision": self._decision(dom_scene, canvas_scene, selected),
        }

    def _select_scene(self, dom_scene: dict[str, Any], canvas_scene: dict[str, Any]) -> dict[str, Any]:
        if self.preferred_mode == "dom":
            return dom_scene
        if self.preferred_mode == "canvas":
            return canvas_scene
        if self._average_confidence(dom_scene) >= 0.95:
            return dom_scene
        return canvas_scene

    def _decision(
        self,
        dom_scene: dict[str, Any],
        canvas_scene: dict[str, Any],
        selected: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "reason": "DOM 节点置信度足够时优先 DOM；DOM 不完整或不可见时降级 Canvas 识别",
            "dom_confidence": round(self._average_confidence(dom_scene), 3),
            "canvas_confidence": round(self._average_confidence(canvas_scene), 3),
            "selected_mode": selected["mode"],
        }

    def _average_confidence(self, scene: dict[str, Any]) -> float:
        elements = scene.get("elements", [])
        if not elements:
            return 0.0
        return sum(element.get("confidence", 0.0) for element in elements) / len(elements)
