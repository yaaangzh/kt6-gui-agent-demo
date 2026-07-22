from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .topology_fusion import TopologyFusionError, fuse_topology_payloads


MAX_JSON_BYTES = 10 * 1024 * 1024


def load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TopologyFusionError(f"cannot read JSON file: {path}") from exc
    if not raw:
        raise TopologyFusionError(f"JSON file is empty: {path}")
    if len(raw) > MAX_JSON_BYTES:
        raise TopologyFusionError(f"JSON file exceeds 10 MB: {path}")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TopologyFusionError(f"file is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise TopologyFusionError(f"JSON root must be an object: {path}")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fuse a KT6 local-CV result with a multimodal-model topology JSON."
    )
    parser.add_argument("cv_json", type=Path, help="KT6 CV capture/result JSON")
    parser.add_argument("model_json", type=Path, help="multimodal-model topology JSON")
    parser.add_argument("--out", type=Path, required=True, help="fused UTF-8 JSON output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        fused = fuse_topology_payloads(load_json(args.cv_json), load_json(args.model_json))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(fused, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(fused["summary"], ensure_ascii=False, indent=2))
        return 0
    except (TopologyFusionError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
