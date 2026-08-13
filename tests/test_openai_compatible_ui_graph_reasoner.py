from __future__ import annotations

import json
import unittest

from kt6_backend.openai_compatible_api import (
    ModelHTTPResponse,
    OpenAICompatibleChatClient,
)
from kt6_backend.openai_compatible_ui_graph_reasoner import (
    OpenAICompatibleUIGraphReasoner,
)
from kt6_backend.ui_graph_reasoner import UIGraphReasoningResponseError


class StubTransport:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def post(self, **kwargs):
        self.calls.append(kwargs)
        return ModelHTTPResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(self.content)},
                        }
                    ]
                }
            ).encode(),
        )


class OpenAICompatibleUIGraphReasonerTest(unittest.TestCase):
    def reasoner(self, content, transport=None):
        transport = transport or StubTransport(content)
        client = OpenAICompatibleChatClient(
            base_url="https://api.deepseek.test/v1",
            api_key="secret",
            model="deepseek-test",
            allowed_hosts=["api.deepseek.test"],
            transport=transport,
        )
        return OpenAICompatibleUIGraphReasoner(client), transport

    def test_requests_json_dry_run_plan_bound_to_graph(self):
        reasoner, transport = self.reasoner(
            {
                "schema_version": "kt6.ui-operation-plan.v1",
                "graph_id": "graph-1",
                "steps": [
                    {
                        "id": "locate-1",
                        "op": "locate",
                        "target_node_id": "dom-1",
                        "depends_on": [],
                        "args": {},
                    }
                ],
            }
        )
        result = reasoner.plan(
            instruction="Locate alarm list",
            ui_graph_text='{"nodes":[{"id":"dom-1"}]}',
            graph_id="graph-1",
        )
        self.assertEqual(result["graph_id"], "graph-1")
        request = json.loads(transport.calls[0]["body"])
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual(request["thinking"], {"type": "disabled"})
        semantic = json.loads(request["messages"][1]["content"])
        self.assertTrue(semantic["trust_boundary"]["dry_run_only"])

    def test_rejects_schema_or_graph_mismatch(self):
        for content in (
            {"schema_version": "wrong", "graph_id": "graph-1", "steps": []},
            {
                "schema_version": "kt6.ui-operation-plan.v1",
                "graph_id": "other",
                "steps": [],
            },
        ):
            with self.subTest(content=content):
                reasoner, _ = self.reasoner(content)
                with self.assertRaises(UIGraphReasoningResponseError):
                    reasoner.plan(
                        instruction="Locate",
                        ui_graph_text="{}",
                        graph_id="graph-1",
                    )


if __name__ == "__main__":
    unittest.main()
