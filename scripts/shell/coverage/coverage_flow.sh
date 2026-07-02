#!/usr/bin/env bash
set -euo pipefail

# 直接回溯当前 shell 入口到 skill 根目录，避免额外中间变量影响路径语义判断
path_skill_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

# 调用 coverage_flow Python 主程序统一执行覆盖率流程，让参数解析、失败判定与退出码策略只维护在这一份实现里
exec python3 "$path_skill_root/scripts/python/coverage/coverage_flow.py" "$@"
