#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path


ATTEMPTS = (
    {
        "name": "vendor_wrapper_auto64",
        "entry": "vendor_wrapper",
        "full64": "auto64",
        "metrics": "line+cond+tgl",
        "format_mode": "",
        "show_mode": "",
        "arch_override": "",
        "loader_mode": "default",
        "shell_mode": "wrapper",
    },
    {
        "name": "vendor_wrapper_full64",
        "entry": "vendor_wrapper",
        "full64": "force64",
        "metrics": "line+cond+tgl",
        "format_mode": "",
        "show_mode": "",
        "arch_override": "",
        "loader_mode": "default",
        "shell_mode": "wrapper",
    },
    {
        "name": "vendor_copied_bash_wrapper_full64",
        "entry": "vendor_wrapper",
        "full64": "force64",
        "metrics": "line+cond+tgl",
        "format_mode": "",
        "show_mode": "",
        "arch_override": "",
        "loader_mode": "default",
        "shell_mode": "copied_bash_wrapper",
    },
    {
        "name": "overlay_wrapper_full64",
        "entry": "overlay_wrapper",
        "full64": "force64",
        "metrics": "line+cond+tgl",
        "format_mode": "",
        "show_mode": "",
        "arch_override": "",
        "loader_mode": "default",
        "shell_mode": "wrapper",
    },
    {
        "name": "overlay_wrapper_arch_linux",
        "entry": "overlay_wrapper",
        "full64": "force64",
        "metrics": "line+cond+tgl",
        "format_mode": "",
        "show_mode": "",
        "arch_override": "linux",
        "loader_mode": "default",
        "shell_mode": "wrapper",
    },
    {
        "name": "overlay_direct_urg1_vcs_lib",
        "entry": "overlay_urg1_direct",
        "full64": "force64",
        "metrics": "line+cond+tgl",
        "format_mode": "",
        "show_mode": "",
        "arch_override": "",
        "loader_mode": "direct_vcs_lib",
        "shell_mode": "direct",
    },
    {
        "name": "overlay_format_text",
        "entry": "overlay_wrapper",
        "full64": "force64",
        "metrics": "line+cond+tgl",
        "format_mode": "text",
        "show_mode": "",
        "arch_override": "",
        "loader_mode": "default",
        "shell_mode": "wrapper",
    },
    {
        "name": "overlay_show_summary",
        "entry": "overlay_wrapper",
        "full64": "force64",
        "metrics": "line+cond+tgl",
        "format_mode": "",
        "show_mode": "summary",
        "arch_override": "",
        "loader_mode": "default",
        "shell_mode": "wrapper",
    },
    {
        "name": "overlay_metric_line",
        "entry": "overlay_wrapper",
        "full64": "force64",
        "metrics": "line",
        "format_mode": "",
        "show_mode": "",
        "arch_override": "",
        "loader_mode": "default",
        "shell_mode": "wrapper",
    },
    {
        "name": "overlay_metric_cond",
        "entry": "overlay_wrapper",
        "full64": "force64",
        "metrics": "cond",
        "format_mode": "",
        "show_mode": "",
        "arch_override": "",
        "loader_mode": "default",
        "shell_mode": "wrapper",
    },
    {
        "name": "overlay_metric_tgl",
        "entry": "overlay_wrapper",
        "full64": "force64",
        "metrics": "tgl",
        "format_mode": "",
        "show_mode": "",
        "arch_override": "",
        "loader_mode": "default",
        "shell_mode": "wrapper",
    },
)


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:] if len(text) > limit else text


def _count_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob("*") if item.is_file())


def _materialize_copied_bash_wrapper(*, workdir: Path, vendor_vcs_home: Path) -> Path:
    source = vendor_vcs_home / "bin" / "urg"
    target_dir = workdir / "urg_troubleshoot" / "vendor_copied_bash_wrapper" / "bin"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "urg"
    lines = source.read_text(encoding="utf-8", errors="ignore").splitlines()
    if lines:
        lines[0] = "#!/usr/bin/env bash"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def _classify_failure(output: str, *, attempt: dict) -> str:
    if "Error-[URG-NLC]" in output or "No license key" in output:
        return "license_missing"
    if "/bin/sh: 0: Illegal option -h" in output:
        return "vendor_wrapper_shell_incompatible"
    if "Cannot find 'vcsMsgReport' script in /bin" in output or "Please make sure VCS_HOME is set correctly" in output:
        return "vendor_bash_wrapper_env_incomplete"
    if "Stack trace follows" in output or "ptrace: Operation not permitted" in output:
        if attempt["entry"] == "overlay_wrapper":
            return "overlay_wrapper_internal_crash"
        if attempt["entry"] == "overlay_urg1_direct":
            return "direct_urg1_internal_crash"
        return "vendor_or_host_blocked"
    if "libncursesw.so.5" in output:
        return "loader_missing_dependency"
    if "libucapi.so" in output or "libsnpsmalloc.so" in output:
        return "loader_missing_dependency"
    if "not found" in output:
        return "loader_missing_dependency"
    return "failed"


