#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$BUNDLE_ROOT/run"
mkdir -p "$RUN_DIR"
cd "$BUNDLE_ROOT"

: "${VCS_HOME:?VCS_HOME must be set by the remote EDA login shell}"
: "${VERDI_HOME:?VERDI_HOME must be set by the remote EDA login shell}"

bash "$SCRIPT_DIR/make_shell_overlay.sh" "$VCS_HOME" "$RUN_DIR/vcs_overlay"
bash "$SCRIPT_DIR/make_shell_overlay.sh" "$VERDI_HOME" "$RUN_DIR/verdi_overlay"

export VCS_HOME="$RUN_DIR/vcs_overlay"
export VERDI_HOME="$RUN_DIR/verdi_overlay"
export PATH="$VCS_HOME/bin:$VERDI_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$VERDI_HOME/share/PLI/VCS/LINUX64:${LD_LIBRARY_PATH:-}"
export DISPLAY="${DISPLAY:-:23}"

python3 "$SCRIPT_DIR/patch_ucapi_overlay.py" \
  --overlay-home "$VCS_HOME" \
  --mode scan \
  --json > "$RUN_DIR/ucapi_patch_scan.json"

if [ "${VCS_VERDI_ALLOW_UCAPI_PATCH:-0}" = "1" ]; then
  python3 "$SCRIPT_DIR/patch_ucapi_overlay.py" \
    --overlay-home "$VCS_HOME" \
    --mode apply \
    --json > "$RUN_DIR/ucapi_patch_apply.json"
  if [ -d "$VCS_HOME/ucapi_patch_lib" ]; then
    export LD_LIBRARY_PATH="$VCS_HOME/ucapi_patch_lib:$LD_LIBRARY_PATH"
  fi
fi

for compat_lib_dir in \
  "$VCS_HOME/vcfca/auxx/monet/lintllm/_internal" \
  "$VERDI_HOME/share/ugo/linux64/bin/ugo_dist/ugo/_internal" \
  "/tools/Xilinx/Vivado/2022.2/gnu/microblaze/lin/x86_64-oesdk-linux/lib"; do
  if [ -f "$compat_lib_dir/libncursesw.so.5" ]; then
    mkdir -p "$RUN_DIR/compat_lib"
    ln -sf "$compat_lib_dir/libncursesw.so.5" "$RUN_DIR/compat_lib/libncursesw.so.5"
    export LD_LIBRARY_PATH="$RUN_DIR/compat_lib:$LD_LIBRARY_PATH"
    break
  fi
done

python3 "$SCRIPT_DIR/check_env.py" --json > "$RUN_DIR/check_env.json"

set +e
python3 "$SCRIPT_DIR/smoke_vcs_verdi.py" \
  --execute \
  --clean \
  --json \
  --source "$BUNDLE_ROOT/assets/minimal_vcs/top.sv" \
  --workdir "$RUN_DIR/smoke" \
  --top top \
  --dump-name waves.fsdb \
  --no-auto-pli \
  --verdi-check fsdbreport \
  --report-signal /top/clk \
  --step-timeout 120 > "$RUN_DIR/smoke.json"
smoke_rc=$?

python3 "$SCRIPT_DIR/smoke_vcs_verdi.py" \
  --manifest "$BUNDLE_ROOT/assets/minimal_vcs/manifest_matrix.json" \
  --execute \
  --clean \
  --json \
  --workdir "$RUN_DIR/mixed" \
  --no-auto-pli \
  --step-timeout 120 > "$RUN_DIR/mixed_smoke.json"
mixed_rc=$?

python3 "$SCRIPT_DIR/smoke_vcs_verdi.py" \
  --execute \
  --clean \
  --json \
  --source "$BUNDLE_ROOT/assets/minimal_vcs/coverage_top.sv" \
  --workdir "$RUN_DIR/coverage_sv" \
  --top top \
  --dump-name waves.fsdb \
  --no-auto-pli \
  --verdi-check fsdbreport \
  --report-signal /top/clk \
  --coverage line \
  --coverage cond \
  --coverage tgl \
  --step-timeout 120 > "$RUN_DIR/coverage_smoke.json"
coverage_smoke_rc=$?

python3 "$SCRIPT_DIR/coverage_flow.py" \
  --workdir "$RUN_DIR/coverage_sv" \
  --execute \
  --timeout 120 \
  --json > "$RUN_DIR/coverage.json"
coverage_rc=$?

probe_strace_args=()
if [ -n "${VCS_VERDI_URG_STRACE_TIMEOUT:-}" ]; then
  probe_strace_args=(--strace-timeout "$VCS_VERDI_URG_STRACE_TIMEOUT")
fi

python3 "$SCRIPT_DIR/urg_runtime_probe.py" \
  --vcs-home "$VCS_HOME" \
  --workdir "$RUN_DIR/coverage_sv" \
  --vdb "$RUN_DIR/coverage_sv/simv.vdb" \
  --report-dir "$RUN_DIR/coverage_sv/urgReport" \
  "${probe_strace_args[@]}" \
  --json > "$RUN_DIR/urg_runtime_probe.json"
probe_rc=$?

python3 "$SCRIPT_DIR/urg_coverage_matrix.py" \
  --vcs-home "$VCS_HOME" \
  --workdir "$RUN_DIR/coverage_sv" \
  --vdb "$RUN_DIR/coverage_sv/simv.vdb" \
  --ucapi-scan "$RUN_DIR/ucapi_patch_scan.json" \
  --timeout 120 \
  --json > "$RUN_DIR/urg_coverage_matrix.json"
matrix_rc=$?

python3 - "$SCRIPT_DIR" "$RUN_DIR" > "$RUN_DIR/conversion.json" <<'PY'
import importlib.util
import json
import sys
from pathlib import Path

script_dir = Path(sys.argv[1])
run_dir = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("fsdb_tools", script_dir / "fsdb_tools.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
cmd = module.build_convert_cmd(run_dir / "smoke" / "waves.fsdb", run_dir / "smoke" / "waves.vcd")
print(json.dumps(module.execute_command(cmd, timeout=120, cwd=run_dir / "smoke"), indent=2, sort_keys=True))
PY
conversion_rc=$?
set -e

job_rc=0
for rc in "$smoke_rc" "$mixed_rc" "$coverage_smoke_rc" "$coverage_rc" "$probe_rc" "$matrix_rc" "$conversion_rc"; do
  if [ "$rc" -ne 0 ]; then
    job_rc="$rc"
    break
  fi
done

python3 "$SCRIPT_DIR/collect_evidence.py" \
  --run-dir "$RUN_DIR" \
  --smoke "$RUN_DIR/smoke.json" \
  --mixed-smoke "$RUN_DIR/mixed_smoke.json" \
  --coverage "$RUN_DIR/coverage.json" \
  --conversion "$RUN_DIR/conversion.json" \
  --ucapi-scan "$RUN_DIR/ucapi_patch_scan.json" \
  --ucapi-manifest "$VCS_HOME/ucapi_patch_manifest.json" \
  --urg-probe "$RUN_DIR/urg_runtime_probe.json" \
  --urg-matrix "$RUN_DIR/urg_coverage_matrix.json" \
  --check-env "$RUN_DIR/check_env.json" \
  --report "$RUN_DIR/smoke/report.txt" \
  --job-exit-code "$job_rc" \
  --output "$RUN_DIR/remote_eda_evidence.json" \
  --json

exit "$job_rc"
