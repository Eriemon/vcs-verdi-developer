# VCS/Verdi Reviewer Gate

Use this checklist before reporting that a VCS/Verdi workflow is ready. It is the Reviewer layer for this skill.

## Scope Gate

- Confirm the request is within the skill promise: environment probe, RC generation, non-GUI VCS smoke flow, VHDL compile planning, FSDB artifact check, fsdbreport check, guarded Verdi load check, Tk/IPython command construction, or remote EDA validation.
- Do not claim complete coverage of every Synopsys VCS or Verdi option. Report unsupported official-tool features as outside the current skill scope.
- Confirm no runtime step depends on the temporary reference directory or local absolute development paths.

## Environment Gate

- `scripts/check_env.py --json` reports `vlogan`, `vhdlan`, `vcs`, direct `VCS_BIN`, `verdi`, `fsdbreport`, versions, `VCS_HOME`, `VERDI_HOME`, Verdi NPI Python, license hints, `DISPLAY`, `SHELL`, `/bin/sh -h` behavior, FSDB readers, and novas/PLI hints.
- Missing EDA tools, missing licenses, missing `DISPLAY`, or incompatible shell wrappers are recorded as blockers or warnings, not as success.
- GUI Verdi is accepted only when `ready_for_gui_verdi` is true or an explicit VNC/X11/Xvfb strategy has been validated.

## RC Gate

- RC generation has tests for aliases, virtual buses, markers, analog rows, unknown colors, missing base/scenario files, and invalid rows.
- Generated RC avoids unknown color tokens and emits deterministic output.
- Scenario files are documented in `references/verdi-rc-format.md`.

## Smoke Gate

- Run `scripts/smoke_vcs_verdi.py --dry-run --json` first.
- Command plan includes compile, elaborate, simulate, and Verdi load steps with explicit cwd, logs, wrapper diagnostics, and artifact paths.
- For real projects, prefer a JSON manifest and verify source lists, multiple filelists, VHDL sources, include dirs, defines, tool overrides, environment injection, stage args, expected artifacts, coverage, plusargs, seeds, and dump command files appear in the dry-run plan.
- When starting from a Makefile/filelist project, use `scripts/import_vcs_project.py` and review preserved original VCS args before execution. For `VCS_FLAGS`/`SRCS` style projects, confirm the imported manifest is not empty and includes sources, include dirs, top, dump artifact, simulation log args, and diagnostics.
- When starting from an Edalize/CAPI2-style project, use `scripts/import_vcs_project.py --edalize-json` and confirm `file_type`, `toplevel`, plusargs, vlogdefines, vlogparams, `vcs_options`, and `run_options` are represented in the manifest.
- When starting from a cocotb Makefile, use `scripts/cocotb_vcs_flow.py` and confirm the plan is non-GUI, VPI-based, and includes `pli.tab`, `+vpi`, `-P`, `+define+COCOTB_SIM=1`, `-load`, `MODULE`, `TOPLEVEL`, and `TOPLEVEL_LANG`; VHDL cocotb inputs must remain `vcs_cocotb_vhdl_unsupported`.
- For course-style Makefiles, confirm line comments are not included in `original_vcs_args`, caller-required variables are passed with `--make-var`, `-top` is not populated from another flag such as `-R`, `$(shell find ...)` sources resolve to real project paths, `./<simv> -l` simulation args are captured, and `urg -dir` produces explicit VDB/report paths without counting URG as executed.
- For PicoRV32/RISCV-DV style inputs, confirm `vcs.f` filelist flags are not treated as sources, coverage metrics are captured, `workdir` is the DV directory, and `simv`/coverage outputs are explicit.
- For ModelSim or Icarus projects, confirm converted manifests include top module, `SIMULATION` define, include dirs, expanded sources, missing source glob diagnostics, and pre-simulation hex artifact diagnostics.
- For KVIPS APB/AHB/AXI4 projects, confirm `scripts/kvips_vcs_flow.py` marks UVM as guarded/optional, includes `-ntb_opts uvm-1.2`, filelist expansion, UVM plusargs, regression test lists, optional FSDB PLI, `fsdbreport`, and non-GUI `verdi -ssf -nologo -exit` checks.
- For FP-Gen projects, confirm `scripts/fpgen_vcs_flow.py` separates Genesis2 generation, generated filelists, VCS/VPD simulation, SAIF runtime, and guarded DC/ICC gate-level commands, with missing generator/library/license inputs recorded as blockers.
- For AutoVeriFix projects, confirm `scripts/autoverifix_vcs_flow.py` separates VCS compile, `simv`, scoreboard log parsing, URG coverage, optional `fsdbreport`, and guarded `verdi -ssf` checks; LLM/API repair loops must remain guarded external dependencies.
- For AISS projects, confirm `scripts/aiss_vcs_flow.py` marks RTL simulation targets as local plans and missing gate-level netlist testbench or `dc_shell` synthesis inputs as guarded blockers, not pass evidence.
- If `UVM_HOME`, `UVM_FLAGS`, `uvm.sv`, or `uvm_dpi.cc` are detected, confirm UVM is reported as `optional_external_dependencies` and is not counted as core non-GUI support.
- For DPI-C flows, confirm dry-run includes `-sv_lib` in the VCS step, `LD_LIBRARY_PATH` when required, and `./simv` as the non-GUI execution step.
- Execute only after missing tools are resolved.
- Full pass requires `waves.fsdb` to exist and have nonzero size after simulation.
- Non-GUI acceptance requires `fsdbreport` or an equivalent Verdi FSDB utility to return 0.

