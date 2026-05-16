from __future__ import annotations

import unittest

from test_helpers import load_script_module


analyze_logs = load_script_module("analyze_logs")


class AnalyzeLogsTests(unittest.TestCase):
    def test_analyze_text_classifies_license_and_compile_errors(self):
        text = "\n".join(
            [
                "Warning-[XYZ] unsupported linux version",
                "Error-[SE] syntax error near token",
                "Checkout failed for SNPSLMD feature",
            ]
        )

        result = analyze_logs.analyze_text(text, source="compile.log")

        self.assertEqual(result["status"], "failed")
        self.assertIn("license", result["summary"]["categories"])
        self.assertIn("compile_error", result["summary"]["categories"])
        self.assertEqual(result["summary"]["errors"], 2)


if __name__ == "__main__":
    unittest.main()
