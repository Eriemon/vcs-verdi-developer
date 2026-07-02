#!/usr/bin/env bash
set -euo pipefail

# 校验调用方是否显式提供源工具目录与 overlay 目标目录，避免误删或误写无关路径。
if [ "$#" -ne 2 ]; then

  # 用固定错误消息阻止参数缺失场景继续执行，避免后续把未解析路径带入删除和写入操作。
  printf '%s\n' '> ERR: [Shell] 用法: make_shell_overlay.sh <source-tool-home> <overlay-tool-home>' >&2

  # 在参数不足时立即停止执行，避免后续路径安全校验建立在缺失输入之上。
  exit 2
fi

# 记录原始 Synopsys 工具安装根目录，后续所有链接与包装都从这里复制语义。
path_source_tool_home="$1"

# 记录本次临时 overlay 的输出目录，确保改写只发生在远端验证工作区。
path_overlay_tool_home="$2"

# 确认源工具目录包含可执行入口所在的 bin 子目录，否则 overlay 无法构造。
if [ ! -d "$path_source_tool_home/bin" ]; then

  # 源工具缺少 bin 时直接失败，避免后续生成一个看似成功但无法启动任何工具的空 overlay。
  printf '%s\n' "> ERR: [Shell] 源工具目录缺少 bin 子目录: $path_source_tool_home" >&2

  # 在源工具布局不完整时立即退出，阻止后续链接和 wrapper 生成写入无效 overlay。
  exit 2
fi

# 拒绝删除空路径、当前目录或根目录，确保 overlay 清理动作只发生在显式工作区目标上。
case "$path_overlay_tool_home" in
  '' | '.' | '/' )

    # 在删除前先拒绝危险目标，避免 overlay 清理逻辑误伤调用方工作目录或系统根路径。
    printf '%s\n' "> ERR: [Shell] overlay 目标路径不安全: $path_overlay_tool_home" >&2

    # 遇到危险删除目标时直接退出，确保后续任何清理动作都不会触碰未授权路径。
    exit 2
    ;;
esac

# 拼出 overlay 目标的物理目标路径，用于判断是否误指向源工具目录本身或其子目录。
path_overlay_tool_home_resolved="$(cd "$(dirname "$path_overlay_tool_home")" && pwd -P)/$(basename "$path_overlay_tool_home")"

# 拒绝把 overlay 目录放进源工具目录树内，避免清理旧 overlay 时误删供应商正式安装内容。
case "$path_overlay_tool_home_resolved" in
  "$(cd "$path_source_tool_home" && pwd -P)" | "$(cd "$path_source_tool_home" && pwd -P)"/* )

    # 当 overlay 目标回落到源目录树内时立即失败，阻止 rm -rf 触碰供应商安装目录。
    printf '%s\n' "> ERR: [Shell] overlay 目标不能位于源工具目录内部: $path_overlay_tool_home_resolved" >&2

    # 只要检测到 overlay 目标侵入源安装树，就立刻终止，避免供应商文件被清理逻辑误删。
    exit 2
    ;;
esac

# 只有旧 overlay 目录实际存在时才执行递归清理，避免把强制删除扩展到不存在的无效目标。
if [ -e "$path_overlay_tool_home" ]; then

  # 仅清理已通过安全边界校验的旧 overlay 目录，保证重建前状态可预测且不触碰供应商安装树。
  rm -r -- "$path_overlay_tool_home"
fi

# 预先创建 overlay 根目录与 bin 目录，后续统一写入符号链接或 bash 包装器。
mkdir -p "$path_overlay_tool_home/bin"

# 把除 bin 以外的目录原样软链接进 overlay，保持许可证、库目录和共享资源仍指向原始安装。
for path_entry in "$path_source_tool_home"/*; do

  # bin 目录会单独按 wrapper 兼容策略处理，这里只复制其他资源入口。
  if [ "${path_entry##*/}" != 'bin' ]; then

    # 非 bin 资源保持软链接透传，确保 overlay 仍引用供应商原始许可证、库目录和附属资源。
    ln -s "$path_entry" "$path_overlay_tool_home/${path_entry##*/}"
  fi
done

# 遍历原始 bin 目录中的每个入口，按需保留软链接或改写 shebang 不兼容的 shell wrapper。
for path_bin_entry in "$path_source_tool_home"/bin/*; do

  # 记录当前 overlay 中对应的目标路径，避免在循环里重复拼接输出文件名。
  path_overlay_bin_entry="$path_overlay_tool_home/bin/${path_bin_entry##*/}"

  # 只有真实文件且 shebang 是 /bin/sh 的入口才需要复制并改写为 bash。
  if [ -f "$path_bin_entry" ] && head -n 1 "$path_bin_entry" | grep -q '^#!/bin/sh'; then

    # 先写入 bash shebang，让远端验证统一绕开 /bin/sh 对 Synopsys wrapper 的兼容问题。
    cat > "$path_overlay_bin_entry" <<'SNPS_WRAPPER_HEADER'
#!/usr/bin/env bash
SNPS_WRAPPER_HEADER

    # urg 的覆盖率装配阶段会单独解析共享库搜索路径，所以只有这个入口需要按环境开关补入 UCAPI patch 库。
    if [ "${path_bin_entry##*/}" = 'urg' ]; then

      # 仅为 urg 包装器补写可选的 patch 装载逻辑，避免其他供应商入口平白引入额外环境分支。
      cat >> "$path_overlay_bin_entry" <<'SNPS_URG_PATCH_LOADER'
if [ "${VCS_VERDI_ALLOW_UCAPI_PATCH:-0}" = "1" ] && [ -n "${VCS_HOME:-}" ] && [ -d "$VCS_HOME/ucapi_patch_lib" ]; then
  case ":${LD_LIBRARY_PATH:-}:" in
    *":$VCS_HOME/ucapi_patch_lib:"*) ;;
    *) export LD_LIBRARY_PATH="$VCS_HOME/ucapi_patch_lib:${LD_LIBRARY_PATH:-}" ;;
  esac
fi
SNPS_URG_PATCH_LOADER
    fi

    # 保留供应商 wrapper 的主体命令与参数处理，只把解释器入口从 /bin/sh 切换到 bash。
      tail -n +2 "$path_bin_entry" >> "$path_overlay_bin_entry"

      # 让复制后的 wrapper 继续具备可执行权限，避免 smoke 阶段调用 overlay/bin 时出现权限失败。
      chmod +x "$path_overlay_bin_entry"

    # 对于本来就不依赖 /bin/sh wrapper 语义的入口，直接保持软链接即可避免制造无意义副本。
  else

    # 原本已经兼容的入口保持软链接透传，避免额外复制把供应商升级差异藏进 overlay。
    ln -s "$path_bin_entry" "$path_overlay_bin_entry"
  fi
done

# 输出结构化信息日志，方便远端排障时确认 overlay 已经在预期目录生成完成。
printf '%s\n' "> INFO: [Shell] 已生成 shell overlay: $path_overlay_tool_home"
