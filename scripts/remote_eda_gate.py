#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path


MINIMAL_BUNDLE_FILES = (
    "scripts/check_env.py",
    "scripts/smoke_vcs_verdi.py",
    "scripts/fsdb_tools.py",
    "scripts/coverage_flow.py",
    "scripts/patch_ucapi_overlay.py",
    "scripts/urg_runtime_probe.py",
    "scripts/urg_coverage_matrix.py",
    "scripts/collect_evidence.py",
    "scripts/make_shell_overlay.sh",
    "scripts/run_remote_eda_smoke.sh",
    "assets/minimal_vcs/top.sv",
    "assets/minimal_vcs/coverage_top.sv",
    "assets/minimal_vcs/core.vhd",
    "assets/minimal_vcs/rtl.f",
    "assets/minimal_vcs/dump_ucli.tcl",
    "assets/minimal_vcs/manifest_matrix.json",
    "assets/minimal_vcs/include/.keep",
    "assets/waves/scn_base.lst",
    "assets/waves/scn_basic.lst",
)

REQUIRED_STEPS = ("compile", "elaborate", "simulate", "verdi-fsdbreport-check")
REQUIRED_ENV_KEYS = ("VCS_HOME", "VERDI_HOME", "SHELL")
REQUIRED_MATRIX = ("minimal_smoke", "mixed_vhdl_sv", "coverage_urg", "fsdb_conversion")


def build_bundle_plan(skill_dir: Path | str, *, remote_dir: str) -> dict:
    root = Path(skill_dir)
    files = []
    file_details = []
    missing = []
    for rel in MINIMAL_BUNDLE_FILES:
        path = root / rel
        files.append(rel)
        file_details.append({"rel": rel, "path": str(path), "exists": path.exists()})
        if not path.exists():
            missing.append(rel)
    remote_commands = [
        f"mkdir -p {remote_dir}",
        f"unzip -o remote-eda-vcs-verdi-bundle.zip -d {remote_dir}",
        f"cd {remote_dir} && chmod +x scripts/*.sh && bash scripts/run_remote_eda_smoke.sh",
    ]
    return {
        "status": "ready" if not missing else "blocked",
        "skill_dir": str(root),
        "remote_dir": remote_dir,
        "files": files,
        "file_details": file_details,
        "missing": missing,
        "remote_commands": remote_commands,
    }


def create_bundle_zip(skill_dir: Path | str, output: Path | str) -> dict:
    root = Path(skill_dir)
    output_path = Path(output)
    plan = build_bundle_plan(root, remote_dir=".")
    if plan["missing"]:
        return {
            "status": "blocked",
            "output": str(output_path),
            "missing": plan["missing"],
            "files": plan["files"],
        }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel in plan["files"]:
            archive.write(root / rel, arcname=rel)
    with zipfile.ZipFile(output_path, "r") as archive:
        names = archive.namelist()
    bad_names = [name for name in names if "\\" in name or name.startswith("/") or ".." in Path(name).parts]
    return {
        "status": "ready" if not bad_names else "blocked",
        "output": str(output_path),
        "files": names,
        "bad_names": bad_names,
    }


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _steps_map(raw_steps) -> dict:
    if isinstance(raw_steps, list):
        return {step.get("name"): step for step in raw_steps}
    return raw_steps or {}


def _freshness(evidence: dict, *, max_age_hours: int | None, now_utc: str | None) -> tuple[bool, str]:
    if evidence.get("fresh") is True:
        return True, ""
    if max_age_hours is None:
        return True, ""
    timestamp = _parse_timestamp(str(evidence.get("timestamp_utc", "")))
    if timestamp is None:
        return False, "timestamp_utc missing or invalid"
    now = _parse_timestamp(now_utc or datetime.now(UTC).isoformat())
    if now is None:
        return False, "now_utc invalid"
    age_hours = (now - timestamp).total_seconds() / 3600
    if age_hours > max_age_hours:
        return False, f"evidence is stale: {age_hours:.1f}h old"
    if age_hours < 0:
        return False, "evidence timestamp is in the future"
    return True, ""


