# VCS and Verdi Flow

Use this reference when the user asks for compile, simulate, dump, or non-GUI Verdi validation commands.

## Minimal Order

1. Check tools with `scripts/check_env.py --json`.
2. Compile SystemVerilog with `vlogan -full64 -sverilog -kdb <sources>`.
3. Elaborate with `vcs -full64 -kdb -sverilog -debug_access+all work.<top> -o <workdir>/simv`.
4. Simulate with an explicit FSDB dump setup from the testbench or simulator command file.
5. Load Verdi with `verdi -ssf <dump.fsdb>` and add `-sswr <layout.rc>` when a generated signal restore file is available.

The helper script uses this command shape:

```sh
python3 scripts/smoke_vcs_verdi.py --dry-run --json \
  --source assets/minimal_vcs/top.sv \
  --workdir build/vcs-verdi-smoke \
  --top top \
  --dump-name waves.fsdb
```

For multi-file projects, pass repeated `--source` entries, `--source-list rtl.f`, or both. Use `--cmd-file dump.cmd` when simulator commands are required for dump setup. The script auto-detects Verdi novas PLI from `VERDI_HOME` or `NOVAS_HOME`; use `--pli-dir <dir>` when the installation needs an explicit `novas.tab` and `pli.a` path, and `--no-auto-pli` for Verdi 2024.09+ flows where `VERDI_HOME` plus `-debug_access` is the supported FSDB path. Use `--execute --clean` only after the dry-run command plan has been reviewed.

The default Verdi check launches `verdi -ssf`. For deterministic CI-style checking, use `--verdi-check fsdbreport --report-signal /top/clk`; this validates that the generated FSDB can be read by Verdi's FSDB utility and returns a normal process status.

## Degraded Results

If proprietary tools or license variables are missing, report the exact missing item and keep the result as an environment finding. Do not say the flow passed.

## Command Planning

Use `scripts/smoke_vcs_verdi.py --dry-run` behavior by default. The dry-run report includes each step's cwd, log path, wrapper diagnostics, execution command, missing tools, and artifact targets. Execute only after reviewing the generated commands, especially on remote servers.

## Pass Criteria

A real smoke pass requires all four commands to return 0 and the FSDB dump artifact to exist with nonzero size. A compile-only success, an empty FSDB, or a failed Verdi load is not a passing flow.
