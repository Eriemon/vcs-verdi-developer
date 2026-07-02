#!/usr/bin/env python3
"""VCS/URG 覆盖率流程规划与执行辅助。

本模块负责构建 `urg` 覆盖率命令、探测覆盖率工件状态、执行一次或两次诊断型运行，并向上层返回结构化结果。
stdout_protocol: json
当 CLI 使用 `--json` 时，stdout 输出单一 JSON 文本，供脚本或外层 skill 直接消费。
"""

# 延后注解求值，避免类型提示在运行期引入不必要的前向定义约束。
from __future__ import annotations

# 命令行、序列化、环境读取与日志模式识别基础库。
import argparse
import json
import os
import re
import sys

# 文件系统、子进程、计时与通用类型工具。
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

# 统一描述覆盖率计划、执行结果与工件状态的结构化映射。
JsonDict = dict[str, Any]  # 覆盖率流程共享的通用 JSON 风格返回体

# 默认覆盖率流程在未显式指定 metric 时沿用的指标顺序。
DEFAULT_METRICS = ("line", "cond", "tgl")  # 默认的行/条件/翻转覆盖率组合

# 命中这些失败原因时，流程允许追加一次 `VCS_USE_MALLOC=1` 回退重试。
MALLOC_RETRY_REASONS = {  # 允许触发 malloc 回退的 URG 失败原因集合
    "urg internal ucapi/snpsmalloc failure",  # libucapi 或 libsnpsmalloc 相关内部失败
    "urg internal failure with ptrace-blocked stack annotation",  # 栈追踪受 ptrace 限制的内部失败
    "urg stack annotator diagnostic blocked by ptrace after internal failure",  # URG 栈注解器被 ptrace 阻断
}

# 把覆盖率指标列表折叠成 `urg` 识别的 `-cm` 参数文本。
def coverage_metrics_arg(metrics: list[str] | tuple[str, ...] | None = None) -> str:
    """
    生成覆盖率指标命令行参数文本。

    参数：
    - metrics: 调用方显式提供的覆盖率指标序列；为空时回退到默认指标组。

    返回：
    - 返回用 `+` 拼接的覆盖率指标文本，供 `-cm` 参数直接复用。

    异常：
    - 无显式异常；空输入会自动回退到默认指标集合。
    """

    # 缺省时回退到默认指标组，再压平成 `urg` 识别的单个参数字符串。
    return "+".join(metrics or DEFAULT_METRICS)

# 优先选择 `VCS_HOME/bin/urg`，否则回退到 PATH 中的 `urg`。
def _preferred_urg_executable() -> str:
    """
    解析本次流程应当优先调用的 `urg` 可执行文件。

    参数：
    - 无业务参数；函数只读取当前进程环境变量。

    返回：
    - 返回优先使用的 `urg` 路径文本；找不到时回退到裸命令 `urg`。

    异常：
    - 无显式异常；路径不存在时直接走保守回退路径。
    """

    # 先读取调用环境注入的 `VCS_HOME`，判断是否存在更稳定的本地 urg 路径。
    str_vcs_home = os.environ.get("VCS_HOME", "")  # 当前环境声明的 VCS 安装根目录

    # 已声明 `VCS_HOME` 时，优先尝试其 `bin/urg`，避免 PATH 指向错误版本。
    if str_vcs_home:

        # 拼出 `VCS_HOME/bin/urg`，检查该安装树里是否自带 urg 可执行文件。
        path_candidate_urg = Path(str_vcs_home) / "bin" / "urg"  # 当前环境首选的 urg 路径候选

        # 路径真实存在时，直接返回这个更稳定的绝对/相对路径文本。
        if path_candidate_urg.exists():

            # 把 `Path` 转成子进程命令行可直接消费的字符串。
            return str(path_candidate_urg)

    # 缺少环境锚点或安装树不完整时，退回 PATH 解析逻辑。
    return "urg"

# 统计单个文件或目录树下的普通文件数量，供覆盖率报告工件摘要复用。
def _count_files(path_target: Path) -> int:
    """
    统计路径对应的文件数量。

    参数：
    - path_target: 待统计的文件或目录路径。

    返回：
    - 返回普通文件数量；不存在时为 `0`，单个文件时为 `1`。

    异常：
    - 无显式异常；不存在路径按空结果处理。
    """

    # 路径不存在时，报告目录和文件数量都应当保守落到零。
    if not path_target.exists():

        # 不存在的覆盖率工件不能贡献任何文件计数。
        return 0

    # 直接指向文件时，不必递归遍历目录树。
    if path_target.is_file():

        # 单一普通文件在统计口径里记为一个工件。
        return 1

    # 目录场景递归枚举所有普通文件，给报告摘要提供文件总数。
    return sum(1 for path_item in path_target.rglob("*") if path_item.is_file())

