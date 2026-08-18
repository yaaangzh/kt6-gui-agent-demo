from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any


ACTION_PLAN_SCHEMA_VERSION = "kt6.action-plan.v1"


class PlanGenerationError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class FixturePlanGenerator:
    """Expand fixture intents into the stable Runner contract."""

    def generate(
        self,
        *,
        start_url: str,
        user_request: str,
        intents: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        asset_ids = {
            str(intent.get("asset_id", "")).strip()
            for intent in intents
            if str(intent.get("asset_id", "")).strip()
        }
        if len(asset_ids) != 1:
            raise PlanGenerationError("execution_plan_requires_one_asset")
        asset_id = next(iter(asset_ids))
        steps: list[dict[str, Any]] = []
        for intent in intents:
            name = str(intent.get("intent", "")).strip()
            current_asset = str(intent.get("asset_id") or asset_id).strip()
            if name == "open_asset_details":
                steps.extend(
                    [
                        {
                            "op": "click",
                            "target": {
                                "asset_id": current_asset,
                                "action": "open_asset_details",
                            },
                        },
                        {
                            "op": "verify",
                            "expected": {
                                "type": "asset_detail_visible",
                                "asset_id": current_asset,
                            },
                        },
                    ]
                )
            elif name == "open_topology":
                steps.extend(
                    [
                        {
                            "op": "click",
                            "target": {
                                "asset_id": asset_id,
                                "action": "open_topology",
                                "text": "拓扑",
                            },
                        },
                        {
                            "op": "wait",
                            "timeout_ms": 5_000,
                            "expected": {
                                "type": "page_ready",
                                "page": "topology",
                                "asset_id": asset_id,
                            },
                        },
                    ]
                )
            elif name == "select_canvas_asset":
                steps.extend(
                    [
                        {
                            "op": "click",
                            "target": {
                                "source": "canvas",
                                "asset_id": current_asset,
                                "action": "select_canvas_asset",
                                "name": current_asset.upper(),
                            },
                        },
                        {
                            "op": "verify",
                            "expected": {
                                "type": "canvas_asset_selected",
                                "asset_id": current_asset,
                            },
                        },
                    ]
                )
            else:
                raise PlanGenerationError("execution_intent_unsupported")
        if not steps:
            raise PlanGenerationError("execution_plan_empty")
        for index, step in enumerate(steps, start=1):
            step["id"] = f"step-{index}"
        digest = hashlib.sha256(
            f"{start_url}\n{user_request}".encode("utf-8")
        ).hexdigest()[:16]
        return {
            "schema_version": ACTION_PLAN_SCHEMA_VERSION,
            "scenario_id": f"scenario-{digest}",
            "start_url": start_url,
            "user_request": user_request,
            "steps": steps,
        }


__all__ = [
    "ACTION_PLAN_SCHEMA_VERSION",
    "FixturePlanGenerator",
    "PlanGenerationError",
]
