#!/usr/bin/env bash
set -euo pipefail

# 直接从当前启动脚本回溯 skill bundle 根目录，避免额外中间路径变量干扰质量门语义判断。
path_bundle_root="$(cd "$(dirname "$0")/../../.." && pwd)"

# 锁定打包后的 Python 脚本目录，所有流程逻辑都统一从这一处实现调用。
dir_python="$path_bundle_root/scripts/python"

# 固定远端验证运行目录，所有 JSON 证据、覆盖率工件和转换产物都写到这里。
dir_run="$path_bundle_root/run"

# 预先创建远端运行目录，避免后续各阶段在不同位置零散落盘。
mkdir -p "$dir_run"

# 切换到 bundle 根目录执行，保证相对资源路径与本地打包布局保持一致。
cd "$path_bundle_root"

# 强制要求远端登录 shell 暴露原始 VCS 安装目录，否则无法构建 overlay 和运行最小用例。
: "${VCS_HOME:?VCS_HOME must be set by the remote EDA host login shell}"

# 强制要求远端登录 shell 暴露原始 Verdi 安装目录，否则无法完成 FSDB 读回与覆盖率链路。
: "${VERDI_HOME:?VERDI_HOME must be set by the remote EDA host login shell}"

# 记录原始 VCS 安装目录，供后续 URG 故障诊断阶段同时对比原始路径与 overlay 路径。
path_vcs_home_original="$VCS_HOME"

# 记录原始 Verdi 安装目录，便于证据中区分供应商原始布局与远端临时 overlay。
path_verdi_home_original="$VERDI_HOME"

# 固定 VCS overlay 目录，让所有 bash 兼容入口、patch 工件和证据引用都落到同一个可回收位置。
path_vcs_home_overlay="$dir_run/vcs_overlay"

# 固定 Verdi overlay 目录，与 VCS 一起构成后续所有工具调用共享的只读兼容层。
path_verdi_home_overlay="$dir_run/verdi_overlay"

# 基于原始 VCS 目录生成 bash 兼容 overlay，避免直接修改远端正式工具安装。
bash "$path_bundle_root/scripts/shell/remote/make_shell_overlay.sh" "$VCS_HOME" "$path_vcs_home_overlay"

# 基于原始 Verdi 目录生成 bash 兼容 overlay，让 smoke 脚本统一经过同一层兼容入口。
bash "$path_bundle_root/scripts/shell/remote/make_shell_overlay.sh" "$VERDI_HOME" "$path_verdi_home_overlay"

# 组装 overlay 生效后的可执行搜索路径，确保 vlogan、vcs、verdi、urg 都优先命中临时入口。
path_exec_search="$path_vcs_home_overlay/bin:$path_verdi_home_overlay/bin:$PATH"

# 把 Verdi PLI 库预置到运行时搜索路径前段，确保 FSDB 读回链路先拿到 overlay 对应的共享库版本。
path_ld_library="$path_verdi_home_overlay/share/PLI/VCS/LINUX64:${LD_LIBRARY_PATH:-}"

# 远端 smoke 默认补一个 DISPLAY 回退值，避免无头环境里某些工具因为 DISPLAY 缺省直接报错。
display_remote="${DISPLAY:-:23}"

# 统一通过 overlay 环境运行后续命令，避免在脚本各处重复展开同一组环境变量。
run_with_overlay_env() {
  env \
    "VCS_HOME=$path_vcs_home_overlay" \
    "VERDI_HOME=$path_verdi_home_overlay" \
    "PATH=$path_exec_search" \
    "LD_LIBRARY_PATH=$path_ld_library" \
    "DISPLAY=$display_remote" \
    "$@"
}

# 先扫描 UCAPI patch 命中情况，把是否需要补丁的信息写入独立证据文件。
run_with_overlay_env python3 "$dir_python/coverage/patch_ucapi_overlay.py" \
  --overlay-home "$path_vcs_home_overlay" \
  --mode scan \
  --json > "$dir_run/ucapi_patch_scan.json"

