# VCS/Verdi Reviewer Gate

Use this checklist before reporting that a VCS/Verdi workflow is ready. It is the Reviewer layer for this skill.

## Scope Gate

- Confirm the request is within the skill promise: environment probe, RC generation, VCS smoke flow, FSDB artifact check, Verdi load check, Tk/IPython command construction, or ThreadFPGA validation.
- Do not claim complete coverage of every Synopsys VCS or Verdi option. Report unsupported official-tool features as outside the current skill scope.
- Confirm no runtime step depends on the temporary reference directory or local absolute development paths.

## Environment Gate

- `scripts/check_env.py --json` reports `vlogan`, `vcs`, `verdi`, `VCS_HOME`, `VERDI_HOME`, license hints, `DISPLAY`, `SHELL`, `/bin/sh -h` behavior, and novas/PLI hints.
- Missing EDA tools, missing licenses, missing `DISPLAY`, or incompatible shell wrappers are recorded as blockers or warnings, not as success.
- GUI Verdi is accepted only when `ready_for_gui_verdi` is true or an explicit VNC/X11/Xvfb strategy has been validated.

## RC Gate

- RC generation has tests for aliases, virtual buses, markers, analog rows, unknown colors, missing base/scenario files, and invalid rows.
- Generated RC avoids unknown color tokens and emits deterministic output.
- Scenario files are documented in `references/verdi-rc-format.md`.

## Smoke Gate

- Run `scripts/smoke_vcs_verdi.py --dry-run --json` first.
- Command plan includes compile, elaborate, simulate, and Verdi load steps with explicit cwd, logs, wrapper diagnostics, and artifact paths.
- Execute only after missing tools are resolved.
- Full pass requires `waves.fsdb` to exist and have nonzero size after simulation.

## ThreadFPGA Gate

- Follow `references/threadfpga-gate.md`.
- Upload only a minimal runtime fixture or validation bundle into a named remote test directory.
- If full flow fails, collect wrapper shebangs, shell choice, return codes, logs, artifact status, and `DISPLAY`/VNC state before proposing fixes.
