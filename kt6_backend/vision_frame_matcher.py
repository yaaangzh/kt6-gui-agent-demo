from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class FrameMatchResult:
    """A conservative, analysis-only old-frame to new-frame transform."""

    reusable: bool
    scale_x: float
    scale_y: float
    translate_x: float
    translate_y: float
    similarity: float
    reason: str
    overlap_ratio: float = 0.0

    @property
    def safe_to_reuse(self) -> bool:
        return self.reusable

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# The matcher deliberately accepts only very small registration residuals. A
# false miss costs another model call; a false hit can hide a topology change.
_MAX_ASPECT_LOG_DELTA = 0.015
_MIN_SCALE = 0.5
_MAX_SCALE = 2.0
_MIN_OVERLAP = 0.92
_MIN_FEATURE_MATCHES = 12
_MIN_INLIER_COUNT = 10
_MIN_INLIER_RATIO = 0.72
_MIN_FEATURE_SPAN = 0.30
_MAX_ROTATION_DEGREES = 0.35
_MAX_MEAN_ABS_ERROR = 1.25
_MAX_CHANGED_PIXEL_RATIO = 0.002
_CHANGED_PIXEL_THRESHOLD = 16
_MAX_UNCOVERED_STDDEV = 8.0
_MAX_UNCOVERED_EDGE_RATIO = 0.003