## Script Matrix Gate

- `references/capability-matrix.md` states supported, guarded, remote-evidence, and unsupported capabilities.
- `scripts/analyze_logs.py` classifies compile, license, platform, PLI, and FSDB failures.
- `scripts/fsdb_tools.py` checks FSDB artifacts, plans NPI-first FSDB info/list/read with CLI fallback, and plans or executes `fsdbreport`, `fsdb2vcd`, `vcd2fsdb`, and `vpd2fsdb`.
- `scripts/coverage_flow.py` plans `-cm` metrics and URG report commands, and records execution evidence when `--execute` is used.
- `scripts/patch_ucapi_overlay.py` scans or applies the guarded UCAPI workaround only inside validation overlays and never modifies `/tools/synopsys`.
- `scripts/urg_runtime_probe.py` records wrapper, `vcs -location`, loader, library hash, and optional strace diagnostics when URG fails.
- `scripts/urg_coverage_matrix.py` records wrapper/direct `urg1` variants for line, cond, tgl, and line+cond+tgl; only a nonempty default `line+cond+tgl__urg__auto64` report can prove `coverage_urg`.
- `scripts/import_vcs_project.py` imports simple VCS Makefile/filelist, ModelSim Tcl, and Icarus Makefile command patterns into a manifest candidate.
- `scripts/import_vcs_project.py` imports Edalize/CAPI2-style VCS descriptions into the same manifest shape without creating a separate execution flow.
- `scripts/cocotb_vcs_flow.py` plans cocotb VCS/VPI non-GUI Verilog/SystemVerilog runs and explicitly rejects GUI flags plus cocotb VCS VHDL/VHPI inputs.
- `scripts/riscv_dv_flow.py` plans PicoRV32/RISCV-DV generator, VCS, simv, URG, and trace-compare flows without requiring UVM.
- `scripts/kvips_vcs_flow.py` plans guarded KVIPS UVM VCS, FSDB, fsdbreport, and regression flows without promoting UVM into core support.
- `scripts/fpgen_vcs_flow.py` plans FP-Gen Genesis2, VCS/VPD, SAIF, and guarded gate-level flows.
- `scripts/autoverifix_vcs_flow.py` plans AutoVeriFix-style compile, simulation, log-scoreboard, URG, fsdbreport, and guarded Verdi checks.
- `scripts/aiss_vcs_flow.py` plans AISS-style VCS/VPD RTL targets and guards incomplete netlist or DC synthesis targets.
- The local gate includes non-UVM `ref/4-sigarch-rtl-project-template` importer dry-runs, a UVM-optional diagnostic importer dry-run, a `ref/5-pysv` style DPI `-sv_lib` dry-run, and `ref/6`/`ref/7`/`ref/8`/`ref/9` import or dry-run planning checks.
- The local gate includes `ref/12-CSCD` and `ref/13-Computer-Organization` mp_setup, mp_pipeline, mp_cache, and mp_verif importer dry-runs for course-style VCS/Verdi Makefile patterns.
- The local gate includes `ref/14-cocotb` adder VCS/VPI dry-run and mixed-language VHDL guard checks.
- The local gate includes `ref/16-AutoVeriFix` coverage-loop dry-run and `ref/18-AISS-Phase-III` AHB/VPD plus guarded netlist dry-runs.
- `scripts/collect_evidence.py` builds a single JSON evidence bundle from smoke, coverage, conversion, env, report, and artifact outputs.
- `scripts/evidence_claim_gate.py` fails unsupported or unevidenced factual claims and allows unverified recommendations only as warnings.
- `scripts/run_regression.py` batches manifest flows and reports JSON/JUnit-style results with per-case missing tool and artifact summaries.
- `scripts/remote_eda_gate.py` validates detached remote EDA evidence, timestamp freshness, environment hints, step commands, matrix checks, and artifacts without syncing the local skill tree.
- `scripts/remote_eda_gate.py` must package shell scripts with LF line endings so Linux remote runners do not fail with `/usr/bin/env: bash\r`.

