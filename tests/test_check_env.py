from __future__ import annotations

import unittest

from test_helpers import load_script_module


check_env = load_script_module("check_env")


class CheckEnvTests(unittest.TestCase):
    def test_vcs_bin_precedence_and_blockers(self):
        env = {
            "VCS_BIN": "C:/vendor/vcs/bin/vcs",
            "VCS_HOME": "",
            "VERDI_HOME": "",
            "VERDI_PYTHON": "",
            "NOVAS_HOME": "",
            "SNPSLMD_LICENSE_FILE": "",
            "LM_LICENSE_FILE": "",
            "DISPLAY": "",
            "XAUTHORITY": "",
            "VNC_DISPLAY": "",
            "SHELL": "/bin/bash",
            "PATH": "/usr/bin",
            "LD_LIBRARY_PATH": "",
        }

        def fake_which(name: str):
            mapping = {
                "vlogan": "/usr/bin/vlogan",
                "verdi": "/usr/bin/verdi",
                "python3": "/usr/bin/python3",
                "python": "/usr/bin/python",
                "bash": "/usr/bin/bash",
            }
            return mapping.get(name)

        report = check_env.check_environment(
            which_func=fake_which,
            env=env,
            sh_compat_func=lambda: {"available": True, "path": "/bin/sh", "supports_dash_h": True},
            version_func=lambda path: f"version:{path}",
            path_exists_func=lambda path: True,
            expose_paths=True,
        )

        self.assertEqual(report["tools"]["vcs"]["path"], "C:/vendor/vcs/bin/vcs")
        self.assertEqual(report["tools"]["vcs"]["source"], "VCS_BIN")
        self.assertIn("VCS_HOME unset", report["overall"]["blockers"])
        self.assertIn("DISPLAY unset", report["overall"]["blockers"])

    def test_json_style_report_redacts_sensitive_values_by_default(self):
        env = {
            "VCS_HOME": "/opt/synopsys/vcs",
            "VCS_BIN": "/opt/synopsys/vcs/bin/vcs",
            "VERDI_HOME": "/opt/synopsys/verdi",
            "VERDI_PYTHON": "/opt/synopsys/verdi/python/bin/python3",
            "NOVAS_HOME": "/opt/synopsys/verdi/share/novas",
            "SNPSLMD_LICENSE_FILE": "27000@<license-server>",
            "LM_LICENSE_FILE": "",
            "DISPLAY": ":12",
            "XAUTHORITY": "/tmp/xauth",
            "VNC_DISPLAY": ":2",
            "SHELL": "/bin/bash",
            "PATH": "/usr/bin:/opt/synopsys/bin",
            "LD_LIBRARY_PATH": "/opt/synopsys/lib",
        }

        report = check_env.check_environment(
            which_func=lambda name: f"/usr/bin/{name}",
            env=env,
            sh_compat_func=lambda: {"available": True, "path": "/bin/sh", "supports_dash_h": True},
            version_func=lambda path: "",
            path_exists_func=lambda path: True,
        )

        self.assertEqual(report["env"]["VCS_HOME"]["value"], "<set>")
        self.assertEqual(report["env"]["SNPSLMD_LICENSE_FILE"]["value"], "<redacted-license-server>")
        self.assertEqual(report["license"]["value"], "<redacted-license-server>")
        self.assertEqual(report["tools"]["vcs"]["path"], "<redacted:vcs>")
        self.assertEqual(report["shell"]["SHELL"], "bash")
        self.assertEqual(report["shell"]["PATH_entry_count"], 2)
        self.assertEqual(report["shell"]["PATH_entries"], [])


if __name__ == "__main__":
    unittest.main()
