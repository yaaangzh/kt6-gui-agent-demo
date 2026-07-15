from __future__ import annotations

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


@dataclass(frozen=True)
class CVTopologyEvidence:
    node_boxes: Mapping[str, Box] = field(default_factory=dict)
    connectors: tuple[DetectedConnector, ...] = ()


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
    """Run RapidOCR and conservative orthogonal-line analysis in-process."""

    MAX_OCR_SPANS = 5000

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
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        _threshold, ink = cv2.threshold(
            gray,
            0,
            255,
            cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU,
        )

        cleaned = ink.copy()
        for span in spans:
            x, y, width, height = span.bbox
            if y >= crop_height:
                continue
            left = max(0, int(math.floor(x)) - 2)
            top = max(0, int(math.floor(y)) - 2)
            right = min(frame.width - 1, int(math.ceil(x + width)) + 2)
            bottom = min(crop_height - 1, int(math.ceil(y + height)) + 2)
            cv2.rectangle(cleaned, (left, top), (right, bottom), 0, thickness=-1)

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
        line_mask = cv2.bitwise_or(horizontal, vertical)
        line_mask = cv2.dilate(
            line_mask,
            cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
            iterations=1,
        )
        if not bool(np.any(line_mask)):
            return CVTopologyEvidence()

        node_boxes = self._node_boxes(
            cleaned,
            diagram_nodes,
            frame_width=frame.width,
            frame_height=crop_height,
        )
        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
            line_mask, connectivity=8
        )
        if count <= 1:
            return CVTopologyEvidence(node_boxes=node_boxes)

        component_sets: dict[str, set[int]] = {}
        minimum_component_area = max(4, min(horizontal_length, vertical_length) // 2)
        for business_id, occurrence in diagram_nodes.items():
            box = node_boxes.get(business_id, occurrence.bbox)
            x, y, width, height = box
            margin = max(8, int(max(occurrence.bbox[3] * 1.5, 8)))
            left = max(0, int(math.floor(x)) - margin)
            top = max(0, int(math.floor(y)) - margin)
            right = min(frame.width, int(math.ceil(x + width)) + margin)
            bottom = min(crop_height, int(math.ceil(y + height)) + margin)
            if left >= right or top >= bottom:
                component_sets[business_id] = set()
                continue
            values = np.unique(labels[top:bottom, left:right])
            components: set[int] = set()
            for raw_label in values.tolist():
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

        layers = self._visual_layers(diagram_nodes)
        connectors: list[DetectedConnector] = []
        for layer_index in range(1, len(layers)):
            child_layer = layers[layer_index]
            for child_id in child_layer:
                child_components = component_sets.get(child_id, set())
                if not child_components:
                    continue
                chosen_parent: str | None = None
                for parent_layer_index in range(layer_index - 1, -1, -1):
                    candidates = [
                        parent_id
                        for parent_id in layers[parent_layer_index]
                        if child_components & component_sets.get(parent_id, set())
                    ]
                    if not candidates:
                        continue
                    child_x = diagram_nodes[child_id].center[0]
                    chosen_parent = min(
                        candidates,
                        key=lambda parent_id: (
                            abs(diagram_nodes[parent_id].center[0] - child_x),
                            parent_id,
                        ),
                    )
                    break
                if chosen_parent is not None and chosen_parent != child_id:
                    confidence = min(
                        0.9,
                        diagram_nodes[chosen_parent].confidence,
                        diagram_nodes[child_id].confidence,
                    )
                    connectors.append(
                        DetectedConnector(chosen_parent, child_id, confidence)
                    )

        connectors.sort(key=lambda item: (item.source, item.target))
        return CVTopologyEvidence(
            node_boxes=node_boxes,
            connectors=tuple(connectors),
        )

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
            occurrence_area = occurrence.bbox[2] * occurrence.bbox[3]
            candidates = []
            for box in contour_boxes:
                x, y, width, height = box
                if not (x <= center_x <= x + width and y <= center_y <= y + height):
                    continue
                if width < occurrence.bbox[2] + 4 or height < occurrence.bbox[3] + 4:
                    continue
                if width * height < occurrence_area * 1.2:
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

    @staticmethod
    def _visual_layers(
        nodes: Mapping[str, DeviceOccurrence],
    ) -> list[list[str]]:
        ordered = sorted(nodes, key=lambda item: (nodes[item].center[1], nodes[item].center[0], item))
        if not ordered:
            return []
        text_heights = [nodes[item].bbox[3] for item in ordered]
        tolerance = max(12.0, median(text_heights) * 1.5)
        layers: list[list[str]] = []
        layer_centers: list[float] = []
        for business_id in ordered:
            center_y = nodes[business_id].center[1]
            if not layers or abs(center_y - layer_centers[-1]) > tolerance:
                layers.append([business_id])
                layer_centers.append(center_y)
                continue
            layers[-1].append(business_id)
            layer_centers[-1] = sum(nodes[item].center[1] for item in layers[-1]) / len(
                layers[-1]
            )
        for layer in layers:
            layer.sort(key=lambda item: (nodes[item].center[0], item))
        return layers


class LocalCVTopologyVisionAdapter:
    """Recognize a single topology image without an Agent or external service."""

    adapter_id = "local-cv-ocr"
    adapter_version = "1.0"
    supports_actionable_grounding = False

    DEFAULT_MIN_OCR_CONFIDENCE = 0.65
    DEFAULT_MAX_IMAGE_PIXELS = 20_000_000
    _SERIAL_GATE = threading.BoundedSemaphore(value=1)
    _DEVICE_PATTERN = re.compile(
        r"(?<![A-Z0-9])"
        r"(CORE|AGG|ACC|LSW|ONU|RTR|GW|AP|AC|FW|SW)"
        r"[\s_\-\u2013\u2014:\uff1a]*"
        r"([0-9OIL]{2,8})"
        r"(?![A-Z0-9])",
        re.IGNORECASE,
    )
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
        for span_index, span in enumerate(spans):
            if span.confidence < self.min_ocr_confidence:
                continue
            normalized = unicodedata.normalize("NFKC", span.text).upper()
            for match in self._DEVICE_PATTERN.finditer(normalized):
                prefix = match.group(1).upper()
                raw_suffix = match.group(2).upper()
                suffix = raw_suffix.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))
                corrected = suffix != raw_suffix
                confidence = max(0.0, span.confidence - (0.08 if corrected else 0.0))
                if confidence < self.min_ocr_confidence:
                    continue
                bbox = self._match_bbox(span.bbox, match.start(), match.end(), len(normalized))
                bbox = self._clamp_box(bbox, frame_width, frame_height)
                occurrences.append(
                    DeviceOccurrence(
                        business_id=f"{prefix}-{suffix}",
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
            relations_by_key[(connector.source, connector.target, relation_type)] = {
                "relation_id": f"local-line:{connector.source}:{connector.target}",
                "source": connector.source,
                "target": connector.target,
                "type": relation_type,
                "confidence": round(max(0.0, min(1.0, connector.confidence)), 4),
                "attributes": {
                    "evidence": "orthogonal_pixel_connector",
                    "direction": "top_to_bottom_projection",
                },
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