# 汇总当前工作目录下 `simv.vdb` 与 `urgReport` 的覆盖率工件状态。
def coverage_status(workdir: Path | str, report_dir: Path | str | None = None) -> JsonDict:
    """
    收集覆盖率数据库与报告目录的存在性摘要。

    参数：
    - workdir: 覆盖率流程的工作目录，通常包含 `simv.vdb`。
    - report_dir: 可选的报告目录覆盖值；为空时默认使用 `urgReport`。

    返回：
    - 返回覆盖率工件状态字典，包含工件路径、是否存在、报告文件数和 `.mode64` 事实。

    异常：
    - 无显式异常；路径缺失时返回保守的 `missing` 状态。
    """

    # 统一把工作目录输入折算成 `Path`，后续工件定位都依赖这一路径锚点。
    path_root = Path(workdir)  # 当前覆盖率工作目录根路径

    # VCS 的覆盖率数据库固定落在 `simv.vdb`，后续状态判断以它为核心。
    path_vdb = path_root / "simv.vdb"  # 当前工作目录下的覆盖率数据库路径

    # 报告目录允许调用方显式覆盖，否则沿用传统 `urgReport` 目录名。
    path_report = Path(report_dir) if report_dir is not None else path_root / "urgReport"  # 当前覆盖率 HTML 报告目录

    # 先缓存数据库存在性，避免后续构造状态时重复触发文件系统查询。
    bool_vdb_exists = path_vdb.exists()  # 覆盖率数据库是否已经落盘

    # 再缓存报告目录是否存在，供状态与工件摘要共同复用。
    bool_report_exists = path_report.exists()  # 覆盖率报告目录是否已经生成

    # `.mode64` 是是否需要 `urg -full64` 的关键信号，提前抽出来作为事实字段。
    bool_vdb_has_mode64 = (path_vdb / ".mode64").exists()  # 当前覆盖率数据库是否声明 64 位模式

    # 只要数据库或报告任一存在，就把覆盖率工件整体视为已经出现。
    str_state = "present" if bool_vdb_exists or bool_report_exists else "missing"  # 当前覆盖率工件整体状态

    # 返回可直接序列化的覆盖率工件摘要，供计划与执行结果统一复用。
    return {
        "state": str_state,
        "status": str_state,
        "vdb_has_mode64": bool_vdb_has_mode64,
        "report_exists": bool_report_exists,
        "report_file_count": _count_files(path_report),
        "artifacts": {
            "simv.vdb": {
                "path": str(path_vdb),
                "exists": bool_vdb_exists,
            },
            "urgReport": {
                "path": str(path_report),
                "exists": bool_report_exists,
            },
        },
    }

# 构建一次 `urg` 覆盖率运行所需的命令、编译参数和工件事实摘要。
def build_coverage_plan(
    workdir: Path | str,
    *,
    metrics: list[str] | tuple[str, ...] | None = None,
    report_dir: Path | str | None = None,
    full64: bool | None = None,
) -> JsonDict:
    """
    生成非 GUI `urg` 覆盖率计划。

    参数：
    - workdir: 覆盖率运行目录，通常也是 `simv.vdb` 与 `urgReport` 的父目录。
    - metrics: 可选的覆盖率指标列表；为空时回退到默认指标组。
    - report_dir: 可选的覆盖率报告目录；为空时默认使用 `urgReport`。
    - full64: 是否显式强制 `urg -full64`；为空时自动跟随 `.mode64` 文件。

    返回：
    - 返回包含命令、覆盖率参数、工件路径和当前工件状态的结构化计划字典。

    异常：
    - 无显式异常；目录缺失时仍返回可执行的保守计划。
    """

    # 把工作目录统一折算成 `Path`，避免后续路径拼接混入字符串边界问题。
    path_root = Path(workdir)  # 当前覆盖率计划绑定的工作目录根路径

    # 先把覆盖率指标序列压平成 `urg` 接受的 `-cm` 参数文本。
    str_metrics_arg = coverage_metrics_arg(metrics)  # 当前计划使用的覆盖率指标参数文本

    # 数据库路径始终指向 `simv.vdb`，供命令构造与状态汇总共同使用。
    path_vdb = path_root / "simv.vdb"  # 当前计划要读取的覆盖率数据库路径

    # 报告输出目录允许显式覆盖，否则仍沿用 `urgReport` 的默认命名。
    path_report = Path(report_dir) if report_dir is not None else path_root / "urgReport"  # 当前计划的报告输出目录

    # `.mode64` 是否存在会影响是否自动追加 `urg -full64`。
    bool_vdb_has_mode64 = (path_vdb / ".mode64").exists()  # 当前数据库是否声明 64 位运行模式

    # 调用方未显式指定时，自动继承 `.mode64` 给出的 64 位运行事实。
    bool_use_full64 = bool_vdb_has_mode64 if full64 is None else full64  # 当前计划是否需要 `urg -full64`

    # 先写入 urg 可执行文件，再按条件逐步补齐命令参数。
    list_cmd = [_preferred_urg_executable()]  # 当前覆盖率计划的基础 urg 命令

    # 需要 64 位 urg 时，优先在命令最前部追加 `-full64`。
    if bool_use_full64:

        # 让命令与 `.mode64` 或显式 CLI 开关保持一致。
        list_cmd.append("-full64")

    # 基础命令行最终总是包含覆盖率数据库目录和报告目录。
    list_cmd.extend(["-dir", str(path_vdb), "-report", str(path_report)])  # 完整的 urg 执行命令参数

    # 返回供 CLI、测试和远端证据采集共同复用的覆盖率计划结构。
    return {
        "workdir": str(path_root),
        "metrics_arg": str_metrics_arg,
        "compile_args": ["-cm", str_metrics_arg],
        "elaborate_args": ["-cm", str_metrics_arg],
        "simulate_args": ["-cm", str_metrics_arg],
        "cmd": list_cmd,
        "vdb": str(path_vdb),
        "report_dir": str(path_report),
        "vdb_has_mode64": bool_vdb_has_mode64,
        "full64": bool_use_full64,
        "coverage": coverage_status(path_root, path_report),
    }

