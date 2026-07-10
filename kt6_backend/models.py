from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


TASK_STATES = {
    "created",
    "planning",
    "waiting_input",
    "locating",
    "perceiving",
    "reasoning",
    "replanning",
    "waiting_user",
    "confirming",
    "executing",
    "verifying",
    "completed",
    "failed",
}


@dataclass
class RuntimeEvent:
    id: int
    type: str
    task_id: str
    timestamp: float
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            **self.payload,
        }


@dataclass
class Task:
    query: str
    task_id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    state: str = "created"
    context: dict[str, Any] = field(default_factory=dict)
    events: list[RuntimeEvent] = field(default_factory=list)
    locks: set[str] = field(default_factory=set)
    next_event_id: int = 1

    def append_event(self, event_type: str, payload: dict[str, Any]) -> RuntimeEvent:
        event = RuntimeEvent(
            id=self.next_event_id,
            type=event_type,
            task_id=self.task_id,
            timestamp=time.time(),
            payload=payload,
        )
        self.next_event_id += 1
        self.events.append(event)
        return event
