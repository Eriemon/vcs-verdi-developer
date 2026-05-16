from __future__ import annotations

import unittest

from test_helpers import load_script_module


import_vcs_project = load_script_module("import_vcs_project")


class ImportVcsProjectTests(unittest.TestCase):
    def test_import_edalize_project_preserves_core_vcs_fields(self):
        edam = {
            "name": "simv_edalize",
            "toplevel": "top",
            "files": [
                {"name": "rtl/top.sv", "file_type": "systemVerilogSource", "include_path": ["rtl/include"]},
                {"name": "rtl/helper.v", "file_type": "verilog2001Source"},
            ],
            "parameters": {
                "WIDTH": {"datatype": "int", "paramtype": "vlogparam", "default": 32},
                "TESTCASE": {"datatype": "str", "paramtype": "plusarg", "default": "smoke"},
                "USE_ASSERT": {"datatype": "bool", "paramtype": "vlogdefine", "default": True},
            },
            "tool_options": {
                "vcs": {
                    "vcs_options": ["-full64", "-lca"],
                    "run_options": ["+ntb_random_seed=1"],
                }
            },
        }

        manifest = import_vcs_project.import_edalize_project(edam)

        self.assertEqual(manifest["top"], "top")
        self.assertIn("rtl/top.sv", manifest["sources"])
        self.assertIn("rtl/helper.v", manifest["sources"])
        self.assertIn("rtl/include", manifest["include_dirs"])
        self.assertIn("-sverilog", manifest["vlogan_args"])
        self.assertIn("+v2k", manifest["vlogan_args"])
        self.assertIn("-pvalue+WIDTH=32", manifest["vcs_args"])
        self.assertIn("-full64", manifest["vcs_args"])
        self.assertEqual(manifest["defines"]["USE_ASSERT"], "1")
        self.assertIn("+TESTCASE=smoke", manifest["plusargs"])
        self.assertEqual(manifest["simv_args"], ["+ntb_random_seed=1"])


if __name__ == "__main__":
    unittest.main()