# 只有远端显式允许时才把 UCAPI patch 应用到 overlay，避免默认篡改供应商库布局。
if [ "${VCS_VERDI_ALLOW_UCAPI_PATCH:-0}" = '1' ]; then

  # 在允许 patch 的场景下生成应用结果证据，便于后续判断 URG 异常是否与补丁相关。
  run_with_overlay_env python3 "$dir_python/coverage/patch_ucapi_overlay.py" \
    --overlay-home "$path_vcs_home_overlay" \
    --mode apply \
    --json > "$dir_run/ucapi_patch_apply.json"

  # 如果 patch 真正落盘，就把补丁库提前到搜索路径最前面，让后续 URG 行为与证据保持一致。
  if [ -d "$path_vcs_home_overlay/ucapi_patch_lib" ]; then

    # 让后续所有子进程优先看到 overlay 内的补丁库，避免 evidence 与真实运行时路径出现偏差。
    path_ld_library="$path_vcs_home_overlay/ucapi_patch_lib:$path_ld_library"
  fi
fi

# 先把 VCS 内置兼容库目录登记为首选候选，优先覆盖供应商主工具链内部的旧版依赖。
path_compat_lib_candidates=(
  "$path_vcs_home_overlay/vcfca/auxx/monet/lintllm/_internal"  # VCS 自带兼容库候选目录
)

# 再把 Verdi 内置兼容库目录追加到候选集合，保持工具链自带兼容库的次优先顺序。
path_compat_lib_candidates+=(
  "$path_verdi_home_overlay/share/ugo/linux64/bin/ugo_dist/ugo/_internal"  # Verdi 自带兼容库候选目录
)

# 允许远端运维通过环境变量补充兼容库目录，避免把站点特定路径硬编码进 skill。
if [ -n "${VCS_VERDI_COMPAT_LIB_DIRS:-}" ]; then

  # 把冒号分隔的额外兼容库目录拆成数组，后续与默认候选列表按顺序合并检查。
  mapfile -t path_extra_compat_lib_dirs < <(printf '%s' "$VCS_VERDI_COMPAT_LIB_DIRS" | tr ':' '\n')

  # 仅把调用方显式给出的非空兼容目录加入候选集合，避免把空字段误当成站点路径。
  # 逐项追加非空路径，保持环境变量中的目录优先级与调用方提供顺序一致。
  for path_extra_compat_lib_dir in "${path_extra_compat_lib_dirs[@]}"; do

    # 只有真实非空的候选项才值得继续纳入兼容库探测，空值会在后续文件判断中制造噪声。
    if [ -n "$path_extra_compat_lib_dir" ]; then

      # 只把非空站点路径纳入候选集合，避免空字段在后续探测阶段制造伪路径命中。
      path_compat_lib_candidates+=("$path_extra_compat_lib_dir")
    fi
  done
fi

# 发现兼容版 libncursesw.so.5 后就构造一个本地兼容目录并前置到运行时搜索路径。
for path_compat_lib_dir in "${path_compat_lib_candidates[@]}"; do

  # 只有命中兼容库文件的候选目录才需要触发本地软链接准备流程。
  if [ -f "$path_compat_lib_dir/libncursesw.so.5" ]; then

    # 为本次验证单独准备兼容库目录，避免直接把供应商内部目录暴露给后续证据消费者。
    path_compat_lib_root="$dir_run/compat_lib"

    # 创建兼容库目录，供符号链接稳定承载 libncursesw.so.5。
    mkdir -p "$path_compat_lib_root"

    # 将命中的兼容库软链接到统一目录，后续只需要维护一个稳定的 LD_LIBRARY_PATH 入口。
    ln -sf "$path_compat_lib_dir/libncursesw.so.5" "$path_compat_lib_root/libncursesw.so.5"

    # 一旦命中兼容库，就让后续所有子进程稳定复用这份本地入口而不是继续探测站点目录。
    path_ld_library="$path_compat_lib_root:$path_ld_library"

    # 找到首个可用兼容库后立即停止继续探测，保持实际加载顺序和证据记录一致。
    break
  fi
done

# 在正式执行 smoke 前记录远端环境快照，作为后续证据和失败归因的共同基础。
run_with_overlay_env python3 "$dir_python/env/check_env.py" --json > "$dir_run/check_env.json"

# 下面的阶段需要即使失败也继续收集证据，所以暂时关闭 set -e 并手工记录各阶段退出码。
set +e