def validate_evidence(evidence: dict, *, max_age_hours: int | None = None, now_utc: str | None = None) -> dict:
    errors: list[str] = []
    if evidence.get("job_exit_code") != 0:
        errors.append(f"job exit code is {evidence.get('job_exit_code')}")
    steps = _steps_map(evidence.get("steps", {}))
    for name in REQUIRED_STEPS:
        step = steps.get(name, {})
        if step.get("returncode") != 0:
            errors.append(f"{name} returncode is {step.get('returncode')}")
        if max_age_hours is not None and not step.get("cmd"):
            errors.append(f"{name} command evidence missing")
    fsdb = evidence.get("artifacts", {}).get("waves.fsdb", {})
    if fsdb.get("bytes", 0) <= 0:
        errors.append("waves.fsdb is missing or zero bytes")
    report_text = evidence.get("report_text", "")
    if "/top/clk" not in report_text:
        errors.append("fsdbreport output does not include /top/clk")
    if max_age_hours is not None:
        fresh, freshness_error = _freshness(evidence, max_age_hours=max_age_hours, now_utc=now_utc)
        if not fresh:
            errors.append(freshness_error)
        env = evidence.get("environment", {})
        for key in REQUIRED_ENV_KEYS:
            if not env.get(key):
                errors.append(f"environment {key} missing")
        if not (env.get("SNPSLMD_LICENSE_FILE") or env.get("LM_LICENSE_FILE")):
            errors.append("license environment hint missing")
        matrix = evidence.get("matrix", {})
        for name in REQUIRED_MATRIX:
            item = matrix.get(name, {})
            if item.get("status") != "passed":
                reason = item.get("reason", "")
                suffix = f": {reason}" if reason else ""
                errors.append(f"remote matrix {name} is {item.get('status', 'missing')}{suffix}")
        coverage = matrix.get("coverage_urg", {})
        if coverage.get("status") not in ("passed", None):
            if not evidence.get("urg_runtime_probe"):
                errors.append("urg runtime probe evidence missing for failed coverage_urg")
            if not evidence.get("urg_coverage_matrix"):
                errors.append("urg coverage matrix evidence missing for failed coverage_urg")
            coverage_summary = evidence.get("coverage_summary", {})
            if not coverage_summary.get("stderr_tail") and not coverage_summary.get("stdout_tail"):
                errors.append("coverage stdout/stderr tail evidence missing for failed coverage_urg")
        if coverage.get("status") == "passed":
            coverage_summary = evidence.get("coverage_summary", {})
            coverage_status = coverage_summary.get("coverage", {})
            if not coverage_status.get("report_exists") or coverage_status.get("report_file_count", 0) <= 0:
                errors.append("coverage_urg passed without nonempty urgReport evidence")
            default_variant = evidence.get("urg_coverage_matrix", {}).get("default_variant", {})
            if (
                default_variant.get("name") != "line+cond+tgl__urg__auto64"
                or default_variant.get("status") != "passed"
                or default_variant.get("report_file_count", 0) <= 0
            ):
                errors.append("default line+cond+tgl URG matrix variant did not pass")
    else:
        fresh = True
    return {
        "status": "passed" if not errors else "failed",
        "fresh": fresh,
        "errors": errors,
        "evidence": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan or validate the minimal remote EDA host VCS/Verdi gate.")
    parser.add_argument("--skill-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--remote-dir", default="validation/vcs-verdi-nongui")
    parser.add_argument("--bundle-zip", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--max-age-hours", type=int)
    parser.add_argument("--now-utc")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.bundle_zip:
        result = create_bundle_zip(args.skill_dir, args.bundle_zip)
    elif args.evidence:
        result = validate_evidence(
            json.loads(args.evidence.read_text(encoding="utf-8")),
            max_age_hours=args.max_age_hours,
            now_utc=args.now_utc,
        )
    else:
        result = build_bundle_plan(args.skill_dir, remote_dir=args.remote_dir)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result["status"])
    return 0 if result["status"] in {"ready", "passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
