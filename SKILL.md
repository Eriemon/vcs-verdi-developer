---
name: vcs-verdi-developer
description: Use when Codex works on scripted non-GUI Synopsys VCS and Verdi workflows, including vlogan/vhdlan/vcs/simv compilation and simulation, VCS_BIN direct-binary environment checks, Makefile, Edalize/CAPI2, or cocotb VCS/VPI import planning, DPI -sv_lib planning, FSDB or VCD dump generation, NPI or fsdbreport FSDB readback, Verdi waveform debug planning, claim/evidence gating, signal restore RC generation, guarded verdi -ssf/-sswr checks, optional Tk send or IPython-driven Verdi control, and remote EDA smoke validation on a selected Linux EDA host.
---

# VCS Verdi Developer

## Core Rule

Discover the EDA environment first, prefer non-GUI scripted validation, keep GUI actions optional, generate repeatable artifacts with scripts, and validate the exact promised flow before claiming a VCS or Verdi workflow works. This skill supports a strict scripted subset and does not claim complete coverage of every official Synopsys VCS/Verdi option.

## Workflow

1. Identify the task type: non-GUI VCS simulation, VHDL compile, DPI shared-library simulation, FSDB/VCD dump generation, Verdi RC layout, guarded Verdi interactive control, or remote EDA validation.
2. Run `scripts/check_env.py --json` locally or remotely before invoking proprietary tools. Treat missing `VCS_HOME`, `VCS_BIN`, `VERDI_HOME`, `VERDI_PYTHON`, `vlogan`, `vhdlan`, `vcs`, `verdi`, `fsdbreport`, license hints, DISPLAY/VNC, NPI Python, novas/PLI, or shell compatibility as explicit findings, not as success. Prefer a validated `VCS_BIN` direct binary over a missing or fragile wrapper.
3. For waveform layout work, use `scripts/generate_rc.py` with a base/scenario config. Read `references/verdi-rc-format.md` before changing config syntax.
4. For VCS/Verdi smoke tests, use `scripts/smoke_vcs_verdi.py --dry-run` first. Use `--manifest` for real projects with mixed VHDL/SystemVerilog, multiple filelists, stage-specific extra args, tool overrides, injected environment, coverage, plusargs, seeds, expected artifacts, and dump command files. Execute only after reviewing the generated command plan and manifest schema errors.
5. For broader scripted work, use `scripts/import_vcs_project.py`, `scripts/cocotb_vcs_flow.py`, `scripts/riscv_dv_flow.py`, `scripts/kvips_vcs_flow.py`, `scripts/fpgen_vcs_flow.py`, `scripts/run_regression.py`, `scripts/analyze_logs.py`, `scripts/coverage_flow.py`, `scripts/urg_coverage_matrix.py`, `scripts/fsdb_tools.py`, `scripts/collect_evidence.py`, and `scripts/evidence_claim_gate.py` instead of hand-building import, cocotb VCS/VPI, RISCV-DV, guarded KVIPS/UVM, FP-Gen/Genesis2, regression, diagnosis, coverage, URG matrix, FSDB utility, evidence bundle, or factual-claim gate commands. Use `import_vcs_project.py --edalize-json` for Edalize/CAPI2-style VCS inputs. Treat detected UVM dependencies as optional guarded dependencies unless the user explicitly asks for UVM support.
6. For a selected Linux EDA host, follow `references/remote-eda-validation.md` plus the strict gate in `references/remote-eda-gate.md`, and use the `erie-remote-ssh` skill. Upload only explicit fixture or validation bundles, never the whole local skill-development tree.
7. For Tk send or IPython Verdi control, read `references/verdi-interaction.md`; confirm an existing Verdi session and Tk name before sending commands.
8. Before reporting readiness, apply `references/review-checklist.md` and run `scripts/run_quality_gate.py --json` locally. Report `local_confidence` separately from `eda_execution_confidence`; only fresh remote or local EDA execution evidence can make EDA execution confidence pass.

## Resource Map

