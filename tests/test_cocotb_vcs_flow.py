from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from test_helpers import load_script_module


cocotb_vcs_flow = load_script_module("cocotb_vcs_flow")


class CocotbVcsFlowTests(unittest.TestCase):
    def test_relative_verilog_sources_stay_under_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hdl").mkdir()
            (root / "hdl" / "adder.sv").write_text("module adder; endmodule\n", encoding="utf-8")
            makefile = root / "Makefile"
            makefile.write_text(
                "\n".join(
                    [
                        "SIM = vcs",
                        "TOPLEVEL = adder",
                        "MODULE = test_adder",
                        "VERILOG_SOURCES = hdl/adder.sv",
                        "SIM_BUILD = sim_build",
                    ]
                ),
                encoding="utf-8",
            )

            plan = cocotb_vcs_flow.build_cocotb_vcs_plan(
                makefile=makefile,
                project_root=root,
                toplevel_lang="verilog",
                cocotb_lib="/path/to/cocotb/libcocotbvpi_vcs.so",
                dry_run=True,
            )

            self.assertEqual(plan["status"], "dry-run")
            self.assertEqual(plan["sources"]["verilog"], ["hdl/adder.sv"])
            self.assertIn("hdl/adder.sv", plan["compile"]["cmd"])

    def test_vhdl_sources_are_guarded_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rtl").mkdir()
            (root / "rtl" / "top.vhd").write_text("entity top is end;\n", encoding="utf-8")
            makefile = root / "Makefile"
            makefile.write_text(
                "\n".join(
                    [
                        "SIM = vcs",
                        "TOPLEVEL = top",
                        "MODULE = test_top",
                        "VHDL_SOURCES = rtl/top.vhd",
                    ]
                ),
                encoding="utf-8",
            )

            plan = cocotb_vcs_flow.build_cocotb_vcs_plan(
                makefile=makefile,
                project_root=root,
                toplevel_lang="vhdl",
                dry_run=True,
            )

            self.assertEqual(plan["status"], "unsupported")
            self.assertEqual(plan["reason"], "vcs_cocotb_vhdl_unsupported")


if __name__ == "__main__":
    unittest.main()
