#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _step(name: str, cmd: list[str], cwd: Path) -> dict:
    return {"name": name, "cmd": [str(item) for item in cmd], "cwd": str(cwd)}


def build_plan(
    *,
    project_root: Path | str,
    dv_root: Path | str,
    test: str,
    seed: int | None = None,
    execute: bool = False,
) -> dict:
    project_root = Path(project_root).resolve()
    dv_root = Path(dv_root).resolve()
    out_dir = dv_root / "out"
    build_dir = out_dir / "build"
    simv = build_dir / "simv"
    hex_path = out_dir / "picorv32" / test / "test.hex"
    trace_path = out_dir / "picorv32" / test / "trace_core_0.log"
    coverage_vdb = build_dir / "coverage.vdb"
    coverage_report = out_dir / "cov_report"
    seed_arg = f"--seed={seed}" if seed is not None else "--seed=1"
    steps = [
        _step(
            "riscv_dv_gen",
            ["python3", str(dv_root / "scripts" / "run_riscv_dv.py"), f"--test={test}", seed_arg],
            dv_root,
        ),
        _step(
            "compile_test",
            ["make", "-C", str(dv_root), "riscv_dv_test", f"TEST={test}", f"SEED={seed if seed is not None else 1}"],
            project_root,
        ),
        _step(
            "vcs_compile",
            [
                "vcs",
                "-full64",
                "-sverilog",
                "-f",
                "cfg/vcs.f",
                "-o",
                str(simv),
                "-debug_access+all",
                "-timescale=1ns/1ps",
                "-cm",
                "line+cond+fsm+tgl+branch",
                "-cm_dir",
                str(coverage_vdb),
            ],
            dv_root,
        ),
        _step(
            "simv",
            [
                str(simv),
                f"+hex={hex_path}",
                f"+trace={trace_path}",
                "-cm",
                "line+cond+fsm+tgl+branch",
            ],
            dv_root,
        ),
        _step("urg_report", ["urg", "-full64", "-dir", str(coverage_vdb), "-report", str(coverage_report)], dv_root),
        _step("trace_compare", ["python3", str(dv_root / "scripts" / "compare_trace.py"), str(trace_path)], dv_root),
    ]
    return {
        "status": "planned" if execute else "dry-run",
        "project_root": str(project_root),
        "dv_root": str(dv_root),
        "test": test,
        "seed": seed,
        "steps": steps,
        "steps_by_name": {step["name"]: step for step in steps},
        "expected_artifacts": {
            "test.hex": _rel(hex_path, project_root),
            "trace_core_0.log": _rel(trace_path, project_root),
            "coverage.vdb": _rel(coverage_vdb, project_root),
            "cov_report": _rel(coverage_report, project_root),
        },
        "optional_external_dependencies": ["riscv-dv"],
    }


def execute_plan(plan: dict, *, timeout: int) -> dict:
    results = []
    for step in plan["steps"]:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                step["cmd"],
                cwd=step["cwd"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
            result = {
                **step,
                "returncode": completed.returncode,
                "status": "passed" if completed.returncode == 0 else "failed",
                "elapsed_sec": round(time.monotonic() - started, 3),
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        except subprocess.TimeoutExpired as exc:
            result = {
                **step,
                "returncode": None,
                "status": "timeout",
                "elapsed_sec": round(time.monotonic() - started, 3),
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or f"timeout after {timeout}s",
            }
        results.append(result)
        if result["status"] != "passed":
            break
    return {**plan, "status": "passed" if results and results[-1]["status"] == "passed" and len(results) == len(plan["steps"]) else "failed", "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan a non-GUI PicoRV32 riscv-dv VCS workflow.")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--dv-root", type=Path, required=True)
    parser.add_argument("--test", required=True)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print the non-executing plan; this is the default.")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    plan = build_plan(project_root=args.project_root, dv_root=args.dv_root, test=args.test, seed=args.seed, execute=args.execute)
    result = execute_plan(plan, timeout=args.timeout) if args.execute else plan
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for step in result["steps"]:
            print(f"{step['name']}: {' '.join(step['cmd'])}")
        print("status: " + result["status"])
    return 0 if result["status"] in {"dry-run", "passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
