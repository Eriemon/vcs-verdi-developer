#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path


DEFAULT_METRICS = ("line", "cond", "tgl")
MALLOC_RETRY_REASONS = {
    "urg internal ucapi/snpsmalloc failure",
    "urg internal failure with ptrace-blocked stack annotation",
    "urg stack annotator diagnostic blocked by ptrace after internal failure",
}


def coverage_metrics_arg(metrics: list[str] | tuple[str, ...] | None = None) -> str:
    return "+".join(metrics or DEFAULT_METRICS)


def _preferred_urg_executable() -> str:
    vcs_home = os.environ.get("VCS_HOME", "")
    if vcs_home:
        candidate = Path(vcs_home) / "bin" / "urg"
        if candidate.exists():
            return str(candidate)
    return "urg"


def _count_files(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return 1
    return sum(1 for item in path.rglob("*") if item.is_file())


def coverage_status(workdir: Path | str, report_dir: Path | str | None = None) -> dict:
    root = Path(workdir)
    vdb = root / "simv.vdb"
    report = Path(report_dir) if report_dir is not None else root / "urgReport"
    report_exists = report.exists()
    artifacts = {
        "simv.vdb": {"path": str(vdb), "exists": vdb.exists()},
        "urgReport": {"path": str(report), "exists": report_exists},
    }
    state = "present" if vdb.exists() or report_exists else "missing"
    return {
        "state": state,
        "status": state,
        "vdb_has_mode64": (vdb / ".mode64").exists(),
        "report_exists": report_exists,
        "report_file_count": _count_files(report),
        "artifacts": artifacts,
    }


def build_coverage_plan(
    workdir: Path | str,
    *,
    metrics: list[str] | tuple[str, ...] | None = None,
    report_dir: Path | str | None = None,
    full64: bool | None = None,
) -> dict:
    root = Path(workdir)
    metrics_arg = coverage_metrics_arg(metrics)
    vdb = root / "simv.vdb"
    report = Path(report_dir) if report_dir is not None else root / "urgReport"
    vdb_has_mode64 = (vdb / ".mode64").exists()
    use_full64 = vdb_has_mode64 if full64 is None else full64
    cmd = [_preferred_urg_executable()]
    if use_full64:
        cmd.append("-full64")
    cmd.extend(["-dir", str(vdb), "-report", str(report)])
    return {
        "workdir": str(root),
        "metrics_arg": metrics_arg,
        "compile_args": ["-cm", metrics_arg],
        "elaborate_args": ["-cm", metrics_arg],
        "simulate_args": ["-cm", metrics_arg],
        "cmd": cmd,
        "vdb": str(vdb),
        "report_dir": str(report),
        "vdb_has_mode64": vdb_has_mode64,
        "full64": use_full64,
        "coverage": coverage_status(root, report),
    }


def diagnose_coverage_failure(output: str) -> str:
    if "Error-[URG-NLC]" in output or "No license key" in output:
        return "urg license missing: VCSTools_Net or VT_CoverageURG"
    if "Stack trace follows" in output:
        return "urg internal failure with ptrace-blocked stack annotation"
    if "ptrace: Operation not permitted" in output:
        return "urg stack annotator diagnostic blocked by ptrace after internal failure"
    if "libncursesw.so.5" in output:
        return "urg runtime missing libncursesw.so.5"
    if "libucapi.so" in output or "libsnpsmalloc.so" in output:
        return "urg internal ucapi/snpsmalloc failure"
    return ""


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:] if len(text) > limit else text


def extract_urg1_command_line(output: str) -> str:
    match = re.search(r"(?m)^Command line:\s*(.+)$", output)
    return match.group(1).strip() if match else ""


def urg_tool_info(cmd: list[str]) -> dict:
    exe = shutil.which(cmd[0]) or cmd[0] if cmd else ""
    path = Path(exe) if exe else Path("")
    first_line = ""
    resolved = ""
    try:
        resolved = str(path.resolve(strict=False))
        first_line = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (FileNotFoundError, IsADirectoryError, IndexError, OSError):
        pass
    return {
        "which": exe,
        "resolved": resolved,
        "first_line": first_line,
    }


