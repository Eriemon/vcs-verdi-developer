#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RemainingArguments
)

# 解析当前 PowerShell 入口回溯后的 skill 根目录，确保脚本总能找到目标 Python 实现
$pathSkillRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..\..')

# 调用 autoverifix_vcs_flow Python 主程序统一执行AutoVeriFix VCS 流程，让参数解析、失败判定与退出码策略只维护在这一份实现里
& python (Join-Path $pathSkillRoot 'scripts\python\flows\autoverifix_vcs_flow.py') @RemainingArguments

# 把 Python 退出码返回给调用方，便于上层自动化继续沿用统一的失败判定
exit $LASTEXITCODE
