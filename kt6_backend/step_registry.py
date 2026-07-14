from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Sequence

from .models import TASK_STATES, Task


StepPhase = Literal["diagnosis", "action"]
StepHandler = Callable[[Task, dict[str, Any], dict[str, Any]], None]


@dataclass(frozen=True)
class StepHandlerSpec:
    phase: StepPhase
    step_id: str
    expected_type: str
    required_fields: frozenset[str]
    handler: StepHandler


class StepHandlerRegistry:
    """Immutable-at-runtime registry for explicitly supported playbook steps."""

    BASE_REQUIRED_FIELDS = frozenset({"id", "name", "type", "state"})

    def __init__(self) -> None:
        self._handlers: dict[tuple[StepPhase, str], StepHandlerSpec] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(
        self,
        phase: StepPhase,
        step_id: str,
        expected_type: str,
        handler: StepHandler,
        *,
        required_fields: Sequence[str] = (),
    ) -> None:
        if self._frozen:
            raise RuntimeError("Step handler registry is frozen")
        if phase not in {"diagnosis", "action"}:
            raise ValueError(f"Unknown step phase: {phase}")
        if not step_id:
            raise ValueError("Step handler id must not be empty")
        if not expected_type:
            raise ValueError("Expected step type must not be empty")
        if not callable(handler):
            raise TypeError("Step handler must be callable")
        key = (phase, step_id)
        if key in self._handlers:
            raise ValueError(f"Step handler is already registered: {phase}/{step_id}")
        self._handlers[key] = StepHandlerSpec(
            phase=phase,
            step_id=step_id,
            expected_type=expected_type,
            required_fields=self.BASE_REQUIRED_FIELDS | frozenset(required_fields),
            handler=handler,
        )

    def freeze(self) -> None:
        self._frozen = True

    def resolve(self, phase: StepPhase, step: Mapping[str, Any]) -> StepHandlerSpec:
        if phase not in {"diagnosis", "action"}:
            raise ValueError(f"Unknown step phase: {phase}")
        if not isinstance(step, Mapping):
            raise TypeError(
                f"Step definition for {phase} must be a mapping, got {type(step).__name__}"
            )
        step_id = step.get("id")
        spec = self._handlers.get((phase, step_id))
        if spec is None:
            raise ValueError(f"Unsupported {phase} step: {step_id}")

        missing_fields = sorted(field for field in spec.required_fields if field not in step)
        if missing_fields:
            raise ValueError(
                f"Missing required fields for {phase} step {step_id}: {', '.join(missing_fields)}"
            )
        actual_type = step.get("type")
        if actual_type != spec.expected_type:
            raise ValueError(
                f"Step type mismatch for {phase} step {step_id}: "
                f"expected {spec.expected_type}, got {actual_type}"
            )
        state = step.get("state")
        if state not in TASK_STATES:
            raise ValueError(f"Unknown task state for {phase} step {step_id}: {state}")
        return spec

    def validate_steps(self, phase: StepPhase, steps: Sequence[Mapping[str, Any]]) -> None:
        seen: set[str] = set()
        for step in steps:
            spec = self.resolve(phase, step)
            if spec.step_id in seen:
                raise ValueError(f"Duplicate {phase} step: {spec.step_id}")
            seen.add(spec.step_id)
