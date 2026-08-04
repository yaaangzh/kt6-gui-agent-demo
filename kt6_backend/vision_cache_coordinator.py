from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .vision_frame_matcher import (
    FrameMatchResult,
    match_vision_frames,
    remap_vision_result,
)
from .vision_recognition import CanvasFrame, CanvasVisionAdapter
from .vision_result_cache import SQLiteVisionResultCacheStore


@dataclass
class _InFlightRecognition:
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: BaseException | None = None


class VisionCacheCoordinator:
    """Reuse completed pixel recognition before invoking a vision adapter.

    Exact screenshot reuse is persistent. Concurrent identical requests are
    coalesced in-process. A previous result may be geometrically reprojected
    only when a conservative matcher proves that the pixels differ by scale
    and translation alone. Every reused result remains analysis-only.
    """

    CACHE_SCHEMA_VERSION = "kt6.vision-cache-entry.v1"
    PIPELINE_VERSION = "kt6.vision-cache-pipeline.v1"
    MAX_FRAME_BYTES = 5 * 1024 * 1024
    MAX_CANDIDATES = 20

    def __init__(
        self,
        store: SQLiteVisionResultCacheStore,
        *,
        asset_root: Path,
        frame_matcher: Callable[
            [str | Path, str | Path, tuple[int, int], tuple[int, int]],
            FrameMatchResult,
        ] = match_vision_frames,
        wait_timeout_seconds: float = 330.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.asset_root = Path(asset_root).resolve()
        self.frame_matcher = frame_matcher
        self.wait_timeout_seconds = self._positive_finite(
            wait_timeout_seconds, "wait_timeout_seconds"
        )
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._clock = clock
        self._lock = threading.RLock()
        self._inflight: dict[str, _InFlightRecognition] = {}

    def recognize(
        self,
        *,
        adapter: CanvasVisionAdapter,
        page: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
        force_refresh: bool = False,
    ) -> dict[str, Any] | None:
        if not frames:
            return adapter.recognize(page=page, frames=frames)

        context = self._cache_context(adapter, page, frames)
        if force_refresh:
            return self._recognize_and_store(adapter, page, frames, context)

        hit = self._safe_get(context["cache_key"])
        if hit is not None:
            restored = self._restore_exact(hit, context)
            if restored is not None:
                return restored

        with self._lock:
            pending = self._inflight.get(context["cache_key"])
            if pending is None:
                pending = _InFlightRecognition()
                self._inflight[context["cache_key"]] = pending
                leader = True
            else:
                leader = False

        if not leader:
            if not pending.event.wait(self.wait_timeout_seconds):
                raise TimeoutError(
                    "timed out waiting for identical vision recognition"
                )
            if pending.error is not None:
                raise RuntimeError(
                    "identical vision recognition failed"
                ) from pending.error
            if pending.result is None:
                return None
            return self._annotate(
                pending.result,
                status="coalesced",
                cache_key=context["cache_key"],
                semantic_change_check="identical_in_flight_request",
                adapter_invocation_avoided=True,
                model_invocation_avoided=True,
            )

        try:
            # Close the race between the first persistent lookup and becoming
            # this process's leader.
            second_hit = self._safe_get(context["cache_key"])
            if second_hit is not None:
                restored = self._restore_exact(second_hit, context)
                if restored is not None:
                    pending.result = copy.deepcopy(restored)
                    return restored

            reprojected = self._try_reproject(context, frames)
            if reprojected is not None:
                pending.result = copy.deepcopy(reprojected)
                return reprojected

            result = self._recognize_and_store(adapter, page, frames, context)
            pending.result = copy.deepcopy(result)
            return result
        except BaseException as exc:
            pending.error = exc
            raise
        finally:
            pending.event.set()
            with self._lock:
                self._inflight.pop(context["cache_key"], None)

    def _recognize_and_store(
        self,
        adapter: CanvasVisionAdapter,
        page: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        recognized = adapter.recognize(
            page=copy.deepcopy(page),
            frames=frames,
        )
        if recognized is None:
            return None
        if not isinstance(recognized, dict):
            return recognized

        raw = copy.deepcopy(recognized)
        raw.pop("vision_cache", None)
        envelope = {
            "schema_version": self.CACHE_SCHEMA_VERSION,
            "frames": copy.deepcopy(context["frames"]),
            "recognized": raw,
            "reprojection_depth": 0,
        }
        self._safe_put(context, envelope)
        return self._annotate(
            raw,
            status="miss",
            cache_key=context["cache_key"],
            semantic_change_check="cache_miss",
            adapter_invocation_avoided=False,
            model_invocation_avoided=False,
        )

    def _restore_exact(
        self,
        entry: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        envelope = self._valid_envelope(entry.get("result"))
        if envelope is None:
            return None
        age_seconds = max(0.0, self._now() - float(entry.get("created_at", 0)))
        return self._annotate(
            envelope["recognized"],
            status="exact_hit",
            cache_key=context["cache_key"],
            semantic_change_check="exact_sha256",
            adapter_invocation_avoided=True,
            model_invocation_avoided=True,
            extra={"age_seconds": round(age_seconds, 3)},
        )

    def _try_reproject(
        self,
        context: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
    ) -> dict[str, Any] | None:
        if len(frames) != 1:
            return None
        current = context["frames"][0]
        if not self._verified_frame_path(current):
            return None
        try:
            candidates = self.store.list_recent_candidates(
                page_route=context["page_route"],
                adapter_id=context["adapter_id"],
                adapter_version=context["adapter_version"],
                limit=self.MAX_CANDIDATES,
            )
        except Exception:
            return None

        for candidate in candidates:
            envelope = self._valid_envelope(candidate.get("result"))
            if envelope is None or envelope.get("reprojection_depth") != 0:
                continue
            old_frames = envelope.get("frames")
            if not isinstance(old_frames, list) or len(old_frames) != 1:
                continue
            old = old_frames[0]
            if not isinstance(old, dict) or not self._compatible_regions(old, current):
                continue
            if not self._verified_frame_path(old):
                continue
            try:
                match = self.frame_matcher(
                    old["screenshot_path"],
                    current["screenshot_path"],
                    (int(old["width"]), int(old["height"])),
                    (int(current["width"]), int(current["height"])),
                )
            except Exception:
                continue
            if not isinstance(match, FrameMatchResult) or not match.safe_to_reuse:
                continue
            try:
                transformed = remap_vision_result(envelope["recognized"], match)
                transformed = self._rebind_canvas_ids(
                    transformed,
                    str(old.get("canvas_id", "")),
                    str(current.get("canvas_id", "")),
                )
                self._validate_reprojected(transformed, frames[0])
            except (TypeError, ValueError, OverflowError):
                continue

            cached = copy.deepcopy(transformed)
            cached.pop("vision_cache", None)
            new_envelope = {
                "schema_version": self.CACHE_SCHEMA_VERSION,
                "frames": copy.deepcopy(context["frames"]),
                "recognized": cached,
                "reprojection_depth": 1,
            }
            self._safe_put(context, new_envelope)
            return self._annotate(
                cached,
                status="reprojected",
                cache_key=context["cache_key"],
                semantic_change_check="conservative_scale_translation_match",
                adapter_invocation_avoided=True,
                model_invocation_avoided=True,
                extra={
                    "source_cache_key": str(candidate.get("cache_key", ""))[:24],
                    "transform": match.to_dict(),
                },
            )
        return None

    def _cache_context(
        self,
        adapter: CanvasVisionAdapter,
        page: Mapping[str, Any],
        frames: tuple[CanvasFrame, ...],
    ) -> dict[str, Any]:
        page_url = str(page.get("url", "")).strip() or "about:blank"
        ui_version = str(page.get("ui_version", "")).strip()
        page_route = page_url[:4096]
        adapter_id = str(getattr(adapter, "adapter_id", "")).strip()
        adapter_version = str(getattr(adapter, "adapter_version", "")).strip()
        if not adapter_id or not adapter_version:
            raise ValueError("CanvasVisionAdapter id and version are required")
        fingerprint = self._adapter_fingerprint(adapter)
        selector_version = f"{adapter_version}+{fingerprint[:20]}"[:100]
        descriptors = [self._frame_descriptor(frame) for frame in frames]
        key_payload = {
            "pipeline": self.PIPELINE_VERSION,
            "page_url": page_url,
            "ui_version": ui_version,
            "adapter_fingerprint": fingerprint,
            "frames": [self._frame_identity(item) for item in descriptors],
        }
        cache_key = "sha256:" + hashlib.sha256(
            self._canonical_json(key_payload).encode("utf-8")
        ).hexdigest()
        return {
            "cache_key": cache_key,
            "page_route": page_route,
            "adapter_id": adapter_id[:200],
            "adapter_version": selector_version,
            "frames": descriptors,
        }

    def _adapter_fingerprint(self, adapter: CanvasVisionAdapter) -> str:
        def describe(value: Any, depth: int = 0) -> dict[str, Any]:
            item: dict[str, Any] = {
                "class": f"{type(value).__module__}.{type(value).__qualname__}",
                "adapter_id": str(getattr(value, "adapter_id", "")),
                "adapter_version": str(getattr(value, "adapter_version", "")),
            }
            if depth < 2:
                for name in ("local_adapter", "model_adapter"):
                    nested = getattr(value, name, None)
                    if nested is not None:
                        item[name] = describe(nested, depth + 1)
            for name in (
                "requested_profile",
                "endpoint",
                "executable",
                "agent",
                "timeout_seconds",
            ):
                setting = getattr(value, name, None)
                if setting is not None:
                    # Operational values can contain hostnames or paths. Only
                    # their digest participates in the cache selector.
                    item[f"{name}_sha256"] = hashlib.sha256(
                        str(setting).encode("utf-8")
                    ).hexdigest()
            return item

        return hashlib.sha256(
            self._canonical_json(describe(adapter)).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _frame_descriptor(frame: CanvasFrame) -> dict[str, Any]:
        return {
            "canvas_id": str(frame.canvas_id),
            "screenshot_path": str(Path(frame.screenshot_path).resolve()),
            "screenshot_sha256": str(frame.screenshot_sha256).lower(),
            "mime_type": str(frame.mime_type),
            "width": int(frame.width),
            "height": int(frame.height),
            "source_kind": str(frame.source_kind),
            "source_type": str(frame.source_type),
            "capture_kind": str(frame.capture_kind),
            "source_ref": str(frame.source_ref),
            "source_canvas_id": str(frame.source_canvas_id),
            "region_selector": str(frame.region_selector),
            "frame_url": str(frame.frame_url),
            "roi_status": str(frame.roi_status),
        }

    @staticmethod
    def _frame_identity(frame: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: frame.get(key)
            for key in (
                "canvas_id",
                "screenshot_sha256",
                "mime_type",
                "width",
                "height",
                "source_kind",
                "source_type",
                "capture_kind",
                "source_ref",
                "source_canvas_id",
                "region_selector",
                "frame_url",
                "roi_status",
            )
        }

    def _verified_frame_path(self, frame: Mapping[str, Any]) -> bool:
        try:
            path = Path(str(frame.get("screenshot_path", ""))).resolve(strict=True)
            path.relative_to(self.asset_root)
            size = path.stat().st_size
            expected = str(frame.get("screenshot_sha256", "")).strip().lower()
            if size <= 0 or size > self.MAX_FRAME_BYTES or len(expected) != 64:
                return False
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest() == expected
        except (OSError, RuntimeError, ValueError):
            return False

    @staticmethod
    def _compatible_regions(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
        for key in (
            "source_kind",
            "source_type",
            "capture_kind",
            "source_ref",
            "source_canvas_id",
            "region_selector",
            "frame_url",
            "roi_status",
        ):
            old_value = str(old.get(key, "")).strip()
            new_value = str(new.get(key, "")).strip()
            if old_value and new_value and old_value != new_value:
                return False
        return True

    @classmethod
    def _valid_envelope(cls, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        if value.get("schema_version") != cls.CACHE_SCHEMA_VERSION:
            return None
        frames = value.get("frames")
        recognized = value.get("recognized")
        depth = value.get("reprojection_depth")
        if not isinstance(frames, list) or not isinstance(recognized, dict):
            return None
        if depth not in {0, 1}:
            return None
        return value

    @staticmethod
    def _validate_reprojected(
        result: Mapping[str, Any], frame: CanvasFrame
    ) -> None:
        objects = result.get("objects")
        if not isinstance(objects, list) or not objects:
            raise ValueError("reprojected result requires objects")
        for item in objects:
            if not isinstance(item, dict):
                raise ValueError("reprojected object is invalid")
            bbox = item.get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4:
                raise ValueError("reprojected object requires bbox")
            try:
                x, y, width, height = (float(value) for value in bbox)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("reprojected bbox is invalid") from exc
            if (
                not all(math.isfinite(value) for value in (x, y, width, height))
                or width <= 0
                or height <= 0
                or x < 0
                or y < 0
                or x + width > frame.width + 1e-6
                or y + height > frame.height + 1e-6
            ):
                raise ValueError("reprojected bbox is outside the current frame")

    @classmethod
    def _rebind_canvas_ids(cls, value: Any, old_id: str, new_id: str) -> Any:
        if isinstance(value, list):
            return [cls._rebind_canvas_ids(item, old_id, new_id) for item in value]
        if not isinstance(value, dict):
            return value
        rebound: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"canvas_id", "canvas_ref", "source_canvas_id"} and (
                not old_id or str(item) == old_id
            ):
                rebound[key] = new_id
            else:
                rebound[key] = cls._rebind_canvas_ids(item, old_id, new_id)
        return rebound

    def _safe_get(self, cache_key: str) -> dict[str, Any] | None:
        try:
            return self.store.get(cache_key)
        except Exception:
            return None

    def _safe_put(self, context: Mapping[str, Any], result: dict[str, Any]) -> None:
        try:
            self.store.put(
                cache_key=str(context["cache_key"]),
                page_route=str(context["page_route"]),
                adapter_id=str(context["adapter_id"]),
                adapter_version=str(context["adapter_version"]),
                result=result,
            )
        except Exception:
            return

    @staticmethod
    def _annotate(
        result: Mapping[str, Any],
        *,
        status: str,
        cache_key: str,
        semantic_change_check: str,
        adapter_invocation_avoided: bool,
        model_invocation_avoided: bool,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        annotated = copy.deepcopy(dict(result))
        metadata = {
            "status": status,
            "cache_key": cache_key[:24],
            "semantic_change_check": semantic_change_check,
            "adapter_invocation_avoided": adapter_invocation_avoided,
            "model_invocation_avoided": model_invocation_avoided,
        }
        if extra:
            metadata.update(copy.deepcopy(dict(extra)))
        annotated["vision_cache"] = metadata
        if status in {"exact_hit", "reprojected", "coalesced"}:
            routing = annotated.get("vision_routing")
            if isinstance(routing, dict):
                updated = copy.deepcopy(routing)
                updated["source_execution_status"] = routing.get("execution_status")
                updated["source_model_invoked"] = routing.get("model_invoked")
                updated["execution_status"] = f"cache_{status}"
                updated["model_invoked"] = False
                updated["adapter_invoked"] = False
                updated["cache_status"] = status
                annotated["vision_routing"] = updated
        return annotated

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise ValueError("clock must return a finite timestamp")
        return value

    @staticmethod
    def _positive_finite(value: Any, name: str) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{name} must be positive and finite") from exc
        if not math.isfinite(numeric) or numeric <= 0:
            raise ValueError(f"{name} must be positive and finite")
        return numeric


__all__ = ["VisionCacheCoordinator"]
