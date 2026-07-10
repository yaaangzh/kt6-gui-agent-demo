from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .playbook_loader import Playbook, PlaybookLoader


@dataclass(frozen=True)
class RouteCandidate:
    scenario_id: str
    name: str
    score: float
    matched_triggers: list[str]
    required_slots: list[str]
    selected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "score": self.score,
            "matched_triggers": self.matched_triggers,
            "required_slots": self.required_slots,
            "selected": self.selected,
        }


@dataclass(frozen=True)
class RouteDecision:
    playbook: Playbook
    confidence: float
    reason: str
    candidates: list[RouteCandidate]

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": {
                "scenario_id": self.playbook.scenario_id,
                "name": self.playbook.name,
                "confidence": self.confidence,
                "reason": self.reason,
            },
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


class PlaybookRouter:
    def __init__(self, playbooks: PlaybookLoader):
        self.playbooks = playbooks

    def route(self, query: str, intent: dict[str, Any]) -> RouteDecision:
        playbooks = [
            self.playbooks.load(item["scenario_id"])
            for item in self.playbooks.list_playbooks()
        ]
        diagnosis_playbooks = [playbook for playbook in playbooks if self._is_diagnosis_playbook(playbook)]
        scored = [
            (self._score(query, intent, playbook), self._matched_triggers(query, playbook), playbook)
            for playbook in diagnosis_playbooks
        ]
        scored.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
        score, matched_triggers, playbook = scored[0]
        candidates = [
            RouteCandidate(
                scenario_id=item_playbook.scenario_id,
                name=item_playbook.name,
                score=item_score,
                matched_triggers=item_matched,
                required_slots=item_playbook.required_slots,
                selected=item_playbook.scenario_id == playbook.scenario_id,
            )
            for item_score, item_matched, item_playbook in scored
        ]
        return RouteDecision(
            playbook=playbook,
            confidence=score,
            reason=self._reason(playbook, matched_triggers, intent),
            candidates=candidates,
        )

    def _score(self, query: str, intent: dict[str, Any], playbook: Playbook) -> float:
        score = 0.0
        score += float(len(self._matched_triggers(query, playbook)))
        if intent.get("preferred_playbook_id") == playbook.scenario_id:
            score += 2.0
        return score

    def _matched_triggers(self, query: str, playbook: Playbook) -> list[str]:
        normalized_query = query.lower()
        return [
            trigger
            for trigger in playbook.trigger_intents
            if trigger and trigger.lower() in normalized_query
        ]

    def _is_diagnosis_playbook(self, playbook: Playbook) -> bool:
        return any(step.get("id") == "create_context" for step in playbook.steps)

    def _reason(self, playbook: Playbook, matched_triggers: list[str], intent: dict[str, Any]) -> str:
        parts = []
        if matched_triggers:
            parts.append(f"命中触发词：{', '.join(matched_triggers)}")
        if intent.get("preferred_playbook_id") == playbook.scenario_id:
            parts.append("意图解析首选链路一致")
        if not parts:
            parts.append("未命中明确触发词，使用默认最高优先级诊断链")
        return "；".join(parts)
