#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


NON_GUI_SCOPE_TERMS = ("non-gui", "scripted", "does not claim complete coverage")

REQUIRED_SKILL_FILES = (
    "SKILL.md",
    "VERSION",
    "agents/openai.yaml",
    "evals/evals.json",
    "references/vcs-verdi-flow.md",
    "references/non-gui-flow.md",
    "references/capability-matrix.md",
    "references/verdi-rc-format.md",
    "references/third-party-extraction.md",
    "references/review-checklist.md",
    "references/remote-eda-gate.md",
    "scripts/check_env.py",
    "scripts/generate_rc.py",
    "scripts/smoke_vcs_verdi.py",
    "scripts/analyze_logs.py",
    "scripts/fsdb_tools.py",
    "scripts/coverage_flow.py",
    "scripts/patch_ucapi_overlay.py",
    "scripts/urg_runtime_probe.py",
    "scripts/urg_coverage_matrix.py",
    "scripts/import_vcs_project.py",
    "scripts/riscv_dv_flow.py",
    "scripts/kvips_vcs_flow.py",
    "scripts/fpgen_vcs_flow.py",
    "scripts/cocotb_vcs_flow.py",
    "scripts/evidence_claim_gate.py",
    "scripts/collect_evidence.py",
    "scripts/run_regression.py",
    "scripts/remote_eda_gate.py",
    "scripts/run_quality_gate.py",
    "assets/evidence/non_gui_claims.json",
    "assets/minimal_vcs/top.sv",
    "assets/minimal_vcs/coverage_top.sv",
    "assets/minimal_vcs/core.vhd",
    "assets/minimal_vcs/rtl.f",
    "assets/minimal_vcs/dump_ucli.tcl",
    "assets/minimal_vcs/manifest_matrix.json",
)

SCRIPT_MATRIX = (
    "check_env.py",
    "generate_rc.py",
    "smoke_vcs_verdi.py",
    "analyze_logs.py",
    "fsdb_tools.py",
    "coverage_flow.py",
    "patch_ucapi_overlay.py",
    "urg_runtime_probe.py",
    "urg_coverage_matrix.py",
    "import_vcs_project.py",
    "riscv_dv_flow.py",
    "kvips_vcs_flow.py",
    "fpgen_vcs_flow.py",
    "cocotb_vcs_flow.py",
    "evidence_claim_gate.py",
    "collect_evidence.py",
    "run_regression.py",
    "remote_eda_gate.py",
    "run_quality_gate.py",
)

CAPABILITY_MATRIX_TERMS = (
    "environment probe",
    "vcs_bin",
    "npi",
    "edalize",
    "fsdb",
    "coverage",
    "regression",
    "import",
    "evidence",
    "claim",
    "cocotb",
    "remote eda",
    "riscv-dv",
    "kvips",
    "fp-gen",
)


def _home_path(*parts: str) -> Path:
    return Path.home().joinpath(*parts)


def _script_if_exists(path: Path) -> str:
    return str(path) if path.exists() else str(path)


def _is_nested_repository_workspace(repo_root: Path, skill_dir: Path) -> bool:
    repository_dir = repo_root / "repository"
    if not repository_dir.exists():
        return False
    try:
        skill_dir.relative_to(repository_dir)
    except ValueError:
        return False
    return skill_dir != repo_root


def _package_probe_cmd(skill_dir: Path, target_dir: Path) -> list[str]:
    probe = (
        "import shutil, subprocess, sys, tempfile; "
        "from pathlib import Path; "
        "src = Path(sys.argv[1]).resolve(); "
        "target = Path(sys.argv[2]).resolve(); "
        "target.mkdir(parents=True, exist_ok=True); "
        "temp_root = Path(tempfile.mkdtemp(prefix='vcs-verdi-probe-')); "
        "probe_src = temp_root / src.name; "
        "shutil.copytree(src, probe_src, ignore=shutil.ignore_patterns('.git', 'build', '__pycache__', '*.egg-info')); "
        "subprocess.run([sys.executable, '-m', 'pip', 'install', '.', '--no-deps', '--upgrade', '--target', str(target)], cwd=probe_src, check=True)"
    )
    return [sys.executable, "-c", probe, str(skill_dir), str(target_dir)]


