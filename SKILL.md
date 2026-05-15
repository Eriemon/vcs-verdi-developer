---
name: vcs-verdi-developer
description: Use when Codex works on Synopsys VCS and Verdi workflows, including vlogan/vcs/simv compilation and simulation, FSDB/VPD dump generation, Verdi waveform debug, signal restore RC generation, addSignal/addBus/addMarker layouts, verdi -ssf/-sswr launch checks, Tk send or IPython-driven Verdi control, and remote EDA smoke validation on ThreadFPGA.
---

# VCS Verdi Developer

## Core Rule

Discover the EDA environment first, keep GUI actions optional, generate repeatable artifacts with scripts, and validate the exact promised flow before claiming a VCS or Verdi workflow works. This skill does not claim complete coverage of every official Synopsys VCS/Verdi option.

## Workflow

1. Identify the task type: VCS simulation, FSDB dump generation, Verdi RC layout, Verdi interactive control, or ThreadFPGA remote validation.
2. Run `scripts/check_env.py --json` locally or remotely before invoking proprietary tools. Treat missing `VCS_HOME`, `VERDI_HOME`, `vlogan`, `vcs`, `verdi`, license hints, DISPLAY/VNC, novas/PLI, or shell compatibility as explicit findings, not as success.
3. For waveform layout work, use `scripts/generate_rc.py` with a base/scenario config. Read `references/verdi-rc-format.md` before changing config syntax.
4. For VCS/Verdi smoke tests, use `scripts/smoke_vcs_verdi.py --dry-run` first. Execute only after reviewing the generated command plan.
5. For ThreadFPGA, follow `references/threadfpga-validation.md` plus the strict gate in `references/threadfpga-gate.md`, and use the `erie-remote-ssh` skill. Upload only explicit fixture or validation bundles, never the whole local skill-development tree.
6. For Tk send or IPython Verdi control, read `references/verdi-interaction.md`; confirm an existing Verdi session and Tk name before sending commands.
7. Before reporting readiness, apply `references/review-checklist.md`.

## Resource Map

- `references/vcs-verdi-flow.md`: VCS compile/elaborate/simulate flow, FSDB dump expectations, and non-GUI Verdi checks.
- `references/verdi-rc-format.md`: scenario/base file format and generated RC behavior.
- `references/verdi-interaction.md`: Tk `send`, generated Python wrappers, and IPython caveats.
- `references/threadfpga-validation.md`: remote validation order and safety boundaries.
- `references/threadfpga-gate.md`: strict ThreadFPGA full-flow acceptance steps.
- `references/review-checklist.md`: reviewer gate for scope, environment, RC, smoke, and remote evidence.
- `scripts/check_env.py`: report tool and environment readiness as JSON or text.
- `scripts/generate_rc.py`: generate Verdi signal restore RC from `scn_base.lst` plus `scn_<scenario>.lst`.
- `scripts/smoke_vcs_verdi.py`: build or execute a minimal VCS/Verdi smoke command plan.
- `scripts/make_shell_overlay.sh`: create a temporary remote-only wrapper overlay when `/bin/sh` rejects Synopsys scripts.
- `scripts/run_threadfpga_smoke.sh`: ThreadFPGA login-shell runner for the minimal full-flow gate.
- `evals/evals.json`: behavior eval scenarios comparing with-skill and without-skill outcomes.
- `assets/minimal_vcs/`: minimal SystemVerilog fixture and dump command.
- `assets/waves/`: base/scenario templates for RC generation.

## Safety Boundaries

- Do not assume GUI Verdi is available over SSH; prefer dry-run or non-GUI checks unless VNC/X11 is explicitly configured.
- Do not run long simulations synchronously over remote SSH; use reviewed detached execution when runtime may exceed the local timeout.
- Do not copy paths, hostnames, usernames, or license details into public docs.
- Do not depend on the temporary reference directory at runtime. Treat it as development evidence only.
