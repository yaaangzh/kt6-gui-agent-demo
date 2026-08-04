from __future__ import annotations

import copy
from typing import Any

from .topology_cv_routing import (
    TopologyCVRoutingError,
    assess_cv_result,
    prepare_cv_payload_for_route,
)
from .topology_fusion import TopologyFusionError, fuse_topology_payloads
from .vision_recognition import CanvasFrame, CanvasVisionAdapter


class HybridCanvasVisionError(RuntimeError):
    """Both hybrid perception branches failed to produce a usable result."""


class HybridCanvasVisionAdapter:
    """Combine local CV grounding with a multimodal topology interpretation."""

    adapter_id = "hybrid-local-cv-multimodal"
    adapter_version = "1.0"
    supports_actionable_grounding = False
    requested_profile = "auto"

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
        routing, local_result_usable = self._assess_local_result(
            local_result,
            local_failed=local_failed,
        )
        if not local_result_usable:
            local_result = None

        trusted_cv_only = (
            routing.get("decision") == "cv_only"
            and routing.get("metrics", {}).get("trusted_adapter") is True
        )
        if trusted_cv_only and local_result is not None:
            prepared_local, disputed_links = prepare_cv_payload_for_route(
                local_result,
                routing,
            )
            result = self._local_only_result(prepared_local)
            return self._with_vision_routing(
                result,
                routing,
                execution_status="completed_without_model",
                disputed_links=disputed_links,
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
                return self._with_vision_routing(
                    self._local_only_result(local_result),
                    self._routing_with_reason(
                        routing,
                        "model_fusion_failed_degraded_to_local_cv",
                    ),
                    execution_status="model_completed_fusion_failed",
                )
            result = copy.deepcopy(fused["result"])
            result["fusion_summary"] = copy.deepcopy(fused["summary"])
            result["fusion_analysis"] = self._fusion_analysis(fused)
            return self._with_vision_routing(
                result,
                routing,
                execution_status="model_completed",
            )

        if local_result is not None:
            reason = (
                "model_branch_failed"
                if model_failed
                else "model_branch_returned_no_result"
            )
            return self._with_vision_routing(
                self._local_only_result(local_result),
                self._routing_with_reason(routing, reason),
                execution_status=(
                    "model_failed" if model_failed else "model_returned_no_result"
                ),
            )
        if model_result is not None:
            return self._with_vision_routing(
                self._model_only_result(model_result),
                routing,
                execution_status="model_completed",
            )
        if local_failed or model_failed:
            raise HybridCanvasVisionError(
                "local CV and multimodal vision did not produce a usable result"
            )
        return None

    def _assess_local_result(
        self,
        local_result: dict[str, Any] | None,
        *,
        local_failed: bool,
    ) -> tuple[dict[str, Any], bool]:
        provenance = {
            "adapter_id": str(getattr(self.local_adapter, "adapter_id", "")),
            "adapter_version": str(
                getattr(self.local_adapter, "adapter_version", "")
            ),
        }
        local_result_usable = local_result is not None
        try:
            routing = assess_cv_result(
                local_result or {"objects": [], "links": []},
                requested_profile=self.requested_profile,
                trusted_provenance=provenance,
            )
        except (TopologyCVRoutingError, TypeError, ValueError):
            # Invalid CV output is not safe fusion context. Route from an empty,
            # bounded artifact and let the model inspect the original pixels.
            routing = assess_cv_result(
                {"objects": [], "links": []},
                requested_profile=self.requested_profile,
                trusted_provenance=provenance,
            )
            routing = self._routing_with_reason(
                routing,
                "local_cv_routing_validation_failed",
            )
            local_result_usable = False

        if local_failed:
            routing = self._routing_with_reason(
                routing,
                "local_cv_branch_failed",
            )
        elif local_result is None:
            routing = self._routing_with_reason(
                routing,
                "local_cv_branch_returned_no_result",
            )

        # assess_cv_result already requires trusted provenance for cv_only. Keep
        # the guard explicit so future policy changes cannot bypass the model.
        if (
            routing.get("decision") == "cv_only"
            and routing.get("metrics", {}).get("trusted_adapter") is not True
        ):
            routing = copy.deepcopy(routing)
            routing.update(
                {
                    "decision": "model_assist",
                    "requirement_satisfied": False,
                    "result_status": "pending_model",
                    "model_invoked": True,
                }
            )
            routing = self._routing_with_reason(
                routing,
                "cv_only_rejected_for_untrusted_adapter",
            )
        return routing, local_result_usable

    @staticmethod
    def _routing_with_reason(
        routing: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        updated = copy.deepcopy(routing)
        reasons = updated.get("reason_codes", [])
        if not isinstance(reasons, list):
            reasons = []
        updated["reason_codes"] = list(dict.fromkeys([*reasons, reason]))
        return updated

    @staticmethod
    def _with_vision_routing(
        result: dict[str, Any],
        routing: dict[str, Any],
        *,
        execution_status: str,
        disputed_links: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        routed_result = copy.deepcopy(result)
        audit = copy.deepcopy(routing)
        audit["execution_status"] = execution_status
        if disputed_links:
            audit["disputed_links"] = copy.deepcopy(disputed_links)
        routed_result["vision_routing"] = audit
        return routed_result

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
                "node_coordinate_mappings",
                "grounded_graph",
                "display_graph",
                "semantic_graph",
                "display_only_links",
                "disputed_links",
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
