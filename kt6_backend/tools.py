from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .perception import HybridPerception


class MockBusinessTools:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.perception = HybridPerception()

    def _read_json(self, name: str) -> dict[str, Any]:
        return json.loads((self.data_dir / name).read_text(encoding="utf-8"))

    def query_topology(self, user: str) -> dict[str, Any]:
        topology = self._read_json("mock_topology.json")
        topology["focused_user"] = user
        perception_result = self.perception.perceive_topology(topology, user)
        topology["raw_scenes"] = perception_result["raw_scenes"]
        topology["ui_perception_candidates"] = perception_result["candidates"]
        topology["ui_perception"] = perception_result["scene"]
        topology["perception_decision"] = perception_result["decision"]
        return topology

    def query_ap_topology(self, ap_id: str) -> dict[str, Any]:
        topology = self._read_json("mock_topology.json")
        topology["focused_ap"] = ap_id
        perception_result = self.perception.perceive_topology(topology, ap_id)
        topology["raw_scenes"] = perception_result["raw_scenes"]
        topology["ui_perception_candidates"] = perception_result["candidates"]
        topology["ui_perception"] = perception_result["scene"]
        topology["perception_decision"] = perception_result["decision"]
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
