# Non-GUI VCS/Verdi Flow

Use this reference when the requested acceptance path must work over SSH, CI, or any environment where Verdi GUI automation is not reliable.

## Supported Scope

This skill supports a scripted non-GUI flow:

1. Probe tools and environment with `scripts/check_env.py --json`.
2. Plan commands from CLI arguments or a JSON manifest with `scripts/smoke_vcs_verdi.py --dry-run --json`.
3. Compile VHDL sources with `vhdlan` when present.
4. Compile Verilog/SystemVerilog sources with `vlogan`.
5. Elaborate with `vcs`, including optional SystemVerilog DPI shared libraries through `-sv_lib`.
6. Run `simv` with dump command files, plusargs, seeds, and explicit timeout handling.
7. Require a nonzero FSDB artifact after simulation.
8. Read the FSDB with `fsdbreport` for deterministic Verdi-family validation.
9. Classify failure logs with `scripts/analyze_logs.py`.
10. Plan or inspect FSDB utilities with `scripts/fsdb_tools.py`, preferring Verdi NPI Python when present and falling back to CLI utilities.
11. Plan or execute VCS coverage and URG report generation with `scripts/coverage_flow.py`.
12. Run multiple manifests through `scripts/run_regression.py`.
13. Import simple Makefile/filelist or Edalize/CAPI2-style projects with `scripts/import_vcs_project.py` when a real project starts from a VCS, ModelSim, Icarus, or Edalize description instead of a manifest.
14. Plan PicoRV32/RISCV-DV style generator, VCS, simv, URG, and trace-compare flows with `scripts/riscv_dv_flow.py`.
15. Plan guarded KVIPS APB/AHB/AXI4 UVM VCS, FSDB, fsdbreport, and regression flows with `scripts/kvips_vcs_flow.py`; do not count UVM as core low-dependency support.
16. Plan FP-Gen Genesis2, VCS/VPD, SAIF, and guarded gate-level simulation flows with `scripts/fpgen_vcs_flow.py`.
17. Collect one JSON evidence bundle with `scripts/collect_evidence.py`.
18. Validate ThreadFPGA detached evidence with `scripts/remote_threadfpga_gate.py`.

The skill does not claim complete coverage of every official Synopsys VCS or Verdi option. Unsupported or unverified options must be reported as outside the current scripted scope.

## JSON Manifest

Use a manifest when project inputs are more complex than a single source file:

```json
{
  "sources": [
    {"path": "rtl/pkg.sv", "language": "sv"},
    {"path": "rtl/top.v", "language": "v"},
    {"path": "rtl/core.vhd", "language": "vhdl"}
  ],
  "source_lists": ["rtl.f", "tb.f"],
  "include_dirs": ["include"],
  "defines": {"SIM": "1"},
  "libraries": ["work"],
  "tools": {"vlogan": "vlogan", "vcs": "vcs", "simv": "./simv", "fsdbreport": "fsdbreport"},
  "env": {"SNPSLMD_LICENSE_FILE": "27000@license-host"},
  "top": "tb_top",
  "timescale": "1ns/1ps",
  "coverage": ["line", "cond"],
  "sv_libs": ["build/libpysv.so"],
  "vlogan_args": ["+v2k"],
  "vcs_args": ["+mindelays", "-negdelay", "+neg_tchk"],
  "simv_args": ["+firmware=inst.data"],
  "fsdbreport_args": ["-bt", "0:100"],
  "plusargs": ["+firmware=inst.data"],
  "seed": 7,
  "cmd_file": "dump.ucli",
  "dump_name": "waves.fsdb",
  "expected_artifacts": {"dump": {"path": "waves.fsdb", "min_bytes": 1}},
  "step_timeout": 120,
  "verdi_check": "fsdbreport",
  "report_signal": "/tb_top/clk"
}
```

Dry-run first:

```sh
python3 scripts/smoke_vcs_verdi.py --manifest manifest.json --dry-run --json
```

Execute only after reviewing the planned commands and environment blockers:

```sh
python3 scripts/smoke_vcs_verdi.py --manifest manifest.json --execute --clean --json
```

## Pass Criteria

A non-GUI pass requires all relevant compile steps, `vcs`, `simv`, and `fsdbreport` to return 0. The FSDB file must exist and have bytes greater than 0. Dry-run output, missing tools, GUI launch success, or compile-only success is not a passing non-GUI flow. Local quality gates report `local_confidence`; only fresh remote or equivalent EDA evidence can report `eda_execution_confidence=passed`.

## Regression and Diagnosis

Use `scripts/run_regression.py` when more than one manifest must be tested. Keep `--dry-run` for command review and `--execute` inside the smoke script for real EDA execution. Use `scripts/analyze_logs.py --json` on compile, elaborate, simulate, and fsdbreport logs before proposing fixes; license, platform, PLI, and FSDB artifact issues should be reported separately from generic syntax errors.

