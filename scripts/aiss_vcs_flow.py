#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


TARGETS = {
    "MCSEtest": {
        "top": "mcse_top_tb",
        "sources": [
            "mcse_top.sv",
            "mcse_control_unit.sv",
            "secure_memory.sv",
            "secure_boot_control.sv",
            "lifecycle_protection.sv",
            "lc_memory.sv",
            "fw_auth.sv",
            "min_security_module.sv",
            "sha_top.sv",
            "sha256_puf_256.v",
            "camellia_top.sv",
            "camellia.v",
            "gpio.v",
            "gpio_regmap.v",
            "oh_dsync.v",
            "io.v",
            "packet2emesh.v",
            "c1908.v",
            "primitives.v",
            "puf.v",
            "bus_translation.sv",
            "data_worker.sv",
            "error_correction.v",
            "mcse_top_tb.sv",
            "lec25dscc25.v",
        ],
        "scope": "rtl",
    },
    "AHBtest": {
        "top": "data_worker_tb",
        "sources": ["data_worker.sv", "data_worker_tb.sv"],
        "scope": "rtl",
    },
    "NETLISTtest": {
        "top": "mcse_top_netlist_tb",
        "sources": ["mcse_netlist.v", "mcse_top_netlist_tb.sv", "lec25dscc25.v"],
        "scope": "guarded_gate_level",
    },
}


FLAGS = ["-sverilog", "-suppress", "-R", "+vcs+vcdpluson"]


def _missing_sources(project_root: Path, sources: list[str]) -> list[str]:
    return [f"missing source: {source}" for source in sources if not (project_root / source).exists()]


def build_plan(*, project_root: Path, target: str = "MCSEtest", dry_run: bool = True) -> dict:
    project_root = project_root.resolve()
    if target == "synthesis":
        script = "compiledc.tcl"
        diagnostics = [] if (project_root / script).exists() else [f"missing source: {script}"]
        return {
            "status": "guarded",
            "target": target,
            "scope": "guarded_synthesis",
            "project_root": str(project_root),
            "guarded_external_dependencies": ["dc_shell", "standard-cell .db libraries"],
            "diagnostics": diagnostics,
            "synthesis": {"cwd": str(project_root), "cmd": ["dc_shell", "-f", script]},
        }

    if target not in TARGETS:
        return {
            "status": "blocked",
            "target": target,
            "reason": "unknown_target",
            "diagnostics": [f"unknown AISS target: {target}"],
        }

    target_spec = TARGETS[target]
    sources = list(target_spec["sources"])
    diagnostics = _missing_sources(project_root, sources)
    scope = target_spec["scope"]
    guarded_deps: list[str] = []
    if scope == "guarded_gate_level":
        guarded_deps = ["standard-cell library netlist", "gate-level testbench", "VCS gate-level simulation"]
    compile_cmd = ["vcs", *sources, *FLAGS]
    status = "guarded" if guarded_deps or diagnostics else ("dry-run" if dry_run else "planned")
    return {
        "status": status,
        "target": target,
        "scope": scope,
        "project_root": str(project_root),
        "top": target_spec["top"],
        "sources": sources,
        "compile": {"cwd": str(project_root), "cmd": compile_cmd},
        "expected_artifacts": ["simv", "vcdplus.vpd"],
        "diagnostics": diagnostics,
        "guarded_external_dependencies": guarded_deps,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan AISS Phase III non-GUI VCS/DC targets.")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--target", default="MCSEtest")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    plan = build_plan(project_root=args.project_root, target=args.target, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print(" ".join(plan.get("compile", plan.get("synthesis", {})).get("cmd", [])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