def failure_diagnostics(output: str) -> dict:
    diagnostics: dict[str, str] = {}
    if "ptrace: Operation not permitted" in output:
        diagnostics["ptrace"] = "stack annotator diagnostic blocked by ptrace"
    if "libucapi.so" in output:
        diagnostics["libucapi"] = "output mentions libucapi.so"
    if "libsnpsmalloc.so" in output:
        diagnostics["libsnpsmalloc"] = "output mentions libsnpsmalloc.so"
    if "Stack trace follows" in output:
        diagnostics["stack_trace"] = "urg emitted internal stack trace"
    return diagnostics


def execution_command(cmd: list[str]) -> list[str]:
    if not cmd:
        return cmd
    exe = shutil.which(cmd[0]) or cmd[0]
    path = Path(exe)
    try:
        first_line = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (FileNotFoundError, IsADirectoryError, IndexError, OSError):
        return cmd
    if first_line.startswith("#!/bin/sh"):
        return ["bash", str(path), *cmd[1:]]
    return cmd


def _execute_once(plan: dict, *, cmd: list[str], tool: dict, timeout: int, env: dict[str, str]) -> dict:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            cwd=plan["workdir"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        status = "passed" if completed.returncode == 0 else "failed"
        combined_output = (completed.stdout or "") + (completed.stderr or "")
        return {
            **plan,
            "returncode": completed.returncode,
            "status": status,
            "reason": "" if status == "passed" else diagnose_coverage_failure(combined_output),
            "diagnostics": failure_diagnostics(combined_output),
            "urg_tool": tool,
            "urg1_command_line": extract_urg1_command_line(combined_output),
            "execution_cmd": cmd,
            "elapsed_sec": round(time.monotonic() - started, 3),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "stdout_tail": _tail(completed.stdout or ""),
            "stderr_tail": _tail(completed.stderr or ""),
            "coverage": coverage_status(plan["workdir"], plan.get("report_dir")),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            **plan,
            "returncode": None,
            "status": "timeout",
            "reason": f"timeout after {timeout}s",
            "diagnostics": {"timeout": f"timeout after {timeout}s"},
            "urg_tool": tool,
            "urg1_command_line": extract_urg1_command_line((exc.stdout or "") + (exc.stderr or "")),
            "execution_cmd": cmd,
            "elapsed_sec": round(time.monotonic() - started, 3),
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or f"timeout after {timeout}s",
            "stdout_tail": _tail(exc.stdout or ""),
            "stderr_tail": _tail(exc.stderr or f"timeout after {timeout}s"),
            "coverage": coverage_status(plan["workdir"], plan.get("report_dir")),
        }


def _should_retry_with_vcs_use_malloc(result: dict, *, env: dict[str, str]) -> bool:
    if env.get("VCS_USE_MALLOC") == "1":
        return False
    if result.get("status") != "failed":
        return False
    return result.get("reason", "") in MALLOC_RETRY_REASONS


def execute_coverage_plan(plan: dict, *, timeout: int = 300) -> dict:
    cmd = execution_command(plan["cmd"])
    tool = urg_tool_info(plan["cmd"])
    base_env = dict(os.environ)
    result = _execute_once(plan, cmd=cmd, tool=tool, timeout=timeout, env=base_env)
    if not _should_retry_with_vcs_use_malloc(result, env=base_env):
        return result
    retry_env = base_env.copy()
    retry_env["VCS_USE_MALLOC"] = "1"
    retry = _execute_once(plan, cmd=cmd, tool=tool, timeout=timeout, env=retry_env)
    retry["fallback_applied"] = True
    retry["fallback_env"] = {"VCS_USE_MALLOC": "1"}
    retry["initial_attempt"] = {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "returncode": result.get("returncode"),
        "stdout_tail": result.get("stdout_tail", ""),
        "stderr_tail": result.get("stderr_tail", ""),
    }
    return retry


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan and inspect VCS coverage/URG non-GUI flow.")
    parser.add_argument("--workdir", type=Path, default=Path("build/vcs-cov"))
    parser.add_argument("--metric", action="append", dest="metrics")
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--full64", action="store_true", help="Force urg -full64 even if simv.vdb/.mode64 is absent.")
    parser.add_argument("--no-auto-full64", action="store_true", help="Disable automatic urg -full64 from simv.vdb/.mode64.")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    full64_override = True if args.full64 else False if args.no_auto_full64 else None
    plan = build_coverage_plan(args.workdir, metrics=args.metrics, report_dir=args.report_dir, full64=full64_override)
    result = execute_coverage_plan(plan, timeout=args.timeout) if args.execute else plan
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(" ".join(result["cmd"]))
    if args.execute and result.get("status") != "passed":
        return int(result.get("returncode") or 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
