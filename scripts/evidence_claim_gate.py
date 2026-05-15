#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


FACTUAL_TYPES = {"factual", "technical", "execution", "capability"}
SUPPORTED_STATUSES = {"supported", "passed"}


def validate_claim_evidence(payload: dict) -> dict:
    evidence = payload.get("evidence", [])
    claims = payload.get("claims", [])
    evidence_ids = {item.get("evidence_id") for item in evidence if item.get("evidence_id")}
    errors: list[str] = []
    warnings: list[str] = []

    for claim in claims:
        claim_id = claim.get("claim_id") or "<missing-claim-id>"
        claim_type = (claim.get("claim_type") or "factual").lower()
        support_status = (claim.get("support_status") or "unverified").lower()
        refs = claim.get("evidence_ids") or []

        if claim_type in FACTUAL_TYPES:
            if support_status not in SUPPORTED_STATUSES:
                errors.append(f"{claim_id} support_status is {support_status}")
            if not refs:
                errors.append(f"{claim_id} has no evidence_ids")
            for ref in refs:
                if ref not in evidence_ids:
                    errors.append(f"{claim_id} references missing evidence_id {ref}")
        elif support_status not in SUPPORTED_STATUSES:
            warnings.append(f"{claim_id} {claim_type} is {support_status}")

    return {
        "status": "failed" if errors else "passed",
        "claim_count": len(claims),
        "evidence_count": len(evidence),
        "errors": errors,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hard-fail unsupported factual claims without evidence.")
    parser.add_argument("--claims-json", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    payload = json.loads(args.claims_json.read_text(encoding="utf-8"))
    result = validate_claim_evidence(payload)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for item in result["errors"]:
            print(f"ERROR: {item}")
        for item in result["warnings"]:
            print(f"WARN: {item}")
        print(result["status"])
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