def match_vision_frames(
    old_path: str | Path,
    new_path: str | Path,
    old_size: Sequence[int],
    new_size: Sequence[int],
) -> FrameMatchResult:
    """Match two persisted screenshots using scale and translation only.

    ``old_size`` and ``new_size`` are pixel ``(width, height)`` pairs and must
    agree with the decoded files. OpenCV is optional: when it or NumPy cannot
    be imported, the function safely returns a miss.
    """

    old_dimensions = _dimensions(old_size)
    new_dimensions = _dimensions(new_size)
    if old_dimensions is None or new_dimensions is None:
        return _miss("invalid_dimensions")
    old_width, old_height = old_dimensions
    new_width, new_height = new_dimensions

    old_aspect = old_width / old_height
    new_aspect = new_width / new_height
    if abs(math.log(new_aspect / old_aspect)) > _MAX_ASPECT_LOG_DELTA:
        return _miss("aspect_ratio_mismatch")

    cv2, np = _load_opencv()
    if cv2 is None or np is None:
        return _miss("opencv_unavailable")

    try:
        old_image = cv2.imread(str(Path(old_path)), cv2.IMREAD_COLOR)
        new_image = cv2.imread(str(Path(new_path)), cv2.IMREAD_COLOR)
    except (OSError, TypeError, ValueError):
        return _miss("image_decode_failed")
    if old_image is None or new_image is None:
        return _miss("image_decode_failed")
    if (
        old_image.ndim != 3
        or new_image.ndim != 3
        or old_image.shape[1] != old_width
        or old_image.shape[0] != old_height
        or new_image.shape[1] != new_width
        or new_image.shape[0] != new_height
    ):
        return _miss("declared_size_mismatch")

    scale_x = new_width / old_width
    scale_y = new_height / old_height
    if not _scale_allowed(scale_x, scale_y):
        return _miss("scale_out_of_range", scale_x=scale_x, scale_y=scale_y)

    if old_dimensions == new_dimensions and np.array_equal(old_image, new_image):
        return _hit(
            reason="exact_pixel_match",
            scale_x=1.0,
            scale_y=1.0,
            translate_x=0.0,
            translate_y=0.0,
            similarity=1.0,
            overlap_ratio=1.0,
        )

    # First try a full-frame resize. It is both more deterministic and safer
    # than estimating a transform from a small cluster of repeated icons.
    interpolation = (
        cv2.INTER_AREA if scale_x < 1.0 or scale_y < 1.0 else cv2.INTER_LINEAR
    )
    resized = cv2.resize(old_image, (new_width, new_height), interpolation=interpolation)
    direct = _score_alignment(
        cv2,
        np,
        resized,
        new_image,
        np.full((new_height, new_width), 255, dtype=np.uint8),
        scale_x=scale_x,
        scale_y=scale_y,
        translate_x=0.0,
        translate_y=0.0,
        success_reason="full_frame_scale_match",
    )
    if direct.reusable:
        return direct

    try:
        old_gray = cv2.cvtColor(old_image, cv2.COLOR_BGR2GRAY)
        new_gray = cv2.cvtColor(new_image, cv2.COLOR_BGR2GRAY)
        detector = cv2.ORB_create(nfeatures=2000, fastThreshold=10)
        old_points, old_descriptors = detector.detectAndCompute(old_gray, None)
        new_points, new_descriptors = detector.detectAndCompute(new_gray, None)
    except (AttributeError, TypeError, ValueError):
        return _miss("feature_detection_failed")
    if (
        old_descriptors is None
        or new_descriptors is None
        or len(old_points) < _MIN_FEATURE_MATCHES
        or len(new_points) < _MIN_FEATURE_MATCHES
    ):
        return _miss("insufficient_features")

    try:
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        candidates = matcher.knnMatch(old_descriptors, new_descriptors, k=2)
    except (AttributeError, TypeError, ValueError):
        return _miss("feature_matching_failed")
    good_matches = [
        pair[0]
        for pair in candidates
        if len(pair) == 2 and pair[0].distance < 0.70 * pair[1].distance
    ]
    if len(good_matches) < _MIN_FEATURE_MATCHES:
        return _miss("insufficient_feature_matches")

    source_points = np.float32(
        [old_points[item.queryIdx].pt for item in good_matches]
    ).reshape(-1, 1, 2)
    target_points = np.float32(
        [new_points[item.trainIdx].pt for item in good_matches]
    ).reshape(-1, 1, 2)
    if not _features_cover_frame(
        np,
        source_points,
        old_width,
        old_height,
        target_points,
        new_width,
        new_height,
    ):
        return _miss("insufficient_feature_coverage")

    try:
        matrix, inliers = cv2.estimateAffinePartial2D(
            source_points,
            target_points,
            method=cv2.RANSAC,
            ransacReprojThreshold=2.0,
            maxIters=3000,
            confidence=0.995,
            refineIters=15,
        )
    except (AttributeError, TypeError, ValueError):
        return _miss("transform_estimation_failed")
    if matrix is None or inliers is None:
        return _miss("transform_estimation_failed")

    inlier_count = int(np.count_nonzero(inliers))
    inlier_ratio = inlier_count / len(good_matches)
    if inlier_count < _MIN_INLIER_COUNT or inlier_ratio < _MIN_INLIER_RATIO:
        return _miss("insufficient_transform_inliers")

    a = float(matrix[0, 0])
    b = float(matrix[0, 1])
    c = float(matrix[1, 0])
    d = float(matrix[1, 1])
    translate_x = float(matrix[0, 2])
    translate_y = float(matrix[1, 2])
    estimated_scale_x = math.hypot(a, c)
    estimated_scale_y = math.hypot(b, d)
    rotation_degrees = math.degrees(math.atan2(c, a))
    if (
        not all(
            math.isfinite(value)
            for value in (
                estimated_scale_x,
                estimated_scale_y,
                translate_x,
                translate_y,
                rotation_degrees,
            )
        )
        or abs(rotation_degrees) > _MAX_ROTATION_DEGREES
        or abs(b) > max(0.005, 0.0075 * estimated_scale_y)
        or abs(c) > max(0.005, 0.0075 * estimated_scale_x)
    ):
        return _miss("rotation_or_shear_detected")

    # Re-score with the off-diagonal terms removed. A transform that only works
    # when even a tiny rotation is retained is intentionally not reusable.
    axis_scale = (estimated_scale_x + estimated_scale_y) / 2.0
    estimated_scale_x = axis_scale
    estimated_scale_y = axis_scale
    if not _scale_allowed(estimated_scale_x, estimated_scale_y):
        return _miss(
            "scale_out_of_range",
            scale_x=estimated_scale_x,
            scale_y=estimated_scale_y,
            translate_x=translate_x,
            translate_y=translate_y,
        )
    axis_matrix = np.float32(
        [
            [estimated_scale_x, 0.0, translate_x],
            [0.0, estimated_scale_y, translate_y],
        ]
    )
    warped = cv2.warpAffine(
        old_image,
        axis_matrix,
        (new_width, new_height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )
    old_mask = np.full((old_height, old_width), 255, dtype=np.uint8)
    overlap_mask = cv2.warpAffine(
        old_mask,
        axis_matrix,
        (new_width, new_height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    return _score_alignment(
        cv2,
        np,
        warped,
        new_image,
        overlap_mask,
        scale_x=estimated_scale_x,
        scale_y=estimated_scale_y,
        translate_x=translate_x,
        translate_y=translate_y,
        success_reason="scale_translation_match",
    )


def remap_bbox(
    bbox: Sequence[float],
    match: FrameMatchResult | Mapping[str, Any],
) -> list[float]:
    scale_x, scale_y, translate_x, translate_y = _safe_transform(match)
    values = _finite_vector(bbox, 4, "bbox")
    x, y, width, height = values
    return [
        _rounded(x * scale_x + translate_x),
        _rounded(y * scale_y + translate_y),
        _rounded(width * scale_x),
        _rounded(height * scale_y),
    ]


def remap_center(
    center: Sequence[float],
    match: FrameMatchResult | Mapping[str, Any],
) -> list[float]:
    scale_x, scale_y, translate_x, translate_y = _safe_transform(match)
    x, y = _finite_vector(center, 2, "center")
    return [
        _rounded(x * scale_x + translate_x),
        _rounded(y * scale_y + translate_y),
    ]


def remap_vision_result(
    result: Mapping[str, Any],
    match: FrameMatchResult | Mapping[str, Any],
) -> dict[str, Any]:
    """Deep-copy and remap recognized geometry, forcing analysis-only output."""

    if not isinstance(result, Mapping):
        raise TypeError("result must be a mapping")
    _safe_transform(match)
    transformed = _remap_value(copy.deepcopy(dict(result)), match)
    transformed["analysis_only"] = True
    transformed["actionable_grounding"] = False
    transformed["interaction_eligible"] = False
    transformed["frame_cache_reuse"] = {
        "status": "analysis_only",
        "reason": "vision_frame_scale_translation_reuse",
        **_match_metadata(match),
    }
    provenance = transformed.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
        transformed["provenance"] = provenance
    provenance.update(
        {
            "actionable_grounding": False,
            "analysis_only": True,
            "geometry_source": "cached_vision_frame_transform",
        }
    )
    return transformed


def _load_opencv() -> tuple[Any | None, Any | None]:
    try:
        import cv2  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except (ImportError, OSError):
        return None, None
    return cv2, np


def _score_alignment(
    cv2: Any,
    np: Any,
    aligned_old: Any,
    new_image: Any,
    overlap_mask: Any,
    *,
    scale_x: float,
    scale_y: float,
    translate_x: float,
    translate_y: float,
    success_reason: str,
) -> FrameMatchResult:
    mask = overlap_mask > 0
    overlap_count = int(np.count_nonzero(mask))
    total_count = int(mask.size)
    overlap_ratio = overlap_count / total_count if total_count else 0.0
    if overlap_ratio < _MIN_OVERLAP:
        return _miss(
            "insufficient_overlap",
            scale_x=scale_x,
            scale_y=scale_y,
            translate_x=translate_x,
            translate_y=translate_y,
            overlap_ratio=overlap_ratio,
        )

    # Remove interpolation uncertainty at the transformed image boundary.
    if overlap_ratio < 1.0:
        kernel = np.ones((3, 3), dtype=np.uint8)
        comparison_mask = cv2.erode(overlap_mask, kernel, iterations=1) > 0
    else:
        comparison_mask = mask
    comparison_count = int(np.count_nonzero(comparison_mask))
    if comparison_count == 0:
        return _miss("insufficient_overlap")

    difference = cv2.absdiff(aligned_old, new_image)
    selected = difference[comparison_mask]
    mean_abs_error = float(selected.mean())
    max_channel_difference = difference.max(axis=2)
    changed_ratio = float(
        np.count_nonzero(
            max_channel_difference[comparison_mask] > _CHANGED_PIXEL_THRESHOLD
        )
        / comparison_count
    )
    similarity = max(
        0.0,
        min(1.0, 1.0 - max(mean_abs_error / 255.0, changed_ratio)),
    )
    if (
        mean_abs_error > _MAX_MEAN_ABS_ERROR
        or changed_ratio > _MAX_CHANGED_PIXEL_RATIO
    ):
        return _miss(
            "pixel_or_color_change_detected",
            scale_x=scale_x,
            scale_y=scale_y,
            translate_x=translate_x,
            translate_y=translate_y,
            similarity=similarity,
            overlap_ratio=overlap_ratio,
        )

    if overlap_ratio < 1.0:
        uncovered = ~mask
        if np.any(uncovered):
            new_gray = cv2.cvtColor(new_image, cv2.COLOR_BGR2GRAY)
            uncovered_values = new_gray[uncovered]
            uncovered_stddev = float(uncovered_values.std())
            edges = cv2.Canny(new_gray, 64, 160)
            uncovered_edge_ratio = float(
                np.count_nonzero(edges[uncovered]) / uncovered_values.size
            )
            if (
                uncovered_stddev > _MAX_UNCOVERED_STDDEV
                or uncovered_edge_ratio > _MAX_UNCOVERED_EDGE_RATIO
            ):
                return _miss(
                    "uncovered_content_detected",
                    scale_x=scale_x,
                    scale_y=scale_y,
                    translate_x=translate_x,
                    translate_y=translate_y,
                    similarity=similarity,
                    overlap_ratio=overlap_ratio,
                )

    return _hit(
        reason=success_reason,
        scale_x=scale_x,
        scale_y=scale_y,
        translate_x=translate_x,
        translate_y=translate_y,
        similarity=similarity,
        overlap_ratio=overlap_ratio,
    )


def _features_cover_frame(
    np: Any,
    source_points: Any,
    old_width: int,
    old_height: int,
    target_points: Any,
    new_width: int,
    new_height: int,
) -> bool:
    source = source_points.reshape(-1, 2)
    target = target_points.reshape(-1, 2)
    return bool(
        float(np.ptp(source[:, 0])) / old_width >= _MIN_FEATURE_SPAN
        and float(np.ptp(source[:, 1])) / old_height >= _MIN_FEATURE_SPAN
        and float(np.ptp(target[:, 0])) / new_width >= _MIN_FEATURE_SPAN
        and float(np.ptp(target[:, 1])) / new_height >= _MIN_FEATURE_SPAN
    )


def _scale_allowed(scale_x: float, scale_y: float) -> bool:
    return bool(
        math.isfinite(scale_x)
        and math.isfinite(scale_y)
        and _MIN_SCALE <= scale_x <= _MAX_SCALE
        and _MIN_SCALE <= scale_y <= _MAX_SCALE
        and abs(math.log(scale_x / scale_y)) <= _MAX_ASPECT_LOG_DELTA
    )


def _dimensions(value: Sequence[int]) -> tuple[int, int] | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 2:
        return None
    try:
        width = int(value[0])
        height = int(value[1])
    except (TypeError, ValueError, OverflowError):
        return None
    if width <= 0 or height <= 0 or width > 50_000 or height > 50_000:
        return None
    return width, height


def _miss(
    reason: str,
    *,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    translate_x: float = 0.0,
    translate_y: float = 0.0,
    similarity: float = 0.0,
    overlap_ratio: float = 0.0,
) -> FrameMatchResult:
    return FrameMatchResult(
        reusable=False,
        scale_x=_rounded(scale_x),
        scale_y=_rounded(scale_y),
        translate_x=_rounded(translate_x),
        translate_y=_rounded(translate_y),
        similarity=_unit_interval(similarity),
        reason=reason,
        overlap_ratio=_unit_interval(overlap_ratio),
    )


def _hit(
    *,
    reason: str,
    scale_x: float,
    scale_y: float,
    translate_x: float,
    translate_y: float,
    similarity: float,
    overlap_ratio: float,
) -> FrameMatchResult:
    return FrameMatchResult(
        reusable=True,
        scale_x=_rounded(scale_x),
        scale_y=_rounded(scale_y),
        translate_x=_rounded(translate_x),
        translate_y=_rounded(translate_y),
        similarity=_unit_interval(similarity),
        reason=reason,
        overlap_ratio=_unit_interval(overlap_ratio),
    )


def _safe_transform(
    match: FrameMatchResult | Mapping[str, Any],
) -> tuple[float, float, float, float]:
    if isinstance(match, FrameMatchResult):
        reusable = match.reusable
        values = (
            match.scale_x,
            match.scale_y,
            match.translate_x,
            match.translate_y,
        )
    elif isinstance(match, Mapping):
        reusable = match.get("reusable") is True
        values = (
            match.get("scale_x"),
            match.get("scale_y"),
            match.get("translate_x"),
            match.get("translate_y"),
        )
    else:
        raise TypeError("match must be a FrameMatchResult or mapping")
    if not reusable:
        raise ValueError("an unsafe frame match cannot be remapped")
    try:
        scale_x, scale_y, translate_x, translate_y = (
            float(value) for value in values
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("frame transform must contain finite numbers") from exc
    if (
        scale_x <= 0
        or scale_y <= 0
        or not all(
            math.isfinite(value)
            for value in (scale_x, scale_y, translate_x, translate_y)
        )
    ):
        raise ValueError("frame transform must contain positive finite scales")
    return scale_x, scale_y, translate_x, translate_y


def _finite_vector(value: Sequence[float], length: int, name: str) -> list[float]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or len(value) != length
    ):
        raise ValueError(f"{name} must contain {length} numbers")
    try:
        values = [float(item) for item in value]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must contain finite numbers") from exc
    if not all(math.isfinite(item) for item in values):
        raise ValueError(f"{name} must contain finite numbers")
    return values


def _remap_value(
    value: Any,
    match: FrameMatchResult | Mapping[str, Any],
) -> Any:
    if isinstance(value, list):
        return [_remap_value(item, match) for item in value]
    if not isinstance(value, dict):
        return value

    remapped: dict[str, Any] = {}
    for key, item in value.items():
        if key == "bbox" and _is_numeric_vector(item, 4):
            remapped[key] = remap_bbox(item, match)
        elif key == "center" and _is_numeric_vector(item, 2):
            remapped[key] = remap_center(item, match)
        else:
            remapped[key] = _remap_value(item, match)

    for flag in (
        "actionable",
        "actionable_grounding",
        "can_click_now",
        "clickable",
        "interaction_eligible",
        "safe_for_execution",
    ):
        if flag in remapped:
            remapped[flag] = False
    if any(
        key in remapped
        for key in ("bbox", "center", "business_id", "element_id", "canvas_ref")
    ):
        remapped["analysis_only"] = True
        remapped["actionable_grounding"] = False
        remapped["interaction_eligible"] = False
        interaction = remapped.get("interaction")
        if not isinstance(interaction, dict):
            interaction = {}
            remapped["interaction"] = interaction
        interaction.update(
            {
                "status": "analysis_only",
                "reason": "cached_vision_frame_transform",
                "actionable": False,
                "safe_for_execution": False,
            }
        )
    return remapped


def _match_metadata(match: FrameMatchResult | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(match, FrameMatchResult):
        values = match.to_dict()
    else:
        values = dict(match)
    return {
        key: copy.deepcopy(values.get(key))
        for key in (
            "scale_x",
            "scale_y",
            "translate_x",
            "translate_y",
            "similarity",
            "reason",
            "overlap_ratio",
        )
    }


def _is_numeric_vector(value: Any, length: int) -> bool:
    try:
        _finite_vector(value, length, "geometry")
    except ValueError:
        return False
    return True


def _rounded(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return round(number, 6) if math.isfinite(number) else 0.0


def _unit_interval(value: float) -> float:
    return _rounded(max(0.0, min(1.0, float(value))))


__all__ = [
    "FrameMatchResult",
    "match_vision_frames",
    "remap_bbox",
    "remap_center",
    "remap_vision_result",
]
