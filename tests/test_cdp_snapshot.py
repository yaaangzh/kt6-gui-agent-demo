import unittest

from kt6_backend.cdp_snapshot import (
    CDPSnapshotNormalizer,
    SCHEMA_VERSION,
    normalize_cdp_snapshot,
)


class StringTable:
    def __init__(self):
        self.values = []

    def index(self, value):
        value = str(value)
        if value not in self.values:
            self.values.append(value)
        return self.values.index(value)


def node_by_backend(result, frame_id, backend_node_id):
    return next(
        node
        for node in result["nodes"]
        if node["frame_id"] == frame_id
        and node["backend_node_id"] == backend_node_id
    )


def normalize_disabled_conflict(*, dom_disabled, ax_disabled):
    attributes = [["disabled", ""]] if dom_disabled else [[]]
    dom_snapshot = {
        "strings": [],
        "documents": [
            {
                "frameId": "main-frame",
                "nodes": {
                    "parentIndex": [-1],
                    "nodeType": [1],
                    "nodeName": ["BUTTON"],
                    "nodeValue": [""],
                    "backendNodeId": [1],
                    "attributes": attributes,
                    "isClickable": {"index": [0]},
                },
                "layout": {"nodeIndex": [0], "bounds": [[0, 0, 20, 10]]},
            }
        ],
    }
    ax_tree = {
        "nodes": [
            {
                "nodeId": "ax-button",
                "frameId": "main-frame",
                "backendDOMNodeId": 1,
                "role": {"value": "button"},
                "properties": [
                    {"name": "disabled", "value": {"value": ax_disabled}},
                    {"name": "focusable", "value": {"value": True}},
                ],
            }
        ]
    }
    return node_by_backend(
        normalize_cdp_snapshot(dom_snapshot, ax_tree), "main-frame", 1
    )


