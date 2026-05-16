from __future__ import annotations

import unittest

from test_helpers import load_script_module


remote_eda_gate = load_script_module("remote_eda_gate")


class RemoteEdaGateTests(unittest.TestCase):
    def test_build_bundle_plan_quotes_remote_dir(self):
        plan = remote_eda_gate.build_bundle_plan(
            remote_eda_gate.Path(__file__).resolve().parents[1],
            remote_dir="validation/test dir; echo injected",
        )

        self.assertEqual(plan["status"], "ready")
        self.assertTrue(any("'validation/test dir; echo injected'" in cmd for cmd in plan["remote_commands"]))
        self.assertFalse(any(" && echo injected" in cmd or "; echo injected" in cmd.replace("'validation/test dir; echo injected'", "") for cmd in plan["remote_commands"]))

    def test_validate_evidence_passes_with_complete_payload(self):
        payload = {
            "job_exit_code": 0,
            "timestamp_utc": "2026-05-16T00:00:00Z",
            "steps": {
                "compile": {"returncode": 0, "cmd": "compile"},
                "elaborate": {"returncode": 0, "cmd": "elaborate"},
                "simulate": {"returncode": 0, "cmd": "simulate"},
                "verdi-fsdbreport-check": {"returncode": 0, "cmd": "fsdbreport"},
            },
            "artifacts": {"waves.fsdb": {"bytes": 64}},
            "report_text": "/top/clk value",
            "environment": {
                "VCS_HOME": "/vendor/vcs",
                "VERDI_HOME": "/vendor/verdi",
                "SHELL": "/bin/bash",
                "LM_LICENSE_FILE": "27000@<license-server>",
            },
            "matrix": {
                "minimal_smoke": {"status": "passed"},
                "mixed_vhdl_sv": {"status": "passed"},
                "coverage_urg": {"status": "passed"},
                "fsdb_conversion": {"status": "passed"},
            },
            "coverage_summary": {
                "coverage": {"report_exists": True, "report_file_count": 3},
            },
            "urg_coverage_matrix": {
                "default_variant": {
                    "name": "line+cond+tgl__urg__auto64",
                    "status": "passed",
                    "report_file_count": 1,
                }
            },
        }

        result = remote_eda_gate.validate_evidence(
            payload,
            max_age_hours=24,
            now_utc="2026-05-16T12:00:00Z",
        )
        self.assertEqual(result["status"], "passed")

    def test_validate_evidence_rejects_stale_timestamp_even_if_fresh_flag_is_true(self):
        payload = {
            "fresh": True,
            "job_exit_code": 0,
            "timestamp_utc": "2026-01-01T00:00:00Z",
            "steps": {
                "compile": {"returncode": 0, "cmd": "compile"},
                "elaborate": {"returncode": 0, "cmd": "elaborate"},
                "simulate": {"returncode": 0, "cmd": "simulate"},
                "verdi-fsdbreport-check": {"returncode": 0, "cmd": "fsdbreport"},
            },
            "artifacts": {"waves.fsdb": {"bytes": 64}},
            "report_text": "/top/clk value",
            "environment": {
                "VCS_HOME": "<set>",
                "VERDI_HOME": "<set>",
                "SHELL": "bash",
                "LM_LICENSE_FILE": "<redacted-license-server>",
            },
            "matrix": {
                "minimal_smoke": {"status": "passed"},
                "mixed_vhdl_sv": {"status": "passed"},
                "coverage_urg": {"status": "passed"},
                "fsdb_conversion": {"status": "passed"},
            },
            "coverage_summary": {"coverage": {"report_exists": True, "report_file_count": 1}},
            "urg_coverage_matrix": {
                "default_variant": {
                    "name": "line+cond+tgl__urg__auto64",
                    "status": "passed",
                    "report_file_count": 1,
                }
            },
        }

        result = remote_eda_gate.validate_evidence(
            payload,
            max_age_hours=24,
            now_utc="2026-05-16T12:00:00Z",
        )
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("stale" in item for item in result["errors"]))

    def test_validate_evidence_fails_on_missing_matrix(self):
        payload = {
            "job_exit_code": 0,
            "steps": {
                "compile": {"returncode": 0},
                "elaborate": {"returncode": 0},
                "simulate": {"returncode": 0},
                "verdi-fsdbreport-check": {"returncode": 0},
            },
            "artifacts": {"waves.fsdb": {"bytes": 64}},
            "report_text": "/top/clk value",
            "matrix": {},
        }

        result = remote_eda_gate.validate_evidence(payload)
        result = remote_eda_gate.validate_evidence(
            payload,
            max_age_hours=24,
            now_utc="2026-05-16T12:00:00Z",
        )
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("remote matrix minimal_smoke" in item for item in result["errors"]))


if __name__ == "__main__":
    unittest.main()
