# Remote EDA Full-Flow Gate

This gate is required before claiming factual confidence for default remote EDA server VCS/Verdi execution.

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
  --dump-name waves.fsdb \
  --verdi-check fsdbreport \
  --report-signal /top/clk
```

Acceptance requires:

- `vlogan` compile returns 0.
- `vcs` elaborate returns 0.
- `./simv` simulate returns 0.
- `run/waves.fsdb` exists and has nonzero size.
- `fsdbreport run/waves.fsdb -s /top/clk` returns 0. A `verdi -ssf ... -exit` check may be added when the remote Verdi launch is known to exit cleanly, but it is not the default non-GUI CI gate.

Strict matrix evidence for `eda_execution_confidence=passed` additionally requires:

- minimal FSDB smoke passes;
- mixed VHDL/SystemVerilog compile, elaborate, simulate, and readback pass;
- pure SystemVerilog coverage fixture builds a valid `simv.vdb`;
- FSDB conversion passes;
- URG coverage report generation passes with a nonempty default `line+cond+tgl__urg__auto64` report in `urg_coverage_matrix.json`.

## Remote EDA Environment Notes

The default remote EDA server may expose the Synopsys environment only through a login shell. Use `bash -lc` for the reviewed full-flow runner so `VCS_HOME`, `VERDI_HOME`, `LM_LICENSE_FILE`, and `SNPSLMD_LICENSE_FILE` are present.

If Synopsys wrappers start with `#!/bin/sh -h` or a Verdi wrapper fails under `/bin/sh`, create a temporary validation overlay with `scripts/make_shell_overlay.sh`. The overlay must live inside the remote validation directory, symlink unmodified tool files to the original install, and patch only copied shell-wrapper shebangs to bash. Do not modify `/tools/synopsys`.

For Verdi 2024.09 and newer, prefer the `VERDI_HOME` plus `-debug_access` FSDB flow. Use `--no-auto-pli` when legacy `-P novas.tab pli.a` causes deprecation errors and no FSDB is produced.

If GUI Verdi or `verdi -batch` does not return naturally, keep that as a GUI automation limitation and use `--verdi-check fsdbreport --report-signal /top/clk` for the deterministic CI gate. This proves the FSDB is readable by Verdi tooling; it does not claim Tk/GUI automation is complete.

Current remote EDA evidence from 2026-05-15 proves minimal FSDB smoke, mixed VHDL/SystemVerilog, and FSDB conversion. It does not prove URG report generation: `coverage_urg` fails after building valid pure-SV coverage VDB inputs, and explicit `-full64` did not resolve the internal `urg1` stack trace. The strict runner uses `coverage_top.sv`, propagates `-cm line+cond+tgl -cm_dir <workdir>/simv.vdb` through `vlogan`, elaborate, and simulation, and records `urg_coverage_matrix.json` to separate VDB, wrapper, loader, and internal URG failure modes. Do not pass `-cm` or `-cm_dir` to `vhdlan`; the current default remote EDA server W-2024.09-SP1 environment rejects them as invalid analysis-time options. Treat URG as an EDA execution blocker until the default combined matrix variant produces a nonzero report directory on the default remote EDA server or an equivalent Linux EDA host.

`scripts/patch_ucapi_overlay.py` is a guarded workaround for the known URG `libucapi.so` crash pattern. The default full-flow runner only scans the VCS overlay and records `ucapi_patch_scan.json`. On the current default remote EDA server W-2024.09-SP1 install, that scan returned `no_match` for the CSDN byte pattern, so the patch path must not be applied. If a future scan returns `match`, apply the patch only when `VCS_VERDI_ALLOW_UCAPI_PATCH=1` is set, and only to copied overlay files or an overlay-local `ucapi_patch_lib` directory. `scripts/urg_runtime_probe.py` must then prove the patched library directory is loader-effective before any patched pass is accepted. Never patch `/tools/synopsys` or any other shared vendor install path.

## Failure Evidence

If any step fails, capture:

- wrapper first lines for `vlogan`, `vcs`, and `verdi`;
- chosen execution command after wrapper normalization;
- `SHELL`, `DISPLAY`, `XAUTHORITY`, VNC/Xvfb process hints, and license variables;
- each step return code, cwd, command, and log path;
- artifact status for `simv`, `waves.fsdb`, and RC;
- UCAPI patch scan/apply status when URG fails;
- URG runtime probe JSON with `vcs -location`, `ldd`, candidate library hashes, and loader activation order;
- URG coverage matrix JSON with wrapper/direct `urg1`, line/cond/tgl/combined variants, report counts, and failure categories;
- coverage stdout/stderr tails and the actual `urg1` command line;
- the shortest reproducible command that fails.

Do not guess a remote environment fix before this evidence is available.
