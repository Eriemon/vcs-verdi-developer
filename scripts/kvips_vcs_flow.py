#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


PROTOCOLS = {
    "apb": {"default_test": "apb_b2b_smoke_test", "top": "tb_top"},
    "ahb": {"default_test": "ahb_smoke_test", "top": "top"},
    "axi4": {"default_test": "axi4_b2b_test", "top": "top"},
}


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _read_filelist(filelist: Path) -> tuple[list[str], list[str]]:
    include_dirs: list[str] = []
    sources: list[str] = []
    if not filelist.exists():
        return include_dirs, sources
    for raw in filelist.read_text(encoding="utf-8").splitlines():
        line = raw.split("//", 1)[0].split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("+incdir+"):
            for item in line.split("+incdir+")[1:]:
                item = item.strip()
                if item:
                    include_dirs.append(item)
        elif line.startswith(("+", "-")):
            continue
        else:
            sources.append(line)
    return include_dirs, sources


def _read_tests(tests_file: Path) -> list[str]:
    if not tests_file.exists():
        return []
    tests = []
    for raw in tests_file.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            tests.append(line)
    return tests


def build_plan(
    *,
    project_root: Path,
    protocol: str,
    test: str | None = None,
    seed: int = 1,
    uvm_verbosity: str = "UVM_LOW",
    plusargs: list[str] | None = None,
    enable_fsdb: bool = False,
    fsdb: str | None = None,
    dry_run: bool = True,
) -> dict:
    protocol = protocol.lower()
    if protocol not in PROTOCOLS:
        raise ValueError(f"unsupported KVIPS protocol: {protocol}")

    project_root = project_root.resolve()
    defaults = PROTOCOLS[protocol]
    test = test or defaults["default_test"]
    plusargs = plusargs or []

    sim_dir = project_root / protocol / "examples" / "uvm_back2back" / "sim"
    filelist = sim_dir / "filelist.f"
    out_dir = project_root / protocol / "examples" / "uvm_back2back" / "sim" / "out" / "vcs"
    abs_filelist = out_dir / "filelist.abs.f"
    include_dirs, sources = _read_filelist(filelist)
    tests = _read_tests(sim_dir / "tests_questa.list")

    diagnostics = [
        "detected UVM dependency; KVIPS is guarded/optional and not part of core low-dependency support",
    ]
    if not filelist.exists():
        diagnostics.append(f"missing KVIPS filelist: {_rel(filelist, project_root)}")
    if not tests:
        diagnostics.append(f"missing or empty regression list: {_rel(sim_dir / 'tests_questa.list', project_root)}")

    compile_cmd = [
        "vcs",
        "-full64",
        "-sverilog",
        "-timescale=1ns/1ps",
        "-ntb_opts",
        "uvm-1.2",
    ]
    optional_deps = ["uvm", "vcs"]
    if enable_fsdb:
        compile_cmd.extend(
            [
                "-P",
                "$VERDI_HOME/share/PLI/VCS/LINUX64/novas.tab",
                "$VERDI_HOME/share/PLI/VCS/LINUX64/pli.a",
                "+define+FSDB",
            ]
        )
        optional_deps.append("verdi/fsdbreport")
        diagnostics.append("FSDB dumping requires VERDI_HOME PLI paths and a matching VCS/Verdi installation")
    compile_cmd.extend(
        [
            "-f",
            _rel(abs_filelist, project_root),
            "-Mdir",
            "csrc",
            "-o",
            "simv",
            "-l",
            _rel(out_dir / "compile.log", project_root),
        ]
    )

    sim_cmd = [
        "./simv",
        f"+UVM_TESTNAME={test}",
        f"+UVM_VERBOSITY={uvm_verbosity}",
        f"+ntb_random_seed={seed}",
        *plusargs,
    ]
    fsdb_path = fsdb or _rel(out_dir / f"kvips_{protocol}_b2b.fsdb", project_root)
    report_cfg = sim_dir / f"fsdbreport_{protocol}.cfg"
    report_out = out_dir / f"kvips_{protocol}_b2b.txt"

    return {
        "status": "dry-run" if dry_run else "planned",
        "scope": "guarded_optional",
        "protocol": protocol,
        "top": defaults["top"],
        "project_root": str(project_root),
        "source_lists": [_rel(filelist, project_root)],
        "include_dirs": include_dirs,
        "sources": sources,
        "optional_external_dependencies": optional_deps,
        "diagnostics": diagnostics,
        "preprocess": {
            "description": "expand filelist.f to filelist.abs.f before VCS compile",
            "input": _rel(filelist, project_root),
            "output": _rel(abs_filelist, project_root),
        },
        "compile": {
            "workdir": _rel(out_dir, project_root),
            "cmd": compile_cmd,
            "log": _rel(out_dir / "compile.log", project_root),
        },
        "simulate": {
            "workdir": _rel(out_dir, project_root),
            "cmd": sim_cmd,
            "log": _rel(out_dir / "run.log", project_root),
        },
        "regression": {
            "tests_file": _rel(sim_dir / "tests_questa.list", project_root),
            "tests": tests,
            "cmd_template": [_rel(sim_dir / "run_vcs.sh", project_root), "+UVM_TESTNAME=<test>"],
        },
        "fsdbreport": {
            "cfg": _rel(report_cfg, project_root),
            "cmd": ["fsdbreport", fsdb_path, "-f", _rel(report_cfg, project_root), "-o", _rel(report_out, project_root)],
            "output": _rel(report_out, project_root),
        },
        "verdi_load_check": {
            "cmd": ["verdi", "-ssf", fsdb_path, "-nologo", "-exit", "-l", _rel(out_dir / "verdi_load.log", project_root)]
        },
        "expected_artifacts": {
            "simv": _rel(out_dir / "simv", project_root),
            "run_log": _rel(out_dir / "run.log", project_root),
            "fsdb": fsdb_path,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan guarded KVIPS VCS/UVM non-GUI flows.")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--protocol", choices=sorted(PROTOCOLS), required=True)
    parser.add_argument("--test")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--uvm-verbosity", default="UVM_LOW")
    parser.add_argument("--plusarg", action="append", default=[])
    parser.add_argument("--enable-fsdb", action="store_true")
    parser.add_argument("--fsdb")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    plan = build_plan(
        project_root=args.project_root,
        protocol=args.protocol,
        test=args.test,
        seed=args.seed,
        uvm_verbosity=args.uvm_verbosity,
        plusargs=args.plusarg,
        enable_fsdb=args.enable_fsdb,
        fsdb=args.fsdb,
        dry_run=not args.execute,
    )
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