# 根据 URG 输出文本给出更可读的失败原因标签。
def diagnose_coverage_failure(output: str) -> str:
    """
    把 URG 原始日志归类成稳定的失败原因摘要。

    参数：
    - output: `urg` 的 stdout 与 stderr 合并文本。

    返回：
    - 返回面向调用方的失败原因标签；未命中已知模式时返回空字符串。

    异常：
    - 无显式异常；未知日志保持空原因，由上层自行继续分析。
    """

    # 许可证报错要优先落成明确的缺证标签，便于环境门禁区分。
    if "Error-[URG-NLC]" in output or "No license key" in output:

        # 把缺证问题压成稳定原因码，方便远端证据汇总。
        return "urg license missing: VCSTools_Net or VT_CoverageURG"

    # 栈追踪前缀是 URG 内部失败的最直接信号，应早于 ptrace 细节命中。
    if "Stack trace follows" in output:

        # 保留“内部失败 + ptrace 注解受阻”的既有诊断口径。
        return "urg internal failure with ptrace-blocked stack annotation"

    # 只出现 ptrace 报错时，说明诊断栈注解环节被宿主机安全策略阻断。
    if "ptrace: Operation not permitted" in output:

        # 单独标记 ptrace 场景，帮助区分真正的许可证或依赖问题。
        return "urg stack annotator diagnostic blocked by ptrace after internal failure"

    # 旧版 ncurses 缺失会阻断 urg 启动，需要单独给出运行时依赖原因。
    if "libncursesw.so.5" in output:

        # 明确指出缺失的是 urg 运行时共享库，而不是覆盖率数据库问题。
        return "urg runtime missing libncursesw.so.5"

    # libucapi/libsnpsmalloc 相关报错需要进入 `VCS_USE_MALLOC` 回退通道。
    if "libucapi.so" in output or "libsnpsmalloc.so" in output:

        # 该标签会驱动后续 malloc fallback 逻辑判断。
        return "urg internal ucapi/snpsmalloc failure"

    # 未命中任何已知模式时，不伪造原因标签，交给上层保守处理。
    return ""

# 截断过长日志尾部，避免执行结果里携带不可控的大体积文本。
def _tail(text: str, limit: int = 4000) -> str:
    """
    只保留文本尾部的有限窗口。

    参数：
    - text: 待截断的完整文本。
    - limit: 允许保留的最大字符数。

    返回：
    - 返回原文本尾部的固定窗口；文本较短时直接原样返回。

    异常：
    - 无显式异常；长度窗口不合法由调用方自己约束。
    """

    # 文本超长时只保留尾部窗口，兼顾定位错误与控制结果体大小。
    return text[-limit:] if len(text) > limit else text

