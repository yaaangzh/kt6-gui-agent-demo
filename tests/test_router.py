import json
from pathlib import Path
import tempfile
import unittest

from kt6_backend.agent import IntentAgent
from kt6_backend.playbook_loader import PlaybookLoader
from kt6_backend.router import PlaybookRouter


class PlaybookRouterTest(unittest.TestCase):
    def setUp(self):
        self.intent_agent = IntentAgent()
        self.router = PlaybookRouter(PlaybookLoader(Path("playbooks")))

    def test_user_experience_query_selects_user_experience_playbook(self):
        query = "用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因"
        decision = self.router.route(query, self.intent_agent.parse(query))

        self.assertEqual(decision.playbook.scenario_id, "user_experience_assurance")
        self.assertGreater(decision.confidence, 0)
        self.assertIn("网速慢", decision.candidates[0].matched_triggers)

    def test_ap_offline_query_selects_ap_offline_playbook(self):
        query = "AP3 昨晚一直离线，帮我看下"
        decision = self.router.route(query, self.intent_agent.parse(query))

        self.assertEqual(decision.playbook.scenario_id, "ap_offline_diagnosis")
        self.assertGreater(decision.confidence, 0)
        self.assertIn("离线", decision.candidates[0].matched_triggers)

    def test_first_turn_router_excludes_action_playbooks(self):
        query = "AP3 昨晚一直离线，帮我看下"
        decision = self.router.route(query, self.intent_agent.parse(query))
        candidate_ids = {candidate.scenario_id for candidate in decision.candidates}

        self.assertIn("ap_offline_diagnosis", candidate_ids)
        self.assertIn("user_experience_assurance", candidate_ids)
        self.assertNotIn("rf_optimization", candidate_ids)
        self.assertNotIn("poe_port_recovery", candidate_ids)

    def test_route_fails_clearly_when_no_diagnosis_playbook_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playbook_dir = Path(temp_dir)
            (playbook_dir / "action_only.json").write_text(
                json.dumps(
                    {
                        "scenario_id": "action_only",
                        "name": "Action only",
                        "steps": [{"id": "execute", "state": "executing"}],
                    }
                ),
                encoding="utf-8",
            )
            router = PlaybookRouter(PlaybookLoader(playbook_dir))

            with self.assertRaisesRegex(RuntimeError, "No diagnosis playbook available"):
                router.route("test", self.intent_agent.parse("test"))


if __name__ == "__main__":
    unittest.main()