Use `scripts/coverage_flow.py --json` for VCS `-cm` and URG command planning. Use `--execute --timeout <seconds>` only in an environment with `urg` available. Coverage planning is supported locally; actual URG report generation requires an environment with Synopsys tools.

Use `scripts/fsdb_tools.py` for `fsdbreport`, Verdi NPI Python read plans, `fsdb2vcd`, `vcd2fsdb`, `vpd2fsdb`, FSDB size checks, command execution evidence, and basic report parsing. A zero-byte FSDB is always a failure. `read-plan --action read-signal` prefers `$VERDI_PYTHON` or `$VERDI_HOME/platform/linux64/Python/bin/python3.6` when available; otherwise it emits deterministic CLI fallback commands.

Use `scripts/fsdb_tools.py vcd-debug-plan dump.vcd waves.fsdb --signal /top/clk --json` for VCD-first examples. The plan must include `vcd2fsdb`, `fsdbreport`, and guarded `verdi -ssf ... -nologo -exit`; real tool execution still requires remote or equivalent EDA evidence.

Use `scripts/import_vcs_project.py --makefile <Makefile> --filelist <filelist.f> --project-root <root> --json` to distill common VCS Makefile patterns such as `+v2k`, `+incdir+...`, `-timescale=...`, `+mindelays`, `-negdelay`, `+neg_tchk`, `VCS_FLAGS`, `SRCS`, `-top`, `+vcs+dumpvars+dump.vcd`, `./simv -l simulation.log`, and Verdi `-ssf` into a manifest candidate. Use repeated `--make-var NAME=VALUE` when a Makefile requires caller-provided variables such as `TOP=cv32e40p_xilinx_tb`.

The importer also handles PicoRV32/RISCV-DV style `cd dv && vcs -f cfg/vcs.f` flows, course-style `cd build && vcs ... -R` flows, `$(shell find $(PWD)/../...)` dynamic source lists, line-commented `VCS_OPTIONS += ... ###` assignments, `./<simv> -l <log>` simulation commands, `urg -dir <vdb>` coverage report commands, compile/sim check scripts, and ModelSim Tcl or Icarus Makefile conversions with missing-source and missing-hex diagnostics. The importer preserves `-R` under `original_vcs_args`; the default smoke flow still uses separate compile, elaborate, simulate, artifact, and fsdbreport stages so compile-only or hidden `-R` behavior cannot be misreported as a complete non-GUI pass. If `UVM_HOME`, `UVM_FLAGS`, `uvm.sv`, or `uvm_dpi.cc` are detected, the importer reports UVM as an optional external dependency and skips those UVM-specific inputs for the core low-dependency flow.

Use `scripts/import_vcs_project.py --edalize-json edam.json --project-root <root> --json` to convert Edalize/CAPI2-style VCS descriptions into the same manifest shape. The importer supports `files[].name`, `file_type`, include paths, `toplevel`, `plusarg`, `vlogdefine`, `vlogparam`, `tool_options.vcs.vcs_options`, and `tool_options.vcs.run_options`. SystemVerilog inputs add `-sverilog`; `verilog2001Source` inputs add `+v2k`; C/C++ files are preserved as VCS-side DPI/support sources rather than treated as standalone GUI work.

Use `scripts/riscv_dv_flow.py --project-root <picorv32-root> --dv-root <picorv32-root>/dv --test <name> --seed <n> --dry-run --json` to plan RISCV-DV generation, test compilation, VCS compile, `simv +hex/+trace`, URG report, and trace compare. Missing RISCV-DV, RISC-V GCC, Spike, VCS, or URG remains an execution blocker and must not be counted as local support failure when the dry-run plan is correct.

Use `scripts/kvips_vcs_flow.py --project-root <kvips-root> --protocol apb|ahb|axi4 --dry-run --json` for KVIPS-style UVM VIP flows. The plan must show `-ntb_opts uvm-1.2`, filelist expansion, UVM test plusargs, seed plusargs, regression list coverage, optional FSDB PLI, `fsdbreport`, and guarded `verdi -ssf ... -nologo -exit`. Because this flow depends on UVM, the plan is `guarded_optional`, not part of the core low-dependency VCS/Verdi confidence target.

Use `scripts/fpgen_vcs_flow.py --project-root <fpgen-root> --product FPGen --dry-run --json` for FP-Gen-style flows. The plan must separate Genesis2 generation, VCS compile, `simv` VPD run, SAIF runtime args, and guarded gate-level DC/ICC commands. Missing generated `genesis_vlog.vf`, Genesis2, DesignWare/GTech libraries, Synopsys licenses, or DC/ICC inputs are execution blockers and must appear as diagnostics or optional external dependencies.

For DPI-C flows like pysv-generated bindings, add generated SystemVerilog binding files to `sources`, put the compiled shared object in `sv_libs`, and set `LD_LIBRARY_PATH` through `env` when the runtime loader needs the shared-library directory. Dry-run must show `-sverilog -sv_lib <library>` in the VCS step and `./simv` in the simulation step.
