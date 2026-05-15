#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _generated_file_diagnostic(project_root: Path, rel_file: str) -> list[str]:
    if (project_root / rel_file).exists():
        return []
    return [f"generated filelist is absent until Genesis2 generation succeeds: {rel_file}"]


def build_plan(
    *,
    project_root: Path,
    product: str = "FPGen",
    include_gate: bool = False,
    dry_run: bool = True,
) -> dict:
    project_root = project_root.resolve()
    top = f"top_{product}"
    vlog_list = "genesis_vlog.vf"
    synth_list = "genesis_vlog.synth.vf"
    verif_list = "genesis_vlog.verif.vf"
    hierarchy = f"{product}.xml"
    simv = _rel(project_root / "simv", project_root)
    diagnostics = _generated_file_diagnostic(project_root, vlog_list)

    optional_deps = ["Genesis2.pl", "DesignWare/gtech libraries", "vcs"]
    generate_cmd = [
        "Genesis2.pl",
        "-gen",
        "-top",
        top,
        "-synthtop",
        top,
        "-depend",
        "depend.list",
        "-product",
        vlog_list,
        "-hierarchy",
        hierarchy,
        "-debug",
        "0",
    ]
    compile_cmd = [
        "vcs",
        "-sverilog",
        "+cli",
        "+memcbk",
        "+lint=PCWM",
        "+libext+.v",
        "-notice",
        "-full64",
        "+v2k",
        "-debug_pp",
        "-timescale=1ps/1ps",
        "+noportcoerce",
        "+vcs+lic+wait",
        "+notimingcheck",
        "+delay_mode_zero",
        "-licqueue",
        "-top",
        top,
        "-y",
        ".",
        "+incdir+.",
        "-f",
        vlog_list,
    ]
    simulate_cmd = [
        simv,
        "-l",
        "simv.log",
        "+vcs+lic+wait",
        "+vpdbufsize+100",
        "+vpdfileswitchsize+100",
        "-l",
        "run_bb.log",
    ]
    saif_cmd = [
        simv,
        "-l",
        f"{simv}.rtl_saif.log",
        "+vcs+lic+wait",
        "+vpdbufsize+100",
        "+vpdfileswitchsize+100",
        "+SAIF",
        "+clk_period=1000",
        "+NumTrans=1000",
        "+notimingcheck",
        "+SignIsPos_DistWeight=50",
        "+Random_DistWeight=200",
        "+Silent",
    ]

    plan = {
        "status": "dry-run" if dry_run else "planned",
        "scope": "supported_local_plan",
        "project_root": str(project_root),
        "product": product,
        "top": top,
        "source_lists": [vlog_list, synth_list, verif_list],
        "optional_external_dependencies": optional_deps,
        "diagnostics": diagnostics,
        "generate": {"cmd": generate_cmd, "outputs": [vlog_list, synth_list, verif_list, hierarchy]},
        "compile": {"workdir": ".", "cmd": compile_cmd, "log": "comp_bb.log"},
        "simulate": {"workdir": ".", "cmd": simulate_cmd, "log": "run_bb.log"},
        "saif": {"scope": "guarded_optional", "simulate_cmd": saif_cmd, "expected_artifact": f"{product}.saif"},
        "expected_artifacts": ["simv", "simv.log", "run_bb.log", "vcdplus.vpd"],
    }
    if include_gate:
        optional_deps.extend(["dc_shell", "icc_shell", "technology libraries"])
        plan["gate_level"] = {
            "scope": "guarded_optional",
            "dc_compile_cmd": [
                "vcs",
                "+define+GATES",
                *compile_cmd[1:],
                "-f",
                verif_list,
                "-o",
                "simv.dc_gate",
            ],
            "icc_compile_cmd": [
                "vcs",
                "+define+GATES",
                *compile_cmd[1:],
                "-f",
                verif_list,
                "-o",
                "simv.icc_gate",
            ],
        }
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan FP-Gen Genesis2 plus VCS non-GUI flows.")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--product", default="FPGen")
    parser.add_argument("--include-gate", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    plan = build_plan(project_root=args.project_root, product=args.product, include_gate=args.include_gate, dry_run=not args.execute)
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
