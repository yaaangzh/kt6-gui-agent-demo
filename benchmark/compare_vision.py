"""Compare two page-capture JSON artifacts (baseline hybrid vs OmniParser).

Usage:
    python -m benchmark.compare_vision baseline.json omniparser.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load(path: str) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise SystemExit(f"{path} is not a JSON object")
    return value


def _row(capture: dict[str, Any], name: str) -> dict[str, str]:
    summary = capture.get("summary", {})
    if not isinstance(summary, dict):
        summary = {}
    scene = capture.get("scene", {})
    if not isinstance(scene, dict):
        scene = {}
    canvas = scene.get("canvas_perception")
    if not isinstance(canvas, dict):
        canvas = capture.get("canvas_perception") or {}
    if not isinstance(canvas, dict):
        canvas = {}
    omni = canvas.get("omniparser_benchmark") or summary.get("omniparser_benchmark")
    if not isinstance(omni, dict):
        omni = {}
    return {
        "label": name,
        "selected_mode": str(summary.get("selected_mode", "-")),
        "scene_type": str(scene.get("scene_type", "-")),
        "objects": str(canvas.get("object_count", scene.get("object_count", "-"))),
        "links": str(canvas.get("relation_count", scene.get("relation_count", "-"))),
        "vision_cache": str(summary.get("vision_cache_status", "-")),
        "mode": str(omni.get("mode", "-")),
        "parsed": str(omni.get("parsed", "-")),
        "elements": str(omni.get("element_count", "-")),
        "texts": str(omni.get("text_count", "-")),
        "icons": str(omni.get("icon_count", "-")),
        "model_ms": str(omni.get("model_ms", "-")),
        "total_ms": str(summary.get("perception_ms", "-")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two vision capture artifacts")
    parser.add_argument("baseline", help="baseline capture JSON (hybrid)")
    parser.add_argument("candidate", help="candidate capture JSON (omniparser)")
    args = parser.parse_args()
    baseline = _load(args.baseline)
    candidate = _load(args.candidate)
    rows = [_row(baseline, "baseline"), _row(candidate, "candidate")]
    headers = list(rows[0].keys())
    widths = {
        header: max(len(header), *(len(row[header]) for row in rows))
        for header in headers
    }
    print(" | ".join(header.ljust(widths[header]) for header in headers))
    print("-|-".join("-" * widths[header] for header in headers))
    for row in rows:
        print(" | ".join(row[header].ljust(widths[header]) for header in headers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
