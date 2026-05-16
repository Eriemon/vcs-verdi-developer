from __future__ import annotations

import unittest

from test_helpers import load_script_module


evidence_claim_gate = load_script_module("evidence_claim_gate")


class EvidenceClaimGateTests(unittest.TestCase):
    def test_passes_with_supported_factual_claim_and_evidence(self):
        payload = {
            "evidence": [{"evidence_id": "env-1"}],
            "claims": [
                {
                    "claim_id": "claim-1",
                    "claim_type": "factual",
                    "support_status": "supported",
                    "evidence_ids": ["env-1"],
                }
            ],
        }

        result = evidence_claim_gate.validate_claim_evidence(payload)
        self.assertEqual(result["status"], "passed")

    def test_fails_when_factual_claim_has_missing_evidence(self):
        payload = {
            "evidence": [{"evidence_id": "env-1"}],
            "claims": [
                {
                    "claim_id": "claim-1",
                    "claim_type": "factual",
                    "support_status": "unverified",
                    "evidence_ids": ["missing-id"],
                }
            ],
        }

        result = evidence_claim_gate.validate_claim_evidence(payload)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("support_status" in item for item in result["errors"]))
        self.assertTrue(any("missing evidence_id" in item for item in result["errors"]))


if __name__ == "__main__":
    unittest.main()
