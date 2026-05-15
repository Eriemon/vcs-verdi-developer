# ThreadFPGA Validation

Use `erie-remote-ssh` for every ThreadFPGA operation.

## Order

1. Discover configured servers and select the default ThreadFPGA server for the current validation task.
2. Run `check` before any remote command.
3. Run read-only environment discovery first: `python scripts/check_env.py --json` in the remote validation bundle.
4. Upload only explicit fixtures or validation bundles, never the whole local skill-development tree.
5. Run `scripts/smoke_vcs_verdi.py --dry-run --json` as a dry run before any execution.
6. Execute the smoke flow only when required tools, license hints, shell behavior, and DISPLAY/VNC strategy are understood.
7. For full-flow acceptance, follow `threadfpga-gate.md`.

## Reporting

Report tool availability, commands attempted, return codes, generated artifact paths, and any degraded conditions. Missing VCS or Verdi is a valid smoke result, but not a passing EDA flow.

Do not report factual 100% confidence unless the full gate passes: compile, elaborate, simulate, nonzero FSDB, and Verdi load.

## Observed Shell Wrapper Issue

On ThreadFPGA, Synopsys W-2024.09-SP1 wrapper scripts may start with `#!/bin/sh -h`. Ubuntu `dash` rejects `-h`, while `ksh` may fail on wrapper syntax. `scripts/smoke_vcs_verdi.py` wraps these scripts with `bash` during execution; if compile passes but `vcs` common elaboration still fails with `/bin/sh: 0: Illegal option -h`, report this as a remote installation or shell compatibility blocker rather than a passing skill-script result.
