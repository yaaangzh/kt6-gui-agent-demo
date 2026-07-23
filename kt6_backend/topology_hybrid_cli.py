from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .codeagent_canvas_vision import CodeAgentVisionError
from .local_cv_canvas_vision import (
    LocalVisionDependencyError,
    LocalVisionRecognitionError,
)
from .topology_artifact_common import TopologyArtifactCLIError, write_json
from .topology_cv_cli import generate_cv_artifact
from .topology_fusion import TopologyFusionError, fuse_topology_payloads
from .topology_model_cli import generate_model_artifact


def run_pipeline(
    image_path: Path,
    *,
    source_id: str,
    output_dir: Path,
    executable: str = "codeagent",
    agent: str | None = None,
    timeout_seconds: float = 600.0,
    workdir: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cv_path = output_dir / "cv-result.json"
    model_path = output_dir / "model-result.json"
    events_path = output_dir / "codeagent-events.jsonl"
    fused_path = output_dir / "fused-result.json"

    for stale_path in (cv_path, model_path, events_path, fused_path):
        try:
            stale_path.unlink(missing_ok=True)
        except OSError as exc:
            raise TopologyArtifactCLIError(
                f"cannot replace stale artifact: {stale_path}"
            ) from exc

    cv_result = generate_cv_artifact(
        image_path,
        source_id=source_id,
        output_path=cv_path,
    )
    model_result = generate_model_artifact(
        image_path,
        source_id=source_id,
        output_path=model_path,
        events_path=events_path,
        cv_path=cv_path,
        executable=executable,
        agent=agent,
        timeout_seconds=timeout_seconds,
        workdir=workdir,
    )
    write_json(fused_path, fuse_topology_payloads(cv_result, model_result))
    return {
        "cv": cv_path,
        "model": model_path,
        "events": events_path,
        "fused": fused_path,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run standalone local CV, standalone CodeAgent recognition, then "
            "offline topology fusion. Intermediate artifacts are retained."
        )
    )
    parser.add_argument("image", type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--executable", default="codeagent")
    parser.add_argument("--agent")
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        paths = run_pipeline(
            args.image,
            source_id=args.source_id,
            output_dir=args.out_dir,
            executable=args.executable,
            agent=args.agent,
            timeout_seconds=args.timeout,
            workdir=args.workdir,
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    **{
                        name: str(path.resolve())
                        for name, path in paths.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (
        CodeAgentVisionError,
        LocalVisionDependencyError,
        LocalVisionRecognitionError,
        TopologyArtifactCLIError,
        TopologyFusionError,
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