# 从 URG 输出里提取底层 `urg1` 的真实命令行，便于后续诊断包装器行为。
def extract_urg1_command_line(output: str) -> str:
    """
    抽取 URG 日志里的 `urg1` 命令行。

    参数：
    - output: `urg` 执行输出的完整文本。

    返回：
    - 返回命中的 `Command line:` 后缀文本；未命中时返回空字符串。

    异常：
    - 无显式异常；正则未命中时按空字符串处理。
    """

    # 用多行正则定位 `Command line:`，保留日志中真实落地的 urg1 参数串。
    match_command_line = re.search(r"(?m)^Command line:\s*(.+)$", output)  # 当前输出里匹配到的 urg1 命令行

    # 命中时去掉首尾空白，未命中时回退为空字符串。
    return match_command_line.group(1).strip() if match_command_line else ""

# 记录 urg 可执行文件的解析结果和 shebang 首行，帮助定位包装器差异。
def urg_tool_info(cmd: list[str]) -> JsonDict:
    """
    收集当前 urg 命令对应的工具解析信息。

    参数：
    - cmd: 当前覆盖率计划准备执行的命令数组。

    返回：
    - 返回 `which` 解析结果、规范化路径和首行 shebang 文本。

    异常：
    - 无显式异常；无法解析路径或读取文件时保守返回空字段。
    """

    # 命令非空时优先走 `which` 解析，否则保守保留空字符串。
    str_executable = (shutil.which(cmd[0]) or cmd[0]) if cmd else ""  # 当前命令解析出的 urg 可执行文本

    # 把可执行文本折算成 `Path`，以便读取解析路径和 shebang 首行。
    path_executable = Path(str_executable) if str_executable else Path("")  # 当前 urg 可执行文件路径对象

    # 先把规范化路径初始化为空，后续只在解析成功时覆盖它。
    str_resolved_path = ""  # 当前 urg 可执行文件的规范化路径文本

    # 再把首行 shebang 初始化为空，读取失败时仍返回可序列化的空字段。
    str_first_line = ""  # 当前 urg 可执行文件读取到的首行文本

    # 规范化路径读取要与 shebang 读取分开处理，避免一处失败抹掉另一处证据。
    try:

        # 先记录规范化路径，帮助定位 PATH 包装器或 overlay 映射行为。
        str_resolved_path = str(path_executable.resolve(strict=False))  # 经 PATH 与 overlay 解析后的 urg 规范化路径

    # 无法解析路径时保留空字段，避免把工具探针本身变成阻断点。
    except (FileNotFoundError, IsADirectoryError, OSError):

        # 路径解析失败说明当前 urg 还无法映射成稳定文件实体。
        str_resolved_path = ""  # 规范化路径解析失败时保留空字段

    # shebang 首行可帮助识别 `#!/bin/sh` 包装器，需要单独读取。
    try:

        # 只取首行即可判断是否需要 `bash` 包装执行。
        str_first_line = path_executable.read_text(encoding="utf-8", errors="ignore").splitlines()[0]  # 当前 urg 包装脚本的首行文本

    # 读取失败或文件为空时，不把 shebang 缺失误判成执行错误。
    except (FileNotFoundError, IsADirectoryError, IndexError, OSError):

        # 无法读取首行时保留空字符串，让上层继续走普通执行路径。
        str_first_line = ""  # 缺少 shebang 证据时返回空首行文本

    # 返回工具解析快照，供执行结果和失败诊断统一携带。
    return {
        "which": str_executable,
        "resolved": str_resolved_path,
        "first_line": str_first_line,
    }

# 从 URG 日志里提炼出额外诊断标签，补充主失败原因之外的佐证信息。
def failure_diagnostics(output: str) -> JsonDict:
    """
    收集 `urg` 输出中的诊断侧信号。

    参数：
    - output: `urg` 的 stdout 与 stderr 合并文本。

    返回：
    - 返回只包含已命中诊断标签的字典；未命中时返回空字典。

    异常：
    - 无显式异常；未知文本不会生成伪诊断字段。
    """

    # 先准备空诊断字典，后续仅在命中明确特征时才追加键值。
    dict_diagnostics: dict[str, str] = {}  # 当前日志提炼出的附加诊断标签集合

    # ptrace 报错说明内部栈注解器受宿主机限制，需要单独记成诊断字段。
    if "ptrace: Operation not permitted" in output:

        # 把 ptrace 约束记录成稳定键名，方便上层做结构化判断。
        dict_diagnostics["ptrace"] = "stack annotator diagnostic blocked by ptrace"  # 标记 ptrace 限制导致的栈注解失败

    # 命中 libucapi 时，需要让调用方知道日志明确提到了该共享库。
    if "libucapi.so" in output:

        # 单独记录 libucapi 痕迹，供回退策略和根因分析复用。
        dict_diagnostics["libucapi"] = "output mentions libucapi.so"  # 标记日志里出现 libucapi 共享库痕迹

    # 命中 libsnpsmalloc 时，需要把 malloc 相关异常痕迹保留下来。
    if "libsnpsmalloc.so" in output:

        # 为 malloc fallback 判定补充分配器侧的共享库证据。
        dict_diagnostics["libsnpsmalloc"] = "output mentions libsnpsmalloc.so"  # 标记日志已暴露 snpsmalloc 分配器共享库名

    # 栈追踪前缀说明 URG 至少输出过内部异常堆栈，需要保留这一事实。
    if "Stack trace follows" in output:

        # 额外记录内部栈追踪，帮助区分许可证问题与内部崩溃场景。
        dict_diagnostics["stack_trace"] = "urg emitted internal stack trace"  # 标记 urg 已输出内部异常堆栈

    # 返回附加诊断标签字典，供执行结果直接并入结构化输出。
    return dict_diagnostics

