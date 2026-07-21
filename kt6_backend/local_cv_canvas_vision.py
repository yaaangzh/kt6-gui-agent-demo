from __future__ import annotations

from bisect import bisect_left
import heapq
import json
import math
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Mapping, Protocol, Sequence

from .topology_vision_contract import RESPONSE_SCHEMA_VERSION, TopologyVisionContract
from .vision_recognition import CanvasFrame


class LocalVisionDependencyError(RuntimeError):
    """The optional local OCR/CV runtime is not installed or could not start."""


class LocalVisionRecognitionError(RuntimeError):
    """The local OCR/CV runtime returned unusable image evidence."""


Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class OCRSpan:
    text: str
    confidence: float
    bbox: Box

    @property
    def center(self) -> tuple[float, float]:
        x, y, width, height = self.bbox
        return x + width / 2, y + height / 2


@dataclass(frozen=True)
class DeviceOccurrence:
    business_id: str
    prefix: str
    confidence: float
    bbox: Box
    raw_text: str
    span_index: int
    corrected_ocr: bool = False
    region: str = "diagram"

    @property
    def center(self) -> tuple[float, float]:
        x, y, width, height = self.bbox
        return x + width / 2, y + height / 2


@dataclass(frozen=True)
class DetectedConnector:
    source: str
    target: str
    confidence: float = 0.85
    evidence: str = "orthogonal_pixel_connector"
    line_style: str | None = None
    line_color: str | None = None
    weight: float | None = None


@dataclass(frozen=True)
class CVTopologyEvidence:
    node_boxes: Mapping[str, Box] = field(default_factory=dict)
    connectors: tuple[DetectedConnector, ...] = ()
    pass_through_nodes: frozenset[str] = frozenset()


class LocalImageBackend(Protocol):
    def recognize_text(self, frame: Any) -> tuple[OCRSpan, ...]:
        ...

    def analyze_connectors(
        self,
        frame: Any,
        *,
        spans: Sequence[OCRSpan],
        diagram_nodes: Mapping[str, DeviceOccurrence],
        diagram_bottom: float,
    ) -> CVTopologyEvidence:
        ...


