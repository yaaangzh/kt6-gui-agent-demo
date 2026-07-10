from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from typing import Any

from .perception import HybridPerception
from .scene_store import InMemorySceneStore, SceneSnapshot, SceneStore
from .topology_change_detector import TopologyChangeDetector


class PerceptionRuntime:
    SCHEMA_VERSION = "scene-graph-v2"

    def __init__(
        self,
        perception: HybridPerception | None = None,
        store: SceneStore | None = None,
        detector: TopologyChangeDetector | None = None,
    ):
        self.perception = perception or HybridPerception()
        self.store = store or InMemorySceneStore()
        self.detector = detector or TopologyChangeDetector()
        self._lock = threading.RLock()

    def resolve(self, topology: dict[str, Any], focus: dict[str, Any] | None = None) -> dict[str, Any]:
        started_at = time.perf_counter()
        base_topology = self._base_topology(topology)
        template_hash = self._template_hash(base_topology)
        content_hash = self._content_hash(base_topology)
        scene_key = self._scene_key(base_topology, template_hash)

        with self._lock:
            latest = self.store.get_latest(scene_key)
            if latest and latest.template_hash == template_hash and latest.content_hash == content_hash:
                perception = copy.deepcopy(latest.perception)
                revision = latest.revision
                changes = self.detector.empty()
                cache_status = "hit"
                cache_age_ms = max(0, int((time.time() - latest.created_at) * 1000))
            else:
                perception = self.perception.perceive_topology(base_topology, "")
                revision = latest.revision + 1 if latest else 1
                changes = (
                    self.detector.diff(latest.perception["scene"], perception["scene"])
                    if latest and latest.template_hash == template_hash
                    else self.detector.empty()
                )
                cache_status = "incremental" if latest else "miss"
                cache_age_ms = 0
                self._decorate_scene(
                    perception,
                    scene_key=scene_key,
                    revision=revision,
                    template_hash=template_hash,
                    content_hash=content_hash,
                )
                self.store.save_snapshot(
                    SceneSnapshot(
                        scene_key=scene_key,
                        revision=revision,
                        template_hash=template_hash,
                        content_hash=content_hash,
                        perception=perception,
                        created_at=time.time(),
                    )
                )
                if latest:
                    self.store.save_change(scene_key, latest.revision, revision, changes)

        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
        meta = {
            "scene_key": scene_key,
            "scene_revision": revision,
            "previous_revision": latest.revision if latest else None,
            "template_hash": template_hash,
            "content_hash": content_hash,
            "source_revision": base_topology.get("topology_revision", content_hash[:12]),
            "schema_version": self.SCHEMA_VERSION,
            "cache_status": cache_status,
            "cache_age_ms": cache_age_ms,
            "perception_ms": elapsed_ms,
            "validated": cache_status == "hit",
            "change_summary": changes["summary"],
        }
        return {
            "perception": copy.deepcopy(perception),
            "meta": meta,
            "changes": copy.deepcopy(changes),
            "focus": copy.deepcopy(focus or {}),
        }

    def validate(self, topology: dict[str, Any], scene_ref: dict[str, Any]) -> dict[str, Any]:
        result = self.resolve(topology)
        current_meta = result["meta"]
        expected_key = scene_ref.get("scene_key")
        expected_revision = int(scene_ref.get("revision", 0))
        target_ids = set(scene_ref.get("target_ids", []))

        if expected_key != current_meta["scene_key"]:
            return {
                "valid": False,
                "rebased": False,
                "reason": "interface_template_changed",
                "changes": result["changes"],
                "result": result,
            }

        current_revision = current_meta["scene_revision"]
        if expected_revision == current_revision:
            return {
                "valid": True,
                "rebased": False,
                "reason": "scene_revision_current",
                "changes": self.detector.empty(),
                "result": result,
            }

        history = self.store.list_changes(expected_key, expected_revision)
        changes = self.detector.merge([item["changes"] for item in history])
        blocking_targets = target_ids & set(changes.get("blocking_business_ids", []))
        rebind_targets = target_ids & set(changes.get("rebind_business_ids", []))

        if blocking_targets:
            return {
                "valid": False,
                "rebased": False,
                "reason": "target_topology_changed",
                "blocking_targets": sorted(blocking_targets),
                "changes": changes,
                "result": result,
            }
        return {
            "valid": True,
            "rebased": bool(rebind_targets),
            "reason": "target_rebound" if rebind_targets else "unrelated_topology_changed",
            "changes": changes,
            "result": result,
        }

    def cache_entries(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.list_latest(limit=limit)

    def _decorate_scene(
        self,
        perception: dict[str, Any],
        scene_key: str,
        revision: int,
        template_hash: str,
        content_hash: str,
    ) -> None:
        perception["scene"].update(
            {
                "scene_key": scene_key,
                "scene_revision": revision,
                "template_hash": template_hash,
                "content_hash": content_hash,
                "schema_version": self.SCHEMA_VERSION,
            }
        )

    def _base_topology(self, topology: dict[str, Any]) -> dict[str, Any]:
        base = copy.deepcopy(topology)
        for key in (
            "focused_user",
            "focused_ap",
            "focus",
            "raw_scenes",
            "ui_perception_candidates",
            "ui_perception",
            "perception_decision",
            "perception_meta",
            "topology_changes",
        ):
            base.pop(key, None)
        return base

    def _scene_key(self, topology: dict[str, Any], template_hash: str) -> str:
        site = str(topology.get("site", "unknown")).replace(":", "_")
        floor = str(topology.get("floor", "unknown")).replace(":", "_")
        return f"wireless-topology:{site}:{floor}:{template_hash[:12]}"

    def _template_hash(self, topology: dict[str, Any]) -> str:
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "ui_version": topology.get("ui_version", "unknown"),
            "site": topology.get("site"),
            "floor": topology.get("floor"),
            "scene": topology.get("scene"),
            "canvas": topology.get("canvas", {}),
            "perception_strategy": self.perception.preferred_mode,
        }
        return self._hash(payload)

    def _content_hash(self, topology: dict[str, Any]) -> str:
        payload = {
            "objects": sorted(topology.get("objects", []), key=lambda item: item.get("business_id", "")),
            "links": sorted(topology.get("links", []), key=self._relation_sort_key),
            "co_channel_relations": sorted(
                topology.get("co_channel_relations", []), key=self._relation_sort_key
            ),
            "visual_grounding": topology.get("visual_grounding", {}),
        }
        return self._hash(payload)

    def _relation_sort_key(self, relation: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(relation.get("source", "")),
            str(relation.get("target", "")),
            str(relation.get("type", relation.get("channel", ""))),
        )

    def _hash(self, payload: dict[str, Any]) -> str:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
