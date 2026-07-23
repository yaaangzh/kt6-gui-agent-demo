from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .local_cv_canvas_vision import (
    LocalCVTopologyVisionAdapter,
    LocalVisionDependencyError,
    LocalVisionRecognitionError,
)
from .topology_artifact_common import (
    TopologyArtifactCLIError,
    build_image_input,
    ensure_distinct_paths,
    write_json,
)
from .vision_recognition import CanvasVisionAdapter


def generate_cv_artifact(
    image_path: Path,
    *,
    source_id: str,
    output_path: Path,
    adapter: CanvasVisionAdapter | None = None,
) -> dict[str, Any]:
    ensure_distinct_paths(image_path, output_path)
    page, frames = build_image_input(image_path, source_id)
    vision = adapter or LocalCVTopologyVisionAdapter()
    result = vision.recognize(page=page, frames=frames)
    if not isinstance(result, dict):
        raise LocalVisionRecognitionError(
            "local CV did not recognize any topology objects"
        )
    write_json(output_path, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recognize one topology image locally and save the raw CV artifact. "
            "No KT6 HTTP server or Agent is used."
        )
    )
    parser.add_argument("image", type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = generate_cv_artifact(
            args.image,
            source_id=args.source_id,
            output_path=args.out,
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "artifact": str(args.out.resolve()),
                    "object_count": len(result.get("objects", [])),
                    "link_count": len(result.get("links", [])),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (
        LocalVisionDependencyError,
        LocalVisionRecognitionError,
        TopologyArtifactCLIError,
        OSError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"error": str(exc), "error_type": type(exc).__name__},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