# 如果 urg 可执行文件其实是 `#!/bin/sh` 包装脚本，就改用 `bash` 显式包裹执行。
def execution_command(cmd: list[str]) -> list[str]:
    """
    规范化最终要交给子进程执行的命令数组。

    参数：
    - cmd: 原始覆盖率命令数组。

    返回：
    - 返回可直接传给 `subprocess.run` 的命令数组；需要时自动插入 `bash` 包装层。

    异常：
    - 无显式异常；读取包装脚本失败时直接返回原始命令。
    """

    # 空命令没有任何可规范化内容，直接保留原值返回。
    if not cmd:

        # 调用方已经给出空命令时，不在这里伪造执行目标。
        return cmd

    # 先解析命令首元素的真实可执行位置，便于读取 shebang。
    str_executable = shutil.which(cmd[0]) or cmd[0]  # 当前命令解析出的实际 urg 路径文本

    # 把可执行文本折算成 `Path`，方便读取 wrapper 首行与 shebang 信息。
    path_executable = Path(str_executable)  # 为读取 wrapper shebang 临时构造的 urg 路径对象

    # 尝试读取包装脚本首行，判断是否需要显式改用 `bash` 执行。
    try:

        # 只需要首行 shebang，就足够识别 Synopsys 常见的 `#!/bin/sh` 包装脚本。
        str_first_line = path_executable.read_text(encoding="utf-8", errors="ignore").splitlines()[0]  # 当前 urg 包装脚本首行

    # 可执行文件不存在、不可读或为空时，直接保守复用原始命令。
    except (FileNotFoundError, IsADirectoryError, IndexError, OSError):

        # 缺少 shebang 证据时，不冒险改写执行命令。
        return cmd

    # 命中 `/bin/sh` shebang 时，改用 `bash` 包住脚本以贴合测试与宿主环境习惯。
    if str_first_line.startswith("#!/bin/sh"):

        # 把 `bash <wrapper> ...` 作为最终执行命令，避免 `/bin/sh` 兼容性差异。
        return ["bash", str(path_executable), *cmd[1:]]

    # 非 `/bin/sh` 包装器沿用原始命令，保持默认行为不变。
    return cmd

