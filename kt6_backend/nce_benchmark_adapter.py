"""Read NCE benchmark screenshots as KT6 Canvas frames."""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .vision_recognition import CanvasFrame


@dataclass(frozen=True)
class NCEScreenshot:
    turn: int
    path: Path
    label: str

    @property
    def is_initial(self) -> bool:
        return self.turn == 0


@dataclass(frozen=True)
class NCEBenchmarkRun:
    run_id: str
    run_dir: Path
    intent: str
    category: str
    screenshots: tuple[NCEScreenshot, ...] = field(default=())
    episode_path: Path | None = None
    validation_path: Path | None = None
    config_path: Path | None = None
    passed: bool | None = None
    difficulty: str | None = None


def _classify_category(intent: str) -> str:
    lower = intent.casefold()
    if "topology" in lower or "topo" in lower:
        return "topology"
    if "alarm" in lower:
        return "alarm_monitor"
    if "network digital map" in lower:
        return "network_digital_map"
    if "network management" in lower:
        return "network_management"
    if "congestion" in lower:
        return "congestion_view"
    if "runbook" in lower:
        return "runbook"
    if any(item in lower for item in ("system", "license", "certificate")):
        return "system_settings"
    if any(item in lower for item in ("portal", "tile", "sorted")):
        return "portal"
    return "other"


def _parse_turn_number(filename: str) -> int | None:
    match = re.fullmatch(r"turn_(\d+)(?:_[A-Za-z0-9_-]+)?", filename)
    return int(match.group(1)) if match is not None else None


class NCEBenchmarkAdapter:
    """Convert the bounded, local NCE benchmark layout into KT6 inputs."""

    def __init__(self, benchmark_dir: Path) -> None:
        self.benchmark_dir = Path(benchmark_dir).expanduser().resolve()
        self._runs_dir = self.benchmark_dir / "runs"
        self._results_cache: dict[str, Any] | None = None

    def list_runs(self) -> list[NCEBenchmarkRun]:
        if not self._runs_dir.is_dir():
            return []
        runs: list[NCEBenchmarkRun] = []
        for entry in sorted(self._runs_dir.iterdir()):
            if not entry.is_dir():
                continue
            run = self._load_run(entry)
            if run is not None:
                runs.append(run)
        return runs

    def get_run(self, run_id: str) -> NCEBenchmarkRun | None:
        normalized = str(run_id).strip()
        if (
            not normalized
            or len(normalized) > 200
            or Path(normalized).name != normalized
        ):
            return None
        run_dir = self._runs_dir / normalized
        if not run_dir.is_dir():
            return None
        return self._load_run(run_dir)

    def screenshot_to_canvas_frame(self, shot: NCEScreenshot) -> CanvasFrame:
        raw = shot.path.read_bytes()
        width, height = self._png_dimensions(raw)
        return CanvasFrame(
            canvas_id=f"nce_{shot.label}",
            screenshot_path=shot.path.resolve(),
            screenshot_sha256=hashlib.sha256(raw).hexdigest(),
            mime_type="image/png",
            width=width,
            height=height,
            client_width=float(width),
            client_height=float(height),
            bbox=(0.0, 0.0, float(width), float(height)),
        )

    def load_episode(self, run: NCEBenchmarkRun) -> list[dict[str, Any]]:
        if run.episode_path is None or not run.episode_path.is_file():
            return []
        payload = self._read_json(run.episode_path)
        return (
            [dict(item) for item in payload if isinstance(item, dict)]
            if isinstance(payload, list)
            else []
        )

    def load_validation(self, run: NCEBenchmarkRun) -> dict[str, Any]:
        if run.validation_path is None or not run.validation_path.is_file():
            return {}
        payload = self._read_json(run.validation_path)
        return dict(payload) if isinstance(payload, dict) else {}

    def page_url_for_turn(self, run: NCEBenchmarkRun, turn: int) -> str:
        if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
            return ""
        state_index = 0
        for entry in self.load_episode(run):
            if entry.get("type") != "state":
                continue
            if state_index == turn:
                data = entry.get("data", {})
                info = data.get("info", {}) if isinstance(data, dict) else {}
                page = info.get("page", {}) if isinstance(info, dict) else {}
                url = page.get("url", "") if isinstance(page, dict) else ""
                return str(url)[:2048]
            state_index += 1
        return ""

    @staticmethod
    def is_topology_page(url: str) -> bool:
        lower = str(url).casefold()
        return any(
            keyword in lower
            for keyword in (
                "topology",
                "topo",
                "networkmap",
                "network-map",
                "digitalmap",
            )
        )

    def _load_run(self, run_dir: Path) -> NCEBenchmarkRun | None:
        run_id = run_dir.name
        config_path = run_dir / "collector_config.json"
        episode_path = run_dir / "episode_0.json"
        validation_path = run_dir / "validation_result.json"
        screenshots_dir = run_dir / "screenshots"

        config = self._read_json(config_path) if config_path.is_file() else {}
        intent = (
            str(config.get("intent", ""))
            if isinstance(config, dict)
            else ""
        )

        screenshots: list[NCEScreenshot] = []
        if screenshots_dir.is_dir():
            for png_path in sorted(screenshots_dir.glob("*.png")):
                turn = _parse_turn_number(png_path.stem)
                if turn is not None:
                    screenshots.append(
                        NCEScreenshot(
                            turn=turn,
                            path=png_path.resolve(),
                            label=png_path.stem,
                        )
                    )
        screenshots.sort(key=lambda item: (item.turn, item.label))

        validation = (
            self._read_json(validation_path)
            if validation_path.is_file()
            else {}
        )
        passed = (
            validation.get("passed")
            if isinstance(validation, dict)
            and isinstance(validation.get("passed"), bool)
            else None
        )
        return NCEBenchmarkRun(
            run_id=run_id,
            run_dir=run_dir.resolve(),
            intent=intent,
            category=_classify_category(intent),
            screenshots=tuple(screenshots),
            episode_path=episode_path.resolve() if episode_path.is_file() else None,
            validation_path=(
                validation_path.resolve() if validation_path.is_file() else None
            ),
            config_path=config_path.resolve() if config_path.is_file() else None,
            passed=passed,
            difficulty=self._get_difficulty(run_id),
        )

    def _get_difficulty(self, run_id: str) -> str | None:
        if self._results_cache is None:
            results_path = self.benchmark_dir / "benchmark_results.json"
            payload = self._read_json(results_path) if results_path.is_file() else {}
            self._results_cache = payload if isinstance(payload, dict) else {}
        results = self._results_cache.get("results", [])
        if not isinstance(results, list):
            return None
        for entry in results:
            if not isinstance(entry, dict):
                continue
            recorded_run = str(entry.get("run_dir", "")).replace("\\", "/")
            if recorded_run.rsplit("/", 1)[-1] == run_id:
                difficulty = entry.get("difficulty")
                return str(difficulty)[:100] if difficulty is not None else None
        return None

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _png_dimensions(raw: bytes) -> tuple[int, int]:
        if (
            len(raw) < 24
            or raw[:8] != b"\x89PNG\r\n\x1a\n"
            or raw[12:16] != b"IHDR"
        ):
            raise ValueError("NCE screenshot is not a valid PNG")
        width, height = struct.unpack(">II", raw[16:24])
        if width <= 0 or height <= 0:
            raise ValueError("NCE screenshot dimensions are invalid")
        return width, height


__all__ = [
    "NCEBenchmarkAdapter",
    "NCEBenchmarkRun",
    "NCEScreenshot",
]
