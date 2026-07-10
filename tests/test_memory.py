from pathlib import Path
import tempfile
import unittest

from kt6_backend.memory import SQLiteMemoryStore
from kt6_backend.playbook_loader import PlaybookLoader
from kt6_backend.runtime import KT6Runtime
from kt6_backend.tools import MockBusinessTools

from tests.test_runtime import wait_for_state


class MemoryStoreTest(unittest.TestCase):
    def test_runtime_persists_task_events_checkpoint_and_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = SQLiteMemoryStore(Path(temp_dir) / "memory.sqlite3")
            runtime = KT6Runtime(
                MockBusinessTools(Path("data")),
                PlaybookLoader(Path("playbooks")),
                event_delay=0,
                memory=memory,
            )

            task = runtime.create_task("用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因")
            wait_for_state(runtime, task.task_id, "waiting_user")
            accepted = runtime.execute_action(task.task_id, "execute_solution", {"solution_id": "rf_optimization"})
            self.assertTrue(accepted)
            wait_for_state(runtime, task.task_id, "completed")

            record = memory.get_task_record(task.task_id)
            self.assertIsNotNone(record)
            self.assertEqual(record["state"], "completed")
            self.assertEqual(record["context"]["root_cause"]["root_cause"], "co_channel_interference")

            events = memory.get_task_events(task.task_id)
            self.assertGreater(len(events), 5)
            self.assertTrue(any(event["type"] == "solutions" for event in events))

            memories = memory.list_memories()
            self.assertEqual(memories[0]["kind"], "wireless_user_experience_resolution")
            self.assertEqual(memories[0]["payload"]["user"], "张三")


if __name__ == "__main__":
    unittest.main()