# 执行一次覆盖率命令，并把返回码、日志尾部和覆盖率工件状态汇总成结构化结果。
def _execute_once(
    plan: JsonDict,
    *,
    cmd: list[str],
    tool: JsonDict,
    timeout: int,
    env: dict[str, str],
) -> JsonDict:
    """
    在给定环境变量下执行一次 `urg` 覆盖率命令。

    参数：
    - plan: 预先构建好的覆盖率计划字典。
    - cmd: 最终要交给子进程的命令数组。
    - tool: 当前 urg 工具解析信息快照。
    - timeout: 子进程超时时间，单位秒。
    - env: 本次执行使用的环境变量字典。

    返回：
    - 返回一次执行的完整结构化结果，包含状态、日志尾部、诊断标签和覆盖率工件摘要。

    异常：
    - 无显式异常；`TimeoutExpired` 会被转成结构化 `timeout` 结果。
    """

    # 先记录单次执行的起始时间，便于结果里回传精确耗时。
    float_started_monotonic = time.monotonic()  # 当前覆盖率执行的单调时钟起点

    # 正常执行路径需要完整捕获 stdout/stderr，供失败诊断与日志裁剪复用。
    try:

        # 运行一次 urg 命令，并把所有文本输出都收集到结构化结果里。
        process_completed = subprocess.run(  # 当前覆盖率执行返回的完成态进程对象
            cmd,  # 当前要执行的 urg 命令数组
            cwd=plan["workdir"],  # 当前覆盖率计划绑定的工作目录
            env=env,  # 本次执行使用的环境变量字典
            text=True,  # 让 stdout/stderr 直接按文本返回
            stdout=subprocess.PIPE,  # 捕获标准输出供诊断与结果回传
            stderr=subprocess.PIPE,  # 捕获标准错误供诊断与结果回传
            timeout=timeout,  # 当前单次 urg 执行的超时秒数
        )

        # 先把 stdout 归一化成字符串，后续失败诊断和尾部裁剪都复用它。
        str_stdout = process_completed.stdout or ""  # 当前 urg 执行采集到的标准输出文本

        # 再把 stderr 归一化成字符串，避免空值打断字符串拼接与裁剪。
        str_stderr = process_completed.stderr or ""  # 当前 urg 执行采集到的标准错误文本

        # 返回码为零视为通过，否则统一落成 failed，方便上层门禁处理。
        str_status = "passed" if process_completed.returncode == 0 else "failed"  # 当前 urg 执行的最终状态标签

        # 合并 stdout/stderr 以驱动失败原因和附加诊断的统一解析。
        str_combined_output = str_stdout + str_stderr  # 当前 urg 执行的合并诊断文本

        # 结束时重新采样单调时钟，形成可序列化的执行耗时摘要。
        float_elapsed_sec = round(time.monotonic() - float_started_monotonic, 3)  # 当前 urg 执行耗时秒数

        # 正常完成后，把计划、日志、诊断与工件状态统一并入结果字典。
        return {
            **plan,
            "returncode": process_completed.returncode,
            "status": str_status,
            "reason": "" if str_status == "passed" else diagnose_coverage_failure(str_combined_output),
            "diagnostics": failure_diagnostics(str_combined_output),
            "urg_tool": tool,
            "urg1_command_line": extract_urg1_command_line(str_combined_output),
            "execution_cmd": cmd,
            "elapsed_sec": float_elapsed_sec,
            "stdout": str_stdout,
            "stderr": str_stderr,
            "stdout_tail": _tail(str_stdout),
            "stderr_tail": _tail(str_stderr),
            "coverage": coverage_status(plan["workdir"], plan.get("report_dir")),
        }

    # 超时场景也要产出结构化结果，而不是把异常直接抛给上层。
    except subprocess.TimeoutExpired as exc_timeout:

        # 先把超时 stdout 归一化成字符串，保持结果字典字段类型稳定。
        str_stdout = exc_timeout.stdout or ""  # 当前超时执行在中断前采集到的标准输出

        # 再把超时 stderr 归一化成字符串，缺失时回填统一的超时文本。
        str_stderr = exc_timeout.stderr or f"timeout after {timeout}s"  # 当前超时执行的标准错误或超时占位文本

        # 合并超时前的输出，便于继续抽取 urg1 命令行等调试线索。
        str_combined_output = str_stdout + str_stderr  # 当前超时执行保留下来的合并输出文本

        # 即使超时，也要把耗时窗口写回结果体，供外层判断是否接近阈值。
        float_elapsed_sec = round(time.monotonic() - float_started_monotonic, 3)  # 当前超时执行的实际耗时秒数

        # 把超时也编码成统一的结果结构，方便上层直接复用同一消费路径。
        return {
            **plan,
            "returncode": None,
            "status": "timeout",
            "reason": f"timeout after {timeout}s",
            "diagnostics": {"timeout": f"timeout after {timeout}s"},
            "urg_tool": tool,
            "urg1_command_line": extract_urg1_command_line(str_combined_output),
            "execution_cmd": cmd,
            "elapsed_sec": float_elapsed_sec,
            "stdout": str_stdout,
            "stderr": str_stderr,
            "stdout_tail": _tail(str_stdout),
            "stderr_tail": _tail(str_stderr),
            "coverage": coverage_status(plan["workdir"], plan.get("report_dir")),
        }

# 判断本次失败是否值得用 `VCS_USE_MALLOC=1` 再试一次。
def _should_retry_with_vcs_use_malloc(result: JsonDict, *, env: dict[str, str]) -> bool:
    """
    决定是否触发 `VCS_USE_MALLOC` 回退重试。

    参数：
    - result: 第一次覆盖率执行得到的结构化结果。
    - env: 第一次执行时使用的环境变量字典。

    返回：
    - 返回布尔值；`True` 表示应该追加一次 `VCS_USE_MALLOC=1` 重试。

    异常：
    - 无显式异常；输入缺失字段时按不重试保守处理。
    """

    # 已经处在 malloc fallback 环境里时，禁止无限递归重试。
    if env.get("VCS_USE_MALLOC") == "1":

        # 避免把同一类内部失败重复放大成无限重试链。
        return False

    # 首次执行如果不是 failed，就没有必要进入诊断型回退分支。
    if result.get("status") != "failed":

        # 只有明确失败才值得继续判断 malloc fallback 条件。
        return False

    # 仅允许已知的内部失败标签触发一次 `VCS_USE_MALLOC=1` 回退。
    return result.get("reason", "") in MALLOC_RETRY_REASONS

