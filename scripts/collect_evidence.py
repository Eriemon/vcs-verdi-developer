#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path


ENV_KEYS = (
    "VCS_HOME",
    "VERDI_HOME",
    "NOVAS_HOME",
    "SNPSLMD_LICENSE_FILE",
    "LM_LICENSE_FILE",
    "DISPLAY",
    "SHELL",
    "LD_LIBRARY_PATH",
)


def _load_json(path: Path | None) -> dict:
    if not path:
        return {}
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "failed", "parse_error": str(exc), "path": str(path)}


def _artifact_from_smoke(smoke: dict, run_dir: Path) -> dict:
    dump = smoke.get("artifact_status", {}).get("dump") or smoke.get("diagnostics", {}).get("artifacts", {}).get("dump", {})
    return {
        "path": dump.get("path", str(run_dir / "waves.fsdb")),
        "state": dump.get("state", "missing"),
        "bytes": dump.get("bytes", 0),
    }


def _matrix_status(result: dict, *, default: str = "not_executed") -> dict:
    if not result:
        return {"status": default}
    status = result.get("status", default)
    if status == "dry-run":
        status = "not_executed"
    return {"status": status, "returncode": result.get("returncode"), "reason": result.get("reason", "")}


def _coverage_matrix_status(coverage: dict, urg_matrix: dict) -> dict:
    if urg_matrix:
        return _matrix_status(urg_matrix)
    return _matrix_status(coverage)


def collect_evidence(
    *,
    run_dir: Path,
    smoke_path: Path,
    check_env_path: Path,
    report_path: Path | None = None,
    mixed_smoke_path: Path | None = None,
    coverage_path: Path | None = None,
    conversion_path: Path | None = None,
    ucapi_scan_path: Path | None = None,
    ucapi_manifest_path: Path | None = None,
    urg_probe_path: Path | None = None,
    urg_matrix_path: Path | None = None,
    job_exit_code: int = 0,
    env: dict[str, str] | None = None,
    timestamp_utc: str | None = None,
) -> dict:
    run_dir = run_dir.resolve()
    smoke = _load_json(smoke_path)
    mixed = _load_json(mixed_smoke_path)
    coverage = _load_json(coverage_path)
    conversion = _load_json(conversion_path)
    ucapi_scan = _load_json(ucapi_scan_path)
    ucapi_manifest = _load_json(ucapi_manifest_path)
    urg_probe = _load_json(urg_probe_path)
    urg_matrix = _load_json(urg_matrix_path)
    check_env = _load_json(check_env_path)
    report_text = ""
    if report_path:
        try:
            report_text = report_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            report_text = ""
    environment = {key: os.environ.get(key, "") for key in ENV_KEYS}
    environment.update(env or {})
    mixed_status = _matrix_status(mixed)
    if mixed_status["status"] == "not_executed" and smoke.get("plan", {}).get("vhdl_sources"):
        mixed_status = {"status": smoke.get("status", "unknown"), "returncode": None, "reason": ""}
    evidence = {
        "timestamp_utc": timestamp_utc or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "fresh": True,
        "job_exit_code": job_exit_code,
        "environment": environment,
        "check_env": check_env,
        "steps": smoke.get("results", []),
        "artifacts": {
            "waves.fsdb": _artifact_from_smoke(smoke, run_dir),
            "smoke.json": {"path": str(smoke_path), "bytes": smoke_path.stat().st_size if smoke_path.exists() else 0},
            "report.txt": {"path": str(report_path or ""), "bytes": report_path.stat().st_size if report_path and report_path.exists() else 0},
        },
        "matrix": {
            "minimal_smoke": _matrix_status(smoke),
            "mixed_vhdl_sv": mixed_status,
            "coverage_urg": _coverage_matrix_status(coverage, urg_matrix),
            "fsdb_conversion": _matrix_status(conversion),
        },
        "ucapi_patch": {
            "scan": ucapi_scan,
            "manifest": ucapi_manifest,
        },
        "urg_runtime_probe": urg_probe,
        "urg_coverage_matrix": urg_matrix,
        "coverage_summary": {
            "status": coverage.get("status", "not_executed") if coverage else "not_executed",
            "returncode": coverage.get("returncode") if coverage else None,
            "reason": coverage.get("reason", "") if coverage else "",
            "diagnostics": coverage.get("diagnostics", {}) if coverage else {},
            "stdout_tail": coverage.get("stdout_tail", "") if coverage else "",
            "stderr_tail": coverage.get("stderr_tail", "") if coverage else "",
            "urg1_command_line": coverage.get("urg1_command_line", "") if coverage else "",
            "coverage": coverage.get("coverage", {}) if coverage else {},
        },
        "report_text": report_text,
        "smoke_status": smoke.get("status", "unknown"),
        "smoke_reason": smoke.get("reason", ""),
    }
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect VCS/Verdi non-GUI execution evidence into one JSON object.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--smoke", type=Path, required=True)
    parser.add_argument("--check-env", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--mixed-smoke", type=Path)
    parser.add_argument("--coverage", type=Path)
    parser.add_argument("--conversion", type=Path)
    parser.add_argument("--ucapi-scan", type=Path)
    parser.add_argument("--ucapi-manifest", type=Path)
    parser.add_argument("--urg-probe", type=Path)
    parser.add_argument("--urg-matrix", type=Path)
    parser.add_argument("--job-exit-code", type=int, default=0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    evidence = collect_evidence(
        run_dir=args.run_dir,
        smoke_path=args.smoke,
        check_env_path=args.check_env,
        report_path=args.report,
        mixed_smoke_path=args.mixed_smoke,
        coverage_path=args.coverage,
        conversion_path=args.conversion,
        ucapi_scan_path=args.ucapi_scan,
        ucapi_manifest_path=args.ucapi_manifest,
        urg_probe_path=args.urg_probe,
        urg_matrix_path=args.urg_matrix,
        job_exit_code=args.job_exit_code,
    )
    text = json.dumps(evidence, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    if args.json or not args.output:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
