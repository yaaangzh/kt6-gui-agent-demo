import copy
import json
from pathlib import Path
import shutil
import threading
import tempfile
import time
import unittest

from kt6_backend.memory import SQLiteMemoryStore
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


class MutableTopologyTools(MockBusinessTools):
    def __init__(self):
        super().__init__(Path("data"))
        self.topology = json.loads(Path("data/mock_topology.json").read_text(encoding="utf-8"))

    def _read_json(self, name: str):
        if name == "mock_topology.json":
            return copy.deepcopy(self.topology)
        return super()._read_json(name)


class FailingStrategyTools(MockBusinessTools):
    def generate_rf_strategy(self, ap_id: str):
        raise RuntimeError(f"strategy backend unavailable for {ap_id}")


class FailingDispatchTools(MockBusinessTools):
    def dispatch_rf_strategy(self, strategy_id: str):
        return {
            "strategy_id": strategy_id,
            "dispatch_status": "failed",
            "message": "controller rejected strategy",
        }


class FailingPoeRestartTools(MockBusinessTools):
    def restart_poe_port(self, switch_name: str, port: str, ap_id: str):
        return {
            "ap_id": ap_id,
            "switch_name": switch_name,
            "port": port,
            "status": "failed",
            "message": "switch rejected restart",
        }


class UnrecoveredExperienceTools(MockBusinessTools):
    def verify_user_recovery(self, user: str):
        return {
            "user": user,
            "experience_score": "poor",
            "summary": "用户体验仍未恢复",
        }


class OfflineVerificationTools(MockBusinessTools):
    def verify_ap_online(self, ap_id: str):
        return {
            "ap_id": ap_id,
            "status": "offline",
            "heartbeat": "missing",
            "summary": "AP 仍离线",
        }


class AbnormalHeartbeatTools(MockBusinessTools):
    def verify_ap_online(self, ap_id: str):
        return {
            "ap_id": ap_id,
            "status": "online",
            "heartbeat": "abnormal",
            "summary": "AP 状态字段在线但心跳异常",
        }


