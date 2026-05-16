#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path


METRICS = ["line", "cond", "fsm"]


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _source_diagnostics(task_root: Path, task: str) -> list[str]:
    diagnostics: list[str] = []
    for name in (f"{task}.v", "testbench.v"):
        if not (task_root / name).exists():
            diagnostics.append(f"missing source: {name}")
    return diagnostics


def _tool(tools: dict[str, str] | None, name: str, default: str) -> str:
    return str((tools or {}).get(name) or default)


def _stage_args(value: list[str] | tuple[str, ...] | None) -> list[str]:
    return [str(item) for item in (value or [])]


def _infer_top(task_root: Path) -> str:
    testbench = task_root / "testbench.v"
    if not testbench.exists():
        return "testbench"
    for line in testbench.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if stripped.startswith("module "):
            return stripped.split()[1].split("(")[0].rstrip(";")
    return "testbench"


def parse_simulation_log(text: str) -> dict:
    match = re.search(r"Mismatches:\s*(\d+)\s+in\s+(\d+)\s+samples", text, re.IGNORECASE)
    if not match:
        return {"status": "unknown", "reason": "mismatch_summary_not_found"}
    mismatches = int(match.group(1))
    samples = int(match.group(2))
    return {
        "status": "passed" if mismatches == 0 else "failed",
        "mismatches": mismatches,
        "samples": samples,
    }


def _artifact_status(task_root: Path, artifacts: list[str]) -> dict:
    status: dict[str, dict] = {}
    for artifact in artifacts:
        path = task_root / artifact
        exists = path.exists()
        bytes_count = 0
        if exists and path.is_file():
            bytes_count = path.stat().st_size
        elif exists and path.is_dir():
            bytes_count = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
        status[artifact] = {
            "path": str(path),
            "exists": exists,
            "bytes": bytes_count,
        }
    return status


def _run_step(step: dict, *, timeout: int) -> dict:
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
        return {
            **step,
            "returncode": completed.returncode,
            "status": "passed" if completed.returncode == 0 else "failed",
            "elapsed_sec": round(time.monotonic() - started, 3),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            **step,
            "returncode": None,
            "status": "timeout",
            "elapsed_sec": round(time.monotonic() - started, 3),
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or f"timeout after {timeout}s",
        }


