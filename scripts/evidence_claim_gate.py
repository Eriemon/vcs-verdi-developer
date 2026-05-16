#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


FACTUAL_TYPES = {"factual", "technical", "execution", "capability"}
SUPPORTED_STATUSES = {"supported", "passed"}
DELIVERY_SCOPES = {"delivery_mainline"}
TRUTH_SCOPES = {"truth_full"}
TRUTH_BLOCKED_PHRASES = (
    "complete support",
    "fully verified",
    "fully validated",
    "only minor",
    "just a small fix",
    "已完整验证",
    "只差少量",
)


def _gate_context(payload: dict) -> dict:
    context = payload.get("gate_context", {})
    if isinstance(context, dict):
        return context
    return {}


def _scope_error(claim: dict, *, gate_context: dict) -> str:
    scope = (claim.get("claim_scope") or "").strip().lower()
    text = str(claim.get("text", "")).lower()
    if not gate_context:
        return ""
    if scope == "boundary":
        return ""
    if scope in DELIVERY_SCOPES:
        if gate_context.get("delivery_execution_confidence") != "passed":
            return f"{claim.get('claim_id') or '<missing-claim-id>'} delivery scope requires delivery_execution_confidence=passed"
    if scope in TRUTH_SCOPES or "complete support" in text or "every official" in text:
        if gate_context.get("truth_execution_confidence") != "passed":
            return f"{claim.get('claim_id') or '<missing-claim-id>'} truth scope requires truth_execution_confidence=passed"
    if gate_context.get("truth_execution_confidence") == "blocked" and gate_context.get("urg_vendor_or_host_blocked") is True:
        if any(phrase in text for phrase in TRUTH_BLOCKED_PHRASES):
            return f"{claim.get('claim_id') or '<missing-claim-id>'} text overstates a truth-blocked URG state"
    return ""


def validate_claim_evidence(payload: dict) -> dict:
    evidence = payload.get("evidence", [])
    claims = payload.get("claims", [])
    gate_context = _gate_context(payload)
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
            scope_error = _scope_error(claim, gate_context=gate_context)
            if scope_error:
                errors.append(scope_error)
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
