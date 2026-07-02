# Third-Party Reference Extraction

Use this reference when mining public or local VCS/Verdi examples during skill development. Reference repositories are development evidence only; they are not runtime dependencies and must not be required by `SKILL.md`, scripts, assets, or packaged releases.

## Rules

- Clone third-party material only into ignored local development snapshots; never make those paths part of the governed runtime contract.
- Record the source URL, commit, license, and extracted lessons in this file or a governed development note.
- Do not copy third-party source code into `scripts/`, `assets/`, or generated release packages.
- Distill command patterns into our own scripts and tests, then validate them through local gates and remote EDA host or equivalent EDA evidence.
- Keep GUI/Tk examples classified as guarded optional unless a live DISPLAY/VNC/Tk route has been verified.

## Current Inputs

| Source | Commit | License | Useful Lessons | Boundary |
| --- | --- | --- | --- | --- |
| Legacy Verdi wave-layout snapshot | local development snapshot | unknown from snapshot | Scenario/base wave-list files are a good Generator pattern for deterministic RC output. | Do not depend on its runtime path. |
| Legacy Verdi IPython interaction snapshot | local development snapshot | unknown from snapshot | Tk `send` and IPython wrappers need an already running Verdi/nWave interpreter. | Guarded GUI/interactive only. |
| Legacy open-source VLSI workflow snapshot | local development snapshot | Apache-2.0 text present in snapshot | Batch command wrappers and tool-result logs reinforce deterministic non-GUI evidence collection. | Open-source VLSI flow only; not a VCS/Verdi runtime dependency. |
| Legacy tinyriscv-derived Makefile snapshot | local development snapshot | Apache-2.0 text present in snapshot | Provides a realistic VCS Makefile/filelist with `+v2k`, `+incdir`, `-timescale`, delay flags, FSDB name, and Verdi-Ultra command shape. | Distill into importer tests and manifests; do not copy project RTL into the skill. |
| [RISC-Verdi](https://github.com/SchaffHub/RISC-Verdi.git) | `4b15ad3ecb4deee3172ecda534276cb939c71ed2` | MIT | Shows a VCS three-step Makefile (`vlogan`, `vcs`, `./simv`) and `$fsdbDumpvars` fixture that produces `waves.fsdb`; also confirms Tk/VC Apps control is GUI-bound. | Use only as cross-check evidence; copied clones stay outside the governed runtime contract. |

## Distilled Requirements

- Keep the default CI-grade Verdi validation on `fsdbreport`, because public GUI/Tk examples assume a live Verdi/nWave window.
- Preserve the explicit non-GUI chain: compile, elaborate, simulate, nonzero FSDB, FSDB readback.
- Treat `waves.fsdb` generation as insufficient by itself; require artifact bytes and a Verdi-family reader step.
- Prefer manifest/filelist support over fixed Makefile assumptions so real projects can map their own source order.
- Keep legacy tinyriscv-derived coverage as an importer/dry-run test concept: preserve Makefile facts in generated manifest fields and keep execution in the split non-GUI smoke flow.