# 运行最小 SystemVerilog smoke，用于验证编译、仿真、FSDB 生成和 fsdbreport 读回主链路。
run_with_overlay_env python3 "$dir_python/validation/vcs_verdi_check.py" \
  --execute \
  --clean \
  --json \
  --source "$path_bundle_root/assets/minimal_vcs/top.sv" \
  --workdir "$dir_run/smoke" \
  --top top \
  --dump-name waves.fsdb \
  --no-auto-pli \
  --verdi-check fsdbreport \
  --report-signal /top/clk \
  --step-timeout 120 > "$dir_run/smoke.json"

# 记录最小 smoke 的退出码，让后续证据汇总能够区分链路失败与诊断脚本失败。
exit_code_smoke=$?

# 运行混合语言 smoke，确认 manifest 驱动的 VHDL+SV 组合链路也能在远端通过。
run_with_overlay_env python3 "$dir_python/validation/vcs_verdi_check.py" \
  --manifest "$path_bundle_root/assets/minimal_vcs/manifest_matrix.json" \
  --execute \
  --clean \
  --json \
  --workdir "$dir_run/mixed" \
  --no-auto-pli \
  --step-timeout 120 > "$dir_run/mixed_smoke.json"

# 记录 mixed manifest smoke 的退出码，避免后续只看 evidence 时丢失阶段粒度。
exit_code_mixed=$?

# 运行专门的覆盖率 smoke，用单独的纯 SV fixture 触发行、条件和翻转覆盖率产物。
run_with_overlay_env python3 "$dir_python/validation/vcs_verdi_check.py" \
  --execute \
  --clean \
  --json \
  --source "$path_bundle_root/assets/minimal_vcs/coverage_top.sv" \
  --workdir "$dir_run/coverage_sv" \
  --top top \
  --dump-name waves.fsdb \
  --no-auto-pli \
  --verdi-check fsdbreport \
  --report-signal /top/clk \
  --coverage line \
  --coverage cond \
  --coverage tgl \
  --step-timeout 120 > "$dir_run/coverage_smoke.json"

# 记录覆盖率 smoke 的退出码，后续 job 结论会用它判断 coverage fixture 是否基本成立。
exit_code_coverage_smoke=$?

# 生成 coverage_flow 的事实汇总 JSON，让 URG probe、矩阵判定和最终 evidence 共用同一份基础结果。
run_with_overlay_env python3 "$dir_python/coverage/coverage_flow.py" \
  --workdir "$dir_run/coverage_sv" \
  --execute \
  --timeout 120 \
  --json > "$dir_run/coverage.json"

# 记录 coverage_flow 主流程退出码，后续 job 结论会把它视作核心交付门之一。
exit_code_coverage=$?

# 把 URG runtime probe 的可选 strace 参数集中到独立数组，避免无配置场景下把空字符串塞进命令行。
args_probe_strace=()

# 只有调用方显式提供 strace 超时值时才扩展 probe 参数，避免默认探测平白引入 ptrace 负担。
if [ -n "${VCS_VERDI_URG_STRACE_TIMEOUT:-}" ]; then

  # 把站点提供的 strace 超时参数单独缓存成数组，避免缺省场景下传入空字符串。
  args_probe_strace=(--strace-timeout "$VCS_VERDI_URG_STRACE_TIMEOUT")
fi

# 采集 URG 运行时探针证据，用于解释覆盖率链路失败时的共享库、ptrace 或启动问题。
run_with_overlay_env python3 "$dir_python/coverage/urg_runtime_probe.py" \
  --vcs-home "$path_vcs_home_overlay" \
  --workdir "$dir_run/coverage_sv" \
  --vdb "$dir_run/coverage_sv/simv.vdb" \
  --report-dir "$dir_run/coverage_sv/urgReport" \
  "${args_probe_strace[@]}" \
  --json > "$dir_run/urg_runtime_probe.json"

# 记录 URG runtime probe 的退出码，便于把诊断脚本异常和主链路异常分开解释。
exit_code_probe=$?

# 运行 URG 覆盖率矩阵检查，确认默认 line+cond+tgl 组合在当前远端环境下的真实状态。
run_with_overlay_env python3 "$dir_python/coverage/urg_coverage_matrix.py" \
  --vcs-home "$path_vcs_home_overlay" \
  --workdir "$dir_run/coverage_sv" \
  --vdb "$dir_run/coverage_sv/simv.vdb" \
  --ucapi-scan "$dir_run/ucapi_patch_scan.json" \
  --timeout 120 \
  --json > "$dir_run/urg_coverage_matrix.json"

