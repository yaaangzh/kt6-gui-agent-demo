from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .perception_runtime import PerceptionRuntime


class MockBusinessTools:
    def __init__(self, data_dir: Path, perception_runtime: PerceptionRuntime | None = None):
        self.data_dir = data_dir
        self.perception_runtime = perception_runtime or PerceptionRuntime()
        self.perception = self.perception_runtime.perception

    def _read_json(self, name: str) -> dict[str, Any]:
        return json.loads((self.data_dir / name).read_text(encoding="utf-8"))

    def query_topology(self, user: str) -> dict[str, Any]:
        topology = self._read_json("mock_topology.json")
        focus = self._focus(topology, "user", user)
        return self._attach_perception(topology, self.perception_runtime.resolve(topology, focus), focus)

    def query_ap_topology(self, ap_id: str) -> dict[str, Any]:
        topology = self._read_json("mock_topology.json")
        focus = self._focus(topology, "ap", ap_id)
        return self._attach_perception(topology, self.perception_runtime.resolve(topology, focus), focus)

    def validate_scene(self, scene_ref: dict[str, Any]) -> dict[str, Any]:
        topology = self._read_json("mock_topology.json")
        validation = self.perception_runtime.validate(topology, scene_ref)
        focus = {
            "kind": "validation",
            "value": None,
            "target_ids": list(scene_ref.get("target_ids", [])),
        }
        validation["topology"] = self._attach_perception(topology, validation["result"], focus)
        validation.pop("result", None)
        return validation

    def list_perception_cache(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.perception_runtime.cache_entries(limit=limit)

    def _focus(self, topology: dict[str, Any], kind: str, value: str) -> dict[str, Any]:
        target_ids: list[str] = []
        for obj in topology.get("objects", []):
            matches = obj.get("business_id") == value or obj.get("label") == value
            if not matches:
                continue
            target_ids.append(obj["business_id"])
            if kind == "user" and obj.get("connected_ap"):
                target_ids.append(obj["connected_ap"])
        return {
            "kind": kind,
            "value": value,
            "target_ids": list(dict.fromkeys(target_ids)),
        }

    def _attach_perception(
        self,
        topology: dict[str, Any],
        result: dict[str, Any],
        focus: dict[str, Any],
    ) -> dict[str, Any]:
        perception = result["perception"]
        topology["focus"] = focus
        if focus.get("kind") == "user":
            topology["focused_user"] = focus.get("value")
        if focus.get("kind") == "ap":
            topology["focused_ap"] = focus.get("value")
        topology["raw_scenes"] = perception["raw_scenes"]
        topology["ui_perception_candidates"] = perception["candidates"]
        topology["ui_perception"] = perception["scene"]
        topology["perception_decision"] = perception["decision"]
        topology["perception_meta"] = result["meta"]
        topology["topology_changes"] = result["changes"]
        return topology

    def query_user_experience(self, user: str, time_range: str) -> dict[str, Any]:
        data = self._read_json("mock_user_experience.json")
        data["user"] = user
        data["time_range"] = time_range
        return data

    def query_associated_device(self, user: str, time_range: str) -> dict[str, Any]:
        data = self._read_json("mock_associated_device.json")
        data["user"] = user
        data["time_range"] = time_range
        return data

    def query_radio_metrics(self, ap_id: str) -> dict[str, Any]:
        data = self._read_json("mock_radio_metrics.json")
        data["ap_id"] = ap_id
        return data

    def query_negative_checks(self, user: str, time_range: str) -> dict[str, Any]:
        data = self._read_json("mock_negative_checks.json")
        data["user"] = user
        data["time_range"] = time_range
        return data

    def query_ap_status(self, ap_id: str, time_range: str) -> dict[str, Any]:
        data = self._read_json("mock_ap_status.json")
        data["ap_id"] = ap_id
        data["time_range"] = time_range
        return data

    def query_switch_port(self, ap_id: str) -> dict[str, Any]:
        data = self._read_json("mock_switch_port.json")
        data["ap_id"] = ap_id
        return data

    def restart_poe_port(self, switch_name: str, port: str, ap_id: str) -> dict[str, Any]:
        return {
            "ap_id": ap_id,
            "switch_name": switch_name,
            "port": port,
            "action": "restart_poe",
            "status": "success",
            "message": f"已对 {switch_name} {port} 执行 PoE 重启",
        }

    def verify_ap_online(self, ap_id: str) -> dict[str, Any]:
        return {
            "ap_id": ap_id,
            "status": "online",
            "heartbeat": "normal",
            "summary": "AP 已恢复在线，心跳正常，交换机端口 PoE 状态恢复正常",
        }

    def generate_rf_strategy(self, ap_id: str) -> dict[str, Any]:
        strategy = self._read_json("mock_rf_strategy.json")
        strategy["target_ap_id"] = ap_id
        return strategy

    def dispatch_rf_strategy(self, strategy_id: str) -> dict[str, Any]:
        return {
            "strategy_id": strategy_id,
            "dispatch_status": "success",
            "message": "策略已下发到站点1/1F AP 射频控制器",
        }

    def verify_user_recovery(self, user: str) -> dict[str, Any]:
        return {
            "user": user,
            "experience_score": "normal",
            "throughput": "normal",
            "retransmission_rate": "normal",
            "summary": "用户体验评分恢复正常，重传率下降，吞吐恢复正常",
        }
