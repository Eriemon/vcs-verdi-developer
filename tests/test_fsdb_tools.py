from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from test_helpers import load_script_module


fsdb_tools = load_script_module("fsdb_tools")


class FsdbToolsTests(unittest.TestCase):
    def test_missing_fsdb_fails(self):
        result = fsdb_tools.build_fsdb_read_plan("missing.fsdb", action="info")
        self.assertEqual(result["status"], "failed")

    def test_cli_read_plan_normalizes_signal_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            fsdb = Path(tmp) / "waves.fsdb"
            fsdb.write_bytes(b"fsdb")

            plan = fsdb_tools.build_fsdb_read_plan(
                fsdb,
                action="read-signal",
                signal="top.clk",
                env={},
                path_exists_func=lambda path: False,
            )

            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["mode"], "cli")
            self.assertEqual(plan["cli_signal"], "/top/clk")
            self.assertIn("fsdbreport", plan["cmd"][0])

    def test_build_convert_cmd_vcd_to_fsdb(self):
        cmd = fsdb_tools.build_convert_cmd("waves.vcd", "waves.fsdb")
        self.assertEqual(cmd[0], "vcd2fsdb")


if __name__ == "__main__":
    unittest.main()