class CDPSnapshotNormalizerTests(unittest.TestCase):
    def test_dom_disabled_true_is_not_cleared_by_ax_false(self):
        button = normalize_disabled_conflict(dom_disabled=True, ax_disabled=False)

        self.assertTrue(button["disabled"])
        self.assertFalse(button["interaction_candidate"])
        self.assertEqual(
            button["provenance"]["field_sources"]["disabled"],
            "DOMSnapshot.captureSnapshot",
        )

    def test_ax_disabled_true_blocks_dom_enabled_candidate(self):
        button = normalize_disabled_conflict(dom_disabled=False, ax_disabled=True)

        self.assertTrue(button["disabled"])
        self.assertFalse(button["interaction_candidate"])
        self.assertEqual(
            button["provenance"]["field_sources"]["disabled"],
            "Accessibility.getFullAXTree",
        )

    def test_merges_dom_and_ax_by_frame_and_backend_node(self):
        table = StringTable()
        blank = table.index("")
        main = table.index("main-frame")
        dom_snapshot = {
            "strings": table.values,
            "documents": [
                {
                    "frameId": main,
                    "documentURL": table.index("https://example.test/topology"),
                    "title": table.index("Topology"),
                    "nodes": {
                        "parentIndex": [-1, 0, 1],
                        "nodeType": [9, 1, 1],
                        "nodeName": [
                            table.index("#document"),
                            table.index("HTML"),
                            table.index("BUTTON"),
                        ],
                        "nodeValue": [blank, blank, blank],
                        "backendNodeId": [1, 2, 3],
                        "attributes": {
                            "index": [2],
                            "value": [
                                [
                                    table.index("aria-label"),
                                    table.index("DOM save label"),
                                    table.index("data-actionable"),
                                    table.index("true"),
                                ]
                            ],
                        },
                        "isClickable": {"index": [2]},
                    },
                    "layout": {
                        "nodeIndex": [2],
                        "bounds": [[10, 20, 100, 30]],
                    },
                }
            ],
        }
        # The StringTable is mutated while the document is assembled.
        dom_snapshot["strings"] = table.values
        ax_tree = {
            "nodes": [
                {
                    "nodeId": "ax-root",
                    "frameId": "main-frame",
                    "backendDOMNodeId": 1,
                    "role": {"type": "role", "value": "RootWebArea"},
                    "name": {"type": "computedString", "value": "Topology"},
                    "childIds": ["ax-save"],
                },
                {
                    "nodeId": "ax-save",
                    "backendDOMNodeId": 3,
                    "parentId": "ax-root",
                    "role": {"type": "role", "value": "button"},
                    "name": {"type": "computedString", "value": "Save changes"},
                    "description": {
                        "type": "computedString",
                        "value": "Save the current configuration",
                    },
                    "properties": [
                        {
                            "name": "disabled",
                            "value": {"type": "boolean", "value": False},
                        },
                        {
                            "name": "focusable",
                            "value": {"type": "booleanOrUndefined", "value": True},
                        },
                        {
                            "name": "safe_for_execution",
                            "value": {"type": "boolean", "value": True},
                        },
                    ],
                },
            ]
        }

        result = normalize_cdp_snapshot(dom_snapshot, ax_tree)

        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(result["node_count"], 3)
        button = node_by_backend(result, "main-frame", 3)
        self.assertEqual(button["node_id"], "cdp:main-frame:3")
        self.assertEqual(button["role"], "button")
        self.assertEqual(button["name"], "Save changes")
        self.assertEqual(button["description"], "Save the current configuration")
        self.assertFalse(button["disabled"])
        self.assertTrue(button["focusable"])
        self.assertTrue(button["is_clickable"])
        self.assertEqual(button["bounds"], [10.0, 20.0, 100.0, 30.0])
        self.assertEqual(button["center"], [60.0, 35.0])
        self.assertEqual(
            button["source"]["semantic_source"], "cdp_dom_accessibility"
        )
        self.assertEqual(
            button["provenance"]["field_sources"]["name"],
            "Accessibility.getFullAXTree",
        )
        self.assertNotIn("safe_for_execution", button["attributes"])

        self.assertTrue(button["interaction_candidate"])
        self.assertFalse(button["interaction_eligible"])
        self.assertFalse(button["interaction"]["can_click_now"])
        self.assertFalse(button["interaction"]["safe_for_execution"])
        self.assertFalse(button["actionable_grounding"])
        self.assertFalse(result["safe_for_execution"])
        self.assertFalse(result["actionable_grounding"])
        self.assertTrue(
            any(
                relation["type"] == "ax_child"
                and relation["source"] == "cdp:main-frame:1"
                and relation["target"] == button["node_id"]
                for relation in result["relations"]
            )
        )

    def test_expresses_iframe_and_shadow_boundaries(self):
        table = StringTable()
        blank = table.index("")
        main = table.index("main")
        child = table.index("child")
        dom_snapshot = {
            "strings": table.values,
            "documents": [
                {
                    "frameId": main,
                    "documentURL": table.index("https://example.test/"),
                    "title": table.index("Main"),
                    "nodes": {
                        "parentIndex": [-1, 0, 1, 1, 3, 4],
                        "nodeType": [9, 1, 1, 1, 11, 1],
                        "nodeName": [
                            table.index("#document"),
                            table.index("HTML"),
                            table.index("IFRAME"),
                            table.index("X-CONTROL"),
                            table.index("#document-fragment"),
                            table.index("BUTTON"),
                        ],
                        "nodeValue": [blank] * 6,
                        "backendNodeId": [1, 2, 3, 4, 5, 6],
                        "attributes": {
                            "index": [2, 3, 5],
                            "value": [
                                [table.index("title"), table.index("Child frame")],
                                [table.index("id"), table.index("host")],
                                [table.index("aria-label"), table.index("Shadow action")],
                            ],
                        },
                        "shadowRootType": {
                            "index": [4],
                            "value": [table.index("open")],
                        },
                        "contentDocumentIndex": {"index": [2], "value": [1]},
                        "isClickable": {"index": [5]},
                    },
                    "layout": {"nodeIndex": [5], "bounds": [[1, 2, 20, 10]]},
                },
                {
                    "frameId": child,
                    "documentURL": table.index("https://child.example.test/"),
                    "title": table.index("Child"),
                    "nodes": {
                        "parentIndex": [-1, 0],
                        "nodeType": [9, 1],
                        "nodeName": [
                            table.index("#document"),
                            table.index("BUTTON"),
                        ],
                        "nodeValue": [blank, blank],
                        "backendNodeId": [101, 102],
                        "attributes": {
                            "index": [1],
                            "value": [
                                [table.index("aria-label"), table.index("Child action")]
                            ],
                        },
                        "isClickable": {"index": [1]},
                    },
                    "layout": {"nodeIndex": [1], "bounds": [[5, 6, 50, 20]]},
                },
            ],
        }
        dom_snapshot["strings"] = table.values

        result = CDPSnapshotNormalizer().normalize(dom_snapshot, {"nodes": []})
        relations = {
            (relation["type"], relation["source"], relation["target"])
            for relation in result["relations"]
        }

        self.assertIn(
            ("iframe_document", "cdp:main:3", "cdp:child:101"), relations
        )
        self.assertIn(("shadow_root", "cdp:main:4", "cdp:main:5"), relations)
        shadow_root = node_by_backend(result, "main", 5)
        self.assertEqual(shadow_root["shadow_root_type"], "open")
        self.assertEqual(shadow_root["parent_id"], "cdp:main:4")
        self.assertEqual(shadow_root["parent_relation"], "shadow_root")
        child_root = node_by_backend(result, "child", 101)
        self.assertEqual(child_root["parent_id"], "cdp:main:3")
        self.assertEqual(child_root["parent_relation"], "iframe_document")
        child_frame = next(
            frame for frame in result["frames"] if frame["frame_id"] == "child"
        )
        self.assertEqual(child_frame["owner_node_id"], "cdp:main:3")
        self.assertEqual(child_frame["parent_frame_id"], "main")

        shadow_button = node_by_backend(result, "main", 6)
        self.assertTrue(shadow_button["interaction_candidate"])
        self.assertFalse(shadow_button["interaction_eligible"])
        self.assertFalse(shadow_button["interaction"]["can_click_now"])

    def test_frame_is_part_of_identity_and_ax_only_nodes_are_retained(self):
        table = StringTable()
        blank = table.index("")
        main = table.index("main")
        child = table.index("child")
        document_name = table.index("#document")
        dom_snapshot = {
            "strings": table.values,
            "documents": [
                {
                    "frameId": main,
                    "nodes": {
                        "parentIndex": [-1],
                        "nodeType": [9],
                        "nodeName": [document_name],
                        "nodeValue": [blank],
                        "backendNodeId": [77],
                    },
                    "layout": {},
                },
                {
                    "frameId": child,
                    "nodes": {
                        "parentIndex": [-1],
                        "nodeType": [9],
                        "nodeName": [document_name],
                        "nodeValue": [blank],
                        "backendNodeId": [77],
                    },
                    "layout": {},
                },
            ],
        }
        ax_tree = {
            "nodes": [
                {
                    "nodeId": "main-root",
                    "frameId": "main",
                    "backendDOMNodeId": 77,
                    "role": {"value": "RootWebArea"},
                    "childIds": ["virtual-button", "ax-only-with-backend"],
                },
                {
                    "nodeId": "child-root",
                    "frameId": "child",
                    "backendDOMNodeId": 77,
                    "role": {"value": "RootWebArea"},
                },
                {
                    "nodeId": "virtual-button",
                    "frameId": "main",
                    "parentId": "main-root",
                    "backendDOMNodeId": 0,
                    "role": {"value": "button"},
                    "name": {"value": "AX-only action"},
                    "properties": [
                        {"name": "focusable", "value": {"value": True}}
                    ],
                },
                {
                    "nodeId": "ax-only-with-backend",
                    "frameId": "main",
                    "parentId": "main-root",
                    "backendDOMNodeId": 999,
                    "role": {"value": "button"},
                    "name": {"value": "Unmatched AX action"},
                    "properties": [
                        {"name": "focusable", "value": {"value": True}}
                    ],
                },
            ]
        }

        result = normalize_cdp_snapshot(dom_snapshot, ax_tree)

        self.assertEqual(node_by_backend(result, "main", 77)["node_id"], "cdp:main:77")
        self.assertEqual(
            node_by_backend(result, "child", 77)["node_id"], "cdp:child:77"
        )
        virtual = next(
            node for node in result["nodes"] if node["name"] == "AX-only action"
        )
        self.assertIsNone(virtual["backend_node_id"])
        self.assertEqual(virtual["node_id"], "cdp:main:ax:virtual-button")
        self.assertEqual(virtual["source"]["semantic_source"], "cdp_accessibility")
        self.assertEqual(virtual["parent_id"], "cdp:main:77")
        self.assertTrue(virtual["semantic_interaction_hint"])
        self.assertFalse(virtual["interaction_candidate"])
        self.assertFalse(virtual["interaction_eligible"])

        unmatched = node_by_backend(result, "main", 999)
        self.assertEqual(unmatched["source"]["semantic_source"], "cdp_accessibility")
        self.assertTrue(unmatched["semantic_interaction_hint"])
        self.assertFalse(unmatched["interaction_candidate"])
        self.assertIsNone(unmatched["interaction"]["target_ref"])

    def test_preflight_rejects_disjoint_dom_and_ax_aggregate(self):
        count = 6_000
        dom_snapshot = {
            "strings": [],
            "documents": [
                {
                    "frameId": "main",
                    "nodes": {
                        "parentIndex": [-1] * count,
                        "nodeType": [1] * count,
                        "nodeName": ["BUTTON"] * count,
                        "nodeValue": [""] * count,
                        "backendNodeId": list(range(1, count + 1)),
                    },
                    "layout": {},
                }
            ],
        }
        ax_tree = {
            "nodes": [
                {
                    "nodeId": f"ax-{index}",
                    "frameId": "main",
                    "backendDOMNodeId": count + index + 1,
                    "role": {"value": "button"},
                }
                for index in range(count)
            ]
        }

        with self.assertRaisesRegex(ValueError, "normalized CDP snapshot exceeds"):
            normalize_cdp_snapshot(dom_snapshot, ax_tree)

    def test_preflight_counts_provable_dom_ax_merges_once(self):
        count = 6_000
        dom_snapshot = {
            "strings": [],
            "documents": [
                {
                    "frameId": "main",
                    "nodes": {
                        "parentIndex": [-1] * count,
                        "nodeType": [1] * count,
                        "nodeName": ["BUTTON"] * count,
                        "nodeValue": [""] * count,
                        "backendNodeId": list(range(1, count + 1)),
                    },
                    "layout": {},
                }
            ],
        }
        ax_tree = {
            "nodes": [
                {
                    "nodeId": f"ax-{index}",
                    "frameId": "main",
                    "backendDOMNodeId": index + 1,
                }
                for index in range(count)
            ]
        }

        result = normalize_cdp_snapshot(dom_snapshot, ax_tree)

        self.assertEqual(result["node_count"], count)

    def test_preflight_rejects_oversized_document_and_node_arrays(self):
        with self.assertRaisesRegex(ValueError, "exceeds 10000 documents"):
            normalize_cdp_snapshot(
                {"strings": [], "documents": [{"nodes": {}}] * 10_001},
                {"nodes": []},
            )
        with self.assertRaisesRegex(ValueError, "exceeds 10000 nodes"):
            normalize_cdp_snapshot(
                {
                    "strings": [],
                    "documents": [
                        {"nodes": {"parentIndex": [-1] * 10_001}}
                    ],
                },
                {"nodes": []},
            )

    def test_accepts_cdp_result_wrappers_and_empty_snapshots(self):
        empty = normalize_cdp_snapshot(
            {"result": {"strings": [], "documents": []}},
            {"result": {"nodes": []}},
        )

        self.assertEqual(empty["nodes"], [])
        self.assertEqual(empty["relations"], [])
        self.assertEqual(empty["candidate_count"], 0)
        self.assertFalse(empty["safe_for_execution"])

    def test_rejects_malformed_top_level_collections(self):
        with self.assertRaisesRegex(ValueError, "DOM snapshot must be an object"):
            normalize_cdp_snapshot([], {"nodes": []})
        with self.assertRaisesRegex(ValueError, "documents must be a list"):
            normalize_cdp_snapshot({"strings": [], "documents": {}}, {"nodes": []})
        with self.assertRaisesRegex(ValueError, "AX tree nodes must be a list"):
            normalize_cdp_snapshot(
                {"strings": [], "documents": []}, {"nodes": {}}
            )


if __name__ == "__main__":
    unittest.main()
