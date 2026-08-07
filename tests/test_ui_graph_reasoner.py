import json
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler

from kt6_backend.ui_graph_reasoner import (
    HTTPUIGraphReasoner,
    UI_GRAPH_REASONING_REQUEST_SCHEMA,
    UI_OPERATION_PLAN_SCHEMA,
    UIGraphHTTPResponse,
    UIGraphReasoningResponseError,
    UIGraphReasoningTransportError,
    _UrllibUIGraphTransport,
)


class RecordingTransport:
    def __init__(self, response: UIGraphHTTPResponse):
        self.response = response
        self.calls = []

    def post(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def valid_response(*, graph_id: str = "graph-1") -> UIGraphHTTPResponse:
    return UIGraphHTTPResponse(
        200,
        {"Content-Type": "application/json; charset=utf-8"},
        json.dumps(
            {
                "schema_version": UI_OPERATION_PLAN_SCHEMA,
                "graph_id": graph_id,
                "steps": [
                    {
                        "id": "step-1",
                        "op": "locate",
                        "target_node_id": "dom:button",
                    }
                ],
            }
        ).encode("utf-8"),
    )


class HTTPUIGraphReasonerTest(unittest.TestCase):
    def test_sends_bounded_text_graph_with_non_authorizing_contract(self):
        transport = RecordingTransport(valid_response())
        reasoner = HTTPUIGraphReasoner(
            "https://glm.internal.example/v1/ui-plan",
            api_key="secret-token",
            timeout_seconds=12,
            transport=transport,
            allowed_hosts={"glm.internal.example"},
        )

        result = reasoner.plan(
            instruction="打开 AP-001 的详情",
            ui_graph_text='{"schema_version":"kt6.ui-graph.v1","nodes":[]}',
            graph_id="graph-1",
        )

        self.assertEqual(result["schema_version"], UI_OPERATION_PLAN_SCHEMA)
        self.assertEqual(result["graph_id"], "graph-1")
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["headers"]["Authorization"], "Bearer secret-token")
        self.assertEqual(call["timeout_seconds"], 12.0)
        self.assertEqual(call["max_response_bytes"], 64 * 1024)
        request = json.loads(call["body"].decode("utf-8"))
        self.assertEqual(request["schema_version"], UI_GRAPH_REASONING_REQUEST_SCHEMA)
        self.assertEqual(request["graph_id"], "graph-1")
        self.assertEqual(request["output_shape"]["graph_id"], "graph-1")
        self.assertTrue(request["trust_boundary"]["dry_run_only"])
        self.assertTrue(
            request["trust_boundary"][
                "model_may_propose_but_not_authorize_actions"
            ]
        )
        self.assertEqual(
            request["constraints"]["allowed_operations"],
            ["locate", "click", "wait", "verify"],
        )

    def test_allows_loopback_http_but_requires_tls_for_remote_hosts(self):
        HTTPUIGraphReasoner(
            "http://127.0.0.1:9000/plan",
            transport=RecordingTransport(valid_response()),
        )
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            HTTPUIGraphReasoner(
                "http://glm.internal.example/plan",
                transport=RecordingTransport(valid_response()),
            )

    def test_rejects_redirect_status_and_non_json_or_duplicate_responses(self):
        cases = (
            (
                UIGraphHTTPResponse(302, {"Content-Type": "application/json"}, b"{}"),
                UIGraphReasoningTransportError,
            ),
            (
                UIGraphHTTPResponse(200, {"Content-Type": "text/plain"}, b"{}"),
                UIGraphReasoningResponseError,
            ),
            (
                UIGraphHTTPResponse(
                    200,
                    {"Content-Type": "application/json"},
                    b'{"schema_version":"a","schema_version":"b"}',
                ),
                UIGraphReasoningResponseError,
            ),
        )
        for response, error in cases:
            with self.subTest(response=response):
                reasoner = HTTPUIGraphReasoner(
                    "https://glm.internal.example/plan",
                    transport=RecordingTransport(response),
                    allowed_hosts={"glm.internal.example"},
                )
                with self.assertRaises(error):
                    reasoner.plan(
                        instruction="inspect",
                        ui_graph_text="{}",
                        graph_id="graph-1",
                    )

    def test_rejects_empty_or_oversized_inputs_before_transport(self):
        transport = RecordingTransport(valid_response())
        reasoner = HTTPUIGraphReasoner(
            "https://glm.internal.example/plan",
            transport=transport,
            allowed_hosts={"glm.internal.example"},
        )
        with self.assertRaisesRegex(ValueError, "instruction is required"):
            reasoner.plan(instruction=" ", ui_graph_text="{}", graph_id="graph-1")
        with self.assertRaisesRegex(ValueError, "ui_graph_text exceeds"):
            reasoner.plan(
                instruction="inspect",
                ui_graph_text="x" * (reasoner.MAX_GRAPH_TEXT_BYTES + 1),
                graph_id="graph-1",
            )
        self.assertEqual(transport.calls, [])


    def test_requires_explicit_exact_allowlist_for_remote_https(self):
        transport = RecordingTransport(valid_response())
        with self.assertRaisesRegex(ValueError, "allowed_hosts"):
            HTTPUIGraphReasoner(
                "https://glm.internal.example/plan",
                transport=transport,
            )
        with self.assertRaisesRegex(ValueError, "allowed_hosts"):
            HTTPUIGraphReasoner(
                "https://api.glm.internal.example/plan",
                allowed_hosts={"glm.internal.example"},
                transport=transport,
            )

        reasoner = HTTPUIGraphReasoner(
            "https://GLM.internal.example/plan",
            allowed_hosts={"glm.internal.example"},
            transport=transport,
        )
        self.assertEqual(reasoner.allowed_hosts, frozenset({"glm.internal.example"}))

    def test_rejects_wildcard_url_and_invalid_allowlist_entries(self):
        invalid_entries = (
            "",
            "*.internal.example",
            "https://glm.internal.example",
            "glm.internal.example/plan",
            " glm.internal.example",
            "glm.internal.example:443",
            None,
        )
        for entry in invalid_entries:
            with self.subTest(entry=entry):
                with self.assertRaisesRegex(ValueError, "allowed_hosts"):
                    HTTPUIGraphReasoner(
                        "http://127.0.0.1:9000/plan",
                        allowed_hosts=[entry],
                        transport=RecordingTransport(valid_response()),
                    )
        with self.assertRaisesRegex(ValueError, "allowed_hosts"):
            HTTPUIGraphReasoner(
                "http://127.0.0.1:9000/plan",
                allowed_hosts="glm.internal.example",
                transport=RecordingTransport(valid_response()),
            )

    def test_default_transport_disables_environment_proxies(self):
        with patch("kt6_backend.ui_graph_reasoner.build_opener") as opener_builder:
            _UrllibUIGraphTransport()
        handlers = opener_builder.call_args.args
        proxy_handlers = [
            handler for handler in handlers if isinstance(handler, ProxyHandler)
        ]

        self.assertEqual(len(proxy_handlers), 1)
        self.assertEqual(proxy_handlers[0].proxies, {})

    def test_rejects_missing_or_mismatched_response_graph_id(self):
        payloads = (
            {"schema_version": UI_OPERATION_PLAN_SCHEMA, "steps": []},
            {
                "schema_version": UI_OPERATION_PLAN_SCHEMA,
                "graph_id": "graph-2",
                "steps": [],
            },
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                response = UIGraphHTTPResponse(
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps(payload).encode("utf-8"),
                )
                reasoner = HTTPUIGraphReasoner(
                    "https://glm.internal.example/plan",
                    allowed_hosts={"glm.internal.example"},
                    transport=RecordingTransport(response),
                )
                with self.assertRaisesRegex(
                    UIGraphReasoningResponseError, "graph_id"
                ):
                    reasoner.plan(
                        instruction="inspect",
                        ui_graph_text="{}",
                        graph_id="graph-1",
                    )

    def test_rejects_over_64_kib_response_from_custom_transport(self):
        response = UIGraphHTTPResponse(
            200,
            {"Content-Type": "application/json"},
            b"x" * (64 * 1024 + 1),
        )
        reasoner = HTTPUIGraphReasoner(
            "https://glm.internal.example/plan",
            allowed_hosts={"glm.internal.example"},
            transport=RecordingTransport(response),
        )

        self.assertEqual(reasoner.MAX_RESPONSE_BYTES, 64 * 1024)
        with self.assertRaisesRegex(UIGraphReasoningResponseError, "exceeds"):
            reasoner.plan(
                instruction="inspect",
                ui_graph_text="{}",
                graph_id="graph-1",
            )

if __name__ == "__main__":
    unittest.main()
