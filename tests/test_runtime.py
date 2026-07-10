from pathlib import Path
import time
import unittest

from kt6_backend.playbook_loader import PlaybookLoader
from kt6_backend.runtime import KT6Runtime
from kt6_backend.tools import MockBusinessTools
from kt6_backend.models import Task


def wait_for_state(runtime: KT6Runtime, task_id: str, state: str, timeout: float = 2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = runtime.get_task(task_id)
        if task and task.state == state:
            return task
        time.sleep(0.02)
    task = runtime.get_task(task_id)
    raise AssertionError(f"Expected state {state}, got {task.state if task else 'missing'}")


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.runtime = KT6Runtime(
            MockBusinessTools(Path("data")),
            PlaybookLoader(Path("playbooks")),
            event_delay=0,
        )

    def test_diagnosis_reaches_waiting_user_with_playbook_events(self):
        task = self.runtime.create_task("用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
        task = wait_for_state(self.runtime, task.task_id, "waiting_user")

        self.assertEqual(task.context["playbook"]["scenario_id"], "user_experience_assurance")
        self.assertEqual(task.context["route_decision"]["selected"]["scenario_id"], "user_experience_assurance")
        self.assertIn(task.context["ui_perception"]["scene_type"], {"irregular_canvas_topology", "dom_topology_view"})
        self.assertIn("ap_001", task.context["ui_perception"]["business_object_bindings"])
        self.assertEqual(task.context["root_cause"]["root_cause"], "co_channel_interference")
        event_types = [event.type for event in task.events]
        self.assertIn("solutions", event_types)
        self.assertIn("ui", event_types)
        self.assertTrue(any(event.payload.get("route_decision") for event in task.events))

    def test_ap_offline_query_routes_to_ap_offline_playbook(self):
        task = self.runtime.create_task("AP3 昨晚一直离线，帮我看下")
        task = wait_for_state(self.runtime, task.task_id, "waiting_user")

        self.assertEqual(task.context["playbook"]["scenario_id"], "ap_offline_diagnosis")
        self.assertEqual(task.context["entities"]["ap_id"], "ap_003")
        self.assertEqual(task.context["root_cause"]["root_cause"], "poe_power_loss")
        self.assertIn("ap_003", task.context["ui_perception"]["business_object_bindings"])
        self.assertTrue(any(event.type == "solutions" for event in task.events))

    def test_missing_user_stops_before_ui_operations(self):
        task = self.runtime.create_task("昨天上午9:00反馈网速慢，帮忙看下是啥原因")
        task = wait_for_state(self.runtime, task.task_id, "waiting_input")

        self.assertEqual(task.context["playbook"]["scenario_id"], "user_experience_assurance")
        self.assertEqual(task.context["missing_slots"][0]["slot"], "user")
        self.assertFalse(any(event.type == "ui" for event in task.events))
        self.assertFalse(any(event.type == "solutions" for event in task.events))
        self.assertTrue(any(event.type == "clarification" for event in task.events))

    def test_user_name_only_requires_symptom_before_ui_operations(self):
        task = self.runtime.create_task("张三")
        task = wait_for_state(self.runtime, task.task_id, "waiting_input")

        missing_slots = {item["slot"] for item in task.context["missing_slots"]}
        self.assertIn("symptom", missing_slots)
        self.assertFalse(any(event.type == "ui" for event in task.events))
        self.assertFalse(any(event.type == "solutions" for event in task.events))

    def test_ap_offline_execute_solution_recovers_ap(self):
        task = self.runtime.create_task("AP3 昨晚一直离线，帮我看下")
        wait_for_state(self.runtime, task.task_id, "waiting_user")

        accepted = self.runtime.execute_action(task.task_id, "execute_solution", {"solution_id": "restart_poe_port"})
        self.assertTrue(accepted)
        task = wait_for_state(self.runtime, task.task_id, "completed")

        self.assertEqual(task.context["ap_recovery"]["status"], "online")
        self.assertEqual(task.locks, set())
        self.assertTrue(any("恢复在线" in event.payload.get("gui_action", "") for event in task.events))

    def test_action_rejected_before_waiting_user(self):
        task = Task(query="用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
        task.state = "planning"
        task.context["intent"] = {"playbook_id": "user_experience_assurance"}
        self.runtime.tasks[task.task_id] = task

        accepted = self.runtime.execute_action(task.task_id, "execute_solution", {"solution_id": "rf_optimization"})

        self.assertFalse(accepted)

    def test_execute_solution_completes_and_releases_locks(self):
        task = self.runtime.create_task("用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
        wait_for_state(self.runtime, task.task_id, "waiting_user")

        accepted = self.runtime.execute_action(task.task_id, "execute_solution", {"solution_id": "rf_optimization"})
        self.assertTrue(accepted)
        task = wait_for_state(self.runtime, task.task_id, "completed")

        self.assertEqual(task.context["recovery"]["experience_score"], "normal")
        self.assertEqual(task.locks, set())
        self.assertTrue(any(event.payload.get("view") == "verify" for event in task.events))


if __name__ == "__main__":
    unittest.main()