# 执行覆盖率计划，并在命中 malloc 相关内部失败时追加一次回退重试。
def execute_coverage_plan(plan: JsonDict, *, timeout: int = 300) -> JsonDict:
    """
    执行覆盖率计划并返回结构化结果。

    参数：
    - plan: 由 `build_coverage_plan` 生成的覆盖率计划字典。
    - timeout: 每次 `urg` 执行允许的超时时间，单位秒。

    返回：
    - 返回一次或两次执行后的最终结果；回退重试场景会额外附带初次失败摘要。

    异常：
    - 无显式异常；子进程失败和超时都转换成结构化结果返回。
    """

    # 先根据包装脚本事实规范化最终执行命令，避免 `/bin/sh` 差异影响结果。
    list_execution_cmd = execution_command(plan["cmd"])  # 当前计划最终交给子进程的命令数组

    # 再采集 urg 工具解析信息，方便执行结果携带包装器与解析路径证据。
    json_dict_tool_info = urg_tool_info(plan["cmd"])  # 当前计划绑定的 urg 工具解析信息

    # 首次执行沿用当前进程环境变量，作为最保守的默认运行上下文。
    dict_base_env = dict(os.environ)  # 当前覆盖率首次执行使用的基础环境变量字典

    # 先执行一次原始计划，后续是否回退完全由第一次结果驱动。
    json_dict_first_result = _execute_once(  # 当前覆盖率计划的首次执行结果
        plan,  # 当前要执行的覆盖率计划
        cmd=list_execution_cmd,  # 已规范化的 urg 命令数组
        tool=json_dict_tool_info,  # 当前 urg 工具解析信息快照
        timeout=timeout,  # 当前单次执行允许的超时秒数
        env=dict_base_env,  # 首次执行沿用的基础环境变量
    )

    # 不满足 malloc fallback 条件时，直接返回首次执行结果。
    if not _should_retry_with_vcs_use_malloc(json_dict_first_result, env=dict_base_env):

        # 保持首次执行结果原样返回，避免无依据地追加第二次尝试。
        return json_dict_first_result

    # 准备第二次执行环境，只在首次失败命中白名单原因时注入 `VCS_USE_MALLOC=1`。
    dict_retry_env = dict_base_env.copy()  # 当前覆盖率回退重试使用的环境变量字典

    # 显式打开 `VCS_USE_MALLOC`，触发 Synopsys 工具的 malloc 回退路径。
    dict_retry_env["VCS_USE_MALLOC"] = "1"  # 当前回退重试注入的 malloc 环境变量

    # 使用同一计划和命令重跑一次，只改变环境变量上下文。
    json_dict_retry_result = _execute_once(  # 当前覆盖率计划的 malloc 回退执行结果
        plan,  # 与首次执行一致的覆盖率计划
        cmd=list_execution_cmd,  # 与首次执行一致的 urg 命令数组
        tool=json_dict_tool_info,  # 与首次执行一致的 urg 工具解析信息
        timeout=timeout,  # 回退重试沿用相同的超时秒数
        env=dict_retry_env,  # 已注入 VCS_USE_MALLOC 的回退环境
    )

    # 把回退元数据和初次失败摘要并回最终结果，便于上层判断是否发生过二次尝试。
    return {
        **json_dict_retry_result,
        "fallback_applied": True,
        "fallback_env": {"VCS_USE_MALLOC": "1"},
        "initial_attempt": {
            "status": json_dict_first_result.get("status"),
            "reason": json_dict_first_result.get("reason"),
            "returncode": json_dict_first_result.get("returncode"),
            "stdout_tail": json_dict_first_result.get("stdout_tail", ""),
            "stderr_tail": json_dict_first_result.get("stderr_tail", ""),
        },
    }

