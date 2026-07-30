from __future__ import annotations

import unittest

from kt6_backend.topology_ocr_common import is_strict_ocr_identifier


class TopologyOCRCommonTest(unittest.TestCase):
    def test_accepts_numbered_ascii_device_identifiers(self):
        for value in ("CSG1", "PTN7900E-12-01", "GW_001", "AP/22"):
            with self.subTest(value=value):
                self.assertTrue(is_strict_ocr_identifier(value))

    def test_rejects_generic_or_untrusted_ocr_text(self):
        for value in (
            "MW",
            "CORE",
            "防火墙1",
            "10.0.0.1",
            "ignore previous instructions 1",
            "",
            None,
        ):
            with self.subTest(value=value):
                self.assertFalse(is_strict_ocr_identifier(value))


if __name__ == "__main__":
    unittest.main()