def audit_skill(skill_dir: Path) -> dict:
    skill_dir = skill_dir.resolve()
    checked: list[str] = []
    errors: list[str] = []
    for rel in REQUIRED_SKILL_FILES:
        checked.append(rel)
        if not (skill_dir / rel).exists():
            errors.append(f"missing required file: {rel}")

    skill_md = skill_dir / "SKILL.md"
    skill_name = ""
    if skill_md.exists():
        text = skill_md.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("name:"):
                skill_name = line.split(":", 1)[1].strip()
                break
        if skill_name != "vcs-verdi-developer":
            errors.append("SKILL.md name must be vcs-verdi-developer")
        lowered = text.lower()
        missing_scope_terms = [term for term in NON_GUI_SCOPE_TERMS if term not in lowered]
        if missing_scope_terms:
            errors.append("SKILL.md must state the non-GUI scripted scope and official-option boundary")
        if "complete coverage of every official synopsys" not in lowered:
            errors.append("SKILL.md must explicitly avoid claiming all official Synopsys options")

    return {
        "status": "passed" if not errors else "failed",
        "skill_dir": str(skill_dir),
        "skill_name": skill_name,
        "checked": checked,
        "errors": errors,
    }


def script_matrix_audit(skill_dir: Path) -> dict:
    skill_dir = skill_dir.resolve()
    errors: list[str] = []
    checked: list[str] = []
    for script in SCRIPT_MATRIX:
        rel = f"scripts/{script}"
        checked.append(rel)
        if not (skill_dir / rel).exists():
            errors.append(f"missing script matrix entry: {rel}")

    matrix = skill_dir / "references" / "capability-matrix.md"
    checked.append("references/capability-matrix.md")
    if not matrix.exists():
        errors.append("missing capability matrix")
    else:
        text = matrix.read_text(encoding="utf-8").lower()
        for term in CAPABILITY_MATRIX_TERMS:
            if term not in text:
                errors.append(f"capability matrix missing topic: {term}")
        if "complete coverage of every official synopsys" not in text:
            errors.append("capability matrix must keep the official-option boundary explicit")

    return {
        "status": "passed" if not errors else "failed",
        "skill_dir": str(skill_dir),
        "checked": checked,
        "errors": errors,
    }


