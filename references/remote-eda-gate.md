# Remote EDA Full-Flow Gate

This gate is required before claiming factual confidence for VCS/Verdi execution on any selected remote Linux EDA host.

## Table of Contents

- [Remote Directory](#remote-directory)
- [Read-Only Probe](#read-only-probe)
- [Required Execution](#required-execution)
- [Remote EDA Environment Notes](#remote-eda-environment-notes)

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
python3 vcs_verdi_check.py --dry-run --json \
  --source top.sv \
  --workdir ./run \
  --top top \
  --dump-name waves.fsdb

python3 vcs_verdi_check.py --execute --clean --json \
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

Strict truth-gate evidence for `truth_execution_confidence=passed` additionally requires:

- minimal FSDB smoke passes;
- mixed VHDL/SystemVerilog compile, elaborate, simulate, and readback pass;
- pure SystemVerilog coverage fixture builds a valid `simv.vdb`;
- FSDB conversion passes;
- URG coverage report generation passes with a nonempty default `line+cond+tgl__urg__auto64` report in `urg_coverage_matrix.json`.

## Remote EDA Environment Notes

A remote EDA host may expose the Synopsys environment only through a login shell. Use `bash -lc` for the reviewed full-flow runner so `VCS_HOME`, `VERDI_HOME`, `LM_LICENSE_FILE`, and `SNPSLMD_LICENSE_FILE` are present.

If Synopsys wrappers start with `#!/bin/sh -h` or a Verdi wrapper fails under `/bin/sh`, create a temporary validation overlay with `scripts/shell/remote/make_shell_overlay.sh`. The overlay must live inside the remote validation directory, symlink unmodified tool files to the original install, and patch only copied shell-wrapper shebangs to bash. Do not modify `/tools/synopsys`.

For Verdi 2024.09 and newer, prefer the `VERDI_HOME` plus `-debug_access` FSDB flow. Use `--no-auto-pli` when legacy `-P novas.tab pli.a` causes deprecation errors and no FSDB is produced.

If GUI Verdi or `verdi -batch` does not return naturally, keep that as a GUI automation limitation and use `--verdi-check fsdbreport --report-signal /top/clk` for the deterministic CI gate. This proves the FSDB is readable by Verdi tooling; it does not claim Tk/GUI automation is complete.

Known remote evidence from 2026-05-15 proves minimal FSDB smoke, mixed VHDL/SystemVerilog, and FSDB conversion on one Linux EDA host. It does not prove URG report generation: `coverage_urg` fails after building valid pure-SV coverage VDB inputs, and explicit `-full64` did not resolve the internal `urg1` stack trace. The strict runner uses `coverage_top.sv`, propagates `-cm line+cond+tgl -cm_dir <workdir>/simv.vdb` through `vlogan`, elaborate, and simulation, and records `urg_coverage_matrix.json` plus `urg_troubleshoot.json` to separate VDB, wrapper, loader, shell-normalization, and internal URG failure modes. Do not pass `-cm` or `-cm_dir` to `vhdlan`; at least one W-2024.09-SP1 host rejects them as invalid analysis-time options. Treat URG as a truth-gate blocker until the default combined matrix variant produces a nonzero report directory on a Linux EDA host.

Latest remote evidence from 2026-05-16 fixed the Windows-generated bundle CRLF shebang issue by normalizing packaged `.sh` files to LF, then reran the strict matrix on the same style of temporary remote validation directory. Minimal FSDB smoke, mixed VHDL/SystemVerilog, and FSDB conversion passed again, so the non-GUI mainline is eligible for `delivery_execution_confidence=passed` when the evidence is fresh and machine-readable. `coverage_urg` still failed: the pure-SV coverage VDB exists and has `.mode64`, but the default `line+cond+tgl__urg__auto64` report directory is absent, all matrix variants emit an internal URG stack trace, and `ucapi_patch_scan.json` reports `no_match`; therefore the guarded UCAPI overlay workaround is not applicable for this tool image and `truth_execution_confidence` must remain blocked.

On the same 2026-05-16 selected remote Linux EDA host, the dedicated `urg_troubleshoot.py` loop now separates shell-layer failures from post-wrapper internal crashes. The vendor wrapper fails immediately with `/bin/sh: 0: Illegal option -h`, the copied-bash vendor wrapper still reports wrapper-relative environment loss, and the overlay/direct `urg1` paths consistently reach the same internal stack-trace failure. When this pattern appears together with `ucapi_patch_scan.status=no_match` and `urg_runtime_probe.status=passed`, classify the truth gate as `vendor_or_host_blocked` instead of continuing to search for an unproven local skill bug.

The latest focused retry also captured a system `gdb` backtrace for the direct `urg1` path. That backtrace shows `SIGSEGV` in `libsnpsmalloc.so` while `libucapi.so` is inside `scl_lc_checkout`, `vcs_checkout`, and `covdb_get_license`. Treat this as a stronger root-cause refinement of the same blocker: `urg_failure_signature.classification=ucapi_license_checkout_segv` still means the selected host or vendor runtime is blocking truth-gate closure, not that a local skill wrapper remains unfixed. The bundled Synopsys `cbug-gdb-64` helper also lacks `libncursesw.so.5` on this host, so its missing-library message is diagnostic-loss noise after the real crash, not the primary crash cause.

On 2026-05-16, a verified workaround recovered the declared truth gate on the same selected remote Linux EDA host: retry URG with `VCS_USE_MALLOC=1` after the known internal stack-trace failure shape. The factual pass condition is now:

- smoke, mixed VHDL/SV, and FSDB conversion still pass
- `coverage_flow.py` produces a nonempty default `urgReport`
- `urg_coverage_matrix.py` produces a passing default combined variant
- `job_exit_code=0`

The direct non-fallback `urg_troubleshoot.py` paths may still reproduce the underlying vendor crash for diagnostic purposes. That no longer blocks the declared non-GUI truth gate once the automatic `VCS_USE_MALLOC=1` retry has generated the default URG report.

The release-grade verification step is stronger than a same-directory rerun: a clean-room zip extraction into a fresh remote validation directory must also pass with the same bundle contents. Treat that cold-start pass as the proof that the workaround belongs to the shipped skill rather than to hand-edited remote residue.

`scripts/python/coverage/patch_ucapi_overlay.py` is a guarded workaround for a known URG `libucapi.so` crash pattern. The default full-flow runner only scans the VCS overlay and records `ucapi_patch_scan.json`. If the scan returns `no_match` while URG still emits an internal stack trace, the matrix must report `urg_internal_stack_trace_ucapi_patch_not_applicable` rather than implying a safe local patch exists. If a future scan returns `match`, apply the patch only when `VCS_VERDI_ALLOW_UCAPI_PATCH=1` is set, and only to copied overlay files or an overlay-local `ucapi_patch_lib` directory. `scripts/python/coverage/urg_runtime_probe.py` must then prove the patched library directory is loader-effective before any patched pass is accepted. Never patch `/tools/synopsys` or any other shared vendor install path.

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
- URG troubleshooting JSON with vendor wrapper versus overlay wrapper, `VCS_ARCH_OVERRIDE=linux`, direct VCS `linux64/lib` loader, `-format text`, `-show summary`, metric variants, and normalized shell behavior;
- coverage stdout/stderr tails and the actual `urg1` command line;
- the shortest reproducible command that fails.

Do not guess a remote environment fix before this evidence is available.
