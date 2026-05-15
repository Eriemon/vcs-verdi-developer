# VCS and Verdi Flow

Use this reference when the user asks for compile, simulate, dump, or non-GUI Verdi validation commands.

## Minimal Order

1. Check tools with `scripts/check_env.py --json`.
2. Compile VHDL with `vhdlan -full64` when VHDL sources are present.
3. Compile Verilog/SystemVerilog with `vlogan -full64 -sverilog -kdb <sources>`.
4. Elaborate with `vcs -full64 -kdb -sverilog -debug_access+all work.<top> -o <workdir>/simv`.
5. Simulate with an explicit FSDB dump setup from the testbench or simulator command file.
6. Prefer `fsdbreport <dump.fsdb> -s <signal>` for deterministic non-GUI Verdi-family validation. Use `verdi -ssf <dump.fsdb> -sswr <layout.rc>` only when a live Verdi load check is explicitly required and known to exit cleanly.

The helper script uses this command shape:

```sh
python3 scripts/smoke_vcs_verdi.py --dry-run --json \
  --source assets/minimal_vcs/top.sv \
  --workdir build/vcs-verdi-smoke \
  --top top \
  --dump-name waves.fsdb
```

For multi-file projects, pass repeated `--source` entries, `--vhdl-source`, `--source-list rtl.f`, or a JSON `--manifest`. Manifest mode supports multiple `source_lists`, stage-specific extra args (`vhdlan_args`, `vlogan_args`, `vcs_args`, `simv_args`, `fsdbreport_args`, `verdi_args`), `tools` overrides, `env` injection, `expected_artifacts`, and `step_timeout`. Use `--cmd-file dump.cmd` when simulator commands are required for dump setup. The script auto-detects Verdi novas PLI from `VERDI_HOME` or `NOVAS_HOME`; use `--pli-dir <dir>` when the installation needs an explicit `novas.tab` and `pli.a` path, and `--no-auto-pli` for Verdi 2024.09+ flows where `VERDI_HOME` plus `-debug_access` is the supported FSDB path. Use `--execute --clean` only after the dry-run command plan has been reviewed.

The default Verdi check launches `verdi -ssf`. For deterministic CI-style checking, use `--verdi-check fsdbreport --report-signal /top/clk`; this validates that the generated FSDB can be read by Verdi's FSDB utility and returns a normal process status.

## Degraded Results

If proprietary tools or license variables are missing, report the exact missing item and keep the result as an environment finding. Do not say the flow passed.

## Command Planning

Use `scripts/smoke_vcs_verdi.py --dry-run` behavior by default. The dry-run report includes each step's cwd, log path, wrapper diagnostics, execution command, missing tools, and artifact targets. Execute only after reviewing the generated commands, especially on remote servers.

When a project provides a VCS Makefile and filelist instead of a manifest, run:

```sh
python3 scripts/import_vcs_project.py --makefile vcs/Makefile --filelist vcs/filelist.f --project-root . --json
```

Review the generated manifest candidate before execution. The importer records original VCS args such as `-R` separately and keeps the default execution path split into compile, elaborate, simulate, artifact check, and fsdbreport.

## Pass Criteria

A real smoke pass requires all four commands to return 0 and the FSDB dump artifact to exist with nonzero size. A compile-only success, an empty FSDB, or a failed Verdi load is not a passing flow.
