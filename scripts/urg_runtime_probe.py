#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


TRACE_EVENTS = "execve,openat,access,stat,statx,readlink,mmap,mprotect,munmap,brk,clone,futex,rt_sigaction,rt_sigprocmask,kill,tgkill,exit_group"


def sha256_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def first_line(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (FileNotFoundError, IsADirectoryError, IndexError, OSError, UnicodeDecodeError):
        return ""


def which_tool(name: str, env: dict[str, str]) -> str:
    found = shutil.which(name, path=env.get("PATH"))
    if found:
        return found
    for entry in env.get("PATH", "").split(os.pathsep):
        candidate = Path(entry) / name
        if candidate.exists():
            return str(candidate)
    return ""


def command_result(cmd: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 10) -> dict:
    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return {
            "cmd": cmd,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired) as exc:
        return {
            "cmd": cmd,
            "returncode": None,
            "stdout": getattr(exc, "stdout", "") or "",
            "stderr": str(exc),
        }


def candidate_libraries(vcs_home: Path) -> dict:
    candidates = {
        "urg1": vcs_home / "linux64" / "bin" / "urg1",
        "libucapi.so": vcs_home / "linux64" / "lib" / "libucapi.so",
        "libsnpsmalloc.so": vcs_home / "linux64" / "lib" / "libsnpsmalloc.so",
        "libhvpapi.so": vcs_home / "linux64" / "lib" / "libhvpapi.so",
        "patched_libucapi.so": vcs_home / "ucapi_patch_lib" / "libucapi.so",
    }
    return {
        name: {
            "path": str(path),
            "exists": path.exists(),
            "is_symlink": path.is_symlink(),
            "sha256": sha256_file(path),
        }
        for name, path in candidates.items()
    }


def activation_status(vcs_home: Path, env: dict[str, str]) -> dict:
    entries = [item for item in env.get("LD_LIBRARY_PATH", "").split(os.pathsep) if item]
    patch_lib = str(vcs_home / "ucapi_patch_lib")
    vcs_lib = str(vcs_home / "linux64" / "lib")
    patch_index = entries.index(patch_lib) if patch_lib in entries else None
    vcs_index = entries.index(vcs_lib) if vcs_lib in entries else None
    return {
        "ld_library_path_entries": entries,
        "patch_lib": patch_lib,
        "vcs_lib": vcs_lib,
        "patch_lib_exists": Path(patch_lib).exists(),
        "patch_lib_precedes_vcs_lib": patch_index is not None and (vcs_index is None or patch_index < vcs_index),
    }


def _summarize_strace(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "exists": False}
    text = path.read_text(encoding="utf-8", errors="replace")
    hits = []
    for line in text.splitlines():
        if any(token in line for token in ("SIGSEGV", "SIGABRT", "SIGBUS", "libucapi", "libsnpsmalloc", "libhvpapi", "exit_group")):
            hits.append(line)
    return {
        "path": str(path),
        "exists": True,
        "bytes": path.stat().st_size,
        "interesting_tail": hits[-80:],
    }


def run_probe(
    *,
    vcs_home: Path | str,
    workdir: Path | str,
    vdb: Path | str,
    report_dir: Path | str,
    env: dict[str, str] | None = None,
    strace_timeout: int = 0,
) -> dict:
    vcs_home = Path(vcs_home)
    workdir = Path(workdir)
    vdb = Path(vdb)
    report_dir = Path(report_dir)
    probe_env = os.environ.copy()
    probe_env.update(env or {})
    probe_env["VCS_HOME"] = str(vcs_home)
    probe_env["PATH"] = str(vcs_home / "bin") + os.pathsep + probe_env.get("PATH", "")

    which_urg = which_tool("urg", probe_env)
    which_vcs = which_tool("vcs", probe_env)
    urg_path = Path(which_urg) if which_urg else vcs_home / "bin" / "urg"
    vcs_cmd = [which_vcs or "vcs"]
    urg1 = vcs_home / "linux64" / "bin" / "urg1"
    libucapi = vcs_home / "linux64" / "lib" / "libucapi.so"

    result = {
        "status": "passed",
        "vcs_home": str(vcs_home),
        "workdir": str(workdir),
        "vdb": str(vdb),
        "report_dir": str(report_dir),
        "which_urg": which_urg,
        "which_vcs": which_vcs,
        "wrapper_first_line": first_line(urg_path),
        "vcs_location": command_result([*vcs_cmd, "-location"], cwd=workdir, env=probe_env),
        "vcs_full64_location": command_result([*vcs_cmd, "-full64", "-location"], cwd=workdir, env=probe_env),
        "ldd_urg1": command_result(["ldd", str(urg1)], cwd=workdir, env=probe_env),
        "ldd_libucapi": command_result(["ldd", str(libucapi)], cwd=workdir, env=probe_env),
        "candidate_libraries": candidate_libraries(vcs_home),
        "activation": activation_status(vcs_home, probe_env),
    }
    if strace_timeout > 0:
        strace_log = workdir / "urg_runtime_probe_strace.log"
        strace_cmd = [
            "strace",
            "-f",
            "-o",
            str(strace_log),
            "-e",
            f"trace={TRACE_EVENTS}",
            which_urg or "urg",
            "-full64",
            "-dir",
            str(vdb),
            "-report",
            str(report_dir),
        ]
        result["strace"] = {
            "execution": command_result(strace_cmd, cwd=workdir, env=probe_env, timeout=strace_timeout),
            "summary": _summarize_strace(strace_log),
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect read-only URG runtime and loader diagnostics.")
    parser.add_argument("--vcs-home", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--vdb", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--strace-timeout", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_probe(
        vcs_home=args.vcs_home,
        workdir=args.workdir,
        vdb=args.vdb,
        report_dir=args.report_dir,
        strace_timeout=args.strace_timeout,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
