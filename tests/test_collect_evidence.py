from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_helpers import load_script_module


collect_evidence = load_script_module("collect_evidence")


class CollectEvidenceTests(unittest.TestCase):
    def test_environment_values_are_redacted_in_evidence_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smoke_path = root / "smoke.json"
            check_env_path = root / "check_env.json"
            smoke_path.write_text(json.dumps({"status": "passed", "results": []}), encoding="utf-8")
            check_env_path.write_text(json.dumps({"overall": {"blockers": []}}), encoding="utf-8")

            evidence = collect_evidence.collect_evidence(
                run_dir=root,
                smoke_path=smoke_path,
                check_env_path=check_env_path,
                env={
                    "VCS_HOME": "/opt/synopsys/vcs",
                    "VERDI_HOME": "/opt/synopsys/verdi",
                    "SNPSLMD_LICENSE_FILE": "27000@<license-server>",
                    "SHELL": "/bin/bash",
                },
            )

            self.assertEqual(evidence["environment"]["VCS_HOME"], "<set>")
            self.assertEqual(evidence["environment"]["VERDI_HOME"], "<set>")
            self.assertEqual(evidence["environment"]["SNPSLMD_LICENSE_FILE"], "<redacted-license-server>")
            self.assertEqual(evidence["environment"]["SHELL"], "bash")


if __name__ == "__main__":
    unittest.main()
