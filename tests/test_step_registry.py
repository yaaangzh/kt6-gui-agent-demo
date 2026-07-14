from pathlib import Path
import unittest

from kt6_backend.models import Task
from kt6_backend.playbook_loader import PlaybookLoader
from kt6_backend.runtime import KT6Runtime
from kt6_backend.step_registry import StepHandlerRegistry
from kt6_backend.tools import MockBusinessTools


def valid_step(**overrides):
    step = {
        "id": "custom_step",
        "name": "Custom step",
        "type": "runtime",
        "state": "planning",
        "required_value": True,
    }
    step.update(overrides)
    return step


class StepHandlerRegistryTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.registry = StepHandlerRegistry()
        self.registry.register(
            "diagnosis",
            "custom_step",
            "runtime",
            self._handler,
            required_fields=("required_value",),
        )

    def _handler(self, task, step, payload):
        self.calls.append((task, step, payload))

    def test_resolve_validates_phase_type_and_required_fields(self):
        spec = self.registry.resolve("diagnosis", valid_step())
        self.assertEqual(spec.step_id, "custom_step")

        with self.assertRaisesRegex(ValueError, "Unsupported action step"):
            self.registry.resolve("action", valid_step())
        with self.assertRaisesRegex(ValueError, "Step type mismatch"):
            self.registry.resolve("diagnosis", valid_step(type="tool_ui"))
        with self.assertRaisesRegex(ValueError, "required_value"):
            step = valid_step()
            step.pop("required_value")
            self.registry.resolve("diagnosis", step)

    def test_resolve_rejects_non_mapping_step(self):
        with self.assertRaisesRegex(TypeError, "must be a mapping"):
            self.registry.resolve("diagnosis", None)

    def test_duplicate_registration_and_registration_after_freeze_fail(self):
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.registry.register("diagnosis", "custom_step", "runtime", self._handler)

        self.registry.freeze()
        with self.assertRaisesRegex(RuntimeError, "frozen"):
            self.registry.register("diagnosis", "another_step", "runtime", self._handler)

    def test_validate_steps_rejects_unknown_and_duplicate_steps(self):
        with self.assertRaisesRegex(ValueError, "Unsupported diagnosis step"):
            self.registry.validate_steps(
                "diagnosis",
                [valid_step(id="unknown_step")],
            )
        with self.assertRaisesRegex(ValueError, "Duplicate diagnosis step"):
            self.registry.validate_steps("diagnosis", [valid_step(), valid_step()])

    def test_runtime_merges_injected_handlers_then_freezes_registry(self):
        runtime = KT6Runtime(
            MockBusinessTools(Path("data")),
            PlaybookLoader(Path("playbooks")),
            event_delay=0,
            step_registry=self.registry,
        )
        task = Task(query="custom")

        runtime._execute_diagnosis_step(task, valid_step())

        self.assertIs(runtime.step_handlers, self.registry)
        self.assertTrue(self.registry.frozen)
        self.assertEqual(task.state, "planning")
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