# 记录 URG 覆盖率矩阵的退出码，让最终结论只把它当作核心交付门之一而非诊断附属品。
exit_code_matrix=$?

# 在 coverage 或 URG 失败时补充供应商原始目录与 overlay 目录对比证据，帮助归因 wrapper 问题。
run_with_overlay_env python3 "$dir_python/coverage/urg_troubleshoot.py" \
  --workdir "$dir_run/coverage_sv" \
  --vdb "$dir_run/coverage_sv/simv.vdb" \
  --vendor-vcs-home "$path_vcs_home_original" \
  --overlay-vcs-home "$path_vcs_home_overlay" \
  --timeout 120 \
  --json > "$dir_run/urg_troubleshoot.json"

# 记录 URG troubleshoot 的退出码，方便后续把诊断工具崩溃与 smoke/coverage 主链路区分开。
exit_code_troubleshoot=$?

# 转换阶段继续固定读取 Python 侧约定的 run_dir / "smoke" / "waves.fsdb"，避免误读 mixed 工程结果。
run_with_overlay_env python3 "$dir_python/diagnosis/fsdb_tools.py" execute \
  fsdb2vcd \
  "$dir_run/smoke/waves.fsdb" \
  -o \
  "$dir_run/smoke/waves.vcd" \
  --cwd "$dir_run/smoke" \
  --timeout 120 \
  --json > "$dir_run/conversion.json"

# 记录 FSDB 转 VCD 的退出码，确保最终 evidence 能说明是转换失败还是上游 smoke 失败。
exit_code_conversion=$?

# 恢复 set -e，让后续证据汇总阶段重新沿用显式失败即停止的默认语义。
set -e

# 默认先采用最小 smoke 阶段的退出码作为核心交付门结果，保证首个关键阶段的失败优先被保留。
exit_code_core_gate_result="$exit_code_smoke"

# 只有最小 smoke 成功时，才继续从后续核心阶段里补抓第一个失败码。
if [ "$exit_code_core_gate_result" -eq 0 ]; then

  # 只从后续核心交付门里挑选第一个失败码，确保最终 shell 退出状态与 collect_evidence 的事实矩阵对齐。
  for exit_code_stage in \
    "$exit_code_mixed" \
    "$exit_code_coverage_smoke" \
    "$exit_code_coverage" \
    "$exit_code_matrix" \
    "$exit_code_conversion"; do

    # 只要遇到首个非零阶段码，就把它固定为最终失败码，防止更晚的诊断阶段覆盖根因信号。
    if [ "$exit_code_stage" -ne 0 ]; then

      # 一旦出现核心阶段失败，就固定最终返回码，避免后续阶段覆盖更早的根因信号。
      exit_code_core_gate_result="$exit_code_stage"

      # 记录完首个失败码后立即结束筛选循环，让最终返回语义稳定对应第一处核心交付失败。
      break
    fi
  done
fi

# 汇总本次远端验证产生的全部事实证据，供 quality gate 与后续 handoff 统一消费。
run_with_overlay_env python3 "$dir_python/evidence/collect_evidence.py" \
  --run-dir "$dir_run" \
  --smoke "$dir_run/smoke.json" \
  --mixed-smoke "$dir_run/mixed_smoke.json" \
  --coverage "$dir_run/coverage.json" \
  --conversion "$dir_run/conversion.json" \
  --ucapi-scan "$dir_run/ucapi_patch_scan.json" \
  --ucapi-manifest "$path_vcs_home_overlay/ucapi_patch_manifest.json" \
  --urg-probe "$dir_run/urg_runtime_probe.json" \
  --urg-matrix "$dir_run/urg_coverage_matrix.json" \
  --urg-troubleshoot "$dir_run/urg_troubleshoot.json" \
  --check-env "$dir_run/check_env.json" \
  --report "$dir_run/smoke/report.txt" \
  --job-exit-code "$exit_code_core_gate_result" \
  --output "$dir_run/remote_eda_evidence.json" \
  --json

# 把核心交付门的最终退出码返回给调用方，让上层自动化无需重复解析 evidence 文件也能判定成败。
exit "$exit_code_core_gate_result"
