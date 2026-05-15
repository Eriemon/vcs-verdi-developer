#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

: "${VCS_HOME:?VCS_HOME must be set by the ThreadFPGA login shell}"
: "${VERDI_HOME:?VERDI_HOME must be set by the ThreadFPGA login shell}"

bash make_shell_overlay.sh "$VCS_HOME" vcs_overlay
bash make_shell_overlay.sh "$VERDI_HOME" verdi_overlay

export VCS_HOME="$PWD/vcs_overlay"
export VERDI_HOME="$PWD/verdi_overlay"
export PATH="$VCS_HOME/bin:$VERDI_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$VERDI_HOME/share/PLI/VCS/LINUX64:${LD_LIBRARY_PATH:-}"
export DISPLAY="${DISPLAY:-:23}"

python3 check_env.py --json
python3 smoke_vcs_verdi.py \
  --execute \
  --clean \
  --json \
  --source top.sv \
  --workdir run \
  --top top \
  --dump-name waves.fsdb \
  --rc-file waves.rc \
  --no-auto-pli \
  --verdi-check fsdbreport \
  --report-signal /top/clk \
  --step-timeout 120
