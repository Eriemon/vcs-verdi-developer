# ThreadFPGA Full-Flow Gate

This gate is required before claiming factual confidence for ThreadFPGA VCS/Verdi execution.

## Remote Directory

Use a dedicated temporary work directory such as:

```sh
mkdir -p ~/workspace/vcs-verdi-smoke
```

Upload only the runtime fixture, optional RC file, and the smoke script. Do not synchronize the local skill repository or temporary reference material.

## Read-Only Probe

Run these checks before any compile:

```sh
which vlogan
which vcs
which verdi
python3 --version || python --version
env | egrep '^(VCS_HOME|VERDI_HOME|NOVAS_HOME|SNPSLMD_LICENSE_FILE|LM_LICENSE_FILE|DISPLAY|SHELL|LD_LIBRARY_PATH)='
head -n 1 "$(which vlogan)"
head -n 1 "$(which vcs)"
head -n 1 "$(which verdi)"
/bin/sh -h -c 'exit 0'
```

Record missing tools, missing licenses, missing display, and `/bin/sh -h` behavior as explicit findings.

## Required Execution

The smoke flow must run in this order:

```sh
python3 smoke_vcs_verdi.py --dry-run --json \
  --source top.sv \
  --workdir ./run \
  --top top \
  --dump-name waves.fsdb

python3 smoke_vcs_verdi.py --execute --clean --json \
  --source top.sv \
  --workdir ./run \
  --top top \
  --dump-name waves.fsdb
```

Acceptance requires:

- `vlogan` compile returns 0.
- `vcs` elaborate returns 0.
- `./simv` simulate returns 0.
- `run/waves.fsdb` exists and has nonzero size.
- `verdi -ssf run/waves.fsdb -nologo -exit` returns 0, with `-sswr` included when RC validation is in scope.

## ThreadFPGA Environment Notes

ThreadFPGA may expose the Synopsys environment only through a login shell. Use `bash -lc` for the reviewed full-flow runner so `VCS_HOME`, `VERDI_HOME`, `LM_LICENSE_FILE`, and `SNPSLMD_LICENSE_FILE` are present.

If Synopsys wrappers start with `#!/bin/sh -h` or a Verdi wrapper fails under `/bin/sh`, create a temporary validation overlay with `scripts/make_shell_overlay.sh`. The overlay must live inside the remote validation directory, symlink unmodified tool files to the original install, and patch only copied shell-wrapper shebangs to bash. Do not modify `/tools/synopsys`.

For Verdi 2024.09 and newer, prefer the `VERDI_HOME` plus `-debug_access` FSDB flow. Use `--no-auto-pli` when legacy `-P novas.tab pli.a` causes deprecation errors and no FSDB is produced.

If GUI Verdi or `verdi -batch` does not return naturally, keep that as a GUI automation limitation and use `--verdi-check fsdbreport --report-signal /top/clk` for the deterministic CI gate. This proves the FSDB is readable by Verdi tooling; it does not claim Tk/GUI automation is complete.

## Failure Evidence

If any step fails, capture:

- wrapper first lines for `vlogan`, `vcs`, and `verdi`;
- chosen execution command after wrapper normalization;
- `SHELL`, `DISPLAY`, `XAUTHORITY`, VNC/Xvfb process hints, and license variables;
- each step return code, cwd, command, and log path;
- artifact status for `simv`, `waves.fsdb`, and RC;
- the shortest reproducible command that fails.

Do not guess a remote environment fix before this evidence is available.