class TrackingSceneValidationTools(MockBusinessTools):
    def __init__(self):
        super().__init__(Path("data"))
        self.scene_validation_calls = 0

    def validate_scene(self, scene_ref, current_capture_id=None):
        self.scene_validation_calls += 1
        return super().validate_scene(scene_ref, current_capture_id)


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.runtime = KT6Runtime(
            MockBusinessTools(Path("data")),
            PlaybookLoader(Path("playbooks")),
            event_delay=0,
        )

    def assert_failed_without_completion(self, runtime, task_id, failed_step):
        task = wait_for_state(runtime, task_id, "failed")
        action_steps = task.context.get("executed_steps", {}).get("action", [])
        self.assertNotIn(failed_step, action_steps)
        self.assertEqual(task.locks, set())
        self.assertEqual(runtime.resource_owners, {})
        self.assertFalse(
            any(
                event.type == "runtime_state"
                and event.payload.get("runtime_state") == "completed"
                for event in task.events
            )
        )
        self.assertFalse(
            any(
                event.payload.get("gui_action", "").startswith("左侧完成态")
                for event in task.events
            )
        )
        return task

    def copy_playbooks(self, root):
        target = Path(root) / "playbooks"
        shutil.copytree(Path("playbooks"), target)
        return target

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
        self.assertEqual(
            task.context["executed_steps"]["diagnosis"],
            ["create_context", "locate_user_topology", "analyze_user_and_ap", "infer_root_cause", "recommend_solutions"],
        )

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

    def test_action_rejected_when_task_intent_has_no_playbook_id(self):
        task = Task(query="corrupt restored task", state="waiting_user")
        task.context["intent"] = {}
        self.runtime.tasks[task.task_id] = task

        accepted = self.runtime.execute_action(
            task.task_id,
            "execute_solution",
            {"solution_id": "rf_optimization"},
        )

        self.assertFalse(accepted)
        self.assertEqual(task.state, "waiting_user")

    def test_execute_solution_rejects_missing_or_unrecommended_solution_id(self):
        task = self.runtime.create_task("用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
        task = wait_for_state(self.runtime, task.task_id, "waiting_user")

        self.assertFalse(self.runtime.execute_action(task.task_id, "execute_solution", {}))
        self.assertFalse(
            self.runtime.execute_action(
                task.task_id,
                "execute_solution",
                {"solution_id": "channel_set_optimization"},
            )
        )
        self.assertFalse(
            self.runtime.execute_action(
                task.task_id,
                "execute_solution",
                {"solution_id": "unknown_solution"},
            )
        )
        self.assertEqual(task.state, "waiting_user")
        self.assertEqual(task.locks, set())
        self.assertEqual(self.runtime.resource_owners, {})

    def test_unknown_playbook_steps_fail_instead_of_being_silently_skipped(self):
        diagnosis_task = Task(query="test")
        with self.assertRaisesRegex(ValueError, "Unsupported diagnosis step"):
            self.runtime._execute_diagnosis_step(
                diagnosis_task,
                {"id": "unknown_diagnosis", "state": "planning"},
            )
        self.assertEqual(diagnosis_task.state, "created")

        action_task = Task(query="test")
        with self.assertRaisesRegex(ValueError, "Unsupported action step"):
            self.runtime._execute_action_step(
                action_task,
                {"id": "unknown_action", "state": "executing"},
                {},
            )
        self.assertEqual(action_task.state, "created")

    def test_diagnosis_preflight_rejects_late_unknown_step_before_side_effects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playbook_dir = self.copy_playbooks(temp_dir)
            path = playbook_dir / "user_experience_assurance.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["steps"].append(
                {
                    "id": "unknown_diagnosis",
                    "name": "Unknown diagnosis",
                    "type": "runtime",
                    "state": "reasoning",
                }
            )
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            runtime = KT6Runtime(
                MockBusinessTools(Path("data")),
                PlaybookLoader(playbook_dir),
                event_delay=0,
            )

            task = runtime.create_task("用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
            task = wait_for_state(runtime, task.task_id, "failed")

            self.assertEqual(task.context, {})
            self.assertFalse(any(event.type in {"chat", "ui", "solutions"} for event in task.events))

    def test_action_preflight_rejects_unknown_step_before_scene_validation_or_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playbook_dir = self.copy_playbooks(temp_dir)
            path = playbook_dir / "rf_optimization.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["steps"].append(
                {
                    "id": "unknown_action",
                    "name": "Unknown action",
                    "type": "runtime",
                    "state": "executing",
                }
            )
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tools = TrackingSceneValidationTools()
            runtime = KT6Runtime(tools, PlaybookLoader(playbook_dir), event_delay=0)
            task = runtime.create_task("用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
            task = wait_for_state(runtime, task.task_id, "waiting_user")

            accepted = runtime.execute_action(
                task.task_id,
                "execute_solution",
                {"solution_id": "rf_optimization"},
            )

            self.assertFalse(accepted)
            self.assertEqual(task.state, "waiting_user")
            self.assertEqual(tools.scene_validation_calls, 0)
            self.assertEqual(task.locks, set())
            self.assertEqual(runtime.resource_owners, {})

    def test_action_preflight_rejects_non_mapping_steps_without_side_effects(self):
        for invalid_step in (None, "not-a-step"):
            with self.subTest(invalid_step=invalid_step), tempfile.TemporaryDirectory() as temp_dir:
                playbook_dir = self.copy_playbooks(temp_dir)
                path = playbook_dir / "rf_optimization.json"
                data = json.loads(path.read_text(encoding="utf-8"))
                tools = TrackingSceneValidationTools()
                runtime = KT6Runtime(tools, PlaybookLoader(playbook_dir), event_delay=0)
                task = runtime.create_task("用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
                task = wait_for_state(runtime, task.task_id, "waiting_user")
                data["steps"].append(invalid_step)
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

                accepted = runtime.execute_action(
                    task.task_id,
                    "execute_solution",
                    {"solution_id": "rf_optimization"},
                )

                self.assertFalse(accepted)
                self.assertEqual(task.state, "waiting_user")
                self.assertEqual(tools.scene_validation_calls, 0)
                self.assertEqual(task.locks, set())
                self.assertEqual(runtime.resource_owners, {})

    def test_task_snapshots_are_thread_safe_and_detached(self):
        task = Task(query="snapshot")
        self.runtime.tasks[task.task_id] = task
        source = {"items": ["original"]}
        self.runtime._update_context(task, source=source)
        source["items"].append("mutated outside runtime")

        def write_context():
            for index in range(300):
                self.runtime._update_context(task, **{f"key_{index}": index})

        writer = threading.Thread(target=write_context)
        writer.start()
        while writer.is_alive():
            snapshot = self.runtime.get_task_snapshot(task.task_id)
            json.dumps(snapshot, ensure_ascii=False)
        writer.join()

        snapshot = self.runtime.get_task_snapshot(task.task_id)
        snapshot["context"]["key_0"] = "changed"
        self.assertEqual(task.context["key_0"], 0)
        self.assertEqual(task.context["source"], {"items": ["original"]})

    def test_execute_solution_completes_and_releases_locks(self):
        task = self.runtime.create_task("用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
        wait_for_state(self.runtime, task.task_id, "waiting_user")

        accepted = self.runtime.execute_action(task.task_id, "execute_solution", {"solution_id": "rf_optimization"})
        self.assertTrue(accepted)
        task = wait_for_state(self.runtime, task.task_id, "completed")

        self.assertEqual(task.context["recovery"]["experience_score"], "normal")
        self.assertEqual(task.locks, set())
        self.assertEqual(self.runtime.resource_owners, {})
        self.assertTrue(any(event.payload.get("view") == "verify" for event in task.events))

    def test_action_failure_releases_only_its_declared_resources(self):
        runtime = KT6Runtime(
            FailingStrategyTools(Path("data")),
            PlaybookLoader(Path("playbooks")),
            event_delay=0,
        )
        task = runtime.create_task("用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
        task = wait_for_state(runtime, task.task_id, "waiting_user")
        unrelated_resource = "resource:external_audit"
        with runtime.lock:
            task.locks.add(unrelated_resource)
            runtime.resource_owners[unrelated_resource] = task.task_id

        accepted = runtime.execute_action(
            task.task_id,
            "execute_solution",
            {"solution_id": "rf_optimization"},
        )
        self.assertTrue(accepted)
        task = wait_for_state(runtime, task.task_id, "failed")

        self.assertEqual(task.locks, {unrelated_resource})
        self.assertEqual(runtime.resource_owners, {unrelated_resource: task.task_id})

    def test_failed_strategy_dispatch_never_completes(self):
        runtime = KT6Runtime(
            FailingDispatchTools(Path("data")),
            PlaybookLoader(Path("playbooks")),
            event_delay=0,
        )
        task = runtime.create_task("用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
        wait_for_state(runtime, task.task_id, "waiting_user")

        self.assertTrue(
            runtime.execute_action(
                task.task_id,
                "execute_solution",
                {"solution_id": "rf_optimization"},
            )
        )
        task = self.assert_failed_without_completion(runtime, task.task_id, "dispatch_strategy")
        self.assertEqual(task.context["dispatch"]["dispatch_status"], "failed")

    def test_failed_poe_restart_never_completes(self):
        runtime = KT6Runtime(
            FailingPoeRestartTools(Path("data")),
            PlaybookLoader(Path("playbooks")),
            event_delay=0,
        )
        task = runtime.create_task("AP3 昨晚一直离线，帮我看下")
        wait_for_state(runtime, task.task_id, "waiting_user")

        self.assertTrue(
            runtime.execute_action(
                task.task_id,
                "execute_solution",
                {"solution_id": "restart_poe_port"},
            )
        )
        task = self.assert_failed_without_completion(runtime, task.task_id, "restart_poe_port")
        self.assertEqual(task.context["poe_action"]["status"], "failed")

    def test_unrecovered_user_experience_never_completes_or_writes_success_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = SQLiteMemoryStore(Path(temp_dir) / "memory.sqlite3")
            runtime = KT6Runtime(
                UnrecoveredExperienceTools(Path("data")),
                PlaybookLoader(Path("playbooks")),
                event_delay=0,
                memory=memory,
            )
            task = runtime.create_task("用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
            wait_for_state(runtime, task.task_id, "waiting_user")

            self.assertTrue(
                runtime.execute_action(
                    task.task_id,
                    "execute_solution",
                    {"solution_id": "rf_optimization"},
                )
            )
            task = self.assert_failed_without_completion(runtime, task.task_id, "verify_recovery")
            self.assertEqual(task.context["recovery"]["experience_score"], "poor")
            self.assertEqual(memory.list_memories(), [])

    def test_ap_requires_online_status_and_normal_heartbeat_to_complete(self):
        for tools_type in (OfflineVerificationTools, AbnormalHeartbeatTools):
            with self.subTest(tools=tools_type.__name__):
                runtime = KT6Runtime(
                    tools_type(Path("data")),
                    PlaybookLoader(Path("playbooks")),
                    event_delay=0,
                )
                task = runtime.create_task("AP3 昨晚一直离线，帮我看下")
                wait_for_state(runtime, task.task_id, "waiting_user")

                self.assertTrue(
                    runtime.execute_action(
                        task.task_id,
                        "execute_solution",
                        {"solution_id": "restart_poe_port"},
                    )
                )
                self.assert_failed_without_completion(runtime, task.task_id, "verify_ap_online")

    def test_resource_lock_rejects_overlapping_actions_across_tasks(self):
        runtime = KT6Runtime(
            MockBusinessTools(Path("data")),
            PlaybookLoader(Path("playbooks")),
            event_delay=0.05,
        )
        first = runtime.create_task("用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
        second = runtime.create_task("用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
        wait_for_state(runtime, first.task_id, "waiting_user")
        wait_for_state(runtime, second.task_id, "waiting_user")

        self.assertTrue(
            runtime.execute_action(first.task_id, "execute_solution", {"solution_id": "rf_optimization"})
        )
        self.assertFalse(
            runtime.execute_action(second.task_id, "execute_solution", {"solution_id": "rf_optimization"})
        )
        wait_for_state(runtime, first.task_id, "completed")
        self.assertTrue(
            runtime.execute_action(second.task_id, "execute_solution", {"solution_id": "rf_optimization"})
        )
        wait_for_state(runtime, second.task_id, "completed")
        self.assertEqual(runtime.resource_owners, {})

    def test_action_replans_when_target_topology_changes(self):
        tools = MutableTopologyTools()
        runtime = KT6Runtime(tools, PlaybookLoader(Path("playbooks")), event_delay=0)
        task = runtime.create_task("用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
        wait_for_state(runtime, task.task_id, "waiting_user")

        tools.topology["links"] = [
            link
            for link in tools.topology["links"]
            if not (link["source"] == "user_zhangsan" and link["target"] == "ap_001")
        ]
        accepted = runtime.execute_action(task.task_id, "execute_solution", {"solution_id": "rf_optimization"})
        self.assertTrue(accepted)

        deadline = time.time() + 2
        while time.time() < deadline:
            task = runtime.get_task(task.task_id)
            solution_events = [event for event in task.events if event.type == "solutions"]
            if task.state == "waiting_user" and len(solution_events) >= 2:
                break
            time.sleep(0.02)

        self.assertEqual(task.state, "waiting_user")
        self.assertEqual(task.context["scene_ref"]["revision"], 2)
        change_events = [event for event in task.events if event.type == "topology_changed"]
        self.assertTrue(change_events)
        self.assertFalse(change_events[-1].payload["action_allowed"])
        self.assertTrue(change_events[-1].payload["invalidate_solutions"])
        self.assertFalse(any(event.payload.get("gui_action", "").startswith("HITL") for event in task.events))

    def test_action_rebinds_moved_target_before_execution(self):
        tools = MutableTopologyTools()
        runtime = KT6Runtime(tools, PlaybookLoader(Path("playbooks")), event_delay=0)
        task = runtime.create_task("用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
        wait_for_state(runtime, task.task_id, "waiting_user")

        ap1 = next(obj for obj in tools.topology["objects"] if obj["business_id"] == "ap_001")
        ap1["x"] += 24
        accepted = runtime.execute_action(task.task_id, "execute_solution", {"solution_id": "rf_optimization"})
        self.assertTrue(accepted)
        task = wait_for_state(runtime, task.task_id, "completed")

        change_events = [event for event in task.events if event.type == "topology_changed"]
        self.assertTrue(change_events)
        self.assertTrue(change_events[-1].payload["action_allowed"])
        self.assertIn("重新绑定目标坐标", change_events[-1].payload["gui_action"])


if __name__ == "__main__":
    unittest.main()
