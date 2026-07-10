from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Playbook:
    scenario_id: str
    name: str
    trigger_intents: list[str]
    required_slots: list[str]
    steps: list[dict[str, Any]]
    actions: dict[str, dict[str, Any]]


class PlaybookLoader:
    def __init__(self, playbook_dir: Path):
        self.playbook_dir = playbook_dir

    def load(self, scenario_id: str) -> Playbook:
        path = self.playbook_dir / f"{scenario_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Playbook not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return Playbook(
            scenario_id=data["scenario_id"],
            name=data["name"],
            trigger_intents=data.get("trigger_intents", []),
            required_slots=data.get("required_slots", []),
            steps=data.get("steps", []),
            actions=data.get("actions", {}),
        )

    def list_playbooks(self) -> list[dict[str, str]]:
        playbooks = []
        for path in sorted(self.playbook_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            playbooks.append({"scenario_id": data["scenario_id"], "name": data["name"]})
        return playbooks