def _command_for_attempt(
    attempt: dict,
    *,
    workdir: Path,
    vendor_vcs_home: Path,
    overlay_vcs_home: Path,
    vdb: Path,
    report_dir: Path,
) -> list[str]:
    home = vendor_vcs_home if attempt["entry"].startswith("vendor") else overlay_vcs_home
    if attempt["entry"] == "overlay_urg1_direct":
        exe = str(overlay_vcs_home / "linux64" / "bin" / "urg1")
    elif attempt["shell_mode"] == "copied_bash_wrapper":
        exe = str(_materialize_copied_bash_wrapper(workdir=workdir, vendor_vcs_home=vendor_vcs_home))
    else:
        exe = str(home / "bin" / "urg")
    cmd = [exe]
    if attempt["full64"] == "force64":
        cmd.append("-full64")
    if attempt["format_mode"]:
        cmd.extend(["-format", attempt["format_mode"]])
    if attempt["show_mode"]:
        cmd.extend(["-show", attempt["show_mode"]])
    cmd.extend(["-metric", attempt["metrics"], "-dir", str(vdb), "-report", str(report_dir)])
    return cmd


def _classify_gdb_root_cause(output: str) -> str:
    if "covdb_get_license" in output and "scl_lc_checkout" in output and "libsnpsmalloc.so" in output:
        return "ucapi_license_checkout_segv"
    return ""


def _system_gdb_probe(*, cmd: list[str], cwd: Path, env: dict[str, str], timeout: int) -> dict:
    gdb = shutil.which("gdb", path=env.get("PATH"))
    if not gdb:
        return {"available": False, "root_cause_signature": ""}
    gdb_cmd = [
        gdb,
        "-q",
        "-batch",
        "-ex",
        "set pagination off",
        "-ex",
        "run",
        "-ex",
        "thread apply all bt",
        "--args",
        *cmd,
    ]
    try:
        completed = subprocess.run(
            gdb_cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        combined = stdout + stderr
        return {
            "available": True,
            "returncode": completed.returncode,
            "stdout_tail": _tail(stdout),
            "stderr_tail": _tail(stderr),
            "root_cause_signature": _classify_gdb_root_cause(combined),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "available": True,
            "returncode": None,
            "stdout_tail": _tail(exc.stdout or ""),
            "stderr_tail": _tail((exc.stderr or "") + f"\n[gdb-timeout-after-{timeout}s]"),
            "root_cause_signature": "",
        }


def run_attempts(
    *,
    workdir: Path,
    vdb: Path,
    vendor_vcs_home: Path,
    overlay_vcs_home: Path,
    timeout: int,
) -> dict:
    workdir = workdir.resolve()
    vdb = vdb.resolve()
    vendor_vcs_home = vendor_vcs_home.resolve()
    overlay_vcs_home = overlay_vcs_home.resolve()
    attempts = []
    shell_compat = {
        "vendor_urg_first_line": (vendor_vcs_home / "bin" / "urg").read_text(encoding="utf-8", errors="ignore").splitlines()[0],
        "overlay_urg_first_line": (overlay_vcs_home / "bin" / "urg").read_text(encoding="utf-8", errors="ignore").splitlines()[0],
    }
    for definition in ATTEMPTS:
        report_dir = workdir / "urg_troubleshoot" / definition["name"]
        env = os.environ.copy()
        env["VCS_HOME"] = str(vendor_vcs_home if definition["entry"].startswith("vendor") else overlay_vcs_home)
        if definition["arch_override"]:
            env["VCS_ARCH_OVERRIDE"] = definition["arch_override"]
        if definition["loader_mode"] == "direct_vcs_lib":
            direct_lib = overlay_vcs_home / "linux64" / "lib"
            env["LD_LIBRARY_PATH"] = str(direct_lib) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        cmd = _command_for_attempt(
            definition,
            workdir=workdir,
            vendor_vcs_home=vendor_vcs_home,
            overlay_vcs_home=overlay_vcs_home,
            vdb=vdb,
            report_dir=report_dir,
        )
        started = time.monotonic()
        try:
            completed = subprocess.run(
                cmd,
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
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or f"timeout after {timeout}s"
            returncode = None
        combined = stdout + stderr
        failure_signature = "passed" if returncode == 0 and _count_files(report_dir) > 0 else _classify_failure(combined, attempt=definition)
        gdb_probe = {}
        if failure_signature == "direct_urg1_internal_crash":
            gdb_probe = _system_gdb_probe(cmd=cmd, cwd=workdir, env=env, timeout=min(timeout, 60))
        combined = stdout + stderr
        attempts.append(
            {
                **definition,
                "cmd": cmd,
                "returncode": returncode,
                "elapsed_sec": round(time.monotonic() - started, 3),
                "report_exists": report_dir.exists(),
                "report_file_count": _count_files(report_dir),
                "failure_signature": failure_signature,
                "root_cause_signature": gdb_probe.get("root_cause_signature", ""),
                "system_gdb": gdb_probe,
                "stdout_tail": _tail(stdout),
                "stderr_tail": _tail(stderr),
            }
        )
    any_passed = any(item["returncode"] == 0 and item["report_file_count"] > 0 for item in attempts)
    summary_reason = "passed" if any_passed else "vendor_or_host_blocked"
    return {
        "status": "passed" if any_passed else "failed",
        "summary": {
            "reason": summary_reason,
            "attempt_count": len(attempts),
            "any_passed": any_passed,
        },
        "shell_compat": shell_compat,
        "attempts": attempts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a focused URG troubleshooting loop on a remote Linux EDA host.")
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--vdb", type=Path, required=True)
    parser.add_argument("--vendor-vcs-home", type=Path, required=True)
    parser.add_argument("--overlay-vcs-home", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_attempts(
        workdir=args.workdir,
        vdb=args.vdb,
        vendor_vcs_home=args.vendor_vcs_home,
        overlay_vcs_home=args.overlay_vcs_home,
        timeout=args.timeout,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result["status"])
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