def build_local_gate(repo_root: Path, *, skill_dir: Path | None = None) -> dict:
    repo_root = repo_root.resolve()
    skill_dir = (skill_dir or repo_root / "skills" / "vcs-verdi-developer").resolve()
    skill_tests_dir = skill_dir / "tests"
    nested_repository_workspace = _is_nested_repository_workspace(repo_root, skill_dir)
    quick_validate = _home_path(".codex", "skills", ".system", "skill-creator", "scripts", "quick_validate.py")
    agents_tools = _home_path(".codex", "skills", "agents-md-generator", "scripts")
    smoke_source = skill_dir / "assets" / "minimal_vcs" / "top.sv"
    manifest_matrix = skill_dir / "assets" / "minimal_vcs" / "manifest_matrix.json"
    ref3_makefile = repo_root / "ref" / "3-tinyriscv-modified" / "vcs" / "Makefile"
    ref3_filelist = repo_root / "ref" / "3-tinyriscv-modified" / "vcs" / "filelist.f"
    ref4_packet_makefile = repo_root / "ref" / "4-sigarch-rtl-project-template" / "packet-processor" / "Makefile"
    ref4_packet_root = repo_root / "ref" / "4-sigarch-rtl-project-template" / "packet-processor"
    ref4_shift_makefile = repo_root / "ref" / "4-sigarch-rtl-project-template" / "shift-reg" / "Makefile"
    ref4_shift_root = repo_root / "ref" / "4-sigarch-rtl-project-template" / "shift-reg"
    ref4_uvm_makefile = repo_root / "ref" / "4-sigarch-rtl-project-template" / "hello-world" / "Makefile"
    ref4_uvm_root = repo_root / "ref" / "4-sigarch-rtl-project-template" / "hello-world"
    ref5_sv_pkg = repo_root / "ref" / "5-pysv" / "tests" / "gold" / "test_generate_sv_binding.sv"
    ref5_tb = repo_root / "ref" / "5-pysv" / "tests" / "vectors" / "test_sv_boxfilter.sv"
    ref5_sv_lib = repo_root / "ref" / "5-pysv" / "build" / "libpysv.so"
    ref6_root = repo_root / "ref" / "6-picorv32"
    ref6_makefile = ref6_root / "dv" / "Makefile"
    ref6_filelist = ref6_root / "dv" / "cfg" / "vcs.f"
    ref7_root = repo_root / "ref" / "7-mipsfpga-plus"
    ref7_program = ref7_root / "programs" / "00_counter"
    ref7_makefile = ref7_program / "makefile"
    ref7_modelsim_tcl = ref7_program / "modelsim_script.tcl"
    ref8_root = repo_root / "ref" / "8-kvips"
    ref9_root = repo_root / "ref" / "9-FP-Gen"
    ref14_adder_root = repo_root / "ref" / "14-cocotb" / "examples" / "adder"
    ref14_adder_makefile = ref14_adder_root / "tests" / "Makefile"
    ref14_mixed_root = repo_root / "ref" / "14-cocotb" / "examples" / "mixed_language"
    ref14_mixed_makefile = ref14_mixed_root / "tests" / "Makefile"
    evidence_claims = skill_dir / "assets" / "evidence" / "non_gui_claims.json"
    ref12_root = repo_root / "ref" / "12-CSCD"
    ref12_makefile = ref12_root / "sim" / "Makefile"
    ref12_filelist = ref12_root / "rtl" / "filelist.f"
    ref13_root = repo_root / "ref" / "13-Computer-Organization"
    ref13_mp_setup_root = ref13_root / "mp_setup"
    ref13_mp_pipeline_root = ref13_root / "mp_pipeline"
    ref13_mp_cache_root = ref13_root / "mp_cache"
    ref13_mp_verif_cov_root = ref13_root / "mp_verif" / "constr_rand_cov"
    help_probe_scripts = (
        ("generate_rc_help", "generate_rc.py"),
        ("analyze_logs_help", "analyze_logs.py"),
        ("fsdb_tools_help", "fsdb_tools.py"),
        ("coverage_flow_help", "coverage_flow.py"),
        ("patch_ucapi_overlay_help", "patch_ucapi_overlay.py"),
        ("urg_runtime_probe_help", "urg_runtime_probe.py"),
        ("urg_coverage_matrix_help", "urg_coverage_matrix.py"),
        ("run_regression_help", "run_regression.py"),
        ("collect_evidence_help", "collect_evidence.py"),
        ("remote_eda_gate_help", "remote_eda_gate.py"),
    )
    steps = [
        {
            "name": "unit_tests",
            "cmd": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            "cwd": str(skill_dir),
            "required": skill_tests_dir.exists(),
        },
        {
            "name": "skill_quick_validate",
            "cmd": [sys.executable, _script_if_exists(quick_validate), str(skill_dir)],
            "cwd": str(repo_root),
            "required": quick_validate.exists(),
        },
        {
            "name": "package_install_probe",
            "cmd": _package_probe_cmd(skill_dir, repo_root / "build" / "vcs-verdi-install-probe"),
            "cwd": str(skill_dir),
            "required": True,
        },
        {
            "name": "agents_verify",
            "cmd": [sys.executable, _script_if_exists(agents_tools / "verify_agents.py"), str(repo_root)],
            "cwd": str(repo_root),
            "required": (agents_tools / "verify_agents.py").exists(),
        },
        {
            "name": "docs_verify",
            "cmd": [sys.executable, _script_if_exists(agents_tools / "manage_docs.py"), "verify", str(repo_root)],
            "cwd": str(repo_root),
            "required": (agents_tools / "manage_docs.py").exists() and not nested_repository_workspace,
        },
        {
            "name": "env_probe",
            "cmd": [sys.executable, str(skill_dir / "scripts" / "check_env.py"), "--json"],
            "cwd": str(repo_root),
            "required": True,
        },
        {
            "name": "smoke_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "smoke_vcs_verdi.py"),
                "--dry-run",
                "--json",
                "--source",
                str(smoke_source),
                "--workdir",
                str(repo_root / "build" / "vcs-verdi-smoke"),
                "--top",
                "top",
                "--dump-name",
                "waves.fsdb",
                "--verdi-check",
                "fsdbreport",
                "--report-signal",
                "/top/clk",
            ],
            "cwd": str(repo_root),
            "required": True,
        },
        {
            "name": "manifest_matrix_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "smoke_vcs_verdi.py"),
                "--manifest",
                str(manifest_matrix),
                "--workdir",
                str(repo_root / "build" / "vcs-verdi-manifest-matrix"),
                "--dry-run",
                "--json",
            ],
            "cwd": str(repo_root),
            "required": True,
            "json_contains": {"plan.plusargs": "+testcase=smoke"},
        },
        {
            "name": "ref3_import_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "import_vcs_project.py"),
                "--makefile",
                str(ref3_makefile),
                "--filelist",
                str(ref3_filelist),
                "--project-root",
                str(repo_root / "ref" / "3-tinyriscv-modified"),
                "--json",
            ],
            "cwd": str(repo_root),
            "required": ref3_makefile.exists() and ref3_filelist.exists(),
        },
        {
            "name": "ref4_packet_import_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "import_vcs_project.py"),
                "--makefile",
                str(ref4_packet_makefile),
                "--project-root",
                str(ref4_packet_root),
                "--json",
            ],
            "cwd": str(repo_root),
            "required": ref4_packet_makefile.exists(),
            "json_contains": {
                "sources": "rtl/packet_processor.sv",
                "include_dirs": "verif",
                "top": "packet_processor_tb",
                "vcs_args": "+vcs+dumpvars+dump.vcd",
                "simv_args": "simulation.log",
            },
        },
        {
            "name": "ref4_shift_import_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "import_vcs_project.py"),
                "--makefile",
                str(ref4_shift_makefile),
                "--project-root",
                str(ref4_shift_root),
                "--json",
            ],
            "cwd": str(repo_root),
            "required": ref4_shift_makefile.exists(),
            "json_contains": {
                "sources": "rtl/shift_register.sv",
                "top": "shift_register_tb",
                "vcs_args": "+vcs+dumpvars+dump.vcd",
                "simv_args": "simulation.log",
            },
        },
        {
            "name": "ref4_uvm_optional_import_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "import_vcs_project.py"),
                "--makefile",
                str(ref4_uvm_makefile),
                "--project-root",
                str(ref4_uvm_root),
                "--json",
            ],
            "cwd": str(repo_root),
            "required": ref4_uvm_makefile.exists(),
            "json_contains": {
                "optional_external_dependencies": "uvm",
                "diagnostics": "detected UVM dependency; skipped UVM_FLAGS for core non-GUI import",
            },
        },
        {
            "name": "ref5_dpi_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "smoke_vcs_verdi.py"),
                "--dry-run",
                "--json",
                "--source",
                str(ref5_sv_pkg),
                "--source",
                str(ref5_tb),
                "--workdir",
                str(repo_root / "build" / "vcs-verdi-dpi"),
                "--top",
                "top",
                "--sv-lib",
                str(ref5_sv_lib),
                "--verdi-check",
                "fsdbreport",
            ],
            "cwd": str(repo_root),
            "required": ref5_sv_pkg.exists() and ref5_tb.exists(),
            "json_contains": {"plan.sv_libs": str(ref5_sv_lib.resolve())},
        },
        {
            "name": "ref6_picorv32_dv_import_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "import_vcs_project.py"),
                "--makefile",
                str(ref6_makefile),
                "--filelist",
                str(ref6_filelist),
                "--project-root",
                str(ref6_root),
                "--json",
            ],
            "cwd": str(repo_root),
            "required": ref6_makefile.exists() and ref6_filelist.exists(),
            "json_contains": {
                "source_lists": "dv/cfg/vcs.f",
                "filelist_entries": "picorv32.v",
                "top": "top",
                "workdir": "dv",
                "vlogan_args": "-sverilog",
                "vcs_args": "-full64",
                "output": "dv/out/build/simv",
            },
        },
        {
            "name": "ref6_riscv_dv_plan_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "riscv_dv_flow.py"),
                "--project-root",
                str(ref6_root),
                "--dv-root",
                str(ref6_root / "dv"),
                "--test",
                "riscv_arithmetic_basic_test",
                "--seed",
                "42",
                "--dry-run",
                "--json",
            ],
            "cwd": str(repo_root),
            "required": ref6_root.exists(),
            "json_contains": {
                "status": "dry-run",
                "optional_external_dependencies": "riscv-dv",
            },
        },
        {
            "name": "ref7_mipsfpga_modelsim_import_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "import_vcs_project.py"),
                "--makefile",
                str(ref7_makefile),
                "--modelsim-tcl",
                str(ref7_modelsim_tcl),
                "--project-root",
                str(ref7_root),
                "--json",
            ],
            "cwd": str(repo_root),
            "required": ref7_makefile.exists() and ref7_modelsim_tcl.exists(),
            "json_contains": {
                "top": "mfp_testbench",
                "include_dirs": "core",
                "sources": "testbench/mfp_testbench.v",
                "pre_sim_artifacts": "program_1fc00000.hex",
                "optional_external_dependencies": "mips-mti-elf-gcc",
            },
        },
        {
            "name": "ref7_mipsfpga_icarus_import_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "import_vcs_project.py"),
                "--makefile",
                str(ref7_makefile),
                "--project-root",
                str(ref7_root),
                "--json",
            ],
            "cwd": str(repo_root),
            "required": ref7_makefile.exists(),
            "json_contains": {
                "top": "mfp_testbench",
                "include_dirs": "system_rtl/uart16550",
                "sources": "testbench/mfp_testbench.v",
                "diagnostics": "source glob matched no files: core/*.v",
            },
        },
        {
            "name": "ref8_kvips_apb_plan_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "kvips_vcs_flow.py"),
                "--project-root",
                str(ref8_root),
                "--protocol",
                "apb",
                "--test",
                "apb_b2b_smoke_test",
                "--seed",
                "7",
                "--plusarg",
                "+APB_PROTOCOL=APB4",
                "--dry-run",
                "--json",
            ],
            "cwd": str(repo_root),
            "required": ref8_root.exists(),
            "json_contains": {
                "status": "dry-run",
                "scope": "guarded_optional",
                "optional_external_dependencies": "uvm",
                "source_lists": "apb/examples/uvm_back2back/sim/filelist.f",
                "include_dirs": "apb/sv/uvm",
                "sources": "apb/sv/if/apb_if.sv",
            },
        },
        {
            "name": "ref8_kvips_axi4_fsdbreport_plan_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "kvips_vcs_flow.py"),
                "--project-root",
                str(ref8_root),
                "--protocol",
                "axi4",
                "--test",
                "axi4_b2b_test",
                "--enable-fsdb",
                "--fsdb",
                "out/vcs/kvips_axi4_b2b.fsdb",
                "--dry-run",
                "--json",
            ],
            "cwd": str(repo_root),
            "required": ref8_root.exists(),
            "json_contains": {
                "status": "dry-run",
                "optional_external_dependencies": "verdi/fsdbreport",
                "compile.cmd": "+define+FSDB",
            },
        },
        {
            "name": "ref9_fpgen_plan_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "fpgen_vcs_flow.py"),
                "--project-root",
                str(ref9_root),
                "--product",
                "FPGen",
                "--include-gate",
                "--dry-run",
                "--json",
            ],
            "cwd": str(repo_root),
            "required": ref9_root.exists(),
            "json_contains": {
                "status": "dry-run",
                "top": "top_FPGen",
                "source_lists": "genesis_vlog.vf",
                "optional_external_dependencies": "Genesis2.pl",
                "expected_artifacts": "vcdplus.vpd",
            },
        },
        {
            "name": "ref14_cocotb_adder_vcs_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "cocotb_vcs_flow.py"),
                "--makefile",
                str(ref14_adder_makefile),
                "--project-root",
                str(ref14_adder_root),
                "--toplevel-lang",
                "verilog",
                "--cocotb-lib",
                "/path/to/cocotb/libcocotbvpi_vcs.so",
                "--dry-run",
                "--json",
            ],
            "cwd": str(repo_root),
            "required": ref14_adder_makefile.exists(),
            "json_contains": {
                "status": "dry-run",
                "top": "adder",
                "module": "test_adder",
                "compile.cmd": "+vpi",
                "sources.verilog": "hdl/adder.sv",
            },
        },
        {
            "name": "ref14_cocotb_mixed_vhdl_guard",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "cocotb_vcs_flow.py"),
                "--makefile",
                str(ref14_mixed_makefile),
                "--project-root",
                str(ref14_mixed_root),
                "--toplevel-lang",
                "verilog",
                "--cocotb-lib",
                "/path/to/cocotb/libcocotbvpi_vcs.so",
                "--dry-run",
                "--json",
            ],
            "cwd": str(repo_root),
            "required": ref14_mixed_makefile.exists(),
            "json_contains": {
                "status": "unsupported",
                "reason": "vcs_cocotb_vhdl_unsupported",
            },
        },
        {
            "name": "claim_evidence_gate",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "evidence_claim_gate.py"),
                "--claims-json",
                str(evidence_claims),
                "--json",
            ],
            "cwd": str(repo_root),
            "required": evidence_claims.exists(),
            "json_contains": {"status": "passed"},
        },
        *[
            {
                "name": name,
                "cmd": [sys.executable, str(skill_dir / "scripts" / script_name), "--help"],
                "cwd": str(repo_root),
                "required": True,
            }
            for name, script_name in help_probe_scripts
        ],
        {
            "name": "ref12_cscd_import_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "import_vcs_project.py"),
                "--makefile",
                str(ref12_makefile),
                "--filelist",
                str(ref12_filelist),
                "--project-root",
                str(ref12_root),
                "--make-var",
                "TOP=cv32e40p_xilinx_tb",
                "--json",
            ],
            "cwd": str(repo_root),
            "required": ref12_makefile.exists() and ref12_filelist.exists(),
            "json_contains": {
                "top": "cv32e40p_xilinx_tb",
                "workdir": "sim/build",
                "dump_name": "waveform.fsdb",
                "filelist_entries": "PE/src/adder_tree.sv",
                "vcs_args": "-debug_region+cell+encrypt",
            },
        },
        {
            "name": "ref13_mp_setup_import_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "import_vcs_project.py"),
                "--makefile",
                str(ref13_mp_setup_root / "sim" / "Makefile"),
                "--project-root",
                str(ref13_mp_setup_root),
                "--json",
            ],
            "cwd": str(repo_root),
            "required": (ref13_mp_setup_root / "sim" / "Makefile").exists(),
            "json_contains": {
                "top": "alu_tb",
                "sources": "hdl/alu.sv",
                "simv_args": "simulation.log",
                "dump_name": "dump.fsdb",
            },
        },
        {
            "name": "ref13_mp_pipeline_import_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "import_vcs_project.py"),
                "--makefile",
                str(ref13_mp_pipeline_root / "sim" / "Makefile"),
                "--project-root",
                str(ref13_mp_pipeline_root),
                "--json",
            ],
            "cwd": str(repo_root),
            "required": (ref13_mp_pipeline_root / "sim" / "Makefile").exists(),
            "json_contains": {
                "top": "top_tb",
                "sources": "hdl/cpu.sv",
                "simv_args": "top_tb_sim.log",
                "coverage.vdb_dir": "sim/sim/top_tb.vdb",
                "post_compile_checks": "bash check_compile_error.sh",
                "post_sim_checks": "bash check_sim_error.sh",
            },
        },
        {
            "name": "ref13_mp_cache_import_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "import_vcs_project.py"),
                "--makefile",
                str(ref13_mp_cache_root / "sim" / "Makefile"),
                "--project-root",
                str(ref13_mp_cache_root),
                "--json",
            ],
            "cwd": str(repo_root),
            "required": (ref13_mp_cache_root / "sim" / "Makefile").exists(),
            "json_contains": {
                "top": "cache_dut_tb",
                "sources": "hdl/cache.sv",
                "simv_args": "sim.log",
                "coverage.vdb_dir": "sim/sim/cache_dut_tb.vdb",
                "post_compile_checks": "bash check_compile_error.sh",
                "post_sim_checks": "bash check_sim_error.sh",
            },
        },
        {
            "name": "ref13_mp_verif_cov_import_dry_run",
            "cmd": [
                sys.executable,
                str(skill_dir / "scripts" / "import_vcs_project.py"),
                "--makefile",
                str(ref13_mp_verif_cov_root / "sim" / "Makefile"),
                "--project-root",
                str(ref13_mp_verif_cov_root),
                "--json",
            ],
            "cwd": str(repo_root),
            "required": (ref13_mp_verif_cov_root / "sim" / "Makefile").exists(),
            "json_contains": {
                "top": "tb",
                "sources": "hvl/tb.sv",
                "simv_args": "tb_sim.log",
                "coverage.vdb_dir": "sim/sim/tb.vdb",
            },
        },
        {
            "name": "script_matrix_audit",
            "cmd": [sys.executable, str(skill_dir / "scripts" / "run_quality_gate.py"), "--script-matrix-only", "--json"],
            "cwd": str(repo_root),
            "required": True,
        },
        {
            "name": "local_skill_audit",
            "cmd": [sys.executable, str(skill_dir / "scripts" / "run_quality_gate.py"), "--audit-only", "--json"],
            "cwd": str(repo_root),
            "required": True,
        },
    ]
    return {"repo_root": str(repo_root), "skill_dir": str(skill_dir), "steps": steps}


