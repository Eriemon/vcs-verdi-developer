from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_helpers import load_script_module


run_quality_gate = load_script_module("run_quality_gate")


class RunQualityGateTests(unittest.TestCase):
    def _touch_tooling(self, fake_home: Path) -> None:
        (fake_home / ".codex" / "skills" / ".system" / "skill-creator" / "scripts").mkdir(parents=True, exist_ok=True)
        (fake_home / ".codex" / "skills" / ".system" / "skill-creator" / "scripts" / "quick_validate.py").write_text("", encoding="utf-8")
        (fake_home / ".codex" / "skills" / "agents-md-generator" / "scripts").mkdir(parents=True, exist_ok=True)
        (fake_home / ".codex" / "skills" / "agents-md-generator" / "scripts" / "verify_agents.py").write_text("", encoding="utf-8")
        (fake_home / ".codex" / "skills" / "agents-md-generator" / "scripts" / "manage_docs.py").write_text("", encoding="utf-8")

    def test_nested_repository_workspace_skips_docs_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "repository" / "vcs-verdi-developer"
            (skill_dir / "tests").mkdir(parents=True)
            fake_home = root / "home"
            self._touch_tooling(fake_home)

            with mock.patch.object(run_quality_gate, "_home_path", side_effect=lambda *parts: fake_home.joinpath(*parts)):
                plan = run_quality_gate.build_local_gate(root, skill_dir=skill_dir)

            steps = {step["name"]: step for step in plan["steps"]}
            self.assertFalse(steps["docs_verify"]["required"])
            self.assertEqual(steps["unit_tests"]["cwd"], str(skill_dir))
            self.assertTrue(steps["package_install_probe"]["required"])

    def test_standalone_skill_keeps_docs_verify_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "skills" / "vcs-verdi-developer"
            (skill_dir / "tests").mkdir(parents=True)
            fake_home = root / "home"
            self._touch_tooling(fake_home)

            with mock.patch.object(run_quality_gate, "_home_path", side_effect=lambda *parts: fake_home.joinpath(*parts)):
                plan = run_quality_gate.build_local_gate(root, skill_dir=skill_dir)

            steps = {step["name"]: step for step in plan["steps"]}
            self.assertTrue(steps["docs_verify"]["required"])


if __name__ == "__main__":
    unittest.main()
