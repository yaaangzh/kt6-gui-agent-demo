from pathlib import Path
import unittest

from kt6_backend.playbook_loader import PlaybookLoader


class PlaybookLoaderTest(unittest.TestCase):
    def test_load_user_experience_playbook(self):
        loader = PlaybookLoader(Path("playbooks"))
        playbook = loader.load("user_experience_assurance")

        self.assertEqual(playbook.scenario_id, "user_experience_assurance")
        self.assertGreaterEqual(len(playbook.steps), 5)
        self.assertIn("execute_solution", playbook.actions)

    def test_list_playbooks(self):
        loader = PlaybookLoader(Path("playbooks"))
        ids = {item["scenario_id"] for item in loader.list_playbooks()}

        self.assertIn("user_experience_assurance", ids)
        self.assertIn("rf_optimization", ids)
        self.assertIn("ap_offline_diagnosis", ids)


if __name__ == "__main__":
    unittest.main()