def run_step(step: dict, *, timeout: int) -> dict:
    if not step.get("required", True):
        return {**step, "returncode": 0, "status": "skipped", "stdout": "", "stderr": "optional tool missing"}
    completed = subprocess.run(
        step["cmd"],
        cwd=step["cwd"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    json_errors: list[str] = []
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        parsed = {}
    if isinstance(parsed, dict) and parsed.get("errors"):
        json_errors = [str(item) for item in parsed.get("errors", [])]
    if isinstance(parsed, dict):
        json_errors.extend(_json_expectation_errors(parsed, step.get("json_contains", {})))
    status = "passed" if completed.returncode == 0 and not json_errors else "failed"
    return {
        **step,
        "returncode": completed.returncode,
        "status": status,
        "json_errors": json_errors,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _json_path_value(data: dict, path: str):
    value = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _json_expectation_errors(parsed: dict, expectations: dict) -> list[str]:
    errors = []
    for path, expected in expectations.items():
        actual = _json_path_value(parsed, path)
        if isinstance(actual, list):
            if expected not in actual:
                errors.append(f"{path} missing expected value {expected!r}")
        elif actual != expected:
            errors.append(f"{path} expected {expected!r}, got {actual!r}")
    return errors


def run_local_gate(repo_root: Path, *, skill_dir: Path | None = None, timeout: int = 300) -> dict:
    plan = build_local_gate(repo_root, skill_dir=skill_dir)
    results = []
    for step in plan["steps"]:
        result = run_step(step, timeout=timeout)
        results.append(result)
        if result["status"] == "failed" and step.get("required", True):
            break
    status = "passed" if all(item["status"] in {"passed", "skipped"} for item in results) and len(results) == len(plan["steps"]) else "failed"
    output = {**plan, "results": results, "status": status}
    output.update(classify_confidence(output))
    return output


def _json_from_step(step: dict) -> dict:
    text = step.get("stdout", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def load_remote_evidence(path: Path, *, max_age_hours: int | None) -> dict:
    import importlib.util

    gate_path = Path(__file__).resolve().parent / "remote_eda_gate.py"
    spec = importlib.util.spec_from_file_location("remote_eda_gate", gate_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    evidence = json.loads(path.read_text(encoding="utf-8"))
    return module.validate_evidence(evidence, max_age_hours=max_age_hours)


def _remote_evidence_passed(remote: dict) -> bool:
    if remote.get("status") != "passed" or remote.get("fresh") is False:
        return False
    evidence = remote.get("evidence", remote)
    fsdb = evidence.get("artifacts", {}).get("waves.fsdb", {})
    if fsdb.get("bytes", 0) <= 0:
        return False
    matrix = evidence.get("matrix", {})
    for name in ("minimal_smoke", "mixed_vhdl_sv", "coverage_urg", "fsdb_conversion"):
        if matrix.get(name, {}).get("status") != "passed":
            return False
    steps = evidence.get("steps", {})
    if isinstance(steps, list):
        steps = {step.get("name"): step for step in steps}
    for name in ("compile", "elaborate", "simulate", "verdi-fsdbreport-check"):
        if steps.get(name, {}).get("returncode") != 0:
            return False
    return True


def classify_confidence(output: dict) -> dict:
    local_confidence = "passed" if output.get("status") == "passed" else "failed"
    reasons: list[str] = []
    remote = output.get("remote_evidence") or {}
    if _remote_evidence_passed(remote):
        return {
            "local_confidence": local_confidence,
            "eda_execution_confidence": "passed",
            "eda_execution_reasons": [],
        }
    for step in output.get("results", []):
        parsed = _json_from_step(step)
        if step.get("name") == "env_probe":
            reasons.extend(parsed.get("overall", {}).get("blockers", []))
        reasons.extend(parsed.get("missing_tools", []))
    if remote:
        reasons.extend(remote.get("errors", []))
        if remote.get("fresh") is False:
            reasons.append("remote evidence is stale")
    deduped = list(dict.fromkeys(str(item) for item in reasons if item))
    return {
        "local_confidence": local_confidence,
        "eda_execution_confidence": "blocked" if deduped else "not_executed",
        "eda_execution_reasons": deduped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the vcs-verdi-developer local quality gate.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--skill-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--script-matrix-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--remote-evidence", type=Path, help="Fresh remote EDA host/equivalent evidence JSON.")
    parser.add_argument("--remote-max-age-hours", type=int, default=24)
    args = parser.parse_args()

    if args.audit_only:
        output = audit_skill(args.skill_dir)
        status = output["status"]
    elif args.script_matrix_only:
        output = script_matrix_audit(args.skill_dir)
        status = output["status"]
    elif args.dry_run:
        output = build_local_gate(args.repo_root, skill_dir=args.skill_dir)
        output["status"] = "passed"
        status = "passed"
    else:
        output = run_local_gate(args.repo_root, skill_dir=args.skill_dir, timeout=args.timeout)
        status = output["status"]

    if args.remote_evidence:
        output["remote_evidence"] = load_remote_evidence(args.remote_evidence, max_age_hours=args.remote_max_age_hours)
        output.update(classify_confidence(output))
        if output.get("eda_execution_confidence") != "passed":
            status = "failed"
            output["status"] = "failed"

    if args.json:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        print(status)
        if output.get("errors"):
            for error in output["errors"]:
                print(f"- {error}")
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
