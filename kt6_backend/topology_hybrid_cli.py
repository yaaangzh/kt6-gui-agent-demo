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
from .topology_fusion_cli import load_json
from .topology_model_cli import generate_model_artifact


def run_pipeline(
    image_path: Path,
    *,
    source_id: str,
    output_dir: Path,
    executable: str = "codeagent",
    agent: str | None = None,
    permission_mode: str = "dontAsk",
    timeout_seconds: float = 600.0,
    workdir: Path | None = None,
    reuse_cv: bool = False,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cv_path = output_dir / "cv-result.json"
    model_path = output_dir / "model-result.json"
    events_path = output_dir / "codeagent-events.jsonl"
    stderr_path = output_dir / "codeagent-stderr.log"
    fused_path = output_dir / "fused-result.json"

    stale_paths = (
        (model_path, events_path, stderr_path, fused_path)
        if reuse_cv
        else (cv_path, model_path, events_path, stderr_path, fused_path)
    )
    for stale_path in stale_paths:
        try:
            stale_path.unlink(missing_ok=True)
        except OSError as exc:
            raise TopologyArtifactCLIError(
                f"cannot replace stale artifact: {stale_path}"
            ) from exc

    if reuse_cv:
        if not cv_path.is_file():
            raise TopologyArtifactCLIError(
                f"cannot reuse missing CV artifact: {cv_path}"
            )
        cv_result = load_json(cv_path)
    else:
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
        stderr_path=stderr_path,
        cv_path=cv_path,
        executable=executable,
        agent=agent,
        permission_mode=permission_mode,
        timeout_seconds=timeout_seconds,
        workdir=workdir,
    )
    write_json(fused_path, fuse_topology_payloads(cv_result, model_result))
    return {
        "cv": cv_path,
        "model": model_path,
        "events": events_path,
        "stderr": stderr_path,
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
    parser.add_argument(
        "--permission-mode",
        choices=("dontAsk", "bypassPermissions"),
        default="dontAsk",
    )
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    parser.add_argument(
        "--reuse-cv",
        action="store_true",
        help="keep and reuse an existing cv-result.json; retry only model/fusion",
    )
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
            permission_mode=args.permission_mode,
            timeout_seconds=args.timeout,
            workdir=args.workdir,
            reuse_cv=args.reuse_cv,
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
    except KeyboardInterrupt:
        events_path = args.out_dir / "codeagent-events.jsonl"
        stderr_path = args.out_dir / "codeagent-stderr.log"
        print(
            json.dumps(
                {
                    "error": "interrupted; CodeAgent process tree was terminated",
                    "error_type": "KeyboardInterrupt",
                    "events": (
                        str(events_path.resolve()) if events_path.exists() else None
                    ),
                    "stderr": (
                        str(stderr_path.resolve()) if stderr_path.exists() else None
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 130
    except (
        CodeAgentVisionError,
        LocalVisionDependencyError,
        LocalVisionRecognitionError,
        TopologyArtifactCLIError,
        TopologyFusionError,
        OSError,
        ValueError,
    ) as exc:
        events_path = args.out_dir / "codeagent-events.jsonl"
        stderr_path = args.out_dir / "codeagent-stderr.log"
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "events": (
                        str(events_path.resolve()) if events_path.exists() else None
                    ),
                    "stderr": (
                        str(stderr_path.resolve()) if stderr_path.exists() else None
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