def build_plan(
    *,
    task_root: Path,
    task: str | None = None,
    dry_run: bool = True,
    fsdb: str | None = None,
    verdi_check: str = "none",
    report_signal: str | None = None,
    tools: dict[str, str] | None = None,
    compile_args: list[str] | None = None,
    simulate_args: list[str] | None = None,
    urg_args: list[str] | None = None,
    verdi_args: list[str] | None = None,
) -> dict:
    task_root = task_root.resolve()
    task = task or task_root.name
    diagnostics = _source_diagnostics(task_root, task)
    metrics_arg = "+".join(METRICS)
    fsdb_name = fsdb or ""
    base = {
        "task": task,
        "task_root": str(task_root),
        "frontend": "AutoVeriFix",
        "scope": "non-gui scripted VCS coverage loop",
        "top": _infer_top(task_root),
        "sources": [f"{task}.v", "testbench.v"],
        "coverage": {"metrics": METRICS},
        "diagnostics": diagnostics,
        "guarded_external_dependencies": ["LLM/API repair loop"],
    }
    if diagnostics:
        return {**base, "status": "blocked", "reason": "missing_sources"}

    compile_cmd = [
        _tool(tools, "vcs", "vcs"),
        *_stage_args(compile_args),
        "-full64",
        "-sverilog",
        "+v2k",
        "-timescale=1ns/1ns",
        "-debug_acc+all",
        "-debug_region+cell+encrypt",
        "-l",
        "compile.log",
        "-cm",
        metrics_arg,
        f"{task}.v",
        "testbench.v",
    ]
    simulate_cmd = [
        _tool(tools, "simv", "./simv"),
        *_stage_args(simulate_args),
        "-l",
        f"{task}_sim.log",
        "-cm",
        metrics_arg,
    ]
    urg_cmd = [
        _tool(tools, "urg", "urg"),
        *_stage_args(urg_args),
        "-dir",
        "simv.vdb",
        "-report",
        "full_report",
        "-format",
        "both",
    ]
    expected_artifacts = ["simv", "simv.vdb", "full_report"]
    steps = [
        {"name": "compile", "cwd": str(task_root), "cmd": compile_cmd},
        {"name": "simulate", "cwd": str(task_root), "cmd": simulate_cmd},
        {"name": "coverage", "cwd": str(task_root), "cmd": urg_cmd},
    ]
    output = {
        **base,
        "status": "dry-run" if dry_run else "planned",
        "steps": steps,
        "compile": {"cwd": str(task_root), "cmd": compile_cmd},
        "simulate": {"cwd": str(task_root), "cmd": simulate_cmd},
        "coverage": {
            "metrics": METRICS,
            "vdb_dir": "simv.vdb",
            "report_dir": "full_report",
            "urg_cmd": urg_cmd,
        },
        "expected_artifacts": expected_artifacts,
        "cleanup": ["*.log", "csrc", "simv*", "*.key", "*.vpd", "DVEfiles", "coverage", "*.vdb", "output.txt"],
    }
    if fsdb_name:
        expected_artifacts.append(fsdb_name)
    if verdi_check == "fsdbreport":
        signal = report_signal or f"/{output['top']}/clk"
        verdi_cmd = [
            _tool(tools, "fsdbreport", "fsdbreport"),
            *_stage_args(verdi_args),
            fsdb_name or "waves.fsdb",
            "-s",
            signal,
        ]
        output["verdi"] = {
            "mode": "fsdbreport",
            "cwd": str(task_root),
            "cmd": verdi_cmd,
            "fsdb": fsdb_name or "waves.fsdb",
            "report_signal": signal,
        }
        steps.append({"name": "verdi-fsdbreport-check", "cwd": str(task_root), "cmd": verdi_cmd})
    elif verdi_check == "verdi":
        verdi_cmd = [
            _tool(tools, "verdi", "verdi"),
            *_stage_args(verdi_args),
            "-ssf",
            fsdb_name or "waves.fsdb",
            "-nologo",
            "-exit",
        ]
        output["verdi"] = {
            "mode": "verdi",
            "cwd": str(task_root),
            "cmd": verdi_cmd,
            "fsdb": fsdb_name or "waves.fsdb",
        }
        steps.append({"name": "verdi-load-check", "cwd": str(task_root), "cmd": verdi_cmd})
    elif verdi_check != "none":
        raise ValueError("verdi_check must be one of: none, fsdbreport, verdi")
    return output


def execute_plan(plan: dict, *, timeout: int = 300) -> dict:
    results = []
    for step in plan.get("steps", []):
        result = _run_step(step, timeout=timeout)
        results.append(result)
        if result["status"] != "passed":
            break
    task_root = Path(plan["task_root"])
    sim_log = task_root / f"{plan['task']}_sim.log"
    if sim_log.exists():
        simulation = parse_simulation_log(sim_log.read_text(encoding="utf-8", errors="ignore"))
    else:
        simulation = {"status": "unknown", "reason": "simulation_log_missing", "path": str(sim_log)}
    artifacts = _artifact_status(task_root, plan.get("expected_artifacts", []))
    command_status = "passed" if results and len(results) == len(plan.get("steps", [])) and all(item["status"] == "passed" for item in results) else "failed"
    status = "passed" if command_status == "passed" and simulation["status"] == "passed" else "failed"
    return {
        **plan,
        "status": status,
        "results": results,
        "simulation": simulation,
        "artifacts": artifacts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan an AutoVeriFix-style non-GUI VCS coverage loop.")
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--task")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--fsdb")
    parser.add_argument("--verdi-check", choices=["none", "fsdbreport", "verdi"], default="none")
    parser.add_argument("--report-signal")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    plan = build_plan(
        task_root=args.task_root,
        task=args.task,
        dry_run=not args.execute,
        fsdb=args.fsdb,
        verdi_check=args.verdi_check,
        report_signal=args.report_signal,
    )
    result = execute_plan(plan, timeout=args.timeout) if args.execute and plan.get("status") != "blocked" else plan
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(" ".join(result.get("compile", {}).get("cmd", [])))
    return 0 if result.get("status") in {"dry-run", "passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
