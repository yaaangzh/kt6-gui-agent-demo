from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .codeagent_canvas_vision import (
    CodeAgentCanvasVisionAdapter,
    CodeAgentProcessResult,
    CodeAgentRunner,
    CodeAgentVisionError,
    SubprocessCodeAgentRunner,
)
from .topology_artifact_common import (
    TopologyArtifactCLIError,
    build_image_input,
    ensure_distinct_paths,
    normalize_cv_context,
    write_json,
)
from .topology_fusion_cli import load_json


class RecordingCodeAgentRunner:
    """Persist successful CodeAgent stdout before response validation."""

    def __init__(
        self,
        output_path: Path,
        *,
        delegate: CodeAgentRunner | None = None,
    ) -> None:
        self.output_path = output_path
        self.delegate = delegate or SubprocessCodeAgentRunner()

    def run(self, **kwargs: Any) -> CodeAgentProcessResult:
        result = self.delegate.run(**kwargs)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_bytes(result.stdout)
        return result


def generate_model_artifact(
    image_path: Path,
    *,
    source_id: str,
    output_path: Path,
    events_path: Path,
    cv_path: Path | None = None,
    executable: str = "codeagent",
    agent: str | None = None,
    timeout_seconds: float = 600.0,
    workdir: Path | None = None,
    runner: CodeAgentRunner | None = None,
) -> dict[str, Any]:
    ensure_distinct_paths(image_path, cv_path, output_path, events_path)
    for stale_path in (output_path, events_path):
        try:
            stale_path.unlink(missing_ok=True)
        except OSError as exc:
            raise TopologyArtifactCLIError(
                f"cannot replace stale artifact: {stale_path}"
            ) from exc
    page, frames = build_image_input(image_path, source_id)
    cv_context = normalize_cv_context(load_json(cv_path)) if cv_path else None
    recording_runner = RecordingCodeAgentRunner(events_path, delegate=runner)
    adapter = CodeAgentCanvasVisionAdapter(
        workdir=(workdir or Path.cwd()),
        executable=executable,
        agent=agent,
        timeout_seconds=timeout_seconds,
        runner=recording_runner,
    )
    if cv_context is None:
        result = adapter.recognize(page=page, frames=frames)
    else:
        result = adapter.recognize_with_context(
            page=page,
            frames=frames,
            cv_observations=cv_context,
        )
    write_json(output_path, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Ask CodeAgent to inspect one topology image and save both its "
            "validated model JSON and raw stream-json events."
        )
    )
    parser.add_argument("image", type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--cv", type=Path, help="optional local-CV artifact")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--executable", default="codeagent")
    parser.add_argument("--agent")
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = generate_model_artifact(
            args.image,
            source_id=args.source_id,
            output_path=args.out,
            events_path=args.events,
            cv_path=args.cv,
            executable=args.executable,
            agent=args.agent,
            timeout_seconds=args.timeout,
            workdir=args.workdir,
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "artifact": str(args.out.resolve()),
                    "events": str(args.events.resolve()),
                    "object_count": len(result.get("objects", [])),
                    "link_count": len(result.get("links", [])),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (CodeAgentVisionError, TopologyArtifactCLIError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "events": (
                        str(args.events.resolve()) if args.events.exists() else None
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
