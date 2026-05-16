from __future__ import annotations

import unittest

from test_helpers import REPO_ROOT, load_script_module


generate_rc = load_script_module("generate_rc")


class GenerateRcTests(unittest.TestCase):
    def test_render_rc_from_wave_templates(self):
        config_dir = REPO_ROOT / "assets" / "waves"

        rc = generate_rc.render_rc(config_dir=config_dir, scenario="basic", base="unit")

        self.assertIn('addRenameSig "/top/CLK" "/top/clk"', rc)
        self.assertIn('addGroup -color ID_GRAY5 "TIMING"', rc)
        self.assertIn('addMarker -time 0 -name "START" -color WHITE', rc)
        self.assertIn("addSignal -h 20 -color ID_YELLOW5 -HEX /top/data\\[2:0\\]", rc)


if __name__ == "__main__":
    unittest.main()
