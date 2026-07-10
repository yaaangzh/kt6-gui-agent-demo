from __future__ import annotations

from typing import Any, Callable

from .tools import MockBusinessTools


ToolFn = Callable[..., Any]


class ToolRegistry:
    def __init__(self, tools: MockBusinessTools):
        self._tools = tools
        self._registry: dict[str, ToolFn] = {
            "topology.query_user_location": self._tools.query_topology,
            "topology.query_ap_location": self._tools.query_ap_topology,
            "topology.validate_scene": self._tools.validate_scene,
            "topology.list_perception_cache": self._tools.list_perception_cache,
            "experience.query_user_metrics": self._tools.query_user_experience,
            "wireless.query_associated_ap": self._tools.query_associated_device,
            "wireless.query_ap_status": self._tools.query_ap_status,
            "wireless.query_switch_port": self._tools.query_switch_port,
            "wireless.restart_poe_port": self._tools.restart_poe_port,
            "wireless.verify_ap_online": self._tools.verify_ap_online,
            "radio.query_metrics": self._tools.query_radio_metrics,
            "network.query_negative_checks": self._tools.query_negative_checks,
            "rf_optimization.generate_strategy": self._tools.generate_rf_strategy,
            "rf_optimization.dispatch_strategy": self._tools.dispatch_rf_strategy,
            "experience.verify_user_recovery": self._tools.verify_user_recovery,
        }

    def call(self, name: str, **kwargs: Any) -> Any:
        if name not in self._registry:
            raise KeyError(f"Tool is not registered: {name}")
        return self._registry[name](**kwargs)

    def list_tools(self) -> list[str]:
        return sorted(self._registry)