- `references/vcs-verdi-flow.md`: VCS compile/elaborate/simulate flow, FSDB dump expectations, and non-GUI Verdi checks.
- `references/non-gui-flow.md`: manifest-driven scripted VCS/Verdi flow and non-GUI pass criteria.
- `references/capability-matrix.md`: supported, guarded, remote-evidence, and unsupported capability boundaries.
- `references/verdi-rc-format.md`: scenario/base file format and generated RC behavior.
- `references/verdi-interaction.md`: Tk `send`, generated Python wrappers, and IPython caveats.
- `references/remote-eda-validation.md`: remote validation order and safety boundaries.
- `references/remote-eda-gate.md`: strict remote EDA full-flow acceptance steps.
- `references/review-checklist.md`: reviewer gate for scope, environment, RC, smoke, and remote evidence.
- `references/third-party-extraction.md`: how to mine public/local examples without adding runtime dependencies.
- `scripts/check_env.py`: report tool and environment readiness as JSON or text, including `VCS_BIN`, license hints, and Verdi NPI Python readiness.
- `scripts/generate_rc.py`: generate Verdi signal restore RC from `scn_base.lst` plus `scn_<scenario>.lst`.
- `scripts/smoke_vcs_verdi.py`: build or execute a minimal VCS/Verdi smoke command plan.
- `scripts/analyze_logs.py`: classify compile, license, platform, PLI, and FSDB failure evidence from logs.
- `scripts/fsdb_tools.py`: build and optionally execute FSDB utility commands, plan NPI-first FSDB info/list/read operations with CLI fallback, parse fsdbreport output, and reject missing or zero-byte waveform artifacts.
- `scripts/coverage_flow.py`: plan or execute VCS `-cm` coverage and URG report checks.
- `scripts/patch_ucapi_overlay.py`: scan or explicitly apply the guarded UCAPI overlay workaround for URG internal failures.
- `scripts/urg_runtime_probe.py`: collect URG wrapper, `vcs -location`, loader, library hash, and optional strace diagnostics for failed coverage gates.
- `scripts/urg_coverage_matrix.py`: run URG wrapper/direct `urg1` diagnostic variants for line, cond, tgl, and line+cond+tgl coverage reports.
- `scripts/import_vcs_project.py`: import simple VCS/Verdi Makefile, VCS filelist, ModelSim Tcl, Icarus-style Makefile, and Edalize/CAPI2-style JSON patterns into a manifest without depending on `ref/` at runtime.
- `scripts/cocotb_vcs_flow.py`: plan a non-GUI cocotb VCS/VPI flow from cocotb Makefile variables, including `pli.tab`, `+vpi`, `-P`, `-load`, module/top environment, and explicit guards for VCS cocotb VHDL/VHPI and GUI requests.
- `scripts/riscv_dv_flow.py`: plan or execute a PicoRV32/RISCV-DV style non-GUI generator, VCS, simv, URG, and trace-compare flow.
- `scripts/kvips_vcs_flow.py`: plan guarded KVIPS APB/AHB/AXI4 UVM VCS runs, regressions, FSDB PLI, fsdbreport, and non-GUI Verdi load checks without counting UVM as core support.
- `scripts/fpgen_vcs_flow.py`: plan FP-Gen Genesis2 generation, VCS compile/simv VPD runs, SAIF runtime, and guarded gate-level DC/ICC simulation commands.
- `scripts/collect_evidence.py`: collect smoke, coverage, FSDB conversion, environment, report, artifact, and matrix evidence into one JSON object.
- `scripts/evidence_claim_gate.py`: hard-fail unsupported or unevidenced factual claims before readiness or release statements.
- `scripts/run_regression.py`: run multiple manifest-driven smoke flows and emit JSON or JUnit-style summaries.
- `scripts/remote_eda_gate.py`: plan the minimal remote EDA bundle and validate detached evidence.
- `scripts/run_quality_gate.py`: project-local quality gate and skill audit for this skill.
- `scripts/make_shell_overlay.sh`: create a temporary remote-only wrapper overlay when `/bin/sh` rejects Synopsys scripts.
- `scripts/run_remote_eda_smoke.sh`: remote EDA login-shell runner for the minimal full-flow gate.
- `evals/evals.json`: behavior eval scenarios comparing with-skill and without-skill outcomes.
- `assets/minimal_vcs/`: minimal SystemVerilog, pure-SV coverage, mixed VHDL/SV fixtures, and dump command.
- `assets/evidence/non_gui_claims.json`: minimal local claim/evidence bundle used by the quality gate.
- `assets/waves/`: base/scenario templates for RC generation, including minimal PicoRV32 and MIPSfpga-style debug layouts.

## Safety Boundaries

- Do not assume GUI Verdi is available over SSH; prefer dry-run or non-GUI checks unless VNC/X11 is explicitly configured.
- Do not run long simulations synchronously over remote SSH; use reviewed detached execution when runtime may exceed the local timeout.
- Do not copy paths, hostnames, usernames, or license details into public docs.
- Do not depend on the temporary reference directory at runtime. Treat it as development evidence only.
- Do not patch a shared vendor install path directly; UCAPI workarounds must be overlay-only and explicit opt-in.