# 解析 CLI 参数，按需输出 JSON 协议结果或人类可读命令摘要。
def main() -> int:
    """
    运行覆盖率流程 CLI 入口。

    参数：
    - 无显式业务参数；函数直接消费命令行参数。

    返回：
    - 返回进程退出码；执行成功时为 `0`，执行失败时回传 `urg` 返回码或 `1`。

    异常：
    - 无显式异常；命令行解析失败由 `argparse` 自行处理退出。
    """

    # 先创建命令行解析器，统一承载覆盖率计划与执行相关开关。
    parser = argparse.ArgumentParser(description="Plan and inspect VCS coverage/URG non-GUI flow.")  # 覆盖率流程 CLI 的参数解析器

    # 工作目录决定 `simv.vdb` 和 `urgReport` 的默认定位根路径。
    parser.add_argument("--workdir", type=Path, default=Path("build/vcs-cov"))

    # `--metric` 允许多次出现，最终汇总成覆盖率指标列表。
    parser.add_argument("--metric", action="append", dest="metrics")

    # `--report-dir` 支持显式覆盖传统 `urgReport` 输出目录。
    parser.add_argument("--report-dir", type=Path)

    # `--full64` 用于强制追加 `urg -full64`，即使 `.mode64` 不存在也生效。
    parser.add_argument(
        "--full64",
        action="store_true",
        help="Force urg -full64 even if simv.vdb/.mode64 is absent.",
    )

    # `--no-auto-full64` 用于关闭 `.mode64` 自动触发的 `urg -full64` 行为。
    parser.add_argument(
        "--no-auto-full64",
        action="store_true",
        help="Disable automatic urg -full64 from simv.vdb/.mode64.",
    )

    # `--execute` 决定当前 CLI 是只出计划，还是实际跑一遍 urg。
    parser.add_argument("--execute", action="store_true")

    # `--timeout` 控制每次 urg 执行的最大等待时间，单位秒。
    parser.add_argument("--timeout", type=int, default=300)

    # `--json` 会把结果按模块声明的 JSON stdout 协议直接输出到终端。
    parser.add_argument("--json", action="store_true")

    # 解析命令行参数后，后续所有分支都只消费这一份参数快照。
    args = parser.parse_args()  # 当前 CLI 调用解析得到的参数命名空间

    # 两个 full64 开关需要折叠成三态覆盖值，供计划构造函数直接消费。
    bool_full64_override: bool | None = True if args.full64 else False if args.no_auto_full64 else None  # 当前 CLI 对 `urg -full64` 的显式覆盖值

    # 先构建覆盖率计划，保证执行与非执行模式共享同一份计划事实。
    json_dict_plan = build_coverage_plan(  # 当前 CLI 生成的覆盖率执行计划
        args.workdir,  # 当前 CLI 指定的覆盖率工作目录
        metrics=args.metrics,  # 当前 CLI 传入的覆盖率指标列表
        report_dir=args.report_dir,  # 当前 CLI 指定的报告目录覆盖值
        full64=bool_full64_override,  # 当前 CLI 折叠后的 full64 三态覆盖值
    )

    # `--execute` 打开时真正运行 urg，否则只返回规划好的命令和工件事实。
    json_dict_cli_report = (  # 当前 CLI 最终要输出的结构化报告
        execute_coverage_plan(json_dict_plan, timeout=args.timeout) if args.execute else json_dict_plan  # 执行模式输出运行结果，否则输出计划本身
    )

    # JSON 模式遵循模块级 stdout 协议，直接输出单一 JSON 载荷供上层程序消费。
    if args.json:

        # 把完整结构化结果序列化到 stdout，供脚本链路直接读取。
        json.dump(json_dict_cli_report, sys.stdout, indent=2, sort_keys=True)

        # JSON 协议输出末尾补一个换行，避免 shell 提示符直接拼到载荷尾部。
        sys.stdout.write("\n")

    # 非 JSON 模式只输出带前缀的精简命令摘要，避免终端直接刷出结构化载荷。
    else:

        # 先把命令数组压平成终端摘要文本，避免 print 直接消费结构化字段。
        str_cmd_summary = " ".join(json_dict_cli_report["cmd"])  # 当前 CLI 要展示的 urg 命令摘要文本

        # 向终端报告最终 urg 命令行，方便人工快速确认 `-full64` 与路径拼接结果。
        print(f"> INFO: [Python] urg command: {str_cmd_summary}")

    # 真正执行且状态不是 passed 时，需要把下游 urg 返回码透传给调用方。
    if args.execute and json_dict_cli_report.get("status") != "passed":

        # 缺失返回码时保守回退到 `1`，确保外层门禁能感知失败。
        return int(json_dict_cli_report.get("returncode") or 1)

    # 计划模式或执行成功模式都以零退出，表示当前 CLI 没有阻断性失败。
    return 0

# 直接脚本执行时，统一从 `main()` 派生最终进程退出码。
if __name__ == "__main__":

    # 把 CLI 返回码转换成进程退出状态，保持脚本调用语义清晰稳定。
    raise SystemExit(main())
