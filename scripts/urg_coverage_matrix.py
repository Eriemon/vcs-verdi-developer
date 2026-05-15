#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path


METRIC_SETS = (("line",), ("cond",), ("tgl",), ("line", "cond", "tgl"))
ENTRIES = ("urg", "urg1")
FULL64_MODES = ("auto64", "force64")
DEFAULT_VARIANT_NAME = "line+cond+tgl__urg__auto64"


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:] if len(text) > limit else text


def _count_files(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return 1
    return sum(1 for item in path.rglob("*") if item.is_file())


def _sha256_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _vdb_summary(vdb: Path) -> dict:
    return {
        "path": str(vdb),
        "exists": vdb.exists(),
        "has_mode64": (vdb / ".mode64").exists(),
        "file_count": _count_files(vdb),
        "mode64_sha256": _sha256_file(vdb / ".mode64"),
    }


def _extract_urg1_command_line(output: str) -> str:
    match = re.search(r"(?m)^Command line:\s*(.+)$", output)
    return match.group(1).strip() if match else ""


def _classify_output(output: str, *, entry: str, ucapi_scan: dict | None = None) -> str:
    if "Error-[URG-NLC]" in output or "No license key" in output:
        return "urg_license_missing"
    if "libncursesw.so.5" in output:
        return "urg_runtime_missing_libncurses"
    if "libucapi.so" in output or "libsnpsmalloc.so" in output:
        return "urg_internal_ucapi_snpsmalloc_failure"
    if "not found" in output and entry == "urg1":
        return "urg_wrapper_loader_failure"
    if "Stack trace follows" in output or "ptrace: Operation not permitted" in output:
        if (ucapi_scan or {}).get("status") == "no_match":
            return "urg_internal_stack_trace_ucapi_patch_not_applicable"
        return "urg_internal_non_ucapi_failure"
    return "urg_failed"


def build_variants(*, workdir: Path | str, vdb: Path | str, vcs_home: Path | str) -> list[dict]:
    root = Path(workdir)
    vdb = Path(vdb)
    vcs_home = Path(vcs_home)
    variants: list[dict] = []
    vdb_has_mode64 = (vdb / ".mode64").exists()
    base_env = dict(os.environ)
    direct_lib = str(vcs_home / "linux64" / "lib")
    for metrics in METRIC_SETS:
        metric_arg = "+".join(metrics)
        metric_key = metric_arg
        for entry in ENTRIES:
            for full64_mode in FULL64_MODES:
                report_dir = root / "urg_matrix" / f"{metric_key}__{entry}__{full64_mode}"
                exe = "urg" if entry == "urg" else str(vcs_home / "linux64" / "bin" / "urg1")
                cmd = [exe]
                if full64_mode == "force64" or (full64_mode == "auto64" and vdb_has_mode64):
                    cmd.append("-full64")
                cmd.extend(["-metric", metric_arg, "-dir", str(vdb), "-report", str(report_dir)])
                env = {}
                if entry == "urg1":
                    env["LD_LIBRARY_PATH"] = direct_lib + os.pathsep + base_env.get("LD_LIBRARY_PATH", "")
                variants.append(
                    {
                        "name": f"{metric_key}__{entry}__{full64_mode}",
                        "metrics": list(metrics),
                        "metrics_arg": metric_arg,
                        "entry": entry,
                        "full64_mode": full64_mode,
                        "cmd": cmd,
                        "report_dir": str(report_dir),
                        "env": env,
                    }
                )
    return variants


def _run_variant(variant: dict, *, workdir: Path, timeout: int, base_env: dict[str, str], ucapi_scan: dict | None) -> dict:
    started = time.monotonic()
    report_dir = Path(variant["report_dir"])
    env = base_env.copy()
    env.update(variant.get("env", {}))
    try:
        completed = subprocess.run(
            variant["cmd"],
            cwd=workdir,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        returncode = completed.returncode
        combined = stdout + stderr
        report_count = _count_files(report_dir)
        status = "passed" if returncode == 0 and report_count > 0 else "failed"
        reason = "passed" if status == "passed" else _classify_output(combined, entry=variant["entry"], ucapi_scan=ucapi_scan)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or f"timeout after {timeout}s"
        returncode = None
        report_count = _count_files(report_dir)
        status = "timeout"
        reason = f"timeout after {timeout}s"
        combined = stdout + stderr
    return {
        **variant,
        "returncode": returncode,
        "status": status,
        "reason": reason,
        "elapsed_sec": round(time.monotonic() - started, 3),
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
        "urg1_command_line": _extract_urg1_command_line(combined),
        "report_exists": report_dir.exists(),
        "report_file_count": report_count,
    }


def _matrix_reason(variants: list[dict], ucapi_scan: dict | None) -> str:
    default = next((item for item in variants if item["name"] == DEFAULT_VARIANT_NAME), {})
    if default.get("status") == "passed" and default.get("report_file_count", 0) > 0:
        return "passed"
    if not variants:
        return "coverage_vdb_invalid"
    reasons = {str(item.get("reason", "")) for item in variants}
    if "urg_internal_stack_trace_ucapi_patch_not_applicable" in reasons:
        return "urg_internal_stack_trace_ucapi_patch_not_applicable"
    if "urg_wrapper_loader_failure" in reasons:
        return "urg_wrapper_loader_failure"
    if "urg_internal_ucapi_snpsmalloc_failure" in reasons:
        return "urg_internal_ucapi_snpsmalloc_failure"
    if "urg_internal_non_ucapi_failure" in reasons:
        return "urg_internal_non_ucapi_failure"
    return sorted(reasons - {""})[0] if reasons - {""} else "coverage_urg_failed"


def run_matrix(
    *,
    workdir: Path | str,
    vdb: Path | str,
    vcs_home: Path | str,
    ucapi_scan: dict | None = None,
    timeout: int = 120,
) -> dict:
    workdir = Path(workdir)
    vdb = Path(vdb)
    vcs_home = Path(vcs_home)
    variants = build_variants(workdir=workdir, vdb=vdb, vcs_home=vcs_home)
    base_env = os.environ.copy()
    base_env["VCS_HOME"] = str(vcs_home)
    base_env["PATH"] = str(vcs_home / "bin") + os.pathsep + base_env.get("PATH", "")
    results = [_run_variant(item, workdir=workdir, timeout=timeout, base_env=base_env, ucapi_scan=ucapi_scan) for item in variants]
    default = next((item for item in results if item["name"] == DEFAULT_VARIANT_NAME), {})
    reason = _matrix_reason(results, ucapi_scan)
    return {
        "status": "passed" if reason == "passed" else "failed",
        "reason": reason,
        "workdir": str(workdir),
        "vcs_home": str(vcs_home),
        "vdb": _vdb_summary(vdb),
        "default_variant": default,
        "variants": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run URG coverage diagnostic variants against an existing VDB.")
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--vdb", type=Path, required=True)
    parser.add_argument("--vcs-home", type=Path, required=True)
    parser.add_argument("--ucapi-scan", type=Path)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ucapi_scan = {}
    if args.ucapi_scan and args.ucapi_scan.exists():
        ucapi_scan = json.loads(args.ucapi_scan.read_text(encoding="utf-8"))
    result = run_matrix(workdir=args.workdir, vdb=args.vdb, vcs_home=args.vcs_home, ucapi_scan=ucapi_scan, timeout=args.timeout)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result["status"])
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
