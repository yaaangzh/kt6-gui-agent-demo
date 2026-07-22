from __future__ import annotations

import copy
from typing import Any

from .topology_fusion import TopologyFusionError, fuse_topology_payloads
from .vision_recognition import CanvasFrame, CanvasVisionAdapter


class HybridCanvasVisionError(RuntimeError):
    """Both hybrid perception branches failed to produce a usable result."""


class HybridCanvasVisionAdapter:
    """Combine local CV grounding with a multimodal topology interpretation."""

    adapter_id = "hybrid-local-cv-multimodal"
    adapter_version = "1.0"
    supports_actionable_grounding = False

    def __init__(
        self,
        *,
        local_adapter: CanvasVisionAdapter,
        model_adapter: CanvasVisionAdapter,
    ) -> None:
        if local_adapter is model_adapter:
            raise ValueError("hybrid vision adapters must be distinct")
        self.local_adapter = local_adapter
        self.model_adapter = model_adapter

    def recognize(
        self,
        *,
        page: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
    ) -> dict[str, Any] | None:
        local_result, local_failed = self._recognize_branch(
            self.local_adapter, page=page, frames=frames
        )
        model_result, model_failed = self._recognize_model_branch(
            self.model_adapter,
            page=page,
            frames=frames,
            cv_observations=local_result,
        )

        if local_result is not None and model_result is not None:
            try:
                fused = fuse_topology_payloads(local_result, model_result)
            except TopologyFusionError:
                # Geometry is the safer fallback when model output cannot be aligned.
                return self._local_only_result(local_result)
            result = copy.deepcopy(fused["result"])
            result["fusion_summary"] = copy.deepcopy(fused["summary"])
            result["fusion_analysis"] = self._fusion_analysis(fused)
            return result

        if local_result is not None:
            return self._local_only_result(local_result)
        if model_result is not None:
            return self._model_only_result(model_result)
        if local_failed or model_failed:
            raise HybridCanvasVisionError(
                "local CV and multimodal vision did not produce a usable result"
            )
        return None

    @staticmethod
    def _recognize_branch(
        adapter: CanvasVisionAdapter,
        *,
        page: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
    ) -> tuple[dict[str, Any] | None, bool]:
        try:
            result = adapter.recognize(page=page, frames=frames)
        except Exception:
            return None, True
        if result is None:
            return None, False
        if not isinstance(result, dict):
            return None, True
        return result, False

    @staticmethod
    def _recognize_model_branch(
        adapter: CanvasVisionAdapter,
        *,
        page: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
        cv_observations: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, bool]:
        try:
            contextual_recognize = getattr(adapter, "recognize_with_context", None)
            if cv_observations is not None and callable(contextual_recognize):
                result = contextual_recognize(
                    page=page,
                    frames=frames,
                    cv_observations=cv_observations,
                )
            else:
                result = adapter.recognize(page=page, frames=frames)
        except Exception:
            return None, True
        if result is None:
            return None, False
        if not isinstance(result, dict):
            return None, True
        return result, False

    @staticmethod
    def _fusion_analysis(fused: dict[str, Any]) -> dict[str, Any]:
        return {
            name: copy.deepcopy(fused.get(name, []))
            for name in (
                "structure_templates",
                "rejected_links",
                "unlocated_objects",
                "unresolved_links",
            )
        }

    @staticmethod
    def _local_only_result(local_result: dict[str, Any]) -> dict[str, Any]:
        try:
            fused = fuse_topology_payloads(
                local_result,
                {"topology": {"nodes": [], "edges": []}},
            )
        except TopologyFusionError:
            return copy.deepcopy(local_result)
        result = copy.deepcopy(fused["result"])
        result["fusion_summary"] = copy.deepcopy(fused["summary"])
        result["fusion_analysis"] = HybridCanvasVisionAdapter._fusion_analysis(fused)
        result["fusion_summary"]["degraded_to"] = "local_cv"
        return result

    @staticmethod
    def _model_only_result(model_result: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(model_result)
        objects = result.get("objects", [])
        links = result.get("links", result.get("relations", []))
        if isinstance(objects, list):
            for item in objects:
                if not isinstance(item, dict):
                    continue
                attributes = item.setdefault("attributes", {})
                if isinstance(attributes, dict):
                    attributes.update(
                        {
                            "fusion_status": "model_only",
                            "evidence_sources": ["multimodal_model"],
                        }
                    )
        if isinstance(links, list):
            for item in links:
                if not isinstance(item, dict):
                    continue
                attributes = item.setdefault("attributes", {})
                if isinstance(attributes, dict):
                    attributes.update(
                        {
                            "fusion_status": "model_only",
                            "evidence_sources": ["multimodal_model"],
                        }
                    )
        result["fusion_summary"] = {
            "degraded_to": "multimodal_model",
            "model_object_count": len(objects) if isinstance(objects, list) else 0,
            "model_link_count": len(links) if isinstance(links, list) else 0,
        }
        return result


__all__ = ["HybridCanvasVisionAdapter", "HybridCanvasVisionError"]