class RapidOCROpenCVBackend:
    """Run RapidOCR and conservative multi-angle line analysis in-process."""

    MAX_OCR_SPANS = 5000
    MAX_LINE_SEGMENTS = 5000
    MAX_CORRIDOR_NODE_PAIRS = 5000
    MAX_LEGACY_CROSSING_PAIRS = 256

    def __init__(self) -> None:
        try:
            import cv2  # type: ignore[import-not-found]
            import numpy as np  # type: ignore[import-not-found]
            from rapidocr import RapidOCR  # type: ignore[import-not-found]
        except (ImportError, ModuleNotFoundError) as exc:
            raise LocalVisionDependencyError(
                "local_cv_ocr requires rapidocr, onnxruntime and opencv; "
                "install requirements-local-vision.txt"
            ) from exc

        self._cv2 = cv2
        self._np = np
        try:
            self._ocr = RapidOCR()
        except Exception as exc:  # pragma: no cover - provider-specific startup failures
            raise LocalVisionDependencyError(
                "RapidOCR could not initialize its local OCR models"
            ) from exc

    def recognize_text(self, frame: Any) -> tuple[OCRSpan, ...]:
        image = self._decode(frame.raw)
        try:
            result = self._ocr(image)
        except Exception as exc:  # pragma: no cover - provider-specific inference failures
            raise LocalVisionRecognitionError("RapidOCR inference failed") from exc
        return self._normalize_ocr_result(result, frame.width, frame.height)

    def analyze_connectors(
        self,
        frame: Any,
        *,
        spans: Sequence[OCRSpan],
        diagram_nodes: Mapping[str, DeviceOccurrence],
        diagram_bottom: float,
    ) -> CVTopologyEvidence:
        if len(diagram_nodes) < 2:
            return CVTopologyEvidence()

        cv2 = self._cv2
        np = self._np
        image = self._decode(frame.raw)
        crop_height = max(1, min(frame.height, int(math.ceil(diagram_bottom))))
        image = image[:crop_height, : frame.width]
        background_color = self._dominant_background_color(image)
        ink = self._foreground_mask(image, background=background_color)

        cleaned = ink.copy()
        for span in spans:
            if span.confidence < 0.65:
                # A low-confidence OCR box is not reliable enough to erase
                # real connector pixels; doing so lets a false OCR proposal
                # break an otherwise continuous edge.
                continue
            x, y, width, height = span.bbox
            if y >= crop_height:
                continue
            left = max(0, int(math.floor(x)) - 2)
            top = max(0, int(math.floor(y)) - 2)
            right = min(frame.width - 1, int(math.ceil(x + width)) + 2)
            bottom = min(crop_height - 1, int(math.ceil(y + height)) + 2)
            cv2.rectangle(cleaned, (left, top), (right, bottom), 0, thickness=-1)

        node_boxes = self._node_boxes(
            cleaned,
            diagram_nodes,
            frame_width=frame.width,
            frame_height=crop_height,
        )
        # OCR boxes describe labels, not necessarily the device glyph that the
        # connector actually touches.  Dense topology views commonly render a
        # compact filled icon above its label.  Keep the OCR-derived box for
        # output coordinates, but use independently detected thick foreground
        # regions as connector anchors whenever one can be associated safely.
        anchor_boxes = self._node_anchor_boxes(
            cleaned,
            node_boxes=node_boxes,
            diagram_nodes=diagram_nodes,
            frame_width=frame.width,
            frame_height=crop_height,
        )

        horizontal_length = max(12, min(80, frame.width // 60))
        vertical_length = max(12, min(80, crop_height // 45))
        horizontal_kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (horizontal_length, 1)
        )
        vertical_kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (1, vertical_length)
        )
        horizontal = cv2.morphologyEx(cleaned, cv2.MORPH_OPEN, horizontal_kernel)
        vertical = cv2.morphologyEx(cleaned, cv2.MORPH_OPEN, vertical_kernel)
        # Keep the pre-v1.2 orthogonal component mask.  The current detector
        # uses precise glyph anchors, which is safer when they are accurate but
        # can lose every edge when a production renderer leaves a visible gap
        # between the connector and its label/icon.  The legacy mask is used
        # only as a low-recall fallback below.
        legacy_line_mask = cv2.bitwise_or(horizontal, vertical)
        legacy_line_mask = cv2.dilate(
            legacy_line_mask,
            cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
            iterations=1,
        )
        segments = self._line_segments(
            cleaned,
            spans=spans,
            frame_width=frame.width,
            frame_height=crop_height,
        )
        segment_mask = np.zeros_like(cleaned)
        segment_thickness = max(
            2,
            min(5, int(round(math.hypot(frame.width, crop_height) / 1000))),
        )
        for x1, y1, x2, y2 in segments:
            cv2.line(
                segment_mask,
                (x1, y1),
                (x2, y2),
                255,
                thickness=segment_thickness,
                lineType=cv2.LINE_AA,
            )
        line_mask = cv2.bitwise_or(cv2.bitwise_or(horizontal, vertical), segment_mask)
        line_mask = cv2.dilate(
            line_mask,
            cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
            iterations=1,
        )
        line_mask = self._bridge_edge_label_gaps(
            line_mask,
            source_mask=cleaned,
            spans=spans,
            diagram_nodes=diagram_nodes,
            anchor_boxes=anchor_boxes,
        )
        line_mask, pass_through_nodes = self._bridge_pass_through_node_labels(
            line_mask,
            source_mask=cleaned,
            diagram_nodes=diagram_nodes,
            anchor_boxes=anchor_boxes,
        )
        if not bool(np.any(line_mask)):
            return CVTopologyEvidence()

        relation_nodes = {
            business_id: occurrence
            for business_id, occurrence in diagram_nodes.items()
            if business_id not in pass_through_nodes
        }
        relation_anchor_boxes = {
            business_id: anchor_boxes[business_id]
            for business_id in relation_nodes
        }
        if len(relation_nodes) < 2:
            return CVTopologyEvidence(
                node_boxes=node_boxes,
                pass_through_nodes=pass_through_nodes,
            )

        candidates: dict[
            frozenset[str],
            tuple[str, str, float, str, str | None, str | None],
        ] = {}
        contact_boxes = self._node_contact_boxes(
            relation_anchor_boxes,
            relation_nodes,
        )
        for first, second, multi_branch in self._component_connector_pairs(
            line_mask,
            source_mask=cleaned,
            contact_boxes=relation_anchor_boxes,
            diagram_nodes=relation_nodes,
            horizontal_mask=horizontal,
            vertical_mask=vertical,
        ):
            confidence = min(
                0.82 if multi_branch else 0.88,
                diagram_nodes[first].confidence,
                diagram_nodes[second].confidence,
            )
            self._remember_connector_candidate(
                candidates,
                source=first,
                target=second,
                confidence=confidence,
                evidence=(
                    "multi_branch_pixel_component"
                    if multi_branch
                    else "connected_pixel_path"
                ),
            )

        for first, second, style, color in self._segment_connector_pairs(
            segments,
            mask=cleaned,
            image=image,
            background=background_color,
            geometry_boxes=relation_anchor_boxes,
            contact_boxes=contact_boxes,
            blocking_boxes=relation_anchor_boxes,
            diagram_nodes=relation_nodes,
        ):
            confidence = min(
                0.93,
                diagram_nodes[first].confidence,
                diagram_nodes[second].confidence,
            )
            self._remember_connector_candidate(
                candidates,
                source=first,
                target=second,
                confidence=confidence,
                evidence="multi_angle_pixel_connector",
                line_style=style,
                line_color=color,
            )

        corridor_occlusion_mask = self._edge_label_occlusion_mask(
            cleaned,
            spans=spans,
            diagram_nodes=relation_nodes,
            anchor_boxes=anchor_boxes,
        )
        for business_id in pass_through_nodes:
            occurrence = diagram_nodes[business_id]
            x, y, width, height = occurrence.bbox
            pad = max(2, min(5, int(round(height * 0.2))))
            left = max(0, int(math.floor(x)) - pad)
            top = max(0, int(math.floor(y)) - pad)
            right = min(frame.width - 1, int(math.ceil(x + width)) + pad)
            bottom = min(crop_height - 1, int(math.ceil(y + height)) + pad)
            cv2.rectangle(
                corridor_occlusion_mask,
                (left, top),
                (right, bottom),
                255,
                thickness=-1,
            )

        for first, second, style, color, path_confidence in self._corridor_connector_pairs(
            cleaned,
            image=image,
            background=background_color,
            geometry_boxes=relation_anchor_boxes,
            contact_boxes=contact_boxes,
            blocking_boxes=relation_anchor_boxes,
            diagram_nodes=relation_nodes,
            occlusion_mask=corridor_occlusion_mask,
            node_occlusion_boxes=self._detached_node_label_boxes(
                spans,
                diagram_nodes=relation_nodes,
                anchor_boxes=anchor_boxes,
            ),
        ):
            confidence = min(
                path_confidence,
                diagram_nodes[first].confidence,
                diagram_nodes[second].confidence,
            )
            self._remember_connector_candidate(
                candidates,
                source=first,
                target=second,
                confidence=confidence,
                evidence="pixel_corridor_connector",
                line_style=style,
                line_color=color,
            )

        # v1.0 associated long connected components with generously padded
        # OCR/node regions and then selected one parent from the nearest visual
        # layer.  Restore that behavior only when the precise v1.2 paths leave
        # most of the recognized nodes disconnected.  This is intentionally a
        # fallback rather than a second always-on topology generator: scenes
        # already handled by the precise detector retain their exact edges.
        fallback_edge_limit = max(1, len(relation_nodes) // 5)
        primary_connected_nodes = {
            business_id
            for source, target, *_rest in candidates.values()
            for business_id in (source, target)
        }
        fallback_node_limit = max(1, int(math.ceil(len(relation_nodes) * 0.4)))
        legacy_recovery_needed = (
            len(candidates) <= fallback_edge_limit
            and len(primary_connected_nodes) < fallback_node_limit
        )
        directional_recovery_needed = len(primary_connected_nodes) < int(
            math.ceil(len(relation_nodes) * 0.75)
        )
        if legacy_recovery_needed or directional_recovery_needed:
            (
                legacy_component_labels,
                legacy_component_stats,
                closed_component_labels,
            ) = self._legacy_component_analysis(
                legacy_line_mask,
                diagram_nodes=relation_nodes,
            )
            padded_segment_pairs = self._legacy_padded_segment_pairs(
                segments,
                mask=cleaned,
                node_boxes={
                    business_id: node_boxes[business_id]
                    for business_id in relation_nodes
                },
                anchor_boxes=relation_anchor_boxes,
                diagram_nodes=relation_nodes,
                component_labels=legacy_component_labels,
                blocked_component_labels=closed_component_labels,
            )
            allow_layered_fallback = (
                len(padded_segment_pairs) <= self.MAX_LEGACY_CROSSING_PAIRS
            )
            crossing_node_groups = (
                self._legacy_straight_crossing_node_groups(padded_segment_pairs)
                if allow_layered_fallback
                else ()
            )
            legacy_pair_evidence = (
                {
                    frozenset((first, second)): (
                        first,
                        second,
                        "legacy_padded_hough_segment",
                    )
                    for first, second, _segment in padded_segment_pairs
                }
                if legacy_recovery_needed
                else {}
            )
            layered_pairs = (
                self._legacy_layered_component_pairs(
                    legacy_line_mask,
                    node_boxes={
                        business_id: node_boxes[business_id]
                        for business_id in relation_nodes
                    },
                    diagram_nodes=relation_nodes,
                    horizontal_length=horizontal_length,
                    vertical_length=vertical_length,
                    component_labels=legacy_component_labels,
                    component_stats=legacy_component_stats,
                    blocked_component_labels=closed_component_labels,
                )
                if legacy_recovery_needed and allow_layered_fallback
                else ()
            )
            directional_pairs: tuple[tuple[str, str], ...] = ()
            directional_crossing_groups: tuple[frozenset[str], ...] = ()
            if directional_recovery_needed and allow_layered_fallback:
                (
                    directional_pairs,
                    directional_crossing_groups,
                ) = self._directional_probe_component_pairs(
                    legacy_line_mask,
                    horizontal_mask=horizontal,
                    vertical_mask=vertical,
                    node_boxes=relation_anchor_boxes,
                    diagram_nodes=relation_nodes,
                    component_labels=legacy_component_labels,
                    component_stats=legacy_component_stats,
                    blocked_component_labels=closed_component_labels,
                )
                crossing_node_groups = tuple(crossing_node_groups) + tuple(
                    directional_crossing_groups
                )
            directional_pair_keys = {
                frozenset((first, second))
                for first, second in directional_pairs
            }
            for first, second in layered_pairs:
                if any(
                    first in group and second in group
                    for group in crossing_node_groups
                ):
                    # Two independent straight Hough paths that cross in their
                    # interiors are stronger evidence than the old visual-layer
                    # projection.  Without this guard, a plain "+" crossing is
                    # expanded into a false hierarchy between all four nodes.
                    continue
                legacy_pair_evidence.setdefault(
                    frozenset((first, second)),
                    (first, second, "legacy_layered_pixel_component"),
                )
            for first, second in directional_pairs:
                pair_key = frozenset((first, second))
                current_evidence = legacy_pair_evidence.get(pair_key)
                if (
                    current_evidence is None
                    or current_evidence[2]
                    == "legacy_layered_pixel_component"
                ):
                    # Evidence priority: direct padded Hough, traceable
                    # directional component, then wide legacy projection.
                    legacy_pair_evidence[pair_key] = (
                        first,
                        second,
                        "directional_probe_component",
                    )
            forest_parent = {
                business_id: business_id
                for business_id in relation_nodes
            }

            def find_root(business_id: str) -> str:
                parent = forest_parent[business_id]
                while parent != forest_parent[parent]:
                    parent = forest_parent[parent]
                while business_id != parent:
                    next_id = forest_parent[business_id]
                    forest_parent[business_id] = parent
                    business_id = next_id
                return parent

            def join(first: str, second: str) -> bool:
                first_root = find_root(first)
                second_root = find_root(second)
                if first_root == second_root:
                    return False
                forest_parent[second_root] = first_root
                return True

            for existing_key in candidates:
                if len(existing_key) == 2:
                    first, second = tuple(existing_key)
                    join(first, second)

            recoverable_legacy_edges: list[tuple[str, str, str]] = []
            for key, edge in legacy_pair_evidence.items():
                if key in candidates:
                    continue
                first, second, fallback_evidence = edge
                if key in directional_pair_keys:
                    # A traceable probe may join two already non-orphaned
                    # partial trees, but never closes a fallback-created cycle.
                    if join(first, second):
                        recoverable_legacy_edges.append(edge)
                    continue
                if (
                    first not in primary_connected_nodes
                    or second not in primary_connected_nodes
                ):
                    recoverable_legacy_edges.append(edge)
                    join(first, second)
            if recoverable_legacy_edges:
                for first, second, fallback_evidence in recoverable_legacy_edges:
                    self._remember_connector_candidate(
                        candidates,
                        source=first,
                        target=second,
                        confidence=min(
                            {
                                "legacy_layered_pixel_component": 0.80,
                                "legacy_padded_hough_segment": 0.78,
                                "directional_probe_component": 0.76,
                            }[fallback_evidence],
                            relation_nodes[first].confidence,
                            relation_nodes[second].confidence,
                        ),
                        evidence=fallback_evidence,
                    )

        connectors: list[DetectedConnector] = []
        for candidate in candidates.values():
            preferred_source, preferred_target, confidence, evidence, style, color = candidate
            source, target = self._orient_connector(
                preferred_source,
                preferred_target,
                diagram_nodes,
                keep_preferred=evidence
                in {
                    "orthogonal_pixel_connector",
                    "legacy_layered_pixel_component",
                    "directional_probe_component",
                },
            )
            connectors.append(
                DetectedConnector(
                    source=source,
                    target=target,
                    confidence=confidence,
                    evidence=evidence,
                    line_style=style,
                    line_color=color,
                )
            )
        weights = self._connector_weights(
            connectors,
            spans=spans,
            diagram_nodes=diagram_nodes,
        )
        connectors = [
            DetectedConnector(
                source=connector.source,
                target=connector.target,
                confidence=connector.confidence,
                evidence=connector.evidence,
                line_style=connector.line_style,
                line_color=connector.line_color,
                weight=weights.get(frozenset((connector.source, connector.target))),
            )
            for connector in connectors
        ]
        connectors.sort(key=lambda item: (item.source, item.target))
        return CVTopologyEvidence(
            node_boxes=node_boxes,
            connectors=tuple(connectors),
            pass_through_nodes=pass_through_nodes,
        )

    def _directional_probe_component_pairs(
        self,
        line_mask: Any,
        *,
        horizontal_mask: Any,
        vertical_mask: Any,
        node_boxes: Mapping[str, Box],
        diagram_nodes: Mapping[str, DeviceOccurrence],
        component_labels: Any,
        component_stats: Any,
        blocked_component_labels: frozenset[int] = frozenset(),
    ) -> tuple[
        tuple[tuple[str, str], ...],
        tuple[frozenset[str], ...],
    ]:
        """Recover topology from traceable node-to-component endpoint probes.

        The method never paints inferred pixels back into the global mask.
        Instead, every endpoint records the original orthogonal component it
        reached and the side of the node it left.  An edge requires both nodes
        to reach that same component with mutually facing exits.  Hierarchical
        edges are restricted to adjacent visual layers, which prevents a
        shared bus from becoming leaf-to-leaf or root-to-grandchild shortcuts.
        """

        cv2 = self._cv2
        np = self._np
        height, width = line_mask.shape[:2]
        typical_height = median(
            occurrence.bbox[3]
            for occurrence in diagram_nodes.values()
            if occurrence.bbox[3] > 0
        )
        maximum_gap = max(32, min(96, int(round(typical_height * 3.6))))
        continuation = max(12, min(36, int(round(typical_height * 0.9))))
        gap_limit = max(4, min(14, int(round(typical_height * 0.55))))
        directions = (
            ("north", 0, -1, vertical_mask),
            ("south", 0, 1, vertical_mask),
            ("west", -1, 0, horizontal_mask),
            ("east", 1, 0, horizontal_mask),
        )
        component_probes: dict[int, dict[str, set[str]]] = {}

        for business_id, occurrence in diagram_nodes.items():
            x, y, box_width, box_height = node_boxes.get(
                business_id,
                occurrence.bbox,
            )
            left = max(0, int(math.floor(x)))
            top = max(0, int(math.floor(y)))
            right = min(width - 1, int(math.ceil(x + box_width)))
            bottom = min(height - 1, int(math.ceil(y + box_height)))
            if left > right or top > bottom:
                continue
            center_x = (left + right) / 2
            center_y = (top + bottom) / 2

            for direction, delta_x, delta_y, direction_mask in directions:
                if delta_y < 0:
                    region_left, region_right = left, right
                    region_top = max(0, top - maximum_gap)
                    region_bottom = top - 1
                elif delta_y > 0:
                    region_left, region_right = left, right
                    region_top = bottom + 1
                    region_bottom = min(height - 1, bottom + maximum_gap)
                elif delta_x < 0:
                    region_left = max(0, left - maximum_gap)
                    region_right = left - 1
                    region_top, region_bottom = top, bottom
                else:
                    region_left = right + 1
                    region_right = min(width - 1, right + maximum_gap)
                    region_top, region_bottom = top, bottom
                if (
                    region_left > region_right
                    or region_top > region_bottom
                ):
                    continue

                direction_region = direction_mask[
                    region_top : region_bottom + 1,
                    region_left : region_right + 1,
                ]
                label_region = component_labels[
                    region_top : region_bottom + 1,
                    region_left : region_right + 1,
                ]
                active_y, active_x = np.nonzero(
                    (direction_region > 0) & (label_region > 0)
                )
                if not len(active_x):
                    continue
                active_x = active_x + region_left
                active_y = active_y + region_top
                targets_by_component: dict[int, list[tuple[int, int]]] = {}
                for target_x, target_y in zip(
                    active_x.tolist(),
                    active_y.tolist(),
                ):
                    label = int(component_labels[target_y, target_x])
                    if label <= 0 or label in blocked_component_labels:
                        continue
                    if int(component_stats[label, cv2.CC_STAT_AREA]) < 4:
                        continue
                    targets_by_component.setdefault(label, []).append(
                        (target_x, target_y)
                    )

                cross_span = box_width if delta_y else box_height
                lateral_limit = max(
                    6.0,
                    min(float(cross_span) * 0.35, occurrence.bbox[3] * 0.75),
                )
                ranked_components: list[
                    tuple[float, int, int, int, int]
                ] = []
                for label, component_targets in targets_by_component.items():
                    ranked_targets: list[tuple[float, int, int, int]] = []
                    for target_x, target_y in component_targets:
                        if delta_y < 0:
                            distance = top - target_y
                            lateral = abs(target_x - center_x)
                        elif delta_y > 0:
                            distance = target_y - bottom
                            lateral = abs(target_x - center_x)
                        elif delta_x < 0:
                            distance = left - target_x
                            lateral = abs(target_y - center_y)
                        else:
                            distance = target_x - right
                            lateral = abs(target_y - center_y)
                        if lateral > lateral_limit:
                            continue
                        ranked_targets.append(
                            (
                                distance + lateral * 0.35,
                                distance,
                                target_x,
                                target_y,
                            )
                        )
                    if not ranked_targets:
                        continue
                    score, distance, target_x, target_y = min(ranked_targets)
                    ranked_components.append(
                        (score, distance, label, target_x, target_y)
                    )

                valid_components: list[tuple[float, int]] = []
                for score, distance, label, target_x, target_y in sorted(
                    ranked_components
                ):
                    far_x = int(
                        max(0, min(width - 1, target_x + delta_x * continuation))
                    )
                    far_y = int(
                        max(0, min(height - 1, target_y + delta_y * continuation))
                    )
                    coverage, runs, maximum_run_gap, leading_gap, _trailing_gap = (
                        self._corridor_support(
                            direction_mask,
                            (float(target_x), float(target_y)),
                            (float(far_x), float(far_y)),
                        )
                    )
                    if leading_gap > 2:
                        continue
                    continuous = coverage >= 0.55 and maximum_run_gap <= gap_limit
                    dashed = (
                        runs >= 2
                        and coverage >= 0.30
                        and maximum_run_gap <= gap_limit
                    )
                    if not continuous and not dashed:
                        continue
                    if delta_y:
                        boundary = (
                            target_x,
                            top if delta_y < 0 else bottom,
                        )
                    else:
                        boundary = (
                            left if delta_x < 0 else right,
                            target_y,
                        )
                    target = (target_x, target_y)
                    if self._segment_crosses_other_node(
                        boundary,
                        target,
                        excluded={business_id},
                        contact_boxes=node_boxes,
                    ):
                        continue
                    valid_components.append((score, label))

                if not valid_components:
                    continue
                ambiguity_margin = max(4.0, occurrence.bbox[3] * 0.2)
                if (
                    len(valid_components) >= 2
                    and valid_components[1][0] - valid_components[0][0]
                    < ambiguity_margin
                ):
                    continue
                accepted_label = valid_components[0][1]
                component_probes.setdefault(accepted_label, {}).setdefault(
                    business_id,
                    set(),
                ).add(direction)

        crossing_labels: set[int] = set()
        crossing_groups: set[frozenset[str]] = set()
        straight_crossing_pairs: set[tuple[str, str]] = set()
        for label, node_probes in component_probes.items():
            side_nodes = {
                side: [
                    business_id
                    for business_id, sides in node_probes.items()
                    if side in sides
                ]
                for side in ("north", "south", "west", "east")
            }
            if not all(side_nodes.values()):
                continue
            group = frozenset(
                business_id
                for business_ids in side_nodes.values()
                for business_id in business_ids
            )
            if len(group) < 4:
                continue
            crossing_labels.add(label)
            crossing_groups.add(group)

            vertical_candidates: list[tuple[str, str]] = []
            if (
                len(side_nodes["south"]) * len(side_nodes["north"])
                <= self.MAX_CORRIDOR_NODE_PAIRS
            ):
                vertical_candidates = [
                    (top, bottom)
                    for top in side_nodes["south"]
                    for bottom in side_nodes["north"]
                    if diagram_nodes[top].center[1]
                    < diagram_nodes[bottom].center[1]
                ]
            if vertical_candidates:
                straight_crossing_pairs.add(
                    min(
                        vertical_candidates,
                        key=lambda pair: (
                            abs(
                                diagram_nodes[pair[0]].center[0]
                                - diagram_nodes[pair[1]].center[0]
                            ),
                            math.hypot(
                                diagram_nodes[pair[0]].center[0]
                                - diagram_nodes[pair[1]].center[0],
                                diagram_nodes[pair[0]].center[1]
                                - diagram_nodes[pair[1]].center[1],
                            ),
                            pair,
                        ),
                    )
                )

            horizontal_candidates: list[tuple[str, str]] = []
            if (
                len(side_nodes["east"]) * len(side_nodes["west"])
                <= self.MAX_CORRIDOR_NODE_PAIRS
            ):
                horizontal_candidates = [
                    (left, right)
                    for left in side_nodes["east"]
                    for right in side_nodes["west"]
                    if diagram_nodes[left].center[0]
                    < diagram_nodes[right].center[0]
                ]
            if horizontal_candidates:
                straight_crossing_pairs.add(
                    min(
                        horizontal_candidates,
                        key=lambda pair: (
                            abs(
                                diagram_nodes[pair[0]].center[1]
                                - diagram_nodes[pair[1]].center[1]
                            ),
                            math.hypot(
                                diagram_nodes[pair[0]].center[0]
                                - diagram_nodes[pair[1]].center[0],
                                diagram_nodes[pair[0]].center[1]
                                - diagram_nodes[pair[1]].center[1],
                            ),
                            pair,
                        ),
                    )
                )

        layers = self._legacy_visual_layers(diagram_nodes)
        layer_indexes = {
            business_id: layer_index
            for layer_index, layer in enumerate(layers)
            for business_id in layer
        }
        role_rank = {
            "GW": 0,
            "CORE": 1,
            "RTR": 1,
            "AGG": 2,
            "FW": 2,
            "AC": 2,
            "ACC": 3,
            "SW": 3,
            "LSW": 3,
            "AP": 4,
            "ONU": 4,
        }
        parent_candidates: dict[
            str,
            list[tuple[float, float, str]],
        ] = {}
        for label, node_probes in component_probes.items():
            if label in crossing_labels:
                continue
            nodes_by_layer: dict[int, list[str]] = {}
            for business_id in node_probes:
                nodes_by_layer.setdefault(
                    layer_indexes[business_id],
                    [],
                ).append(business_id)

            occupied_child_layers = sorted(
                layer_index
                for layer_index in nodes_by_layer
                if layer_index > 0 and layer_index - 1 in nodes_by_layer
            )
            for child_layer_index in occupied_child_layers:
                parents = [
                    business_id
                    for business_id in nodes_by_layer.get(
                        child_layer_index - 1,
                        (),
                    )
                    if "south" in node_probes[business_id]
                ]
                children = [
                    business_id
                    for business_id in nodes_by_layer.get(
                        child_layer_index,
                        (),
                    )
                    if "north" in node_probes[business_id]
                ]
                if not parents or not children:
                    continue

                eligible_parent_lists: dict[
                    int | None,
                    tuple[list[float], list[str]],
                ] = {}
                for child in children:
                    child_rank = role_rank.get(diagram_nodes[child].prefix)
                    eligible = eligible_parent_lists.get(child_rank)
                    if eligible is None:
                        sorted_parents = sorted(
                            (
                                diagram_nodes[parent].center[0],
                                parent,
                            )
                            for parent in parents
                            if (
                                child_rank is None
                                or role_rank.get(diagram_nodes[parent].prefix)
                                is None
                                or role_rank[diagram_nodes[parent].prefix]
                                < child_rank
                            )
                        )
                        eligible = (
                            [item[0] for item in sorted_parents],
                            [item[1] for item in sorted_parents],
                        )
                        eligible_parent_lists[child_rank] = eligible
                    parent_xs, eligible_parents = eligible
                    if not eligible_parents:
                        continue

                    child_center = diagram_nodes[child].center
                    insertion_index = bisect_left(parent_xs, child_center[0])
                    nearest_indexes = {
                        max(0, insertion_index - 1),
                        min(len(eligible_parents) - 1, insertion_index),
                    }
                    chosen_parent = min(
                        (eligible_parents[index] for index in nearest_indexes),
                        key=lambda parent: (
                            abs(
                                diagram_nodes[parent].center[0]
                                - child_center[0]
                            ),
                            math.hypot(
                                diagram_nodes[parent].center[0]
                                - child_center[0],
                                diagram_nodes[parent].center[1]
                                - child_center[1],
                            ),
                            parent,
                        ),
                    )
                    parent_center = diagram_nodes[chosen_parent].center
                    parent_candidates.setdefault(child, []).append(
                        (
                            abs(parent_center[0] - child_center[0]),
                            math.hypot(
                                parent_center[0] - child_center[0],
                                parent_center[1] - child_center[1],
                            ),
                            chosen_parent,
                        )
                    )

        pairs = set(straight_crossing_pairs)
        pairs.update(
            (min(candidates)[2], child)
            for child, candidates in parent_candidates.items()
        )
        return (
            tuple(sorted(pairs)),
            tuple(sorted(crossing_groups, key=lambda group: tuple(sorted(group)))),
        )

    def _legacy_layered_component_pairs(
        self,
        line_mask: Any,
        *,
        node_boxes: Mapping[str, Box],
        diagram_nodes: Mapping[str, DeviceOccurrence],
        horizontal_length: int,
        vertical_length: int,
        component_labels: Any | None = None,
        component_stats: Any | None = None,
        blocked_component_labels: frozenset[int] = frozenset(),
    ) -> tuple[tuple[str, str], ...]:
        """Recover the high-recall v1.0 orthogonal relation path.

        Unlike the precise component detector, this path does not cut node
        regions out of the line mask.  Each visual child is attached to at
        most one parent in the nearest preceding layer, so a shared trunk is
        expanded into a tree instead of a clique.
        """

        cv2 = self._cv2
        np = self._np
        if component_labels is None or component_stats is None:
            count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
                line_mask,
                connectivity=8,
            )
        else:
            labels = component_labels
            stats = component_stats
            count = len(stats)
        if count <= 1:
            return ()

        image_height, image_width = line_mask.shape[:2]
        minimum_component_area = max(
            4,
            min(horizontal_length, vertical_length) // 2,
        )
        component_sets: dict[str, set[int]] = {}
        for business_id, occurrence in diagram_nodes.items():
            x, y, width, height = node_boxes.get(
                business_id,
                occurrence.bbox,
            )
            margin = max(8, int(max(occurrence.bbox[3] * 1.5, 8)))
            left = max(0, int(math.floor(x)) - margin)
            top = max(0, int(math.floor(y)) - margin)
            right = min(image_width, int(math.ceil(x + width)) + margin)
            bottom = min(image_height, int(math.ceil(y + height)) + margin)
            if left >= right or top >= bottom:
                component_sets[business_id] = set()
                continue

            components: set[int] = set()
            for raw_label in np.unique(labels[top:bottom, left:right]).tolist():
                label = int(raw_label)
                if label <= 0:
                    continue
                area = int(stats[label, cv2.CC_STAT_AREA])
                component_width = int(stats[label, cv2.CC_STAT_WIDTH])
                component_height = int(stats[label, cv2.CC_STAT_HEIGHT])
                if area < minimum_component_area:
                    continue
                if (
                    component_width < horizontal_length
                    and component_height < vertical_length
                ):
                    continue
                components.add(label)
            component_sets[business_id] = components

        layers = self._legacy_visual_layers(diagram_nodes)
        layer_indexes = {
            business_id: layer_index
            for layer_index, layer in enumerate(layers)
            for business_id in layer
        }
        component_nodes: dict[int, set[str]] = {}
        for business_id, components in component_sets.items():
            for label in components:
                component_nodes.setdefault(label, set()).add(business_id)

        unsafe_components: set[int] = set(blocked_component_labels)
        for label, attached_nodes in component_nodes.items():
            if len(attached_nodes) < 2:
                continue
            if label in unsafe_components:
                continue

        if unsafe_components:
            for components in component_sets.values():
                components.difference_update(unsafe_components)

        pairs: set[tuple[str, str]] = set()
        for layer_index in range(1, len(layers)):
            for child_id in layers[layer_index]:
                child_components = component_sets.get(child_id, set())
                if not child_components:
                    continue
                chosen_parent: str | None = None
                for parent_layer_index in range(layer_index - 1, -1, -1):
                    possible_parents = [
                        parent_id
                        for parent_id in layers[parent_layer_index]
                        if child_components & component_sets.get(parent_id, set())
                    ]
                    if not possible_parents:
                        continue
                    child_x = diagram_nodes[child_id].center[0]
                    chosen_parent = min(
                        possible_parents,
                        key=lambda parent_id: (
                            abs(diagram_nodes[parent_id].center[0] - child_x),
                            parent_id,
                        ),
                    )
                    break
                if chosen_parent is not None and chosen_parent != child_id:
                    pairs.add((chosen_parent, child_id))
        return tuple(sorted(pairs))

    def _legacy_padded_segment_pairs(
        self,
        segments: Sequence[tuple[int, int, int, int]],
        *,
        mask: Any,
        node_boxes: Mapping[str, Box],
        anchor_boxes: Mapping[str, Box],
        diagram_nodes: Mapping[str, DeviceOccurrence],
        component_labels: Any | None = None,
        blocked_component_labels: frozenset[int] = frozenset(),
    ) -> tuple[tuple[str, str, tuple[int, int, int, int]], ...]:
        """Associate direct Hough segments with the wider v1.0 node halo."""

        association_boxes: dict[str, Box] = {}
        association_centers: dict[str, tuple[float, float]] = {}
        for business_id, occurrence in diagram_nodes.items():
            anchor = anchor_boxes.get(business_id, occurrence.bbox)
            has_independent_anchor = any(
                abs(anchor[index] - occurrence.bbox[index]) > 1e-6
                for index in range(4)
            )
            box = (
                anchor
                if has_independent_anchor
                else node_boxes.get(business_id, occurrence.bbox)
            )
            association_boxes[business_id] = box
            if has_independent_anchor:
                association_centers[business_id] = (
                    box[0] + box[2] / 2,
                    box[1] + box[3] / 2,
                )
            else:
                association_centers[business_id] = occurrence.center

        def endpoint_owner(point: tuple[int, int]) -> str | None:
            ranked: list[tuple[float, float, str]] = []
            for business_id, occurrence in diagram_nodes.items():
                distance = self._point_box_distance(
                    point,
                    association_boxes[business_id],
                )
                maximum_distance = max(12.0, occurrence.bbox[3] * 1.4)
                if distance <= maximum_distance:
                    ranked.append((distance, maximum_distance, business_id))
            ranked.sort(key=lambda item: (item[0], item[2]))
            if not ranked:
                return None
            if len(ranked) >= 2:
                ambiguity_margin = max(
                    6.0,
                    min(ranked[0][1], ranked[1][1]) * 0.2,
                )
                if ranked[1][0] - ranked[0][0] < ambiguity_margin:
                    return None
            return ranked[0][2]

        pairs: dict[
            frozenset[str],
            tuple[str, str, tuple[int, int, int, int], float],
        ] = {}
        for x1, y1, x2, y2 in segments:
            if self._directional_line_support(
                mask,
                (x1, y1),
                (x2, y2),
            ) < 0.60:
                continue
            first = endpoint_owner((x1, y1))
            second = endpoint_owner((x2, y2))
            if first is None or second is None or first == second:
                continue
            if (
                component_labels is not None
                and blocked_component_labels
                and self._segment_uses_blocked_component(
                    component_labels,
                    (x1, y1),
                    (x2, y2),
                    blocked_component_labels,
                )
            ):
                continue

            first_node = diagram_nodes[first]
            second_node = diagram_nodes[second]
            first_center = association_centers[first]
            second_center = association_centers[second]
            segment_length = math.hypot(x2 - x1, y2 - y1)
            node_distance = math.hypot(
                second_center[0] - first_center[0],
                second_center[1] - first_center[1],
            )
            if node_distance > 0 and segment_length < node_distance * 0.45:
                continue

            perpendicular_limit = max(
                10.0,
                first_node.bbox[3] * 0.9,
                second_node.bbox[3] * 0.9,
            )

            def perpendicular_distance(point: tuple[float, float]) -> float:
                return abs(
                    (point[0] - x1) * (y2 - y1)
                    - (point[1] - y1) * (x2 - x1)
                ) / max(1.0, segment_length)

            if (
                perpendicular_distance(first_center) > perpendicular_limit
                or perpendicular_distance(second_center) > perpendicular_limit
            ):
                continue

            first_distance, first_projection = self._point_segment_distance(
                first_center,
                (x1, y1),
                (x2, y2),
            )
            second_distance, second_projection = self._point_segment_distance(
                second_center,
                (x1, y1),
                (x2, y2),
            )
            if first_projection > second_projection:
                first_distance, second_distance = second_distance, first_distance
                first_projection, second_projection = (
                    second_projection,
                    first_projection,
                )
            center_gap_limit = max(
                48.0,
                first_node.bbox[3] * 3.0,
                second_node.bbox[3] * 3.0,
            )
            if first_distance > center_gap_limit or second_distance > center_gap_limit:
                continue
            if first_projection > 0.30 or second_projection < 0.70:
                continue
            if self._segment_crosses_other_node(
                (x1, y1),
                (x2, y2),
                excluded={first, second},
                contact_boxes=association_boxes,
            ):
                continue
            pair = frozenset((first, second))
            current = pairs.get(pair)
            segment = (x1, y1, x2, y2)
            if current is None or segment_length > current[3]:
                pairs[pair] = (first, second, segment, segment_length)
        return tuple(
            (first, second, segment)
            for first, second, segment, _length in sorted(
                pairs.values(),
                key=lambda item: tuple(sorted((item[0], item[1]))),
            )
        )

    def _legacy_component_analysis(
        self,
        line_mask: Any,
        *,
        diagram_nodes: Mapping[str, DeviceOccurrence],
    ) -> tuple[Any, Any, frozenset[int]]:
        """Label line components and identify closed container components.

        RETR_CCOMP exposes only enclosed holes, so this scan distinguishes a
        closed panel/container border from an open trunk.  The returned labels
        let both legacy paths reject the border pixels themselves without
        rejecting a genuine connector merely because it is inside the panel.
        """

        cv2 = self._cv2
        np = self._np
        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
            line_mask,
            connectivity=8,
        )
        if count <= 1:
            return labels, stats, frozenset()

        contour_result = cv2.findContours(
            line_mask,
            cv2.RETR_CCOMP,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        contours = contour_result[-2]
        hierarchy = contour_result[-1]
        if hierarchy is None:
            return labels, stats, frozenset()

        bucket_size = 64
        node_buckets: dict[
            tuple[int, int],
            list[tuple[float, float]],
        ] = {}
        for occurrence in diagram_nodes.values():
            center = occurrence.center
            node_buckets.setdefault(
                (int(center[0]) // bucket_size, int(center[1]) // bucket_size),
                [],
            ).append(center)

        closed_labels: set[int] = set()
        for contour_index, contour in enumerate(contours):
            parent_index = int(hierarchy[0][contour_index][3])
            if parent_index < 0:
                continue
            if abs(float(cv2.contourArea(contour))) < 64.0:
                continue
            x, y, width, height = cv2.boundingRect(contour)
            first_bucket_x = x // bucket_size
            last_bucket_x = (x + width) // bucket_size
            first_bucket_y = y // bucket_size
            last_bucket_y = (y + height) // bucket_size
            bucket_cell_count = (
                last_bucket_x - first_bucket_x + 1
            ) * (last_bucket_y - first_bucket_y + 1)
            if bucket_cell_count <= max(1, len(node_buckets) * 2):
                candidate_centers = (
                    center
                    for bucket_y in range(first_bucket_y, last_bucket_y + 1)
                    for bucket_x in range(first_bucket_x, last_bucket_x + 1)
                    for center in node_buckets.get((bucket_x, bucket_y), ())
                )
            else:
                candidate_centers = (
                    center
                    for (bucket_x, bucket_y), centers in node_buckets.items()
                    if first_bucket_x <= bucket_x <= last_bucket_x
                    and first_bucket_y <= bucket_y <= last_bucket_y
                    for center in centers
                )
            contained_count = 0
            for center in candidate_centers:
                if not (
                    x <= center[0] <= x + width
                    and y <= center[1] <= y + height
                ):
                    continue
                if cv2.pointPolygonTest(contour, center, False) >= 0:
                    contained_count += 1
                    if contained_count >= 2:
                        break
            if contained_count < 2:
                continue

            parent_points = contours[parent_index].reshape(-1, 2)
            if parent_points.size == 0:
                continue
            xs = np.clip(parent_points[:, 0], 0, labels.shape[1] - 1)
            ys = np.clip(parent_points[:, 1], 0, labels.shape[0] - 1)
            observed = labels[ys, xs]
            observed = observed[observed > 0]
            if observed.size == 0:
                continue
            closed_labels.add(int(np.bincount(observed).argmax()))
        return labels, stats, frozenset(closed_labels)

    def _segment_uses_blocked_component(
        self,
        component_labels: Any,
        first: tuple[int, int],
        second: tuple[int, int],
        blocked_component_labels: frozenset[int],
    ) -> bool:
        """Return whether a segment follows a blocked frame component."""

        np = self._np
        length = math.hypot(second[0] - first[0], second[1] - first[1])
        sample_count = max(2, min(600, int(round(length)) + 1))
        xs = np.rint(np.linspace(first[0], second[0], sample_count)).astype(int)
        ys = np.rint(np.linspace(first[1], second[1], sample_count)).astype(int)
        xs = np.clip(xs, 0, component_labels.shape[1] - 1)
        ys = np.clip(ys, 0, component_labels.shape[0] - 1)
        observed = component_labels[ys, xs]
        foreground = observed > 0
        foreground_count = int(np.count_nonzero(foreground))
        if foreground_count < max(4, int(round(sample_count * 0.30))):
            return False
        blocked = np.isin(observed, tuple(blocked_component_labels))
        blocked_count = int(np.count_nonzero(blocked))
        return (
            blocked_count >= int(round(sample_count * 0.55))
            and blocked_count >= int(round(foreground_count * 0.70))
        )

    @classmethod
    def _legacy_straight_crossing_node_groups(
        cls,
        segment_pairs: Sequence[
            tuple[str, str, tuple[int, int, int, int]]
        ],
    ) -> tuple[frozenset[str], ...]:
        """Find two disjoint, near-perpendicular edges crossing internally."""

        if len(segment_pairs) > cls.MAX_LEGACY_CROSSING_PAIRS:
            return ()
        groups: set[frozenset[str]] = set()
        for first_index, first_entry in enumerate(segment_pairs):
            first_source, first_target, first_segment = first_entry
            first_nodes = frozenset((first_source, first_target))
            first_x1, first_y1, first_x2, first_y2 = first_segment
            first_dx = first_x2 - first_x1
            first_dy = first_y2 - first_y1
            first_length = math.hypot(first_dx, first_dy)
            if first_length <= 1e-6:
                continue
            for second_source, second_target, second_segment in segment_pairs[
                first_index + 1 :
            ]:
                second_nodes = frozenset((second_source, second_target))
                if not first_nodes.isdisjoint(second_nodes):
                    continue
                second_x1, second_y1, second_x2, second_y2 = second_segment
                second_dx = second_x2 - second_x1
                second_dy = second_y2 - second_y1
                second_length = math.hypot(second_dx, second_dy)
                if second_length <= 1e-6:
                    continue
                normalized_dot = abs(
                    first_dx * second_dx + first_dy * second_dy
                ) / (first_length * second_length)
                if normalized_dot > 0.30:
                    continue

                denominator = first_dx * second_dy - first_dy * second_dx
                if abs(denominator) <= 1e-6:
                    continue
                offset_x = second_x1 - first_x1
                offset_y = second_y1 - first_y1
                first_projection = (
                    offset_x * second_dy - offset_y * second_dx
                ) / denominator
                second_projection = (
                    offset_x * first_dy - offset_y * first_dx
                ) / denominator
                if not (
                    0.15 <= first_projection <= 0.85
                    and 0.15 <= second_projection <= 0.85
                ):
                    continue
                groups.add(first_nodes | second_nodes)
        return tuple(sorted(groups, key=lambda group: tuple(sorted(group))))

    @staticmethod
    def _legacy_visual_layers(
        nodes: Mapping[str, DeviceOccurrence],
    ) -> list[list[str]]:
        ordered = sorted(
            nodes,
            key=lambda item: (
                nodes[item].center[1],
                nodes[item].center[0],
                item,
            ),
        )
        if not ordered:
            return []
        tolerance = max(
            12.0,
            median(nodes[item].bbox[3] for item in ordered) * 1.5,
        )
        layers: list[list[str]] = []
        layer_centers: list[float] = []
        for business_id in ordered:
            center_y = nodes[business_id].center[1]
            if not layers or abs(center_y - layer_centers[-1]) > tolerance:
                layers.append([business_id])
                layer_centers.append(center_y)
                continue
            layers[-1].append(business_id)
            layer_centers[-1] = sum(
                nodes[item].center[1]
                for item in layers[-1]
            ) / len(layers[-1])
        for layer in layers:
            layer.sort(key=lambda item: (nodes[item].center[0], item))
        return layers

    def _component_connector_pairs(
        self,
        line_mask: Any,
        *,
        source_mask: Any,
        contact_boxes: Mapping[str, Box],
        diagram_nodes: Mapping[str, DeviceOccurrence],
        horizontal_mask: Any,
        vertical_mask: Any,
    ) -> tuple[tuple[str, str, bool], ...]:
        """Split junctions at node regions and retain two-ended pixel paths.

        Removing node contact regions is important for star graphs: it turns a
        shared hub component into independent rays, so peripheral nodes are not
        incorrectly connected to one another merely because all paths meet at
        the hub.
        """

        cv2 = self._cv2
        np = self._np
        separated = line_mask.copy()
        height, width = separated.shape[:2]
        for box in contact_boxes.values():
            x, y, box_width, box_height = box
            left = max(0, int(math.floor(x)))
            top = max(0, int(math.floor(y)))
            right = min(width - 1, int(math.ceil(x + box_width)))
            bottom = min(height - 1, int(math.ceil(y + box_height)))
            if left <= right and top <= bottom:
                cv2.rectangle(separated, (left, top), (right, bottom), 0, thickness=-1)

        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
            separated,
            connectivity=8,
        )
        if count <= 1:
            return ()
        raw_near = cv2.dilate(
            source_mask,
            cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
            iterations=1,
        )
        raw_overlap_by_label = np.bincount(
            labels[raw_near > 0].ravel(),
            minlength=count,
        )
        horizontal_support_by_label = np.bincount(
            labels[horizontal_mask > 0].ravel(),
            minlength=count,
        )
        vertical_support_by_label = np.bincount(
            labels[vertical_mask > 0].ravel(),
            minlength=count,
        )

        component_nodes: dict[int, set[str]] = {}
        touch_margin = max(4, min(10, int(round(math.hypot(width, height) / 500))))
        for business_id, box in contact_boxes.items():
            x, y, box_width, box_height = box
            left = max(0, int(math.floor(x)) - touch_margin)
            top = max(0, int(math.floor(y)) - touch_margin)
            right = min(width, int(math.ceil(x + box_width)) + touch_margin)
            bottom = min(height, int(math.ceil(y + box_height)) + touch_margin)
            if left >= right or top >= bottom:
                continue
            for raw_label in np.unique(labels[top:bottom, left:right]).tolist():
                label = int(raw_label)
                if label <= 0:
                    continue
                if int(stats[label, cv2.CC_STAT_AREA]) < 4:
                    continue
                component_nodes.setdefault(label, set()).add(business_id)

        pairs: set[tuple[str, str, bool]] = set()
        for label, attached_nodes in component_nodes.items():
            component_area = int(stats[label, cv2.CC_STAT_AREA])
            raw_overlap = int(raw_overlap_by_label[label])
            if raw_overlap < max(4, int(round(component_area * 0.10))):
                # line_mask also contains Hough reconstructions and explicit
                # OCR-gap bridges.  Those pixels may restore continuity, but
                # they cannot create a component with almost no source-pixel
                # support of its own.
                continue
            if len(attached_nodes) == 2:
                first, second = sorted(attached_nodes)
                if raw_overlap < max(4, int(round(component_area * 0.30))):
                    # A two-node component must be substantially backed by
                    # captured pixels.  Short endpoint stubs plus a long
                    # Hough reconstruction are not proof of a direct edge.
                    continue
                if not self._component_pair_has_compatible_raw_endpoints(
                    first,
                    second,
                    component_labels=labels,
                    component_label=label,
                    raw_near=raw_near,
                    contact_boxes=contact_boxes,
                    diagram_nodes=diagram_nodes,
                ):
                    continue
                pairs.add((first, second, False))
                continue
            if len(attached_nodes) < 3:
                continue
            attached_nodes = {
                business_id
                for business_id in attached_nodes
                if self._raw_component_endpoint_directions(
                    contact_boxes[business_id],
                    component_labels=labels,
                    component_label=label,
                    raw_near=raw_near,
                    label_height=diagram_nodes[business_id].bbox[3],
                )
            }
            if len(attached_nodes) == 2:
                # A nearby Hough-only third branch must not erase an otherwise
                # real two-ended path.  Downgrade to the stricter pair checks.
                first, second = sorted(attached_nodes)
                if (
                    raw_overlap >= max(4, int(round(component_area * 0.30)))
                    and self._component_pair_has_compatible_raw_endpoints(
                        first,
                        second,
                        component_labels=labels,
                        component_label=label,
                        raw_near=raw_near,
                        contact_boxes=contact_boxes,
                        diagram_nodes=diagram_nodes,
                    )
                ):
                    pairs.add((first, second, False))
                continue
            if len(attached_nodes) < 2:
                continue
            horizontal_support = int(horizontal_support_by_label[label])
            vertical_support = int(vertical_support_by_label[label])
            # Multi-contact projection is limited to a visible orthogonal
            # trunk/T structure. Diagonal X crossings remain independent
            # straight edges and must never become a shared bus.
            minimum_orthogonal_support = max(
                8,
                int(round(component_area * 0.05)),
            )
            if (
                horizontal_support < minimum_orthogonal_support
                or vertical_support < minimum_orthogonal_support
            ):
                continue
            root = self._multi_branch_root(attached_nodes, diagram_nodes)
            if root is None:
                geometric_roots = [
                    business_id
                    for business_id in sorted(attached_nodes)
                    if self._multi_branch_fanout_is_layered(
                        business_id,
                        attached_nodes,
                        diagram_nodes,
                    )
                ]
                if len(geometric_roots) != 1:
                    continue
                root = geometric_roots[0]
            if not self._multi_branch_fanout_is_layered(
                root,
                attached_nodes,
                diagram_nodes,
            ):
                continue
            for leaf in sorted(attached_nodes - {root}):
                pairs.add((root, leaf, True))
        return tuple(sorted(pairs))

    def _component_pair_has_compatible_raw_endpoints(
        self,
        first: str,
        second: str,
        *,
        component_labels: Any,
        component_label: int,
        raw_near: Any,
        contact_boxes: Mapping[str, Box],
        diagram_nodes: Mapping[str, DeviceOccurrence],
    ) -> bool:
        """Require real endpoint exits compatible with one two-node path.

        A reconstructed component may contain Hough and OCR-gap pixels.  Real
        source pixels must therefore leave both node anchors.  Two leaves
        touching the same side of an upstream bus are not a direct connection,
        regardless of their role names; both exits must face the other node.
        """

        first_node = diagram_nodes[first]
        second_node = diagram_nodes[second]
        first_directions = self._raw_component_endpoint_directions(
            contact_boxes[first],
            component_labels=component_labels,
            component_label=component_label,
            raw_near=raw_near,
            label_height=first_node.bbox[3],
        )
        second_directions = self._raw_component_endpoint_directions(
            contact_boxes[second],
            component_labels=component_labels,
            component_label=component_label,
            raw_near=raw_near,
            label_height=second_node.bbox[3],
        )
        if not first_directions or not second_directions:
            return False

        vectors = {
            "north": (0.0, -1.0),
            "south": (0.0, 1.0),
            "west": (-1.0, 0.0),
            "east": (1.0, 0.0),
        }
        delta_x = second_node.center[0] - first_node.center[0]
        delta_y = second_node.center[1] - first_node.center[1]
        distance = math.hypot(delta_x, delta_y)
        if distance <= 1e-6:
            return False
        first_faces_second = any(
            (
                vectors[direction][0] * delta_x
                + vectors[direction][1] * delta_y
            )
            / distance
            >= 0.5
            for direction in first_directions
        )
        second_faces_first = any(
            (
                vectors[direction][0] * -delta_x
                + vectors[direction][1] * -delta_y
            )
            / distance
            >= 0.5
            for direction in second_directions
        )
        return first_faces_second and second_faces_first

    def _raw_component_endpoint_directions(
        self,
        box: Box,
        *,
        component_labels: Any,
        component_label: int,
        raw_near: Any,
        label_height: float,
    ) -> frozenset[str]:
        """Return node sides with a short, source-backed component exit."""

        np = self._np
        height, width = component_labels.shape[:2]
        x, y, box_width, box_height = box
        left = max(0, int(math.floor(x)))
        top = max(0, int(math.floor(y)))
        right = min(width - 1, int(math.ceil(x + box_width)))
        bottom = min(height - 1, int(math.ceil(y + box_height)))
        probe_length = max(6, min(18, int(round(label_height * 0.75))))
        maximum_gap = max(3, int(round(label_height * 0.30)))
        directions: set[str] = set()

        def supported(
            top_bound: int,
            bottom_bound: int,
            left_bound: int,
            right_bound: int,
        ) -> Any:
            return (
                component_labels[
                    top_bound:bottom_bound,
                    left_bound:right_bound,
                ]
                == component_label
            ) & (
                raw_near[
                    top_bound:bottom_bound,
                    left_bound:right_bound,
                ]
                > 0
            )

        strips = {
            "north": supported(
                max(0, top - probe_length),
                top,
                left,
                right + 1,
            )[::-1, :],
            "south": supported(
                bottom + 1,
                min(height, bottom + 1 + probe_length),
                left,
                right + 1,
            ),
            "west": supported(
                top,
                bottom + 1,
                max(0, left - probe_length),
                left,
            )[:, ::-1],
            "east": supported(
                top,
                bottom + 1,
                right + 1,
                min(width, right + 1 + probe_length),
            ),
        }
        for direction, strip in strips.items():
            if strip.size == 0:
                continue
            occupancy = (
                np.any(strip, axis=1)
                if direction in {"north", "south"}
                else np.any(strip, axis=0)
            )
            active_indexes = np.flatnonzero(occupancy)
            if not len(active_indexes) or int(active_indexes[0]) > 2:
                continue
            active_span = occupancy[: int(active_indexes[-1]) + 1]
            inactive = (~active_span).astype(np.int8)
            transitions = np.diff(np.pad(inactive, (1, 1), constant_values=0))
            starts = np.flatnonzero(transitions == 1)
            ends = np.flatnonzero(transitions == -1)
            gap_lengths = ends - starts
            if len(gap_lengths) and int(np.max(gap_lengths)) > maximum_gap:
                continue
            if float(np.mean(active_span)) < 0.45:
                continue
            if len(active_span) < max(4, int(round(probe_length * 0.45))):
                continue
            directions.add(direction)
        return frozenset(directions)

    @staticmethod
    def _multi_branch_root(
        attached_nodes: set[str],
        diagram_nodes: Mapping[str, DeviceOccurrence],
    ) -> str | None:
        """Choose one conservative anchor for a bus/T component.

        A multi-contact component is never expanded into a clique. It is only
        projected as one-to-many when a unique topology role or a clearly
        separated vertical endpoint identifies the common anchor.
        """

        role_rank = {
            "GW": 0,
            "CORE": 1,
            "AGG": 2,
            "FW": 2,
            "AC": 2,
            "ACC": 3,
            "SW": 3,
            "LSW": 3,
            "AP": 4,
            "ONU": 4,
        }
        ranked = [
            (role_rank[node.prefix], business_id)
            for business_id in attached_nodes
            if (node := diagram_nodes.get(business_id)) is not None
            and node.prefix in role_rank
        ]
        if ranked:
            minimum_rank = min(item[0] for item in ranked)
            best = sorted(
                business_id
                for rank, business_id in ranked
                if rank == minimum_rank
            )
            if len(best) == 1 and (
                minimum_rank <= 2 or len(ranked) == len(attached_nodes)
            ):
                return best[0]

        return None

    @staticmethod
    def _multi_branch_fanout_is_layered(
        root: str,
        attached_nodes: set[str],
        diagram_nodes: Mapping[str, DeviceOccurrence],
    ) -> bool:
        """Require bus leaves to form one visual layer away from the root."""

        root_node = diagram_nodes.get(root)
        leaves = [
            diagram_nodes[business_id]
            for business_id in attached_nodes
            if business_id != root and business_id in diagram_nodes
        ]
        if root_node is None or len(leaves) < 2:
            return False

        leaf_xs = [node.center[0] for node in leaves]
        leaf_ys = [node.center[1] for node in leaves]
        horizontal_layer_tolerance = max(
            24.0,
            median(node.bbox[3] for node in leaves) * 2.5,
        )
        vertical_layer_tolerance = max(
            40.0,
            median(node.bbox[2] for node in leaves) * 1.25,
        )
        vertical_separation = max(18.0, horizontal_layer_tolerance * 0.75)
        horizontal_separation = max(30.0, vertical_layer_tolerance * 0.75)

        leaves_share_row = max(leaf_ys) - min(leaf_ys) <= horizontal_layer_tolerance
        root_outside_row = (
            root_node.center[1] <= min(leaf_ys) - vertical_separation
            or root_node.center[1] >= max(leaf_ys) + vertical_separation
        )
        leaves_share_column = max(leaf_xs) - min(leaf_xs) <= vertical_layer_tolerance
        root_outside_column = (
            root_node.center[0] <= min(leaf_xs) - horizontal_separation
            or root_node.center[0] >= max(leaf_xs) + horizontal_separation
        )
        return (leaves_share_row and root_outside_row) or (
            leaves_share_column and root_outside_column
        )

    def _foreground_mask(self, image: Any, *, background: Any | None = None) -> Any:
        """Extract visible ink on both light and dark topology backgrounds."""

        np = self._np
        cv2 = self._cv2
        if background is None:
            background = self._dominant_background_color(image)
        delta = np.abs(image.astype(np.int16) - background.astype(np.int16))
        contrast = np.max(delta, axis=2).astype(np.uint8)
        otsu_threshold, _unused = cv2.threshold(
            contrast,
            0,
            255,
            cv2.THRESH_BINARY | cv2.THRESH_OTSU,
        )
        threshold = max(10, min(80, int(round(otsu_threshold))))
        foreground = np.where(contrast >= threshold, 255, 0).astype(np.uint8)

        # Canny preserves thin anti-aliased and low-saturation connectors that
        # can fall below a global color-distance threshold.
        edges = cv2.Canny(contrast, max(5, threshold // 2), max(20, threshold * 2))
        foreground = cv2.bitwise_or(foreground, edges)
        return cv2.morphologyEx(
            foreground,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        )

    def _dominant_background_color(self, image: Any) -> Any:
        np = self._np
        height, width = image.shape[:2]
        stride = max(1, int(math.sqrt(max(1, width * height) / 50_000)))
        sample = image[::stride, ::stride].reshape(-1, 3)
        quantized = sample.astype(np.uint16) >> 4
        keys = (
            (quantized[:, 0] << 8)
            | (quantized[:, 1] << 4)
            | quantized[:, 2]
        )
        counts = np.bincount(keys, minlength=4096)
        dominant_key = int(np.argmax(counts))
        dominant_pixels = sample[keys == dominant_key]
        if dominant_pixels.size:
            return np.median(dominant_pixels, axis=0)
        return np.median(sample, axis=0)  # pragma: no cover

    def _line_segments(
        self,
        cleaned: Any,
        *,
        spans: Sequence[OCRSpan],
        frame_width: int,
        frame_height: int,
    ) -> tuple[tuple[int, int, int, int], ...]:
        """Detect straight connector evidence at arbitrary angles.

        Hough's ``maxLineGap`` deliberately bridges short OCR-label gaps and
        dashed-line gaps, while endpoint-to-node validation prevents those
        bridges from becoming topology edges on their own.
        """

        cv2 = self._cv2
        np = self._np
        text_heights = [span.bbox[3] for span in spans if span.bbox[3] > 0]
        typical_text_height = median(text_heights) if text_heights else 12.0
        diagonal = math.hypot(frame_width, frame_height)
        minimum_length = max(
            8,
            int(round(typical_text_height * 1.15)),
            int(round(diagonal * 0.006)),
        )
        maximum_gap = max(
            8,
            min(40, int(round(typical_text_height * 1.35))),
        )
        hough_threshold = max(8, int(round(minimum_length * 0.65)))
        raw_lines = cv2.HoughLinesP(
            cleaned,
            rho=1,
            theta=np.pi / 360,
            threshold=hough_threshold,
            minLineLength=minimum_length,
            maxLineGap=maximum_gap,
        )
        if raw_lines is None:
            return ()

        lines: list[tuple[int, int, int, int]] = []
        seen: set[tuple[int, int, int, int]] = set()
        for raw_line in raw_lines:
            x1, y1, x2, y2 = (int(value) for value in raw_line.reshape(-1)[:4])
            if math.hypot(x2 - x1, y2 - y1) < minimum_length:
                continue
            if (x2, y2) < (x1, y1):
                x1, y1, x2, y2 = x2, y2, x1, y1
            # Quantized deduplication removes the parallel duplicates emitted
            # for the two anti-aliased sides of the same thick connector.
            key = (
                int(round(x1 / 3)),
                int(round(y1 / 3)),
                int(round(x2 / 3)),
                int(round(y2 / 3)),
            )
            if key in seen:
                continue
            seen.add(key)
            lines.append((x1, y1, x2, y2))

        lines.sort(
            key=lambda line: math.hypot(line[2] - line[0], line[3] - line[1]),
            reverse=True,
        )
        return tuple(lines[: self.MAX_LINE_SEGMENTS])

    def _node_contact_boxes(
        self,
        node_boxes: Mapping[str, Box],
        diagram_nodes: Mapping[str, DeviceOccurrence],
    ) -> dict[str, Box]:
        centers = {}
        for business_id, occurrence in diagram_nodes.items():
            x, y, width, height = node_boxes.get(business_id, occurrence.bbox)
            centers[business_id] = (x + width / 2, y + height / 2)
        contacts: dict[str, Box] = {}
        for business_id, occurrence in diagram_nodes.items():
            base = node_boxes.get(business_id, occurrence.bbox)
            nearest_distance = min(
                (
                    math.hypot(
                        centers[business_id][0] - other_center[0],
                        centers[business_id][1] - other_center[1],
                    )
                    for other_id, other_center in centers.items()
                    if other_id != business_id
                ),
                default=float("inf"),
            )
            desired_pad = max(8.0, occurrence.bbox[3] * 1.25)
            if math.isfinite(nearest_distance):
                desired_pad = min(desired_pad, max(6.0, nearest_distance * 0.32))
            x, y, width, height = base
            contacts[business_id] = (
                x - desired_pad,
                y - desired_pad,
                width + desired_pad * 2,
                height + desired_pad * 2,
            )
        return contacts

    def _segment_connector_pairs(
        self,
        segments: Sequence[tuple[int, int, int, int]],
        *,
        mask: Any,
        image: Any,
        background: Any,
        geometry_boxes: Mapping[str, Box],
        contact_boxes: Mapping[str, Box],
        blocking_boxes: Mapping[str, Box],
        diagram_nodes: Mapping[str, DeviceOccurrence],
    ) -> tuple[tuple[str, str, str | None, str | None], ...]:
        pairs: dict[
            frozenset[str],
            tuple[str, str, str | None, str | None, float],
        ] = {}
        for x1, y1, x2, y2 in segments:
            first = self._nearest_contact((x1, y1), contact_boxes)
            second = self._nearest_contact((x2, y2), contact_boxes)
            if first is None or second is None or first == second:
                continue
            extension = max(
                6.0,
                min(
                    18.0,
                    diagram_nodes[first].bbox[3] * 0.75,
                    diagram_nodes[second].bbox[3] * 0.75,
                ),
            )
            if not self._segment_reaches_box(
                (x1, y1),
                (x2, y2),
                geometry_boxes[first],
                extension=extension,
            ) or not self._segment_reaches_box(
                (x1, y1),
                (x2, y2),
                geometry_boxes[second],
                extension=extension,
            ):
                continue
            if self._segment_crosses_other_node(
                (x1, y1),
                (x2, y2),
                excluded={first, second},
                contact_boxes=blocking_boxes,
            ):
                continue
            first_geometry = geometry_boxes[first]
            second_geometry = geometry_boxes[second]
            node_distance = math.hypot(
                (first_geometry[0] + first_geometry[2] / 2)
                - (second_geometry[0] + second_geometry[2] / 2),
                (first_geometry[1] + first_geometry[3] / 2)
                - (second_geometry[1] + second_geometry[3] / 2),
            )
            segment_length = math.hypot(x2 - x1, y2 - y1)
            if node_distance > 0 and segment_length < node_distance * 0.45:
                continue
            if self._directional_line_support(
                mask,
                (x1, y1),
                (x2, y2),
            ) < 0.52:
                continue
            style, color = self._segment_appearance(
                image,
                (x1, y1),
                (x2, y2),
                background=background,
            )
            key = frozenset((first, second))
            existing = pairs.get(key)
            if (
                existing is None
                or segment_length > existing[4] * 1.05
                or (
                    style == "dashed"
                    and existing[2] != "dashed"
                    and segment_length >= existing[4] * 0.8
                )
            ):
                pairs[key] = (first, second, style, color, segment_length)
        return tuple(
            (first, second, style, color)
            for first, second, style, color, _length in pairs.values()
        )

    def _corridor_connector_pairs(
        self,
        cleaned: Any,
        *,
        image: Any,
        background: Any,
        geometry_boxes: Mapping[str, Box],
        contact_boxes: Mapping[str, Box],
        blocking_boxes: Mapping[str, Box],
        diagram_nodes: Mapping[str, DeviceOccurrence],
        occlusion_mask: Any | None = None,
        node_occlusion_boxes: Mapping[str, tuple[Box, ...]] | None = None,
    ) -> tuple[tuple[str, str, str | None, str | None, float], ...]:
        """Recover direct lines missed by Hough in dense multi-branch graphs."""

        business_ids = sorted(diagram_nodes)
        pair_count = len(business_ids) * (len(business_ids) - 1) // 2
        node_heights = [
            occurrence.bbox[3]
            for occurrence in diagram_nodes.values()
            if occurrence.bbox[3] > 0
        ]
        typical_node_height = median(node_heights) if node_heights else 16.0

        centers = {
            business_id: (
                geometry_boxes[business_id][0] + geometry_boxes[business_id][2] / 2,
                geometry_boxes[business_id][1] + geometry_boxes[business_id][3] / 2,
            )
            for business_id in business_ids
        }

        def ranked_pairs():
            for first_index, first in enumerate(business_ids):
                first_center = centers[first]
                for second in business_ids[first_index + 1 :]:
                    second_center = centers[second]
                    distance = math.hypot(
                        second_center[0] - first_center[0],
                        second_center[1] - first_center[1],
                    )
                    yield distance, first, second

        if pair_count <= self.MAX_CORRIDOR_NODE_PAIRS:
            candidate_pairs = tuple(ranked_pairs())
        else:
            # A global nearest-pair cutoff drops long but valid cross-region and
            # star spokes as soon as a scene passes 100 nodes.  Give every node
            # a small angular quota (nearest and farthest evidence per sector),
            # then fill the remaining bounded budget with global neighbours.
            # This stays deterministic and prevents a dense cluster from using
            # the entire fallback budget before isolated long edges are tested.
            per_node_quota = max(
                1,
                self.MAX_CORRIDOR_NODE_PAIRS // max(1, len(business_ids)),
            )
            # Reserve both a near and far proposal for every sector within the
            # per-node quota.  Angular resolution decreases gradually for very
            # large scenes instead of silently starving high-numbered sectors.
            sector_count = min(12, max(1, per_node_quota // 2))
            per_node: dict[
                str,
                dict[int, dict[str, tuple[float, str, str]]],
            ] = {business_id: {} for business_id in business_ids}
            nearest_heap: list[tuple[float, str, str]] = []

            def remember_sector(
                business_id: str,
                sector: int,
                item: tuple[float, str, str],
            ) -> None:
                sector_items = per_node[business_id].setdefault(sector, {})
                nearest = sector_items.get("nearest")
                if nearest is None or item < nearest:
                    sector_items["nearest"] = item
                farthest = sector_items.get("farthest")
                if farthest is None or item > farthest:
                    sector_items["farthest"] = item

            for item in ranked_pairs():
                distance, first, second = item
                if len(nearest_heap) < self.MAX_CORRIDOR_NODE_PAIRS:
                    heapq.heappush(nearest_heap, (-distance, first, second))
                elif distance < -nearest_heap[0][0]:
                    heapq.heapreplace(nearest_heap, (-distance, first, second))

                first_center = centers[first]
                second_center = centers[second]
                angle = math.atan2(
                    second_center[1] - first_center[1],
                    second_center[0] - first_center[0],
                )
                first_sector = int(
                    ((angle % (2 * math.pi)) / (2 * math.pi)) * sector_count
                ) % sector_count
                second_sector = (first_sector + sector_count // 2) % sector_count
                remember_sector(first, first_sector, item)
                remember_sector(second, second_sector, item)

            selected: dict[frozenset[str], tuple[float, str, str]] = {}
            for business_id in business_ids:
                sectors = sorted(per_node[business_id])
                fair_candidates: list[tuple[float, str, str]] = []
                local_seen: set[frozenset[str]] = set()

                def append_once(item: tuple[float, str, str] | None) -> None:
                    if item is None:
                        return
                    key = frozenset((item[1], item[2]))
                    if key not in local_seen:
                        local_seen.add(key)
                        fair_candidates.append(item)

                # A sector containing just one candidate is strong evidence of
                # an isolated long direction, so preserve it before a dense set
                # of early-numbered sectors can exhaust this node's quota.
                for sector in sectors:
                    sector_items = per_node[business_id][sector]
                    nearest = sector_items.get("nearest")
                    farthest = sector_items.get("farthest")
                    if nearest is not None and nearest == farthest:
                        append_once(nearest)
                # Round-robin by evidence kind: every occupied sector gets its
                # nearest proposal before any sector consumes a second slot.
                evidence_order = (
                    ("farthest", "nearest")
                    if per_node_quota == 1
                    else ("nearest", "farthest")
                )
                for evidence_kind in evidence_order:
                    for sector in sectors:
                        append_once(per_node[business_id][sector].get(evidence_kind))
                for item in fair_candidates[:per_node_quota]:
                    selected[frozenset((item[1], item[2]))] = item

            global_nearest = sorted(
                (-negative_distance, first, second)
                for negative_distance, first, second in nearest_heap
            )
            for item in global_nearest:
                if len(selected) >= self.MAX_CORRIDOR_NODE_PAIRS:
                    break
                selected.setdefault(frozenset((item[1], item[2])), item)
            candidate_pairs = tuple(
                sorted(selected.values(), key=lambda item: (item[0], item[1], item[2]))
            )

        accepted: list[tuple[str, str, str | None, str | None, float]] = []
        for _distance, first, second in candidate_pairs:
            endpoints = self._box_boundary_points(
                geometry_boxes[first],
                geometry_boxes[second],
            )
            if endpoints is None:
                continue
            start, end = endpoints
            length = math.hypot(end[0] - start[0], end[1] - start[1])
            minimum_length = max(
                8.0,
                diagram_nodes[first].bbox[3] * 0.75,
                diagram_nodes[second].bbox[3] * 0.75,
            )
            if length < minimum_length:
                continue
            rounded_start = (int(round(start[0])), int(round(start[1])))
            rounded_end = (int(round(end[0])), int(round(end[1])))
            if self._segment_crosses_other_node(
                rounded_start,
                rounded_end,
                excluded={first, second},
                contact_boxes=blocking_boxes,
            ):
                continue

            coverage, runs, maximum_gap, leading_gap, trailing_gap = (
                self._corridor_support(
                    cleaned,
                    start,
                    end,
                    occlusion_mask=occlusion_mask,
                    occlusion_boxes=(
                        (
                            node_occlusion_boxes.get(first, ())
                            + node_occlusion_boxes.get(second, ())
                        )
                        if node_occlusion_boxes is not None
                        else ()
                    ),
                )
            )
            endpoint_limit = max(
                8,
                min(
                    24,
                    int(round(typical_node_height * 0.75)),
                    int(round(length * 0.06)),
                ),
            )
            if leading_gap > endpoint_limit or trailing_gap > endpoint_limit:
                continue
            # A single large blank interval is not a solid connector.  The
            # old percentage-only threshold joined unrelated collinear
            # fragments in dense views (for a long edge, even a 50px hole
            # was accepted).  Small gaps stay scale-aware; a larger gap is
            # allowed only when _corridor_support can explain it with a
            # compact edge-label OCR box.
            solid_gap_limit = max(
                8,
                min(
                    int(round(max(24.0, typical_node_height * 1.2))),
                    int(round(length * 0.06)),
                ),
            )
            dashed_gap_limit = max(
                12,
                min(
                    int(round(max(32.0, typical_node_height * 2.2))),
                    int(round(length * 0.18)),
                ),
            )
            solid_like = coverage >= 0.72 and maximum_gap <= solid_gap_limit
            dashed_like = (
                runs >= 3
                and coverage >= 0.28
                and maximum_gap <= dashed_gap_limit
            )
            if not solid_like and not dashed_like:
                continue
            if self._directional_line_support(
                cleaned,
                rounded_start,
                rounded_end,
            ) < 0.52:
                continue

            style, color = self._segment_appearance(
                image,
                rounded_start,
                rounded_end,
                background=background,
            )
            if dashed_like and (not solid_like or coverage < 0.94):
                style = "dashed"
            path_confidence = min(
                0.91,
                0.82 + min(0.09, coverage * 0.1),
            )
            accepted.append(
                (first, second, style, color, round(path_confidence, 4))
            )
        return tuple(accepted)

    @staticmethod
    def _box_boundary_points(
        first: Box,
        second: Box,
    ) -> tuple[tuple[float, float], tuple[float, float]] | None:
        first_x, first_y, first_width, first_height = first
        second_x, second_y, second_width, second_height = second
        first_center = (
            first_x + first_width / 2,
            first_y + first_height / 2,
        )
        second_center = (
            second_x + second_width / 2,
            second_y + second_height / 2,
        )
        delta_x = second_center[0] - first_center[0]
        delta_y = second_center[1] - first_center[1]
        if abs(delta_x) < 1e-6 and abs(delta_y) < 1e-6:
            return None

        def scale(width: float, height: float) -> float:
            candidates: list[float] = []
            if abs(delta_x) >= 1e-6:
                candidates.append((width / 2) / abs(delta_x))
            if abs(delta_y) >= 1e-6:
                candidates.append((height / 2) / abs(delta_y))
            return min(candidates)

        first_scale = scale(first_width, first_height)
        second_scale = scale(second_width, second_height)
        if first_scale + second_scale >= 0.98:
            return None
        return (
            (
                first_center[0] + delta_x * first_scale,
                first_center[1] + delta_y * first_scale,
            ),
            (
                second_center[0] - delta_x * second_scale,
                second_center[1] - delta_y * second_scale,
            ),
        )

    def _corridor_support(
        self,
        mask: Any,
        first: tuple[float, float],
        second: tuple[float, float],
        *,
        occlusion_mask: Any | None = None,
        occlusion_boxes: Sequence[Box] = (),
    ) -> tuple[float, int, int, int, int]:
        np = self._np
        delta_x = second[0] - first[0]
        delta_y = second[1] - first[1]
        length = max(1, int(round(math.hypot(delta_x, delta_y))))
        perpendicular_x = -delta_y / length
        perpendicular_y = delta_x / length
        base_xs = np.linspace(first[0], second[0], length + 1)
        base_ys = np.linspace(first[1], second[1], length + 1)
        offsets = np.arange(-2, 3, dtype=float)
        xs = np.rint(base_xs[:, None] + perpendicular_x * offsets[None, :]).astype(int)
        ys = np.rint(base_ys[:, None] + perpendicular_y * offsets[None, :]).astype(int)
        xs = np.clip(xs, 0, mask.shape[1] - 1)
        ys = np.clip(ys, 0, mask.shape[0] - 1)
        active = np.any(mask[ys, xs] > 0, axis=1)
        # Text recognition deliberately erases OCR rectangles before line
        # analysis.  An interface/weight label can therefore create a genuine
        # gap in a connector.  Treat only explicitly marked compact regions as
        # supported for continuity, while retaining real-pixel coverage and run
        # count for style/confidence decisions.
        supported = active
        if occlusion_mask is not None:
            occluded = np.any(occlusion_mask[ys, xs] > 0, axis=1)
            supported = active | occluded
        if occlusion_boxes:
            box_occluded = np.zeros(len(active), dtype=bool)
            for x, y, width, height in occlusion_boxes:
                inside = (
                    (xs >= x)
                    & (xs <= x + width)
                    & (ys >= y)
                    & (ys <= y + height)
                )
                box_occluded |= np.any(inside, axis=1)
            supported = supported | box_occluded

        active_indexes = np.flatnonzero(supported)
        if not len(active_indexes):
            return 0.0, 0, len(active), len(active), len(active)

        leading_gap = int(active_indexes[0])
        trailing_gap = int(len(active) - 1 - active_indexes[-1])
        trimmed = active[active_indexes[0] : active_indexes[-1] + 1]
        supported_trimmed = supported[
            active_indexes[0] : active_indexes[-1] + 1
        ]
        runs = int(trimmed[0]) + int(
            np.count_nonzero((~trimmed[:-1]) & trimmed[1:])
        )
        inactive = (~supported_trimmed).astype(np.int8)
        transitions = np.diff(np.pad(inactive, (1, 1), constant_values=0))
        gap_starts = np.flatnonzero(transitions == 1)
        gap_ends = np.flatnonzero(transitions == -1)
        gap_lengths = gap_ends - gap_starts
        return (
            float(np.mean(trimmed)),
            runs,
            int(np.max(gap_lengths)) if len(gap_lengths) else 0,
            leading_gap,
            trailing_gap,
        )

    def _edge_label_occlusion_mask(
        self,
        template: Any,
        *,
        spans: Sequence[OCRSpan],
        diagram_nodes: Mapping[str, DeviceOccurrence],
        anchor_boxes: Mapping[str, Box] | None = None,
    ) -> Any:
        """Mark compact edge labels that may legitimately interrupt a line.

        Titles and long prose are intentionally excluded.  The resulting mask
        is only continuity evidence; real pixels on both sides and directional
        support are still required before a connector is accepted.
        """

        cv2 = self._cv2
        np = self._np
        mask = np.zeros_like(template)
        height, width = template.shape[:2]
        for span in self._edge_label_spans(
            spans,
            diagram_nodes=diagram_nodes,
            anchor_boxes=anchor_boxes,
        ):
            x, y, box_width, box_height = span.bbox
            pad = max(2, min(5, int(round(box_height * 0.2))))
            left = max(0, int(math.floor(x)) - pad)
            top = max(0, int(math.floor(y)) - pad)
            right = min(width - 1, int(math.ceil(x + box_width)) + pad)
            bottom = min(height - 1, int(math.ceil(y + box_height)) + pad)
            if left <= right and top <= bottom:
                cv2.rectangle(mask, (left, top), (right, bottom), 255, thickness=-1)
        return mask

    def _detached_node_label_boxes(
        self,
        spans: Sequence[OCRSpan],
        *,
        diagram_nodes: Mapping[str, DeviceOccurrence],
        anchor_boxes: Mapping[str, Box],
    ) -> dict[str, tuple[Box, ...]]:
        result: dict[str, tuple[Box, ...]] = {}
        for business_id, occurrence in diagram_nodes.items():
            if not 0 <= occurrence.span_index < len(spans):
                continue
            anchor = anchor_boxes.get(business_id)
            if anchor is None or self._boxes_overlap(anchor, occurrence.bbox):
                continue
            # Joined OCR identifiers can span two or more raw OCR boxes.  The
            # selected occurrence bbox is the complete erased label region;
            # using only its first raw span leaves the remaining gap invisible
            # to endpoint-specific corridor recovery.
            result[business_id] = (occurrence.bbox,)
        return result

    def _edge_label_spans(
        self,
        spans: Sequence[OCRSpan],
        *,
        diagram_nodes: Mapping[str, DeviceOccurrence],
        anchor_boxes: Mapping[str, Box] | None = None,
        include_detached_node_labels: bool = False,
    ) -> tuple[OCRSpan, ...]:
        node_span_owners = {
            occurrence.span_index: business_id
            for business_id, occurrence in diagram_nodes.items()
        }
        node_heights = [
            occurrence.bbox[3]
            for occurrence in diagram_nodes.values()
            if occurrence.bbox[3] > 0
        ]
        typical_node_height = median(node_heights) if node_heights else 16.0
        accepted: list[OCRSpan] = []
        for index, span in enumerate(spans):
            owner = node_span_owners.get(index)
            if owner is not None:
                occurrence = diagram_nodes[owner]
                anchor = (
                    anchor_boxes.get(owner)
                    if anchor_boxes is not None
                    else None
                )
                if anchor is None or self._boxes_overlap(anchor, occurrence.bbox):
                    continue
                if not include_detached_node_labels:
                    continue
            normalized = unicodedata.normalize("NFKC", span.text).strip()
            if not normalized or len(normalized) > 32:
                continue
            if owner is None and (
                span.confidence < 0.65
                or not self._looks_like_edge_label(normalized)
            ):
                continue
            _x, _y, width, height = span.bbox
            if width <= 0 or height <= 0:
                continue
            if (
                height > max(32.0, typical_node_height * 1.8)
                or width > max(220.0, height * 14.0)
            ):
                continue
            # Joined OCR spans can leave a raw fragment alongside the selected
            # node occurrence.  Never reinterpret a label overlapping a node
            # as an edge occlusion.
            if any(
                business_id != owner
                and self._boxes_overlap(span.bbox, occurrence.bbox)
                for business_id, occurrence in diagram_nodes.items()
            ):
                continue
            accepted.append(span)
        return tuple(accepted)

    def _bridge_edge_label_gaps(
        self,
        line_mask: Any,
        *,
        source_mask: Any | None = None,
        spans: Sequence[OCRSpan],
        diagram_nodes: Mapping[str, DeviceOccurrence],
        anchor_boxes: Mapping[str, Box] | None = None,
    ) -> Any:
        """Reconnect straight or orthogonal line fragments around OCR labels."""

        cv2 = self._cv2
        np = self._np
        bridged = line_mask.copy()
        evidence = source_mask if source_mask is not None else line_mask
        image_height, image_width = bridged.shape[:2]
        bridge_candidates: list[tuple[OCRSpan, str | None]] = [
            (span, None)
            for span in self._edge_label_spans(
                spans,
                diagram_nodes=diagram_nodes,
                anchor_boxes=anchor_boxes,
            )
        ]
        if anchor_boxes is not None:
            for business_id, occurrence in diagram_nodes.items():
                anchor = anchor_boxes.get(business_id)
                if anchor is None or self._boxes_overlap(anchor, occurrence.bbox):
                    continue
                # Treat a split/joined node identifier as one occlusion.  Raw
                # spans cannot individually see both sides of the erased label.
                bridge_candidates.append(
                    (
                        OCRSpan(
                            text=occurrence.raw_text,
                            confidence=occurrence.confidence,
                            bbox=occurrence.bbox,
                        ),
                        business_id,
                    )
                )

        for span, owner in bridge_candidates:
            x, y, width, height = span.bbox
            left = max(0, int(math.floor(x)) - 2)
            top = max(0, int(math.floor(y)) - 2)
            right = min(image_width - 1, int(math.ceil(x + width)) + 2)
            bottom = min(image_height - 1, int(math.ceil(y + height)) + 2)
            margin = max(4, min(14, int(round(height * 0.45))))
            minimum_run = max(2, int(round(margin * 0.35)))
            sides: dict[str, tuple[int, int]] = {}

            if left > 0 and top <= bottom:
                strip = evidence[
                    top : bottom + 1,
                    max(0, left - margin) : left + 1,
                ]
                if strip.size:
                    counts = np.count_nonzero(strip > 0, axis=1)
                    index = int(np.argmax(counts))
                    boundary_depth = min(2, strip.shape[1])
                    if (
                        int(counts[index]) >= minimum_run
                        and bool(np.any(strip[index, -boundary_depth:] > 0))
                    ):
                        sides["left"] = (left, top + index)
            if right < image_width - 1 and top <= bottom:
                strip = evidence[
                    top : bottom + 1,
                    right : min(image_width, right + margin + 1),
                ]
                if strip.size:
                    counts = np.count_nonzero(strip > 0, axis=1)
                    index = int(np.argmax(counts))
                    boundary_depth = min(2, strip.shape[1])
                    if (
                        int(counts[index]) >= minimum_run
                        and bool(np.any(strip[index, :boundary_depth] > 0))
                    ):
                        sides["right"] = (right, top + index)
            if top > 0 and left <= right:
                strip = evidence[
                    max(0, top - margin) : top + 1,
                    left : right + 1,
                ]
                if strip.size:
                    counts = np.count_nonzero(strip > 0, axis=0)
                    index = int(np.argmax(counts))
                    boundary_depth = min(2, strip.shape[0])
                    if (
                        int(counts[index]) >= minimum_run
                        and bool(np.any(strip[-boundary_depth:, index] > 0))
                    ):
                        sides["top"] = (left + index, top)
            if bottom < image_height - 1 and left <= right:
                strip = evidence[
                    bottom : min(image_height, bottom + margin + 1),
                    left : right + 1,
                ]
                if strip.size:
                    counts = np.count_nonzero(strip > 0, axis=0)
                    index = int(np.argmax(counts))
                    boundary_depth = min(2, strip.shape[0])
                    if (
                        int(counts[index]) >= minimum_run
                        and bool(np.any(strip[:boundary_depth, index] > 0))
                    ):
                        sides["bottom"] = (left + index, bottom)

            if len(sides) != 2:
                continue
            first_side, second_side = sorted(sides)
            first = sides[first_side]
            second = sides[second_side]
            opposite = {first_side, second_side} in (
                {"left", "right"},
                {"top", "bottom"},
            )
            thickness = max(2, min(5, int(round(min(width, height) * 0.15))))
            bridge_segments: list[
                tuple[tuple[int, int], tuple[int, int]]
            ] = []
            if opposite:
                alignment_tolerance = max(
                    5.0,
                    min(12.0, height * 0.5),
                )
                if {first_side, second_side} == {"left", "right"}:
                    if abs(first[1] - second[1]) > alignment_tolerance:
                        continue
                elif abs(first[0] - second[0]) > alignment_tolerance:
                    continue
                bridge_segments.append((first, second))
            else:
                # Adjacent sides describe a connector elbow hidden by the
                # label.  The corner stays inside the erased OCR rectangle.
                corner = (second[0], first[1])
                if not (
                    left <= corner[0] <= right
                    and top <= corner[1] <= bottom
                ):
                    corner = (first[0], second[1])
                if not (
                    left <= corner[0] <= right
                    and top <= corner[1] <= bottom
                ):
                    continue
                bridge_segments.extend(((first, corner), (corner, second)))

            if owner is not None:
                anchor = (
                    anchor_boxes.get(owner)
                    if anchor_boxes is not None
                    else None
                )
                if anchor is None or not self._owner_label_bridge_supported(
                    evidence,
                    bridge_segments=bridge_segments,
                    anchor=anchor,
                    label_height=height,
                ):
                    continue
            for segment_start, segment_end in bridge_segments:
                cv2.line(
                    bridged,
                    segment_start,
                    segment_end,
                    255,
                    thickness=thickness,
                )
        return bridged

    def _owner_label_bridge_supported(
        self,
        mask: Any,
        *,
        bridge_segments: Sequence[
            tuple[tuple[int, int], tuple[int, int]]
        ],
        anchor: Box,
        label_height: float,
    ) -> bool:
        """Require real pixels from one label side back to its owner glyph."""

        extension = math.hypot(mask.shape[1], mask.shape[0])
        for segment_start, segment_end in bridge_segments:
            if segment_start == segment_end:
                continue
            # The owner must lie outside one end of the proposed bridge.  More
            # importantly, the unmodified source pixels from that side to the
            # glyph must form a solid/dashed corridor; merely sharing the same
            # infinite axis is insufficient in a dense view.
            for inner, outer in (
                (segment_start, segment_end),
                (segment_end, segment_start),
            ):
                if not self._segment_reaches_box(
                    inner,
                    outer,
                    anchor,
                    extension=extension,
                ):
                    continue
                point_box = (
                    float(outer[0]) - 1.0,
                    float(outer[1]) - 1.0,
                    2.0,
                    2.0,
                )
                endpoints = self._box_boundary_points(point_box, anchor)
                if endpoints is None:
                    continue
                path_start, path_end = endpoints
                path_length = math.hypot(
                    path_end[0] - path_start[0],
                    path_end[1] - path_start[1],
                )
                if path_length < 2:
                    return True
                coverage, runs, maximum_gap, leading_gap, trailing_gap = (
                    self._corridor_support(mask, path_start, path_end)
                )
                endpoint_limit = max(4, min(12, int(round(label_height * 0.5))))
                solid_gap_limit = max(5, min(14, int(round(label_height * 0.65))))
                dashed_gap_limit = max(10, min(32, int(round(label_height * 1.8))))
                solid_like = coverage >= 0.68 and maximum_gap <= solid_gap_limit
                dashed_like = (
                    runs >= 2
                    and coverage >= 0.3
                    and maximum_gap <= dashed_gap_limit
                )
                if (
                    leading_gap <= endpoint_limit
                    and trailing_gap <= endpoint_limit
                    and (solid_like or dashed_like)
                    and self._directional_line_support(
                        mask,
                        (int(round(path_start[0])), int(round(path_start[1]))),
                        (int(round(path_end[0])), int(round(path_end[1]))),
                    )
                    >= 0.48
                ):
                    return True
        return False

    def _bridge_pass_through_node_labels(
        self,
        line_mask: Any,
        *,
        source_mask: Any,
        diagram_nodes: Mapping[str, DeviceOccurrence],
        anchor_boxes: Mapping[str, Box],
    ) -> tuple[Any, frozenset[str]]:
        """Bridge only uncertain OCR nodes that sit inside a straight line.

        A high-confidence, text-only node between two links is visually
        indistinguishable from a false OCR label drawn over one continuous
        connector, so it must remain a real endpoint.  Automatic pass-through
        is therefore limited to low-confidence occurrences with no
        independently detected glyph and with aligned pixel runs touching two
        opposite sides of the OCR-erased rectangle. Identifier correction alone
        is not evidence that an otherwise high-confidence node is spurious.
        """

        cv2 = self._cv2
        np = self._np
        bridged = line_mask.copy()
        image_height, image_width = bridged.shape[:2]
        pass_through: set[str] = set()

        for business_id, occurrence in diagram_nodes.items():
            if occurrence.confidence > 0.75:
                continue
            # Device identifiers such as ACC-002 and testNE49932 remain real
            # topology endpoints even when OCR confidence is modest.  The old
            # recognizer never collapsed these nodes into a line, and doing so
            # removes every incident edge before the recall fallback can run.
            if any(character.isdigit() for character in business_id):
                continue
            anchor = anchor_boxes.get(business_id)
            if anchor is None or any(
                abs(anchor[index] - occurrence.bbox[index]) > 1e-6
                for index in range(4)
            ):
                continue

            x, y, width, height = occurrence.bbox
            left = max(0, int(math.floor(x)) - 2)
            top = max(0, int(math.floor(y)) - 2)
            right = min(image_width - 1, int(math.ceil(x + width)) + 2)
            bottom = min(image_height - 1, int(math.ceil(y + height)) + 2)
            margin = max(4, min(14, int(round(height * 0.45))))
            minimum_run = max(2, int(round(margin * 0.35)))
            sides: dict[str, tuple[int, int]] = {}

            if left > 0 and top <= bottom:
                strip = source_mask[
                    top : bottom + 1,
                    max(0, left - margin) : left + 1,
                ]
                if strip.size:
                    counts = np.count_nonzero(strip > 0, axis=1)
                    index = int(np.argmax(counts))
                    boundary_depth = min(2, strip.shape[1])
                    if (
                        int(counts[index]) >= minimum_run
                        and bool(np.any(strip[index, -boundary_depth:] > 0))
                    ):
                        sides["left"] = (left, top + index)
            if right < image_width - 1 and top <= bottom:
                strip = source_mask[
                    top : bottom + 1,
                    right : min(image_width, right + margin + 1),
                ]
                if strip.size:
                    counts = np.count_nonzero(strip > 0, axis=1)
                    index = int(np.argmax(counts))
                    boundary_depth = min(2, strip.shape[1])
                    if (
                        int(counts[index]) >= minimum_run
                        and bool(np.any(strip[index, :boundary_depth] > 0))
                    ):
                        sides["right"] = (right, top + index)
            if top > 0 and left <= right:
                strip = source_mask[
                    max(0, top - margin) : top + 1,
                    left : right + 1,
                ]
                if strip.size:
                    counts = np.count_nonzero(strip > 0, axis=0)
                    index = int(np.argmax(counts))
                    boundary_depth = min(2, strip.shape[0])
                    if (
                        int(counts[index]) >= minimum_run
                        and bool(np.any(strip[-boundary_depth:, index] > 0))
                    ):
                        sides["top"] = (left + index, top)
            if bottom < image_height - 1 and left <= right:
                strip = source_mask[
                    bottom : min(image_height, bottom + margin + 1),
                    left : right + 1,
                ]
                if strip.size:
                    counts = np.count_nonzero(strip > 0, axis=0)
                    index = int(np.argmax(counts))
                    boundary_depth = min(2, strip.shape[0])
                    if (
                        int(counts[index]) >= minimum_run
                        and bool(np.any(strip[:boundary_depth, index] > 0))
                    ):
                        sides["bottom"] = (left + index, bottom)

            alignment_tolerance = max(5.0, min(12.0, height * 0.5))
            bridges: list[tuple[tuple[int, int], tuple[int, int]]] = []
            if "left" in sides and "right" in sides:
                first, second = sides["left"], sides["right"]
                if abs(first[1] - second[1]) <= alignment_tolerance:
                    bridges.append((first, second))
            if "top" in sides and "bottom" in sides:
                first, second = sides["top"], sides["bottom"]
                if abs(first[0] - second[0]) <= alignment_tolerance:
                    bridges.append((first, second))
            if not bridges:
                continue

            thickness = max(2, min(5, int(round(min(width, height) * 0.15))))
            for first, second in bridges:
                cv2.line(bridged, first, second, 255, thickness=thickness)
            pass_through.add(business_id)

        return bridged, frozenset(pass_through)

    @staticmethod
    def _boxes_overlap(first: Box, second: Box) -> bool:
        first_x, first_y, first_width, first_height = first
        second_x, second_y, second_width, second_height = second
        return (
            min(first_x + first_width, second_x + second_width)
            > max(first_x, second_x)
            and min(first_y + first_height, second_y + second_height)
            > max(first_y, second_y)
        )

    @staticmethod
    def _looks_like_edge_label(text: str) -> bool:
        """Accept conservative interface, rate and numeric weight labels."""

        compact = re.sub(r"\s+", "", text)
        if not any(character.isdigit() for character in compact):
            return False
        if re.fullmatch(r"[+-]?\d{1,8}(?:[.,]\d{1,8})?", compact):
            return True
        if re.fullmatch(r"[A-Za-z0-9_.:/+%#()\-]+", compact) is None:
            return False
        lowered = compact.lower()
        return (
            "/" in compact
            or ":" in compact
            or "-" in compact
            or any(
                keyword in lowered
                for keyword in (
                    "eth",
                    "trunk",
                    "port",
                    "gige",
                    "xge",
                    "vlan",
                    "lag",
                    "link",
                    "mbps",
                    "gbps",
                )
            )
        )

    def _connector_weights(
        self,
        connectors: Sequence[DetectedConnector],
        *,
        spans: Sequence[OCRSpan],
        diagram_nodes: Mapping[str, DeviceOccurrence],
    ) -> dict[frozenset[str], float]:
        """Bind decimal OCR labels to the nearest confirmed straight edge."""

        numeric_spans: list[tuple[OCRSpan, float]] = []
        for span in spans:
            if span.confidence < 0.65:
                continue
            normalized = unicodedata.normalize("NFKC", span.text).strip()
            if re.fullmatch(r"[+-]?\d{1,8}(?:[.,]\d{1,8})", normalized) is None:
                continue
            try:
                value = float(normalized.replace(",", "."))
            except ValueError:  # pragma: no cover - regex guarantees conversion
                continue
            if math.isfinite(value):
                numeric_spans.append((span, value))

        assignments: dict[frozenset[str], tuple[float, float, float]] = {}
        for span, value in numeric_spans:
            best: tuple[float, frozenset[str]] | None = None
            for connector in connectors:
                source = diagram_nodes.get(connector.source)
                target = diagram_nodes.get(connector.target)
                if source is None or target is None:
                    continue
                distance, projection = self._point_segment_distance(
                    span.center,
                    source.center,
                    target.center,
                )
                maximum_distance = max(8.0, span.bbox[3] * 1.25)
                if distance > maximum_distance or not 0.12 <= projection <= 0.88:
                    continue
                pair = frozenset((connector.source, connector.target))
                candidate = (distance, pair)
                if best is None or candidate[0] < best[0]:
                    best = candidate
            if best is None:
                continue
            distance, pair = best
            ranking = (distance, -span.confidence, value)
            current = assignments.get(pair)
            if current is None or ranking < current:
                assignments[pair] = ranking
        return {pair: ranking[2] for pair, ranking in assignments.items()}

    @staticmethod
    def _point_segment_distance(
        point: tuple[float, float],
        first: tuple[float, float],
        second: tuple[float, float],
    ) -> tuple[float, float]:
        delta_x = second[0] - first[0]
        delta_y = second[1] - first[1]
        squared_length = delta_x * delta_x + delta_y * delta_y
        if squared_length <= 1e-9:
            return math.hypot(point[0] - first[0], point[1] - first[1]), 0.0
        projection = (
            (point[0] - first[0]) * delta_x
            + (point[1] - first[1]) * delta_y
        ) / squared_length
        clamped = max(0.0, min(1.0, projection))
        closest = (
            first[0] + clamped * delta_x,
            first[1] + clamped * delta_y,
        )
        return math.hypot(point[0] - closest[0], point[1] - closest[1]), projection

    @staticmethod
    def _point_box_distance(point: tuple[int, int], box: Box) -> float:
        px, py = point
        x, y, width, height = box
        dx = max(x - px, 0.0, px - (x + width))
        dy = max(y - py, 0.0, py - (y + height))
        return math.hypot(dx, dy)

    def _nearest_contact(
        self,
        point: tuple[int, int],
        contact_boxes: Mapping[str, Box],
    ) -> str | None:
        nearest = min(
            (
                (self._point_box_distance(point, box), business_id)
                for business_id, box in contact_boxes.items()
            ),
            key=lambda item: (item[0], item[1]),
            default=None,
        )
        if nearest is None:
            return None
        distance, business_id = nearest
        return business_id if distance <= 5.0 else None

    def _segment_crosses_other_node(
        self,
        first: tuple[int, int],
        second: tuple[int, int],
        *,
        excluded: set[str],
        contact_boxes: Mapping[str, Box],
    ) -> bool:
        cv2 = self._cv2
        for business_id, box in contact_boxes.items():
            if business_id in excluded:
                continue
            x, y, width, height = box
            rectangle = (
                int(math.floor(x)),
                int(math.floor(y)),
                max(1, int(math.ceil(width))),
                max(1, int(math.ceil(height))),
            )
            intersects, _clipped_first, _clipped_second = cv2.clipLine(
                rectangle,
                first,
                second,
            )
            if intersects:
                return True
        return False

    def _segment_reaches_box(
        self,
        first: tuple[int, int],
        second: tuple[int, int],
        box: Box,
        *,
        extension: float,
    ) -> bool:
        """Require a candidate line to approach an anchor along its own axis."""

        delta_x = second[0] - first[0]
        delta_y = second[1] - first[1]
        length = math.hypot(delta_x, delta_y)
        if length <= 1e-6:
            return False
        unit_x = delta_x / length
        unit_y = delta_y / length
        extended_first = (
            int(round(first[0] - unit_x * extension)),
            int(round(first[1] - unit_y * extension)),
        )
        extended_second = (
            int(round(second[0] + unit_x * extension)),
            int(round(second[1] + unit_y * extension)),
        )
        x, y, width, height = box
        perpendicular_tolerance = 3
        rectangle = (
            int(math.floor(x)) - perpendicular_tolerance,
            int(math.floor(y)) - perpendicular_tolerance,
            max(1, int(math.ceil(width)) + perpendicular_tolerance * 2),
            max(1, int(math.ceil(height)) + perpendicular_tolerance * 2),
        )
        intersects, _clipped_first, _clipped_second = self._cv2.clipLine(
            rectangle,
            extended_first,
            extended_second,
        )
        return bool(intersects)

    def _directional_line_support(
        self,
        mask: Any,
        first: tuple[int, int],
        second: tuple[int, int],
    ) -> float:
        """Measure whether foreground near a candidate follows its direction.

        Occupancy alone is insufficient in dense diagrams: a row of vertical
        strokes can look like a dashed horizontal corridor.  Genuine line
        pixels keep foreground support along the candidate axis, whereas such
        crossings primarily have support in the perpendicular direction.
        """

        np = self._np
        delta_x = second[0] - first[0]
        delta_y = second[1] - first[1]
        length = math.hypot(delta_x, delta_y)
        if length < 4:
            return 0.0
        unit_x = delta_x / length
        unit_y = delta_y / length
        perpendicular_x = -unit_y
        perpendicular_y = unit_x
        sample_count = max(2, min(1200, int(round(length)) + 1))
        base_xs = np.linspace(first[0], second[0], sample_count)
        base_ys = np.linspace(first[1], second[1], sample_count)
        def sampled(offset_x: float, offset_y: float) -> Any:
            xs = np.rint(base_xs + offset_x).astype(int)
            ys = np.rint(base_ys + offset_y).astype(int)
            xs = np.clip(xs, 0, mask.shape[1] - 1)
            ys = np.clip(ys, 0, mask.shape[0] - 1)
            return mask[ys, xs] > 0

        center = (
            sampled(0.0, 0.0)
            | sampled(perpendicular_x, perpendicular_y)
            | sampled(-perpendicular_x, -perpendicular_y)
        )
        active_count = int(np.count_nonzero(center))
        if active_count < max(4, int(round(sample_count * 0.08))):
            return 0.0
        parallel_count = np.zeros(sample_count, dtype=np.int8)
        perpendicular_count = np.zeros(sample_count, dtype=np.int8)
        # Multiple non-harmonic distances avoid a fixed-probe resonance where
        # regularly spaced perpendicular strokes happen to land exactly on the
        # two horizontal probe points.  Strict directional dominance also
        # rejects an isotropic filled grid while retaining genuine thick lines.
        for probe in (3.0, 5.0, 8.0):
            parallel_count += sampled(
                unit_x * probe,
                unit_y * probe,
            ).astype(np.int8)
            parallel_count += sampled(
                -unit_x * probe,
                -unit_y * probe,
            ).astype(np.int8)
            perpendicular_count += sampled(
                perpendicular_x * probe,
                perpendicular_y * probe,
            ).astype(np.int8)
            perpendicular_count += sampled(
                -perpendicular_x * probe,
                -perpendicular_y * probe,
            ).astype(np.int8)
        aligned = center & (parallel_count >= 2) & (
            parallel_count >= perpendicular_count + 2
        )
        return float(np.count_nonzero(aligned) / active_count)

    def _segment_appearance(
        self,
        image: Any,
        first: tuple[int, int],
        second: tuple[int, int],
        *,
        background: Any | None = None,
    ) -> tuple[str | None, str | None]:
        cv2 = self._cv2
        np = self._np
        length = max(1, int(round(math.hypot(second[0] - first[0], second[1] - first[1]))))
        base_xs = np.linspace(first[0], second[0], length + 1)
        base_ys = np.linspace(first[1], second[1], length + 1)
        delta_x = second[0] - first[0]
        delta_y = second[1] - first[1]
        segment_length = max(1.0, math.hypot(delta_x, delta_y))
        perpendicular_x = -delta_y / segment_length
        perpendicular_y = delta_x / segment_length
        offsets = np.arange(-2, 3, dtype=float)
        xs = np.rint(base_xs[:, None] + perpendicular_x * offsets[None, :]).astype(int)
        ys = np.rint(base_ys[:, None] + perpendicular_y * offsets[None, :]).astype(int)
        xs = np.clip(xs, 0, image.shape[1] - 1)
        ys = np.clip(ys, 0, image.shape[0] - 1)
        corridor_pixels = image[ys, xs]
        if corridor_pixels.size == 0:
            return None, None

        if background is None:
            background = self._dominant_background_color(image)
        background = background.astype(np.int16)
        contrast = np.max(
            np.abs(corridor_pixels.astype(np.int16) - background),
            axis=2,
        )
        active_corridor = contrast >= 12
        active = np.any(active_corridor, axis=1)
        active_pixels = corridor_pixels[active_corridor]
        if active_pixels.size == 0:
            return None, None

        hsv = cv2.cvtColor(
            active_pixels.reshape(-1, 1, 3),
            cv2.COLOR_BGR2HSV,
        ).reshape(-1, 3)
        saturated = hsv[:, 1] >= 60
        color: str | None = None
        if bool(np.any(saturated)):
            hue = float(np.median(hsv[saturated, 0]))
            if hue < 8 or hue >= 172:
                color = "red"
            elif hue < 28:
                color = "orange"
            elif hue < 38:
                color = "yellow"
            elif 38 <= hue < 85:
                color = "green"
            elif 85 <= hue < 105:
                color = "cyan"
            elif 105 <= hue < 145:
                color = "blue"
            else:
                color = "magenta"

        active_indexes = np.flatnonzero(active)
        trimmed = active[active_indexes[0] : active_indexes[-1] + 1]
        starts = int(trimmed[0]) + int(
            np.count_nonzero((~trimmed[:-1]) & trimmed[1:])
        )
        coverage = float(np.mean(trimmed))
        style = "dashed" if starts >= 3 and coverage < 0.94 else "solid"
        return style, color

    @staticmethod
    def _remember_connector_candidate(
        candidates: dict[
            frozenset[str],
            tuple[str, str, float, str, str | None, str | None],
        ],
        *,
        source: str,
        target: str,
        confidence: float,
        evidence: str,
        line_style: str | None = None,
        line_color: str | None = None,
    ) -> None:
        if source == target:
            return
        key = frozenset((source, target))
        existing = candidates.get(key)
        if existing is not None and existing[2] >= confidence:
            existing_style = existing[4]
            existing_color = existing[5]
            if (
                (line_style == "dashed" and existing_style != "dashed")
                or (existing_style is None and line_style is not None)
                or (existing_color is None and line_color is not None)
            ):
                candidates[key] = (
                    existing[0],
                    existing[1],
                    existing[2],
                    existing[3],
                    (
                        "dashed"
                        if line_style == "dashed"
                        else existing_style or line_style
                    ),
                    existing_color or line_color,
                )
            return
        candidates[key] = (
            source,
            target,
            confidence,
            evidence,
            line_style,
            line_color,
        )

    @staticmethod
    def _orient_connector(
        first: str,
        second: str,
        nodes: Mapping[str, DeviceOccurrence],
        *,
        keep_preferred: bool,
    ) -> tuple[str, str]:
        if keep_preferred:
            return first, second

        role_rank = {
            "GW": 0,
            "CORE": 1,
            "AGG": 2,
            "FW": 2,
            "AC": 2,
            "ACC": 3,
            "SW": 3,
            "LSW": 3,
            "AP": 4,
            "ONU": 4,
        }
        first_rank = role_rank.get(nodes[first].prefix)
        second_rank = role_rank.get(nodes[second].prefix)
        if first_rank is not None and second_rank is not None and first_rank != second_rank:
            return (first, second) if first_rank < second_rank else (second, first)

        return (first, second) if first < second else (second, first)

    def _decode(self, raw: bytes) -> Any:
        buffer = self._np.frombuffer(raw, dtype=self._np.uint8)
        image = self._cv2.imdecode(buffer, self._cv2.IMREAD_UNCHANGED)
        if image is None:
            raise LocalVisionRecognitionError("OpenCV could not decode the verified image")
        shape = getattr(image, "shape", ())
        if len(shape) == 2:
            image = self._cv2.cvtColor(image, self._cv2.COLOR_GRAY2BGR)
        elif len(shape) == 3 and shape[2] == 4:
            alpha = image[:, :, 3:4].astype(self._np.float32) / 255.0
            color = image[:, :, :3].astype(self._np.float32)
            image = (color * alpha + 255.0 * (1.0 - alpha)).astype(self._np.uint8)
        elif len(shape) != 3 or shape[2] != 3:
            raise LocalVisionRecognitionError("OpenCV decoded an unsupported image layout")
        return image

    def _normalize_ocr_result(
        self,
        result: Any,
        image_width: int,
        image_height: int,
    ) -> tuple[OCRSpan, ...]:
        rows: list[tuple[Any, Any, Any]] = []
        if hasattr(result, "boxes") or hasattr(result, "txts") or hasattr(result, "scores"):
            boxes = getattr(result, "boxes", None)
            texts = getattr(result, "txts", None)
            scores = getattr(result, "scores", None)
            if boxes is None and texts is None and scores is None:
                return ()
            if boxes is None or texts is None or scores is None:
                raise LocalVisionRecognitionError("RapidOCR returned incomplete output columns")
            boxes = boxes.tolist() if hasattr(boxes, "tolist") else list(boxes)
            texts = list(texts)
            scores = list(scores)
            if not (len(boxes) == len(texts) == len(scores)):
                raise LocalVisionRecognitionError("RapidOCR output columns have different lengths")
            rows = list(zip(boxes, texts, scores))
        else:
            legacy_rows = result
            if isinstance(result, tuple) and len(result) == 2:
                legacy_rows = result[0]
            if legacy_rows is None:
                return ()
            if not isinstance(legacy_rows, (list, tuple)):
                raise LocalVisionRecognitionError("RapidOCR returned an unsupported output shape")
            for row in legacy_rows:
                if not isinstance(row, (list, tuple)) or len(row) < 3:
                    raise LocalVisionRecognitionError("RapidOCR returned a malformed OCR row")
                rows.append((row[0], row[1], row[2]))

        if len(rows) > self.MAX_OCR_SPANS:
            raise LocalVisionRecognitionError("RapidOCR returned too many text spans")
        spans: list[OCRSpan] = []
        for box_value, text_value, score_value in rows:
            if not isinstance(text_value, str):
                raise LocalVisionRecognitionError("RapidOCR text must be a string")
            text = text_value.strip()
            if not text:
                continue
            if len(text) > 1000:
                raise LocalVisionRecognitionError("RapidOCR text span is too long")
            try:
                confidence = float(score_value)
            except (TypeError, ValueError) as exc:
                raise LocalVisionRecognitionError("RapidOCR confidence is invalid") from exc
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise LocalVisionRecognitionError("RapidOCR confidence must be in [0, 1]")
            bbox = self._quad_bbox(box_value, image_width, image_height)
            spans.append(OCRSpan(text=text, confidence=confidence, bbox=bbox))
        return tuple(spans)

    def _quad_bbox(self, value: Any, image_width: int, image_height: int) -> Box:
        value = value.tolist() if hasattr(value, "tolist") else value
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise LocalVisionRecognitionError("RapidOCR box must contain four points")
        points: list[tuple[float, float]] = []
        for point in value:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise LocalVisionRecognitionError("RapidOCR box points must contain x and y")
            try:
                x, y = float(point[0]), float(point[1])
            except (TypeError, ValueError) as exc:
                raise LocalVisionRecognitionError("RapidOCR box coordinate is invalid") from exc
            if not math.isfinite(x) or not math.isfinite(y):
                raise LocalVisionRecognitionError("RapidOCR box coordinate must be finite")
            if x < -1 or y < -1 or x > image_width + 1 or y > image_height + 1:
                raise LocalVisionRecognitionError("RapidOCR box lies outside the input image")
            points.append((x, y))
        left = max(0.0, min(point[0] for point in points))
        top = max(0.0, min(point[1] for point in points))
        right = min(float(image_width), max(point[0] for point in points))
        bottom = min(float(image_height), max(point[1] for point in points))
        if right <= left or bottom <= top:
            raise LocalVisionRecognitionError("RapidOCR box must have positive area")
        return left, top, right - left, bottom - top

    def _node_boxes(
        self,
        cleaned: Any,
        diagram_nodes: Mapping[str, DeviceOccurrence],
        *,
        frame_width: int,
        frame_height: int,
    ) -> dict[str, Box]:
        cv2 = self._cv2
        contours_result = cv2.findContours(cleaned, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        contours = contours_result[-2]
        contour_boxes: list[Box] = []
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            if width < 8 or height < 8:
                continue
            if width > frame_width * 0.8 or height > frame_height * 0.45:
                continue
            contour_boxes.append((float(x), float(y), float(width), float(height)))

        result: dict[str, Box] = {}
        for business_id, occurrence in diagram_nodes.items():
            center_x, center_y = occurrence.center
            occurrence_width = occurrence.bbox[2]
            occurrence_height = occurrence.bbox[3]
            occurrence_area = occurrence_width * occurrence_height
            maximum_area = occurrence_area * 16
            maximum_width = max(occurrence_width * 3.0, occurrence_height * 10.0)
            maximum_height = max(occurrence_height * 7.0, occurrence_width * 1.5)
            candidates = []
            for box in contour_boxes:
                x, y, width, height = box
                if not (x <= center_x <= x + width and y <= center_y <= y + height):
                    continue
                if any(
                    other_id != business_id
                    and x <= other.center[0] <= x + width
                    and y <= other.center[1] <= y + height
                    for other_id, other in diagram_nodes.items()
                ):
                    # Group/container outlines often surround several labels.
                    # Such a contour is not the glyph of every contained node.
                    continue
                if width < occurrence.bbox[2] + 4 or height < occurrence.bbox[3] + 4:
                    continue
                if width * height < occurrence_area * 1.2:
                    continue
                # A connector component can surround the OCR center and look
                # like a node contour. Reject such graph-sized boxes so a star
                # or bus never becomes the contact region of one label.
                if width * height > maximum_area:
                    continue
                if width > maximum_width or height > maximum_height:
                    continue
                candidates.append(box)
            if candidates:
                result[business_id] = min(candidates, key=lambda box: box[2] * box[3])
                continue
            result[business_id] = self._padded_box(
                occurrence.bbox,
                frame_width=frame_width,
                frame_height=frame_height,
            )
        return result

    def _node_anchor_boxes(
        self,
        cleaned: Any,
        *,
        node_boxes: Mapping[str, Box],
        diagram_nodes: Mapping[str, DeviceOccurrence],
        frame_width: int,
        frame_height: int,
    ) -> dict[str, Box]:
        """Associate thick device glyphs with OCR labels for line grounding.

        A topology node is often rendered as a compact circle/rectangle with
        its identifier some distance below it.  OCR coordinates remain the
        safest public bbox, but using that label as the connector endpoint
        causes both misses and cross-node attachments in dense views.  A
        distance transform separates thick glyph interiors from thin connector
        strokes even when they belong to the same foreground component.
        """

        cv2 = self._cv2
        np = self._np
        anchors: dict[str, Box] = {}
        for business_id, occurrence in diagram_nodes.items():
            output_box = node_boxes.get(business_id, occurrence.bbox)
            padded_fallback = self._padded_box(
                occurrence.bbox,
                frame_width=frame_width,
                frame_height=frame_height,
            )
            # _node_boxes returns the deterministic padded OCR box when it did
            # not find a safe contour.  Keep raw OCR geometry in that case;
            # otherwise reuse the independently validated single-node contour
            # (important for large outlined rectangle glyphs).
            if all(
                abs(output_box[index] - padded_fallback[index]) <= 1e-6
                for index in range(4)
            ):
                anchors[business_id] = occurrence.bbox
            else:
                anchors[business_id] = output_box
        if not diagram_nodes or not bool(np.any(cleaned)):
            return anchors

        text_heights = [
            occurrence.bbox[3]
            for occurrence in diagram_nodes.values()
            if occurrence.bbox[3] > 0
        ]
        text_widths = [
            occurrence.bbox[2]
            for occurrence in diagram_nodes.values()
            if occurrence.bbox[2] > 0
        ]
        typical_height = median(text_heights) if text_heights else 14.0
        typical_width = median(text_widths) if text_widths else typical_height * 4
        # A glyph interior is materially thicker than a connector stroke.  A
        # text-scale threshold keeps 5-8px production lines from joining every
        # icon into one distance-transform component while retaining the core
        # of normal filled node circles/rectangles.
        seed_radius = max(5.5, min(12.0, typical_height * 0.35))

        binary = np.where(cleaned > 0, 255, 0).astype(np.uint8)
        distance = cv2.distanceTransform(binary, cv2.DIST_L2, 5)
        thick = np.where(distance >= seed_radius, 255, 0).astype(np.uint8)
        thick = cv2.morphologyEx(
            thick,
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        )
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
            thick,
            connectivity=8,
        )

        raw_candidates: list[Box] = []
        minimum_seed_area = max(5, int(round(seed_radius * seed_radius * 0.4)))
        maximum_dimension = max(64.0, typical_width * 2.5, typical_height * 10.0)
        expand = int(math.ceil(seed_radius))
        for label in range(1, count):
            area = int(stats[label, cv2.CC_STAT_AREA])
            if area < minimum_seed_area:
                continue
            x = int(stats[label, cv2.CC_STAT_LEFT])
            y = int(stats[label, cv2.CC_STAT_TOP])
            width = int(stats[label, cv2.CC_STAT_WIDTH])
            height = int(stats[label, cv2.CC_STAT_HEIGHT])
            if width <= 0 or height <= 0:
                continue
            seed_fill_ratio = area / float(width * height)
            if seed_fill_ratio < 0.18:
                # A thick X crossing can produce a large, near-square distance
                # transform component, but its two diagonal arms occupy only a
                # small fraction of that square.  Real filled topology glyphs
                # have a compact high-density interior.
                continue
            aspect = max(width / height, height / width)
            if aspect > 2.1:
                continue
            left = max(0, x - expand)
            top = max(0, y - expand)
            right = min(frame_width, x + width + expand)
            bottom = min(frame_height, y + height + expand)
            candidate_width = right - left
            candidate_height = bottom - top
            minimum_dimension = max(10.0, typical_height * 0.9)
            if (
                min(candidate_width, candidate_height) < minimum_dimension
                or candidate_width > maximum_dimension
                or candidate_height > maximum_dimension
            ):
                continue
            raw_candidates.append(
                (
                    float(left),
                    float(top),
                    float(max(1, candidate_width)),
                    float(max(1, candidate_height)),
                )
            )

        # Distance-transform seeds intentionally target filled glyphs.  Thin
        # outlined circles have no thick interior, so supplement them with
        # compact, high-circularity contours.  Long connector paths, X
        # crossings and container borders have low circularity or excessive
        # dimensions and are excluded here.
        contour_result = cv2.findContours(
            binary,
            cv2.RETR_LIST,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        contours = contour_result[-2]
        contour_minimum = max(10.0, typical_height * 0.65)
        contour_maximum = max(64.0, typical_height * 4.0, typical_width)
        for contour in contours:
            contour_area = abs(float(cv2.contourArea(contour)))
            perimeter = float(cv2.arcLength(contour, True))
            if contour_area <= 0 or perimeter <= 0:
                continue
            circularity = 4.0 * math.pi * contour_area / (perimeter * perimeter)
            if circularity < 0.55:
                continue
            x, y, width, height = cv2.boundingRect(contour)
            if width <= 0 or height <= 0:
                continue
            aspect = max(width / height, height / width)
            if aspect > 1.8:
                continue
            if (
                min(width, height) < contour_minimum
                or max(width, height) > contour_maximum
            ):
                continue
            expand_contour = 2
            left = max(0, x - expand_contour)
            top = max(0, y - expand_contour)
            right = min(frame_width, x + width + expand_contour)
            bottom = min(frame_height, y + height + expand_contour)
            raw_candidates.append(
                (
                    float(left),
                    float(top),
                    float(max(1, right - left)),
                    float(max(1, bottom - top)),
                )
            )

        # A complex glyph can contain several distance-transform peaks.  Merge
        # overlapping peaks before the one-to-one label association so nearby
        # labels cannot claim duplicate pieces of the same icon.
        candidates: list[Box] = []
        for box in sorted(
            raw_candidates,
            key=lambda item: item[2] * item[3],
            reverse=True,
        ):
            x, y, width, height = box
            area = width * height
            duplicate = False
            for existing in candidates:
                other_x, other_y, other_width, other_height = existing
                intersection_width = max(
                    0.0,
                    min(x + width, other_x + other_width) - max(x, other_x),
                )
                intersection_height = max(
                    0.0,
                    min(y + height, other_y + other_height) - max(y, other_y),
                )
                intersection = intersection_width * intersection_height
                if intersection >= min(area, other_width * other_height) * 0.35:
                    duplicate = True
                    break
            if not duplicate:
                candidates.append(box)

        matches: list[tuple[float, str, int]] = []
        for business_id, occurrence in diagram_nodes.items():
            label_center_x, label_center_y = occurrence.center
            search_limit = max(
                48.0,
                occurrence.bbox[2] * 1.75,
                occurrence.bbox[3] * 8.0,
            )
            horizontal_limit = max(
                36.0,
                occurrence.bbox[2] * 1.25,
                occurrence.bbox[3] * 5.0,
            )
            for candidate_index, box in enumerate(candidates):
                x, y, width, height = box
                center = (x + width / 2, y + height / 2)
                distance_to_label = self._point_box_distance(
                    (int(round(center[0])), int(round(center[1]))),
                    occurrence.bbox,
                )
                horizontal_delta = abs(center[0] - label_center_x)
                vertical_delta = abs(center[1] - label_center_y)
                axis_alignment_limit = max(12.0, occurrence.bbox[3] * 1.5)
                if (
                    distance_to_label > search_limit
                    or horizontal_delta > horizontal_limit
                    or center[1]
                    > label_center_y + max(8.0, occurrence.bbox[3] * 1.5)
                    or (
                        horizontal_delta > axis_alignment_limit
                        and vertical_delta > axis_alignment_limit
                    )
                ):
                    continue
                # Alignment is a useful tie-breaker for vertically stacked
                # icon/label pairs without forbidding side-mounted labels.
                score = distance_to_label + horizontal_delta * 0.9
                score += vertical_delta * 0.03
                if center[1] > label_center_y:
                    # Network topology labels are normally below their glyph.
                    # Without this soft directional prior, an icon in a dense
                    # ring can be marginally closer to the label above it than
                    # to its own label below it and get claimed by the wrong
                    # node.  The match remains possible as a fallback when no
                    # conventional icon-above-label candidate exists.
                    score += max(24.0, occurrence.bbox[3] * 3.0)
                matches.append((score, business_id, candidate_index))

        assigned_nodes: set[str] = set()
        assigned_candidates: set[int] = set()
        for _score, business_id, candidate_index in sorted(matches):
            if (
                business_id in assigned_nodes
                or candidate_index in assigned_candidates
            ):
                continue
            anchors[business_id] = candidates[candidate_index]
            assigned_nodes.add(business_id)
            assigned_candidates.add(candidate_index)
        return anchors

    @staticmethod
    def _padded_box(box: Box, *, frame_width: int, frame_height: int) -> Box:
        x, y, width, height = box
        pad_x = max(5.0, height * 0.35)
        pad_y = max(4.0, height * 0.25)
        left = max(0.0, x - pad_x)
        top = max(0.0, y - pad_y)
        right = min(float(frame_width), x + width + pad_x)
        bottom = min(float(frame_height), y + height + pad_y)
        return left, top, max(1.0, right - left), max(1.0, bottom - top)

class LocalCVTopologyVisionAdapter:
    """Recognize a single topology image without an Agent or external service."""

    adapter_id = "local-cv-ocr"
    adapter_version = "1.3"
    supports_actionable_grounding = False

    DEFAULT_MIN_OCR_CONFIDENCE = 0.65
    DEFAULT_MAX_IMAGE_PIXELS = 20_000_000
    _SERIAL_GATE = threading.BoundedSemaphore(value=1)
    _LEGACY_DEVICE_PATTERN = re.compile(
        r"(?<![A-Za-z0-9_])"
        r"(CORE|AGG|ACC|LSW|ONU|RTR|GW|AP|AC|FW|SW)"
        r"[\s_\-\u2013\u2014:\uff1a]*"
        r"([0-9OIL]{2,8})"
        r"(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )
    _IDENTIFIER_PATTERNS = (
        (
            "TESTNE",
            "testNE",
            True,
            re.compile(
                r"(?<![A-Za-z0-9_])test[\s_-]*NE[\s_-]*(?P<suffix>[0-9OIL]{3,12})"
                r"(?![A-Za-z0-9_])",
                re.IGNORECASE,
            ),
        ),
        (
            "COMMONSUBNET",
            "CommonSubnet",
            True,
            re.compile(
                r"(?<![A-Za-z0-9_])Common[\s_-]*Subnet[\s_-]*"
                r"(?P<suffix>[0-9OIL]{2,12})(?![A-Za-z0-9_])",
                re.IGNORECASE,
            ),
        ),
        (
            "SUBNETA",
            "SUBNETA_",
            True,
            re.compile(
                r"(?<![A-Za-z0-9_])SUBNETA[_-]+(?P<suffix>[0-9OIL]{4,12})"
                r"(?![A-Za-z0-9_])",
                re.IGNORECASE,
            ),
        ),
        (
            "SUBNET",
            "Subnet_",
            False,
            re.compile(
                r"(?<![A-Za-z0-9_])Subnet[_-]+(?P<suffix>[A-Za-z0-9]{4,64})"
                r"(?![A-Za-z0-9_])",
                re.IGNORECASE,
            ),
        ),
        (
            "NAME",
            "Name_",
            False,
            re.compile(
                r"(?<![A-Za-z0-9_])Name[_-]+(?P<suffix>[A-Za-z0-9]{4,64})"
                r"(?![A-Za-z0-9_])",
                re.IGNORECASE,
            ),
        ),
        (
            "V2SN",
            "V2SN_",
            False,
            re.compile(
                r"(?<![A-Za-z0-9_])V2SN[_-]+(?P<suffix>[A-Za-z0-9]{4,64})"
                r"(?![A-Za-z0-9_])",
                re.IGNORECASE,
            ),
        ),
        (
            "OSS",
            "OSS",
            False,
            re.compile(
                r"(?<![A-Za-z0-9_])OSS(?![A-Za-z0-9_])",
                re.IGNORECASE,
            ),
        ),
        (
            "CAMERAROOT",
            "CameraRoot",
            False,
            re.compile(
                r"(?<![A-Za-z0-9_])CameraRoot(?![A-Za-z0-9_])",
                re.IGNORECASE,
            ),
        ),
    )
    _SPLIT_PREFIX_PATTERN = re.compile(
        r"(?:"
        r"CORE|AGG|ACC|LSW|ONU|RTR|GW|AP|AC|FW|SW|"
        r"test[\s_-]*NE|Common[\s_-]*Subnet|"
        r"SUBNETA[_-]+|Subnet[_-]+|Name[_-]+|V2SN[_-]+"
        r")[\s_\-\u2013\u2014:\uff1a]*",
        re.IGNORECASE,
    )
    _NUMERIC_OCR_TRANSLATION = str.maketrans({"O": "0", "I": "1", "L": "1"})
    _TYPE_BY_PREFIX = {
        "GW": "gateway",
        "CORE": "core_switch",
        "AGG": "aggregation_device",
        "ACC": "access_switch",
        "AP": "wireless_ap",
        "AC": "wireless_controller",
        "FW": "firewall",
        "SW": "switch",
        "LSW": "switch",
        "ONU": "onu",
        "RTR": "router",
        "TESTNE": "network_device",
        "OSS": "oss",
        "CAMERAROOT": "camera_root",
        "COMMONSUBNET": "common_subnet",
        "SUBNET": "subnet",
        "NAME": "unknown",
        "SUBNETA": "subnet_a",
        "V2SN": "v2sn",
    }

    def __init__(
        self,
        *,
        backend: LocalImageBackend | None = None,
        contract: TopologyVisionContract | None = None,
        min_ocr_confidence: float = DEFAULT_MIN_OCR_CONFIDENCE,
        max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS,
        parse_table_relations: bool = True,
    ) -> None:
        try:
            threshold = float(min_ocr_confidence)
        except (TypeError, ValueError) as exc:
            raise ValueError("min_ocr_confidence must be a number in [0, 1]") from exc
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("min_ocr_confidence must be a number in [0, 1]")
        if isinstance(max_image_pixels, bool) or not isinstance(max_image_pixels, int):
            raise ValueError("max_image_pixels must be a positive integer")
        if max_image_pixels <= 0 or max_image_pixels > 100_000_000:
            raise ValueError("max_image_pixels must be in (0, 100000000]")
        self.min_ocr_confidence = threshold
        self.max_image_pixels = max_image_pixels
        self.parse_table_relations = bool(parse_table_relations)
        self._contract = contract or TopologyVisionContract()
        self._backend = backend or RapidOCROpenCVBackend()

    def recognize(
        self,
        *,
        page: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
    ) -> dict[str, Any] | None:
        # Validate and bound caller metadata, but never use it as topology evidence.
        self._contract.prepare_page(page)
        prepared = self._contract.prepare_frames(frames)
        if len(prepared.frames) != 1:
            raise LocalVisionRecognitionError(
                "local_cv_ocr accepts exactly one Canvas image per recognition"
            )
        frame = prepared.frames[0]
        if frame.width * frame.height > self.max_image_pixels:
            raise LocalVisionRecognitionError("local_cv_ocr image exceeds the local pixel limit")

        with self._SERIAL_GATE:
            spans = self._backend.recognize_text(frame)
            occurrences = self._device_occurrences(spans, frame.width, frame.height)
            if not occurrences:
                return None

            table_top, table_bottom = self._section_bounds(spans, frame.height)
            occurrences = tuple(
                self._with_region(item, table_top=table_top, table_bottom=table_bottom)
                for item in occurrences
            )
            selected = self._select_occurrences(occurrences)
            diagram_nodes = {
                business_id: occurrence
                for business_id, occurrence in selected.items()
                if occurrence.region == "diagram"
            }
            evidence = self._backend.analyze_connectors(
                frame,
                spans=spans,
                diagram_nodes=diagram_nodes,
                diagram_bottom=table_top,
            )
            table_relations, table_details = self._table_evidence(
                spans,
                occurrences,
                table_top=table_top,
                table_bottom=table_bottom,
                frame_width=frame.width,
            )
            payload = self._payload(
                frame,
                selected,
                evidence,
                table_relations=table_relations,
                table_details=table_details,
            )

        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        return self._contract.parse_response_bytes(encoded, prepared.frame_dimensions)

    def _device_occurrences(
        self,
        spans: Sequence[OCRSpan],
        frame_width: int,
        frame_height: int,
    ) -> tuple[DeviceOccurrence, ...]:
        occurrences: list[DeviceOccurrence] = []
        candidates: list[tuple[int, OCRSpan]] = list(enumerate(spans))
        candidates.extend(self._joined_identifier_spans(spans))
        seen: set[tuple[str, int, int, int, int]] = set()
        for span_index, span in candidates:
            if span.confidence < self.min_ocr_confidence:
                continue
            normalized = unicodedata.normalize("NFKC", span.text)
            for match in self._LEGACY_DEVICE_PATTERN.finditer(normalized):
                prefix = match.group(1).upper()
                raw_suffix = match.group(2).upper()
                suffix = raw_suffix.translate(self._NUMERIC_OCR_TRANSLATION)
                corrected = suffix != raw_suffix
                confidence = max(0.0, span.confidence - (0.08 if corrected else 0.0))
                if confidence < self.min_ocr_confidence:
                    continue
                bbox = self._match_bbox(span.bbox, match.start(), match.end(), len(normalized))
                bbox = self._clamp_box(bbox, frame_width, frame_height)
                business_id = f"{prefix}-{suffix}"
                occurrence_key = self._occurrence_key(business_id, bbox)
                if occurrence_key in seen:
                    continue
                seen.add(occurrence_key)
                occurrences.append(
                    DeviceOccurrence(
                        business_id=business_id,
                        prefix=prefix,
                        confidence=round(confidence, 4),
                        bbox=bbox,
                        raw_text=span.text[:300],
                        span_index=span_index,
                        corrected_ocr=corrected,
                    )
                )
            for prefix, canonical_stem, numeric_suffix, pattern in self._IDENTIFIER_PATTERNS:
                for match in pattern.finditer(normalized):
                    raw_suffix = match.groupdict().get("suffix")
                    corrected = False
                    if raw_suffix is None:
                        business_id = canonical_stem
                    elif numeric_suffix:
                        normalized_suffix = raw_suffix.upper()
                        suffix = normalized_suffix.translate(self._NUMERIC_OCR_TRANSLATION)
                        corrected = suffix != normalized_suffix
                        business_id = f"{canonical_stem}{suffix}"
                    else:
                        business_id = f"{canonical_stem}{raw_suffix}"
                    confidence = max(
                        0.0,
                        span.confidence - (0.08 if corrected else 0.0),
                    )
                    if confidence < self.min_ocr_confidence:
                        continue
                    bbox = self._match_bbox(
                        span.bbox,
                        match.start(),
                        match.end(),
                        len(normalized),
                    )
                    bbox = self._clamp_box(bbox, frame_width, frame_height)
                    occurrence_key = self._occurrence_key(business_id, bbox)
                    if occurrence_key in seen:
                        continue
                    seen.add(occurrence_key)
                    occurrences.append(
                        DeviceOccurrence(
                            business_id=business_id,
                            prefix=prefix,
                            confidence=round(confidence, 4),
                            bbox=bbox,
                            raw_text=span.text[:300],
                            span_index=span_index,
                            corrected_ocr=corrected,
                        )
                    )
        if len(occurrences) > 1000:
            raise LocalVisionRecognitionError("local OCR found too many topology identifiers")
        return tuple(occurrences)

    def _joined_identifier_spans(
        self,
        spans: Sequence[OCRSpan],
    ) -> tuple[tuple[int, OCRSpan], ...]:
        """Join a known OCR prefix with a nearby suffix on the same text row."""

        joined: list[tuple[int, OCRSpan]] = []
        for left_index, left in enumerate(spans):
            if left.confidence < self.min_ocr_confidence:
                continue
            left_text = unicodedata.normalize("NFKC", left.text).strip()
            if self._SPLIT_PREFIX_PATTERN.fullmatch(left_text) is None:
                continue
            left_x, left_y, left_width, left_height = left.bbox
            left_right = left_x + left_width
            left_center_y = left_y + left_height / 2
            ranked: list[tuple[float, int, OCRSpan]] = []
            for right_index, right in enumerate(spans):
                if right_index == left_index or right.confidence < self.min_ocr_confidence:
                    continue
                right_x, right_y, _right_width, right_height = right.bbox
                gap = right_x - left_right
                row_tolerance = max(left_height, right_height) * 0.55
                right_center_y = right_y + right_height / 2
                maximum_gap = max(6.0, max(left_height, right_height) * 0.8)
                if gap < -max(left_height, right_height) * 0.2 or gap > maximum_gap:
                    continue
                if abs(right_center_y - left_center_y) > row_tolerance:
                    continue
                ranked.append((max(0.0, gap), right_index, right))
            if not ranked:
                continue
            _gap, _right_index, right = min(ranked, key=lambda item: (item[0], item[1]))
            right_x, right_y, right_width, right_height = right.bbox
            right_edge = right_x + right_width
            bottom = max(left_y + left_height, right_y + right_height)
            bbox = (
                min(left_x, right_x),
                min(left_y, right_y),
                max(left_right, right_edge) - min(left_x, right_x),
                bottom - min(left_y, right_y),
            )
            joined.append(
                (
                    left_index,
                    OCRSpan(
                        text=f"{left.text.strip()}{right.text.strip()}",
                        confidence=min(left.confidence, right.confidence),
                        bbox=bbox,
                    ),
                )
            )
        return tuple(joined)

    @staticmethod
    def _occurrence_key(
        business_id: str,
        bbox: Box,
    ) -> tuple[str, int, int, int, int]:
        return (
            business_id,
            int(round(bbox[0])),
            int(round(bbox[1])),
            int(round(bbox[2])),
            int(round(bbox[3])),
        )

    @staticmethod
    def _match_bbox(box: Box, start: int, end: int, text_length: int) -> Box:
        x, y, width, height = box
        if text_length <= 0:
            return box
        left_ratio = max(0.0, min(1.0, start / text_length))
        right_ratio = max(left_ratio, min(1.0, end / text_length))
        matched_width = max(1.0, width * (right_ratio - left_ratio))
        return x + width * left_ratio, y, matched_width, height

    @staticmethod
    def _clamp_box(box: Box, frame_width: int, frame_height: int) -> Box:
        x, y, width, height = box
        left = max(0.0, min(float(frame_width), x))
        top = max(0.0, min(float(frame_height), y))
        right = max(left, min(float(frame_width), x + width))
        bottom = max(top, min(float(frame_height), y + height))
        if right <= left or bottom <= top:
            raise LocalVisionRecognitionError("local OCR produced an empty identifier box")
        return left, top, right - left, bottom - top

    @staticmethod
    def _section_bounds(spans: Sequence[OCRSpan], frame_height: int) -> tuple[float, float]:
        table_markers: list[float] = []
        ending_markers: list[float] = []
        for span in spans:
            normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", span.text))
            if "设备详情" in normalized:
                table_markers.append(span.bbox[1])
            if "特殊标记设备" in normalized or "架构特点" in normalized:
                ending_markers.append(span.bbox[1])
        table_top = min(table_markers) if table_markers else float(frame_height)
        table_bottom_candidates = [value for value in ending_markers if value > table_top]
        table_bottom = min(table_bottom_candidates) if table_bottom_candidates else float(frame_height)
        return max(1.0, table_top), max(table_top, table_bottom)

    @staticmethod
    def _with_region(
        occurrence: DeviceOccurrence,
        *,
        table_top: float,
        table_bottom: float,
    ) -> DeviceOccurrence:
        center_y = occurrence.center[1]
        if center_y < table_top:
            region = "diagram"
        elif center_y < table_bottom:
            region = "device_detail_table"
        else:
            region = "annotation"
        return DeviceOccurrence(
            business_id=occurrence.business_id,
            prefix=occurrence.prefix,
            confidence=occurrence.confidence,
            bbox=occurrence.bbox,
            raw_text=occurrence.raw_text,
            span_index=occurrence.span_index,
            corrected_ocr=occurrence.corrected_ocr,
            region=region,
        )

    @staticmethod
    def _select_occurrences(
        occurrences: Sequence[DeviceOccurrence],
    ) -> dict[str, DeviceOccurrence]:
        priority = {"diagram": 0, "device_detail_table": 1, "annotation": 2}
        selected: dict[str, DeviceOccurrence] = {}
        for occurrence in occurrences:
            current = selected.get(occurrence.business_id)
            candidate_key = (
                priority.get(occurrence.region, 9),
                -occurrence.confidence,
                occurrence.bbox[1],
                occurrence.bbox[0],
            )
            if current is None:
                selected[occurrence.business_id] = occurrence
                continue
            current_key = (
                priority.get(current.region, 9),
                -current.confidence,
                current.bbox[1],
                current.bbox[0],
            )
            if candidate_key < current_key:
                selected[occurrence.business_id] = occurrence
        return selected

    def _table_evidence(
        self,
        spans: Sequence[OCRSpan],
        occurrences: Sequence[DeviceOccurrence],
        *,
        table_top: float,
        table_bottom: float,
        frame_width: int,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        if not self.parse_table_relations or table_top >= table_bottom:
            return [], {}

        header_device: OCRSpan | None = None
        header_downstream: OCRSpan | None = None
        for span in spans:
            center_y = span.center[1]
            if not table_top <= center_y < table_bottom:
                continue
            normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", span.text)).upper()
            if normalized == "设备" and header_device is None:
                header_device = span
            if "下方AP" in normalized and header_downstream is None:
                header_downstream = span
        if header_device is None or header_downstream is None:
            return [], {}

        header_y = max(header_device.center[1], header_downstream.center[1])
        parent_boundary = (header_device.center[0] + header_downstream.center[0]) / 2
        child_boundary = max(parent_boundary, header_downstream.bbox[0] - frame_width * 0.02)
        table_occurrences = [
            item
            for item in occurrences
            if item.region == "device_detail_table" and item.center[1] > header_y
        ]
        parents = [item for item in table_occurrences if item.center[0] < parent_boundary]
        parents.sort(key=lambda item: (item.center[1], item.center[0], item.business_id))
        if not parents:
            return [], {}

        relations: list[dict[str, Any]] = []
        details: dict[str, str] = {}
        for index, parent in enumerate(parents):
            previous_y = parents[index - 1].center[1] if index else header_y
            next_y = parents[index + 1].center[1] if index + 1 < len(parents) else table_bottom
            lower = (previous_y + parent.center[1]) / 2
            upper = (parent.center[1] + next_y) / 2
            row_spans = [span for span in spans if lower <= span.center[1] < upper]
            row_text = " | ".join(span.text.strip() for span in row_spans if span.text.strip())
            if row_text:
                details[parent.business_id] = row_text[:500]
            children = [
                item
                for item in table_occurrences
                if lower <= item.center[1] < upper
                and item.center[0] >= child_boundary
                and item.business_id != parent.business_id
            ]
            for child in sorted(children, key=lambda item: (item.center[0], item.business_id)):
                relations.append(
                    {
                        "relation_id": f"local-table:{parent.business_id}:{child.business_id}",
                        "source": parent.business_id,
                        "target": child.business_id,
                        "type": "downstream_membership",
                        "confidence": round(min(0.9, parent.confidence, child.confidence), 4),
                        "attributes": {
                            "evidence": "device_detail_table",
                            "directness": "unknown",
                        },
                    }
                )
        return relations, details

    def _payload(
        self,
        frame: Any,
        selected: Mapping[str, DeviceOccurrence],
        evidence: CVTopologyEvidence,
        *,
        table_relations: Sequence[dict[str, Any]],
        table_details: Mapping[str, str],
    ) -> dict[str, Any]:
        objects: list[dict[str, Any]] = []
        for business_id in sorted(selected):
            occurrence = selected[business_id]
            bbox = evidence.node_boxes.get(business_id, occurrence.bbox)
            bbox = self._clamp_box(bbox, frame.width, frame.height)
            attributes: dict[str, Any] = {
                "recognizer": "rapidocr",
                "source_region": occurrence.region,
                "ocr_text": occurrence.raw_text,
                "ocr_confidence": occurrence.confidence,
            }
            if occurrence.corrected_ocr:
                attributes["ocr_identifier_corrected"] = True
            if business_id in evidence.pass_through_nodes:
                attributes["pixel_role"] = "pass_through_ocr_candidate"
                attributes["relation_excluded_reason"] = (
                    "uncertain_text_over_continuous_connector"
                )
            if business_id in table_details:
                attributes["device_detail_row"] = table_details[business_id]
            objects.append(
                {
                    "business_id": business_id,
                    "type": self._TYPE_BY_PREFIX.get(occurrence.prefix, "network_device"),
                    "label": business_id,
                    "canvas_id": frame.canvas_id,
                    "bbox": [round(value, 3) for value in bbox],
                    "confidence": occurrence.confidence,
                    "attributes": attributes,
                }
            )

        object_ids = set(selected)
        relations_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        for connector in evidence.connectors:
            if connector.source not in object_ids or connector.target not in object_ids:
                continue
            relation_type = "topology_link"
            relation_attributes: dict[str, Any] = {
                "evidence": connector.evidence,
                "direction": "undirected",
                "directed": False,
            }
            if connector.line_style:
                relation_attributes["line_style"] = connector.line_style
            if connector.line_color:
                relation_attributes["line_color"] = connector.line_color
            if connector.weight is not None:
                relation_attributes["weight"] = connector.weight
            relations_by_key[(connector.source, connector.target, relation_type)] = {
                "relation_id": f"local-line:{connector.source}:{connector.target}",
                "source": connector.source,
                "target": connector.target,
                "type": relation_type,
                "confidence": round(max(0.0, min(1.0, connector.confidence)), 4),
                "attributes": relation_attributes,
            }
        for relation in table_relations:
            source = str(relation.get("source", ""))
            target = str(relation.get("target", ""))
            if source not in object_ids or target not in object_ids:
                continue
            relation_type = str(relation.get("type", "relation"))
            relations_by_key.setdefault((source, target, relation_type), dict(relation))
        links = sorted(
            relations_by_key.values(),
            key=lambda item: (str(item["source"]), str(item["target"]), str(item["type"])),
        )
        scores = [float(item["confidence"]) for item in objects]
        scores.extend(float(item["confidence"]) for item in links)
        global_confidence = round(sum(scores) / len(scores), 4) if scores else 0.0
        return {
            "schema_version": RESPONSE_SCHEMA_VERSION,
            "confidence": global_confidence,
            "objects": objects,
            "links": links,
            "co_channel_relations": [],
        }


__all__ = [
    "CVTopologyEvidence",
    "DetectedConnector",
    "DeviceOccurrence",
    "LocalCVTopologyVisionAdapter",
    "LocalImageBackend",
    "LocalVisionDependencyError",
    "LocalVisionRecognitionError",
    "OCRSpan",
    "RapidOCROpenCVBackend",
]