## Quality Gate

- Run `scripts/run_quality_gate.py --json` before release or readiness claims.
- Confirm `local_confidence`, `delivery_execution_confidence`, and `truth_execution_confidence` are reported separately. A Windows-only local run may pass `local_confidence` while leaving delivery or truth confidence blocked by missing proprietary tools.
- The local skill audit must target `vcs-verdi-developer`, not another installed skill's resource inventory.
- The script matrix audit must pass; new supported capabilities require tests or dry-run coverage.
- Factual readiness statements must either cite a supported local or remote evidence record or remain blocked/unsupported.

## remote EDA host Gate

- Follow `references/remote-eda-gate.md`.
- Upload only a minimal runtime fixture or validation bundle into a named remote test directory.
- Fresh factual EDA confidence requires the remote matrix to pass: minimal smoke, mixed VHDL/SV, coverage/URG, and FSDB conversion.
- If full flow fails, collect wrapper shebangs, shell choice, return codes, logs, artifact status, and `DISPLAY`/VNC state before proposing fixes.
- If `coverage_urg` fails, review `coverage_summary`, `urg_runtime_probe.json`, `urg_coverage_matrix.json`, `urg_troubleshoot.json`, and `ucapi_patch_scan.json` before enabling `VCS_VERDI_ALLOW_UCAPI_PATCH=1`; the workaround must remain overlay-only, loader-effective, and explicit opt-in.
- Confirm `urg_failure_signature.primary_repro_attempt`, `shell_layer_failure`, and `internal_crash_after_shell_fix` match the raw `urg_troubleshoot.json` attempts. If vendor wrapper shell failure is followed by identical overlay/direct internal crashes and `ucapi_patch_scan.status=no_match`, record `vendor_or_host_blocked` and stop guessing local fixes.
- When `urg_troubleshoot.json` includes a system `gdb` probe for `overlay_direct_urg1_vcs_lib`, confirm whether `root_cause_signature=ucapi_license_checkout_segv` is present. If it is, treat `libsnpsmalloc.so -> libucapi.so -> scl_lc_checkout -> covdb_get_license` as the authoritative root cause, and treat any missing `libncursesw.so.5` message from Synopsys `cbug-gdb-64` as secondary diagnostic noise rather than the primary fault.
- Confirm the automatic `VCS_USE_MALLOC=1` fallback is conditional, not unconditional. The retry must trigger only after the known URG internal stack-trace failure shape and must preserve `initial_attempt`, `fallback_applied`, and `fallback_env` evidence when it succeeds.
- Confirm `run_remote_eda_smoke.sh` does not let diagnostic sidecars such as `urg_runtime_probe.py` or `urg_troubleshoot.py` drag `job_exit_code` to nonzero after smoke, mixed-language smoke, conversion, coverage, and matrix have factually passed.
- Confirm a fresh clean-room zip bundle run on the selected Linux EDA host can reproduce the pass without hand-editing remote scripts after extraction.
- If `ucapi_patch_scan.json` reports `no_match`, keep the failure as a vendor/toolchain blocker and do not enable the guarded patch path for that tool image.
- If `coverage_urg` passes, confirm `urgReport/` is nonempty and `urg_coverage_matrix.default_variant` is `line+cond+tgl__urg__auto64` with `status=passed`.
