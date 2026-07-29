from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

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
from .vision_recognition import CanvasFrame, CanvasVisionAdapter


CV_ARTIFACT_METADATA_SCHEMA_VERSION = "kt6.cv-artifact-metadata.v1"


def build_cv_artifact_metadata(
    *,
    source_id: str,
    frames: Sequence[CanvasFrame],
    adapter: CanvasVisionAdapter,
    artifact_sha256: str,
) -> dict[str, Any]:
    """Bind a reusable CV artifact to the exact source image and adapter."""

    adapter_id = str(
        getattr(adapter, "adapter_id", adapter.__class__.__name__)
    ).strip()
    adapter_version = str(getattr(adapter, "adapter_version", "unknown")).strip()
    if not adapter_id or not adapter_version:
        raise TopologyArtifactCLIError(
            "CV adapter identity is required for reusable artifact metadata"
        )
    return {
        "schema_version": CV_ARTIFACT_METADATA_SCHEMA_VERSION,
        "source_id": str(source_id).strip(),
        "adapter_id": adapter_id,
        "adapter_version": adapter_version,
        "frames": _frame_records(frames),
        "artifact_sha256": artifact_sha256,
    }


def validate_cv_artifact_metadata(
    image_path: Path,
    *,
    source_id: str,
    metadata: Mapping[str, Any],
    cv_artifact_path: Path | None = None,
) -> dict[str, Any]:
    """Reject stale --reuse-cv artifacts before they can influence routing."""

    if not isinstance(metadata, Mapping):
        raise TopologyArtifactCLIError("CV artifact metadata must be an object")
    if metadata.get("schema_version") != CV_ARTIFACT_METADATA_SCHEMA_VERSION:
        raise TopologyArtifactCLIError(
            "CV artifact metadata schema is missing or unsupported; rerun without --reuse-cv"
        )
    normalized_source = str(source_id).strip()
    if metadata.get("source_id") != normalized_source:
        raise TopologyArtifactCLIError(
            "reused CV artifact source-id does not match the current request"
        )
    adapter_id = metadata.get("adapter_id")
    adapter_version = metadata.get("adapter_version")
    if not isinstance(adapter_id, str) or not adapter_id.strip():
        raise TopologyArtifactCLIError("CV artifact metadata adapter-id is missing")
    if not isinstance(adapter_version, str) or not adapter_version.strip():
        raise TopologyArtifactCLIError(
            "CV artifact metadata adapter-version is missing"
        )
    if (
        adapter_id != LocalCVTopologyVisionAdapter.adapter_id
        or adapter_version != LocalCVTopologyVisionAdapter.adapter_version
    ):
        raise TopologyArtifactCLIError(
            "CV artifact metadata uses an unsupported local CV adapter version"
        )
    _page, frames = build_image_input(image_path, normalized_source)
    if metadata.get("frames") != _frame_records(frames):
        raise TopologyArtifactCLIError(
            "reused CV artifact does not match the current image bytes or dimensions"
        )
    artifact_sha256 = metadata.get("artifact_sha256")
    if (
        not isinstance(artifact_sha256, str)
        or len(artifact_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in artifact_sha256
        )
    ):
        raise TopologyArtifactCLIError(
            "CV artifact metadata payload hash is missing or invalid"
        )
    if (
        cv_artifact_path is not None
        and _sha256_file(cv_artifact_path) != artifact_sha256
    ):
        raise TopologyArtifactCLIError(
            "reused cv-result.json does not match its metadata payload hash"
        )
    return dict(metadata)


def _frame_records(frames: Sequence[CanvasFrame]) -> list[dict[str, Any]]:
    return [
        {
            "canvas_id": frame.canvas_id,
            "sha256": frame.screenshot_sha256,
            "mime_type": frame.mime_type,
            "width": frame.width,
            "height": frame.height,
        }
        for frame in frames
    ]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate_cv_artifact(
    image_path: Path,
    *,
    source_id: str,
    output_path: Path,
    adapter: CanvasVisionAdapter | None = None,
    metadata_output_path: Path | None = None,
) -> dict[str, Any]:
    ensure_distinct_paths(image_path, output_path, metadata_output_path)
    page, frames = build_image_input(image_path, source_id)
    vision = adapter or LocalCVTopologyVisionAdapter()
    result = vision.recognize(page=page, frames=frames)
    if not isinstance(result, dict):
        raise LocalVisionRecognitionError(
            "local CV did not recognize any topology objects"
        )
    write_json(output_path, result)
    if metadata_output_path is not None:
        write_json(
            metadata_output_path,
            build_cv_artifact_metadata(
                source_id=source_id,
                frames=frames,
                adapter=vision,
                artifact_sha256=_sha256_file(output_path),
            ),
        )
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
    parser.add_argument(
        "--metadata-out",
        type=Path,
        help=(
            "optional reusable-artifact metadata bound to image SHA-256 and dimensions"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = generate_cv_artifact(
            args.image,
            source_id=args.source_id,
            output_path=args.out,
            metadata_output_path=args.metadata_out,
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "artifact": str(args.out.resolve()),
                    "object_count": len(result.get("objects", [])),
                    "link_count": len(result.get("links", [])),
                    "metadata": (
                        str(args.metadata_out.resolve())
                        if args.metadata_out is not None
                        else None
                    ),
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
