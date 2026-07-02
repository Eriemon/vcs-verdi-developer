#!/usr/bin/env python3
"""运行 URG coverage 诊断矩阵并输出稳定摘要。

本模块负责围绕同一个 VDB 生成多组 URG/URG1 诊断变体，执行覆盖率报告命令，并把默认变体与全部尝试结果汇总成稳定 JSON。
stdout_protocol: json
当 CLI 使用 `--json` 时，stdout 输出单一 JSON 文本；否则仅输出最终状态字符串。
"""

# 延后注解求值，避免脚本级类型提示在导入阶段引入额外前向依赖。
from __future__ import annotations

# 标准库中的命令行、哈希与序列化能力。
import argparse
import hashlib
import json

# 宿主环境、正则、子进程、进程退出与轻量对象封装能力。
import os
import re
import subprocess
import sys
import time
from types import SimpleNamespace

# 路径对象与通用类型注解用于组织矩阵输入输出。
from pathlib import Path
from typing import Any

# 诊断矩阵固定展开四组 coverage metric 组合。
METRIC_SETS = (  # 变体矩阵按固定顺序遍历这些 metric 组合
    ("line",),  # 单独 line coverage 用于最小基线
    ("cond",),  # 单独 cond coverage 用于条件覆盖排查
    ("tgl",),  # 单独 tgl coverage 用于翻转覆盖排查
    ("line", "cond", "tgl"),  # 全量 metric 组合用于默认主观察变体
)

# 矩阵同时覆盖 wrapper 入口和直接 urg1 入口。
ENTRIES = ("urg", "urg1")  # 固定展开这两个执行入口

# full64 策略既要覆盖自动模式，也要覆盖强制模式。
FULL64_MODES = ("auto64", "force64")  # 固定展开这两种 full64 策略

# 交付与真值判断默认围绕这条主观察变体进行。
DEFAULT_VARIANT_NAME = "line+cond+tgl__urg__auto64"  # 默认主观察变体名称

# 这些失败原因允许在第二轮强制打开 malloc fallback。
MALLOC_RETRY_REASONS = {  # 命中这些原因时允许追加 malloc retry
    "urg_internal_ucapi_snpsmalloc_failure",  # UCAPI 或 snpsmalloc 相关内部失败
    "urg_internal_non_ucapi_failure",  # 栈追踪存在但尚未归类到 UCAPI 的内部失败
    "urg_internal_stack_trace_ucapi_patch_not_applicable",  # UCAPI patch 不适用导致的内部失败
}

# 截断过长输出尾部，避免 JSON 里携带过量日志内容。
def _tail_text(str_text: str, int_limit: int = 4000) -> str:
    """
    截断字符串尾部。

    参数：
    - str_text: 待截断的原始文本。
    - int_limit: 允许保留的最大字符数。

    返回：
    - 若文本长度未超过限制则返回原文；否则仅返回尾部 `int_limit` 个字符。

    异常：
    - 无显式异常；长度判断失败场景由 Python 字符串内建能力处理。
    """

    # 文本长度未超限时，直接保留原始内容即可。
    if len(str_text) <= int_limit:

        # 短文本不需要裁剪，原样返回即可。
        return str_text

    # 超长文本只保留尾部，方便聚焦最后一段错误上下文。
    return str_text[-int_limit:]

# 统一统计文件或目录下的普通文件数量。
def _count_regular_files(path_target: Path) -> int:
    """
    统计路径承载的普通文件数量。

    参数：
    - path_target: 待统计的文件或目录路径。

    返回：
    - 不存在路径返回 0；普通文件返回 1；目录路径递归统计内部普通文件数量。

    异常：
    - 无显式异常；不存在路径会稳定回退到 0。
    """

    # 路径不存在时不应计入任何工件数量。
    if not path_target.exists():

        # 缺失目标统一折算成 0，减少上层分支判断。
        return 0

    # 单文件路径本身就代表一个可计数工件。
    if path_target.is_file():

        # 文件目标不需要递归扫描，直接返回 1。
        return 1

    # 目录路径则递归统计所有普通文件。
    return sum(1 for path_item in path_target.rglob("*") if path_item.is_file())

# 为 `.mode64` 这类小文件生成稳定 SHA256 摘要。
def _sha256_file(path_target: Path) -> str:
    """
    计算单文件的 SHA256 摘要。

    参数：
    - path_target: 待计算摘要的文件路径。

    返回：
    - 文件不存在或不是普通文件时返回空字符串；否则返回十六进制 SHA256 摘要。

    异常：
    - 无显式异常；文件缺失场景会回退为空字符串。
    """

    # 只有真实存在的普通文件才值得计算摘要。
    if not path_target.exists() or not path_target.is_file():

        # 缺失或目录目标都不具备可比较的文件摘要。
        return ""

    # 返回文件内容的十六进制 SHA256，用于跨环境比对 mode64 标记。
    return hashlib.sha256(path_target.read_bytes()).hexdigest()

# 汇总 VDB 目录的存在性、mode64 标记和文件数量。
def _build_vdb_summary(path_vdb: Path) -> dict[str, Any]:
    """
    构造 VDB 摘要字典。

    参数：
    - path_vdb: 待汇总的 VDB 目录路径。

    返回：
    - 返回写入矩阵结果的 VDB 摘要字典，包含路径、存在性、`.mode64` 标记和文件数量。

    异常：
    - 无显式异常；缺失路径会稳定回退到空摘要字段。
    """

    # 先准备 `.mode64` 标记文件路径，供多个字段复用。
    path_mode64 = path_vdb / ".mode64"  # 当前 VDB 目录下的 `.mode64` 标记文件路径

    # 返回统一的 VDB 摘要结构，供主矩阵结果直接挂载。
    return {
        "path": str(path_vdb),
        "exists": path_vdb.exists(),
        "has_mode64": path_mode64.exists(),
        "file_count": _count_regular_files(path_vdb),
        "mode64_sha256": _sha256_file(path_mode64),
    }

# 从 URG 输出里提取直接 urg1 打印的原始命令行。
def _extract_urg1_command_line(str_output: str) -> str:
    """
    提取 URG 输出中的命令行记录。

    参数：
    - str_output: 合并后的 stdout/stderr 文本。

    返回：
    - 找到 `Command line:` 记录时返回其去首尾空白后的内容；否则返回空字符串。

    异常：
    - 无显式异常；未匹配场景会稳定回退为空字符串。
    """

    # 先按固定格式匹配命令行记录，便于直接 urg1 场景溯源。
    obj_match = re.search(r"(?m)^Command line:\s*(.+)$", str_output)  # 输出里匹配到的命令行正则结果

    # 只有成功匹配时才返回命令行文本。
    if obj_match:

        # 命令行记录需要去掉首尾空白后再写入结果。
        return obj_match.group(1).strip()

    # 未出现命令行记录时回退为空字符串。
    return ""

# 根据合并日志内容把失败归类成稳定原因码。
def _classify_variant_output(
    str_output: str,
    *,
    str_entry: str,
    dict_ucapi_scan: dict[str, Any] | None = None,
) -> str:
    """
    把单次 URG 输出归类成稳定失败原因码。

    参数：
    - str_output: 合并后的 stdout/stderr 文本。
    - str_entry: 当前变体的执行入口名称。
    - dict_ucapi_scan: 可选 UCAPI 扫描结果，用于区分 patch 是否适用。

    返回：
    - 返回稳定失败原因码；未命中特定模式时回退到通用 `urg_failed`。

    异常：
    - 无显式异常；缺失 UCAPI 扫描输入会回退到通用内部失败分类。
    """

    # 许可证缺失优先级最高，因为这类失败无需继续分析内部日志。
    if "Error-[URG-NLC]" in str_output or "No license key" in str_output:

        # 明确许可证缺失后，直接返回统一许可证失败原因。
        return "urg_license_missing"

    # 栈追踪或 ptrace 被拒绝时，需要结合 UCAPI 扫描结果细分原因。
    if "Stack trace follows" in str_output or "ptrace: Operation not permitted" in str_output:

        # UCAPI 未命中时，说明 patch 不适用而不是普通内部故障。
        if (dict_ucapi_scan or {}).get("status") == "no_match":

            # 扫描明确 no_match 时返回 patch 不适用原因。
            return "urg_internal_stack_trace_ucapi_patch_not_applicable"

        # 其余栈追踪场景统一视为非 UCAPI 的内部失败。
        return "urg_internal_non_ucapi_failure"

    # 缺少 ncurses 依赖时，应该归类为运行时库缺失。
    if "libncursesw.so.5" in str_output:

        # 运行时依赖库缺失要单独归类，避免与普通内部故障混淆。
        return "urg_runtime_missing_libncurses"

    # UCAPI 或 snpsmalloc 相关动态库痕迹说明进入了对应内部失败路径。
    if "libucapi.so" in str_output or "libsnpsmalloc.so" in str_output:

        # 命中 UCAPI 或 snpsmalloc 痕迹时返回专门原因码。
        return "urg_internal_ucapi_snpsmalloc_failure"

    # wrapper 找不到依赖或子程序时，urg1 入口通常表现为 loader 失败。
    if "not found" in str_output and str_entry == "urg1":

        # 直接 urg1 入口的 loader 故障要单独暴露出来。
        return "urg_wrapper_loader_failure"

    # 其余未识别场景统一回退到通用失败码。
    return "urg_failed"

# 根据 entry 与 full64 策略构造单个矩阵变体计划。
def _build_variant_plan(
    obj_context: SimpleNamespace,
    tuple_metrics: tuple[str, ...],
    str_entry: str,
    str_full64_mode: str,
) -> dict[str, Any]:
    """
    构造单个 URG 变体执行计划。

    参数：
    - obj_context: 当前矩阵共享的变体构造上下文。
    - tuple_metrics: 当前变体使用的 metric 组合。
    - str_entry: 当前变体使用的执行入口。
    - str_full64_mode: 当前变体使用的 full64 策略。

    返回：
    - 返回单个变体计划字典，包含名称、命令、报告目录与局部环境覆盖。

    异常：
    - 无显式异常；输入路径仅被拼装，不在本函数中执行 IO 校验。
    """

    # 先把 metric 组合折算成命令行参数和稳定键名。
    str_metric_arg = "+".join(tuple_metrics)  # 当前变体的 `-metric` 参数文本

    # 再根据命名规范拼装当前变体的稳定名称。
    str_variant_name = f"{str_metric_arg}__{str_entry}__{str_full64_mode}"  # 当前变体的稳定名称

    # 然后准备当前变体专属报告目录。
    path_report_dir = obj_context.path_root / "urg_matrix" / str_variant_name  # 当前变体的报告输出目录

    # 再根据入口类型选择 wrapper 还是直接 urg1 可执行文件。
    if str_entry == "urg":

        # wrapper 入口优先复用标准 urg 可执行文件。
        str_executable = str(obj_context.path_vcs_home / "bin" / "urg")  # 当前变体调用的 wrapper 入口路径

    # 直连入口需要绕过 wrapper，直接指向 linux64/bin/urg1。
    else:

        # 直连入口要绕过 wrapper，直接调用 linux64/bin/urg1。
        str_executable = str(obj_context.path_vcs_home / "linux64" / "bin" / "urg1")  # 当前变体调用的 urg1 直连入口路径

    # 先准备基础命令列表，后续再按策略追加参数。
    list_command = [str_executable]  # 当前变体的基础命令列表

    # force64 或 auto64 且 VDB 自带 mode64 时，需要显式加上 `-full64`。
    if str_full64_mode == "force64" or (str_full64_mode == "auto64" and obj_context.bool_vdb_has_mode64):

        # 当前变体需要启用 64 位模式，因此把 `-full64` 写入命令。
        list_command.append("-full64")

    # 再补齐当前变体固定需要的 metric、VDB 和报告目录参数。
    list_command.extend(["-metric", str_metric_arg, "-dir", str(obj_context.path_vdb), "-report", str(path_report_dir)])

    # 默认先准备空环境覆盖，只有 urg1 直连入口才需要补库路径。
    dict_env: dict[str, str] = {}  # 当前变体专属环境覆盖字典

    # 直连 urg1 时要把 linux64/lib 置于 LD_LIBRARY_PATH 前面。
    if str_entry == "urg1":

        # 先取出宿主环境原有的库搜索路径，后面要把 vendor 目录插到它前面。
        str_base_library_path = obj_context.dict_base_env.get("LD_LIBRARY_PATH", "")  # 宿主环境已有的 LD_LIBRARY_PATH 文本

        # 直连入口必须自己拼库路径前缀，否则绕过 wrapper 后容易直接在 loader 阶段失败。
        dict_env["LD_LIBRARY_PATH"] = (  # urg1 直连时最终写入环境的库搜索路径
            obj_context.str_direct_lib  # vendor linux64/lib 要放在最前面优先命中
            + os.pathsep  # 当前平台使用的路径分隔符
            + str_base_library_path  # 宿主环境已有的库搜索路径要完整保留在后面
        )

    # 先复制出当前变体计划字典，后续逐项写入字段，避免大块字面量降低可读性。
    dict_variant_plan: dict[str, Any] = {}  # 当前构造出的单个变体计划字典

    # 名称字段既是默认主观察项查找键，也是后续结果归档的稳定索引。
    dict_variant_plan["name"] = str_variant_name  # 默认主观察项和归档记录共用的唯一变体键

    # 记录当前变体的 metric 列表，供上游结果展示与调试复用。
    dict_variant_plan["metrics"] = list(tuple_metrics)  # 当前变体展开后的 metric 列表

    # 这个字段直接复刻 `-metric` 入参文本，便于日志复盘时原样重放命令。
    dict_variant_plan["metrics_arg"] = str_metric_arg  # 复跑命令时可直接回填给 URG `-metric`

    # 写入执行入口类型，便于区分 wrapper 与 urg1 直连结果。
    dict_variant_plan["entry"] = str_entry  # 当前变体使用的执行入口

    # 写入 full64 策略，便于排查 auto64 与 force64 的差异。
    dict_variant_plan["full64_mode"] = str_full64_mode  # 当前变体的 full64 策略

    # 保存最终命令列表，供实际执行与结果回显直接复用。
    dict_variant_plan["cmd"] = list_command  # 当前变体最终执行的命令列表

    # 保存报告目录路径，供报告计数与调试结果定位复用。
    dict_variant_plan["report_dir"] = str(path_report_dir)  # 当前变体的报告目录路径

    # 局部环境覆盖专门暴露 urg1 直连入口的库路径修正，便于排查 wrapper 差异。
    dict_variant_plan["env"] = dict_env  # 仅保留当前变体需要覆写的局部环境差异

    # 返回单个变体计划，供构造完整矩阵时直接追加。
    return dict_variant_plan

# 构造所有 URG 诊断矩阵变体。
def build_variants(
    *,
    workdir: Path | str,
    vdb: Path | str,
    vcs_home: Path | str,
) -> list[dict[str, Any]]:
    """
    构造完整 URG 诊断矩阵计划。

    参数：
    - workdir: 矩阵执行工作目录。
    - vdb: 待分析的 VDB 目录。
    - vcs_home: VCS 安装根目录。

    返回：
    - 返回按固定顺序展开的变体计划列表。

    异常：
    - 无显式异常；路径输入会被规范化为 `Path` 但不在本函数中执行存在性校验。
    """

    # 先把路径输入规范化为 `Path`，避免后续混用字符串路径。
    path_root = Path(workdir)  # 当前矩阵工作目录路径对象

    # 再把 VDB 输入规范化为 `Path`，便于后续拼接 `.mode64`。
    path_vdb = Path(vdb)  # 当前矩阵目标 VDB 路径对象

    # 然后把 VCS_HOME 输入规范化为 `Path`，便于拼接 bin 与 lib 子目录。
    path_vcs_home = Path(vcs_home)  # 当前矩阵使用的 VCS 安装根目录路径对象

    # 接着准备最终返回的变体列表。
    list_variants: list[dict[str, Any]] = []  # 按固定顺序累积的变体计划列表

    # 再判断 VDB 是否带有 `.mode64` 标记，供 auto64 策略复用。
    bool_vdb_has_mode64 = (path_vdb / ".mode64").exists()  # 输入 VDB 是否存在 `.mode64` 标记

    # 再复制当前宿主环境，供 urg1 直连入口拼接库路径时参考。
    dict_base_env = dict(os.environ)  # 构造 urg1 环境覆盖时参考的宿主环境快照

    # 最后准备 urg1 直连入口固定需要追加的 linux64/lib 路径。
    str_direct_lib = str(path_vcs_home / "linux64" / "lib")  # urg1 直连入口要补到最前面的库目录

    # 把跨全部变体共享的上下文打包，避免内层 helper 继续扩张参数列表。
    obj_context = SimpleNamespace(  # 构造单个变体时复用的共享上下文对象
        path_root=path_root,  # 整张矩阵共享的工作目录
        path_vdb=path_vdb,  # 整张矩阵共享的目标 VDB 路径
        path_vcs_home=path_vcs_home,  # 整张矩阵共享的 VCS_HOME 路径
        bool_vdb_has_mode64=bool_vdb_has_mode64,  # auto64 策略是否应补 `-full64`
        dict_base_env=dict_base_env,  # 直连 urg1 入口拼接库路径时参考的宿主环境快照
        str_direct_lib=str_direct_lib,  # 直连 urg1 时要插到前面的 linux64/lib 目录
    )

    # 先遍历全部 metric 组合，确保矩阵覆盖 line、cond、tgl 与全量组合。
    for tuple_metrics in METRIC_SETS:

        # 再展开 wrapper 与直接 urg1 两种入口。
        for str_entry in ENTRIES:

            # 最后展开 auto64 与 force64 两种 full64 策略。
            for str_full64_mode in FULL64_MODES:

                # 先构造当前循环位置的单个变体计划，便于必要时单步调试。
                dict_variant_plan = _build_variant_plan(  # 当前循环位置构造出的单个变体计划
                    obj_context,  # 当前矩阵共享的变体构造上下文
                    tuple_metrics=tuple_metrics,  # 当前变体展开使用的 metric 组合
                    str_entry=str_entry,  # 当前变体展开使用的执行入口
                    str_full64_mode=str_full64_mode,  # 当前变体展开使用的 full64 策略
                )

                # 再按固定顺序把该变体计划追加到结果列表。
                list_variants.append(dict_variant_plan)

    # 返回完整矩阵计划列表，供实际执行阶段逐项运行。
    return list_variants

# 执行单次 URG 变体尝试并汇总 stdout/stderr/report 状态。
def _run_single_variant_attempt(
    dict_variant: dict[str, Any],
    *,
    path_workdir: Path,
    int_timeout: int,
    dict_env: dict[str, str],
    dict_ucapi_scan: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    执行单个变体的一次尝试。

    参数：
    - dict_variant: 当前变体计划字典。
    - path_workdir: 命令执行工作目录。
    - int_timeout: 单次执行超时时间，单位秒。
    - dict_env: 本次执行使用的完整环境字典。
    - dict_ucapi_scan: 可选 UCAPI 扫描结果。

    返回：
    - 返回包含退出码、状态、耗时、日志尾部和报告目录状态的结果字典。

    异常：
    - 无显式异常；超时由 `TimeoutExpired` 捕获并折算成结构化结果。
    """

    # 先准备本次尝试的报告目录路径，供命令执行前后复用。
    path_report_dir = Path(dict_variant["report_dir"])  # 当前变体的报告目录路径对象

    # 再记录单次尝试的起始时间，便于最终输出稳定耗时。
    float_started = time.monotonic()  # 当前单次尝试的起始单调时间

    # 再尝试运行外部 URG 命令，并把常见失败折算成结构化字段。
    try:

        # 先执行外部 URG 命令，保留完整 stdout/stderr 供后续分类使用。
        completed_process_result: subprocess.CompletedProcess[str] = subprocess.run(  # 执行当前变体命令并捕获完整输出
            dict_variant["cmd"],  # 当前变体最终要执行的命令列表
            cwd=path_workdir,  # 当前变体执行时切换到的工作目录
            env=dict_env,  # 当前变体本次尝试使用的完整环境字典
            text=True,  # 子进程输出按文本模式解码
            stdout=subprocess.PIPE,  # stdout 需要完整捕获到内存供后续归因使用
            stderr=subprocess.PIPE,  # stderr 需要保留 loader 与 stack trace 文本供诊断使用
            timeout=int_timeout,  # 单次变体执行允许的最长时长
        )  # 当前变体的子进程执行结果对象

        # stdout 主要承载 URG 常规输出与 dashboard 生成提示，因此先单独规范化。
        str_stdout = completed_process_result.stdout or ""  # 当前变体捕获到的 stdout 文本

        # stderr 更容易携带 loader、stack trace 与库缺失文本，因此单独保留。
        str_stderr = completed_process_result.stderr or ""  # 保留 loader、stack trace 与库缺失相关 stderr 文本

        # 然后记录原始退出码，供上层判断是否真正通过。
        int_returncode = completed_process_result.returncode  # 当前变体外部进程的退出码

        # 再把 stdout/stderr 合并，供失败归因与命令行提取共用。
        str_combined_output = str_stdout + str_stderr  # 当前变体合并后的输出文本

        # 再统计报告目录里的工件数量，避免只凭退出码判断成功。
        int_report_count = _count_regular_files(path_report_dir)  # 当前变体报告目录内的普通文件数量

        # 退出码为 0 且报告目录非空时，才认为该变体真正通过。
        str_status = "passed" if int_returncode == 0 and int_report_count > 0 else "failed"  # 当前变体单次尝试的结构化状态

        # 通过时直接返回 passed，否则基于合并输出归类失败原因。
        str_reason = (  # 当前变体单次尝试的结构化原因码
            "passed"  # 通过场景直接使用固定原因码
            if str_status == "passed"  # 通过场景无需再进入失败归因逻辑
            else _classify_variant_output(  # 失败场景才需要根据输出文本继续归因
                str_combined_output,  # 合并后的输出文本作为失败归因输入
                str_entry=dict_variant["entry"],  # 当前失败变体实际使用的执行入口
                dict_ucapi_scan=dict_ucapi_scan,  # 当前失败归因参考的 UCAPI 扫描结果
            )
        )

    # 子进程超时时要把异常对象折算成统一结构化结果。
    except subprocess.TimeoutExpired as obj_exc:

        # 超时时仍要保留已捕获输出，便于上层观察挂死前的日志尾部。
        str_stdout = obj_exc.stdout or ""  # 超时异常里携带的 stdout 文本

        # stderr 缺失时补固定超时提示，避免结果原因过于空泛。
        str_stderr = obj_exc.stderr or f"timeout after {int_timeout}s"  # 超时异常里携带的 stderr 或回退提示

        # 超时不再暴露整数退出码，而是显式置空。
        int_returncode = None  # 超时场景下的返回码回退值

        # 即使超时也要统计已生成的报告文件数量，辅助判断生成进度。
        int_report_count = _count_regular_files(path_report_dir)  # 超时场景下报告目录内的普通文件数量

        # 状态字段要显式写成 timeout，避免上游把它当成普通 failed。
        str_status = "timeout"  # 超时场景专用状态值

        # 原因字段保留固定超时文本，便于 shell 门禁直接做字符串判断。
        str_reason = f"timeout after {int_timeout}s"  # 超时场景专用原因文本

        # 超时也保留合并输出，便于提取命令行与日志尾部。
        str_combined_output = str_stdout + str_stderr  # 超时场景下合并后的输出文本

    # 先复制原始变体计划字段，保证执行结果保留原命令与报告目录信息。
    dict_attempt_result = dict(dict_variant)  # 本次尝试最终返回的结构化结果字典

    # 写入原始退出码，便于调用方区分失败、通过与超时。
    dict_attempt_result["returncode"] = int_returncode  # 本次尝试的外部进程退出码

    # 写入结构化状态，供矩阵级汇总直接判断 passed/failed/timeout。
    dict_attempt_result["status"] = str_status  # 本次尝试的结构化状态值

    # 写入稳定原因码，便于 shell/证据聚合层直接消费。
    dict_attempt_result["reason"] = str_reason  # 本次尝试的结构化原因码

    # 记录稳定耗时，避免直接暴露原始单调时间戳。
    dict_attempt_result["elapsed_sec"] = round(time.monotonic() - float_started, 3)  # 本次尝试的执行耗时，单位秒

    # 保留 stdout 尾部，便于后续回看 dashboard 生成提示或普通报错。
    dict_attempt_result["stdout_tail"] = _tail_text(str_stdout)  # 本次尝试的 stdout 尾部文本

    # stderr 尾部往往保留 loader、缺库或栈追踪的最后证据，因此单独截取出来。
    dict_attempt_result["stderr_tail"] = _tail_text(str_stderr)  # 末段 stderr 证据，优先用于缺库与栈追踪复盘

    # 额外提取 urg1 的命令行记录，供直连入口失败溯源使用。
    dict_attempt_result["urg1_command_line"] = _extract_urg1_command_line(str_combined_output)  # 从输出里提取到的 urg1 原始命令行

    # 报告目录是否存在要显式暴露，避免只看文件数量无法区分目录缺失。
    dict_attempt_result["report_exists"] = path_report_dir.exists()  # 本次尝试的报告目录是否存在

    # 最后写入报告文件数量，供矩阵级真值判定直接复用。
    dict_attempt_result["report_file_count"] = int_report_count  # 本次尝试报告目录内的普通文件数量

    # 返回本次尝试的完整结构化结果，供首轮或 fallback 结果直接复用。
    return dict_attempt_result

# 判断首轮失败是否值得再追加一次 malloc fallback。
def _should_retry_with_malloc(dict_result: dict[str, Any], dict_env: dict[str, str]) -> bool:
    """
    判断是否应追加 `VCS_USE_MALLOC=1` 的重试。

    参数：
    - dict_result: 首轮尝试结果字典。
    - dict_env: 首轮尝试使用的环境字典。

    返回：
    - 仅当首轮尚未启用 malloc、状态为 failed 且原因命中允许集合时返回 `True`。

    异常：
    - 无显式异常；缺失字段会回退到不重试。
    """

    # 已经显式启用 malloc 时，不应再次重复同样的 fallback。
    if dict_env.get("VCS_USE_MALLOC") == "1":

        # 当前环境已经带有 malloc fallback，直接禁止二次重试。
        return False

    # 只有普通失败状态才值得进入 malloc retry 判断。
    if dict_result.get("status") != "failed":

        # 通过、超时或其他状态都不应再进入 malloc fallback。
        return False

    # 首轮失败原因命中允许集合时，才执行 malloc fallback。
    return str(dict_result.get("reason", "")) in MALLOC_RETRY_REASONS

# 运行单个变体，并在必要时自动补一轮 malloc fallback。
def _run_variant(
    dict_variant: dict[str, Any],
    *,
    path_workdir: Path,
    int_timeout: int,
    dict_base_env: dict[str, str],
    dict_ucapi_scan: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    运行单个矩阵变体，并在特定失败原因下追加 malloc fallback。

    参数：
    - dict_variant: 当前变体计划字典。
    - path_workdir: 命令执行工作目录。
    - int_timeout: 单次执行超时时间，单位秒。
    - dict_base_env: 执行当前变体时的基础环境字典。
    - dict_ucapi_scan: 可选 UCAPI 扫描结果。

    返回：
    - 返回首轮结果或带 fallback 元数据的重试结果字典。

    异常：
    - 无显式异常；子进程异常由下层 helper 折算成结构化结果。
    """

    # 先复制基础环境，避免单个变体污染外层共享环境。
    dict_variant_env = dict_base_env.copy()  # 当前变体首轮尝试使用的完整环境字典

    # 再叠加当前变体自己的局部环境覆盖。
    dict_variant_env.update(dict_variant.get("env", {}))

    # 然后运行首轮尝试，先观察默认环境下的真实失败形态。
    dict_result = _run_single_variant_attempt(  # 当前变体首轮尝试的结构化结果
        dict_variant,  # 当前要执行的单个变体计划
        path_workdir=path_workdir,  # 本次执行使用的工作目录
        int_timeout=int_timeout,  # 首轮尝试沿用的单变体超时时间
        dict_env=dict_variant_env,  # 首轮尝试使用的完整环境字典
        dict_ucapi_scan=dict_ucapi_scan,  # 首轮失败归因参考的 UCAPI 扫描结果
    )

    # 首轮结果不满足 fallback 条件时，直接把它当作最终结果返回。
    if not _should_retry_with_malloc(dict_result, dict_variant_env):

        # 首轮状态已经足够说明问题，无需再做 malloc fallback。
        return dict_result

    # 再复制一份环境，并显式开启 malloc fallback。
    dict_retry_env = dict_variant_env.copy()  # 当前变体第二轮尝试使用的环境字典

    # `VCS_USE_MALLOC=1` 是本模块唯一允许的自动 fallback 开关。
    dict_retry_env["VCS_USE_MALLOC"] = "1"  # 第二轮尝试显式启用的 malloc fallback 开关

    # 然后执行第二轮尝试，验证 malloc fallback 是否能恢复默认主线。
    dict_retry = _run_single_variant_attempt(  # 当前变体开启 malloc fallback 后的结构化结果
        dict_variant,  # 当前要使用 malloc fallback 重跑的变体计划
        path_workdir=path_workdir,  # 第二轮尝试沿用同一个工作目录
        int_timeout=int_timeout,  # 第二轮尝试沿用的单变体超时时间
        dict_env=dict_retry_env,  # 第二轮尝试使用的完整 fallback 环境字典
        dict_ucapi_scan=dict_ucapi_scan,  # 第二轮失败归因继续参考同一份 UCAPI 扫描结果
    )

    # 把 fallback 元数据一次性合并进第二轮结果，便于上层直接消费。
    dict_retry.update(
        {
            "fallback_applied": True,
            "fallback_env": {"VCS_USE_MALLOC": "1"},
            "initial_attempt": {
                "status": dict_result.get("status"),
                "reason": dict_result.get("reason"),
                "returncode": dict_result.get("returncode"),
                "stdout_tail": dict_result.get("stdout_tail", ""),
                "stderr_tail": dict_result.get("stderr_tail", ""),
            },
        }
    )

    # 返回带 fallback 元数据的第二轮结果。
    return dict_retry

# 按稳定优先级从完整矩阵结果中归纳总原因码。
def _matrix_reason(list_variants: list[dict[str, Any]]) -> str:
    """
    从全部变体结果中归纳矩阵级原因码。

    参数：
    - list_variants: 全部变体的执行结果列表。

    返回：
    - 若默认变体成功则返回 `passed`；否则按固定优先级返回矩阵级失败原因码。

    异常：
    - 无显式异常；空列表会稳定回退为 `coverage_vdb_invalid`。
    """

    # 先尝试找到默认主观察变体，供整体通过判定优先使用。
    dict_default_variant = next(  # 与默认名称匹配到的主观察变体结果字典
        (dict_item for dict_item in list_variants if dict_item["name"] == DEFAULT_VARIANT_NAME),  # 在全部变体结果里筛出默认主观察项
        {},
    )

    # 默认主观察变体成功且报告目录非空时，整张矩阵视为通过。
    if dict_default_variant.get("status") == "passed" and dict_default_variant.get("report_file_count", 0) > 0:

        # 默认主观察变体已经成功生成报告，因此矩阵整体通过。
        return "passed"

    # 变体列表为空通常意味着输入 VDB 或矩阵构造本身无效。
    if not list_variants:

        # 空矩阵没有可分析结果，统一回退为 VDB 无效。
        return "coverage_vdb_invalid"

    # 再收集全部非空原因码，供后续稳定优先级判断复用。
    set_reasons = {str(dict_item.get("reason", "")) for dict_item in list_variants}  # 全部变体结果里提取到的原因码集合

    # UCAPI patch 不适用的内部失败优先级最高。
    if "urg_internal_stack_trace_ucapi_patch_not_applicable" in set_reasons:

        # 该原因代表最关键的 vendor/host 阻断场景，应优先暴露。
        return "urg_internal_stack_trace_ucapi_patch_not_applicable"

    # 直接 urg1 loader 失败排在第二层优先级。
    if "urg_wrapper_loader_failure" in set_reasons:

        # wrapper loader 失败通常比普通内部失败更利于快速定位环境问题。
        return "urg_wrapper_loader_failure"

    # UCAPI 或 snpsmalloc 内部失败排在第三层优先级。
    if "urg_internal_ucapi_snpsmalloc_failure" in set_reasons:

        # 命中 UCAPI/snpsmalloc 相关内部失败时，直接返回该专门原因。
        return "urg_internal_ucapi_snpsmalloc_failure"

    # 其后才是一般性的非 UCAPI 内部失败。
    if "urg_internal_non_ucapi_failure" in set_reasons:

        # 栈追踪存在但未归因到 UCAPI 时，统一返回普通内部失败原因。
        return "urg_internal_non_ucapi_failure"

    # 最后按字典序挑选首个非空原因码，确保回退行为稳定可比。
    return sorted(set_reasons - {""})[0] if set_reasons - {""} else "coverage_urg_failed"

# 运行完整 URG 诊断矩阵，并汇总默认变体与全部尝试结果。
def run_matrix(
    *,
    workdir: Path | str,
    vdb: Path | str,
    vcs_home: Path | str,
    ucapi_scan: dict[str, Any] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """
    运行完整 URG 诊断矩阵。

    参数：
    - workdir: 矩阵执行工作目录。
    - vdb: 待分析的 VDB 目录。
    - vcs_home: VCS 安装根目录。
    - ucapi_scan: 可选 UCAPI 扫描结果。
    - timeout: 单个变体执行超时时间，单位秒。

    返回：
    - 返回矩阵级结构化结果，包含总体状态、总原因、默认变体、全部变体和 VDB 摘要。

    异常：
    - 无显式异常；子进程失败由下层 helper 折算成结构化结果。
    """

    # 先规范化工作目录路径，避免结果里混入相对路径歧义。
    path_workdir = Path(workdir)  # 当前矩阵执行工作目录路径对象

    # 再规范化 VDB 路径，这一步专门服务 `.mode64` 检查与报告文件计数。
    path_vdb = Path(vdb)  # 后续 `.mode64` 检查与摘要统计共用的 VDB 路径对象

    # 然后规范化 VCS_HOME 路径，这一步专门服务 bin 与 linux64/lib 子路径拼接。
    path_vcs_home = Path(vcs_home)  # 后续 bin 与 linux64/lib 拼接共用的 VCS_HOME 路径对象

    # 先构造完整变体计划列表，保证执行顺序稳定。
    list_variants = build_variants(workdir=path_workdir, vdb=path_vdb, vcs_home=path_vcs_home)  # 当前矩阵的完整变体计划列表

    # 再复制基础环境，供所有变体在同一宿主环境基线上展开。
    dict_base_env = os.environ.copy()  # 当前矩阵执行阶段共用的基础环境字典

    # 明确把 VCS_HOME 写入基础环境，保证下游命令可复现。
    dict_base_env["VCS_HOME"] = str(path_vcs_home)  # 当前矩阵执行时强制写入的 VCS_HOME

    # 再把 VCS bin 前缀插到 PATH 前面，确保优先解析目标工具链。
    dict_base_env["PATH"] = str(path_vcs_home / "bin") + os.pathsep + dict_base_env.get("PATH", "")  # 当前矩阵执行时优先解析的 PATH 前缀

    # 接着准备最终结果列表，按计划顺序逐个写入执行结果。
    list_results: list[dict[str, Any]] = []  # 当前矩阵全部变体的执行结果列表

    # 按固定变体顺序逐个运行，确保日志和输出结果可比较。
    for dict_variant in list_variants:

        # 先运行单个变体，保留中间结果便于逐项观察失败形态。
        dict_variant_result = _run_variant(  # 当前循环位置得到的单个变体执行结果
            dict_variant,  # 当前顺序执行的单个变体计划
            path_workdir=path_workdir,  # 整张矩阵统一复用的执行工作目录
            int_timeout=timeout,  # 当前矩阵统一使用的单变体超时时间
            dict_base_env=dict_base_env,  # 整张矩阵共享的基础环境字典
            dict_ucapi_scan=ucapi_scan,  # 当前矩阵共享的 UCAPI 扫描结果
        )

        # 再按计划顺序把该变体执行结果追加到总列表。
        list_results.append(dict_variant_result)

    # 再定位默认主观察变体，这里专门为最终 JSON 顶层 `default_variant` 字段服务。
    dict_default_variant = next(  # 最终矩阵结果里要直接暴露的默认主观察变体结果字典
        (dict_item for dict_item in list_results if dict_item["name"] == DEFAULT_VARIANT_NAME),  # 在全部执行结果里筛出默认主观察项
        {},
    )

    # 再按稳定优先级归纳矩阵级原因码。
    str_reason = _matrix_reason(list_results)  # 当前矩阵的总原因码

    # 先准备矩阵级结果字典，后续逐项写入字段，避免把大对象字面量挤在一处。
    dict_matrix_result: dict[str, Any] = {}  # 当前 CLI/上游要消费的矩阵级结构化结果字典

    # 先写入总体状态，供 CLI 与证据聚合层快速判断通过或失败。
    dict_matrix_result["status"] = "passed" if str_reason == "passed" else "failed"  # 当前矩阵的总体状态

    # 总原因码让上游可以直接映射矩阵结论，而不必重新扫描 16 个变体。
    dict_matrix_result["reason"] = str_reason  # 单值矩阵结论，供上游一跳判断整体状态

    # 记录工作目录路径，便于后续定位报告输出与中间产物。
    dict_matrix_result["workdir"] = str(path_workdir)  # 当前矩阵执行使用的工作目录路径

    # 记录 VCS_HOME，便于复核当前矩阵绑定到的工具链根目录。
    dict_matrix_result["vcs_home"] = str(path_vcs_home)  # 当前矩阵执行使用的 VCS_HOME 路径

    # 附带 VDB 摘要，供上游观察 `.mode64` 与目录完整性。
    dict_matrix_result["vdb"] = _build_vdb_summary(path_vdb)  # 当前矩阵输入 VDB 的摘要结构

    # 暴露默认主观察变体，供严格真值门禁直接判断主线结果。
    dict_matrix_result["default_variant"] = dict_default_variant  # 当前矩阵默认主观察变体的结果字典

    # 完整变体结果要整体挂出，方便 evidence 与调试脚本逐项对比入口和 metric 组合。
    dict_matrix_result["variants"] = list_results  # 逐条保留全部组合结果，供横向比较入口与 metric 差异

    # 返回矩阵级完整结构化结果，供 CLI 与上游 evidence 聚合直接消费。
    return dict_matrix_result

# 读取可选的 UCAPI 扫描 JSON，缺失时回退为空字典。
def _load_ucapi_scan(path_ucapi_scan: Path | None) -> dict[str, Any]:
    """
    读取可选 UCAPI 扫描 JSON。

    参数：
    - path_ucapi_scan: 可选 UCAPI 扫描结果路径。

    返回：
    - 文件存在时返回解析后的 JSON 字典；否则返回空字典。

    异常：
    - 无显式异常；缺失路径会稳定回退为空字典。
    """

    # 未提供路径时直接回退为空字典，表示没有 UCAPI 扫描上下文。
    if path_ucapi_scan is None:

        # 缺失输入时不阻断矩阵执行，直接返回空字典。
        return {}

    # 文件不存在时同样回退为空字典，避免 CLI 因可选输入失败。
    if not path_ucapi_scan.exists():

        # 可选扫描文件缺失时不抛错，保持行为稳定。
        return {}

    # 读取并解析 UTF-8 JSON，供失败归因逻辑进一步细分。
    return json.loads(path_ucapi_scan.read_text(encoding="utf-8"))

# 注册 CLI 参数，保持 main 逻辑只负责调度。
def _register_cli_args(cli_parser: argparse.ArgumentParser) -> None:
    """
    注册当前脚本的 CLI 参数。

    参数：
    - cli_parser: 待写入参数定义的 `ArgumentParser`。

    返回：
    - 无返回值；参数定义直接写入传入的解析器对象。

    异常：
    - 无显式异常；参数冲突由 `argparse` 自身处理。
    """

    # 工作目录决定报告目录与子进程执行位置，因此是必填项。
    cli_parser.add_argument("--workdir", type=Path, required=True)

    # VDB 是矩阵分析的核心输入，因此同样必须显式提供。
    cli_parser.add_argument("--vdb", type=Path, required=True)

    # VCS_HOME 决定 urg/urg1 与库路径来源，因此必须显式提供。
    cli_parser.add_argument("--vcs-home", type=Path, required=True)

    # UCAPI 扫描文件仅用于细化失败原因，因此保持可选。
    cli_parser.add_argument("--ucapi-scan", type=Path)

    # timeout 控制单个变体最大运行时长，避免 URG 长时间挂死。
    cli_parser.add_argument("--timeout", type=int, default=120)

    # json 模式输出完整结构化结果，供上游 evidence 聚合直接消费。
    cli_parser.add_argument("--json", action="store_true")

# CLI 入口负责解析参数、运行矩阵并输出文本或 JSON。
def main() -> int:
    """
    执行命令行入口。

    参数：
    - 无显式参数；所有输入均从 CLI 解析得到。

    返回：
    - 矩阵通过时返回 0，否则返回 1。

    异常：
    - 无显式异常；参数解析和子进程失败均折算为返回码与结构化输出。
    """

    # 先创建参数解析器，统一描述当前脚本的用途。
    argument_parser_cli_parser: argparse.ArgumentParser = argparse.ArgumentParser(  # 当前脚本的 CLI 参数解析器
        description="Run URG coverage diagnostic variants against an existing VDB.",  # CLI 顶层帮助文本
    )

    # 再注册全部 CLI 参数，避免 main 里塞入过多参数定义细节。
    _register_cli_args(argument_parser_cli_parser)

    # 然后解析用户输入，供矩阵执行阶段直接消费。
    namespace_cli_args: argparse.Namespace = argument_parser_cli_parser.parse_args()  # 当前脚本解析得到的 CLI 参数对象

    # 再读取可选 UCAPI 扫描 JSON，供失败归因逻辑补充上下文。
    dict_ucapi_scan = _load_ucapi_scan(namespace_cli_args.ucapi_scan)  # 当前矩阵使用的 UCAPI 扫描结果字典

    # 接着执行完整矩阵，得到可直接输出的结构化结果。
    dict_result = run_matrix(  # 当前 CLI 调用得到的矩阵结构化结果
        workdir=namespace_cli_args.workdir,  # 当前 CLI 指定的矩阵工作目录
        vdb=namespace_cli_args.vdb,  # 当前 CLI 指定的 VDB 目录
        vcs_home=namespace_cli_args.vcs_home,  # 当前 CLI 指定的 VCS 安装根目录
        ucapi_scan=dict_ucapi_scan,  # 当前 CLI 传入的 UCAPI 扫描结果
        timeout=namespace_cli_args.timeout,  # 当前 CLI 指定的单变体超时时间
    )

    # JSON 模式下输出完整结果，供上游脚本直接反序列化。
    if namespace_cli_args.json:

        # JSON 模式直接写 stdout，避免额外提示前缀破坏结构化协议。
        sys.stdout.write(json.dumps(dict_result, indent=2, sort_keys=True) + "\n")

    # 纯文本模式只输出带前缀的简短摘要，满足 current-project CLI 协议。
    else:

        # 人类可读模式只暴露简短状态摘要，避免 stdout 混入结构化大对象。
        sys.stdout.write(f'> INFO: [Python] matrix_status={dict_result["status"]}\n')

    # 最终返回码只由矩阵总体状态决定。
    return 0 if dict_result["status"] == "passed" else 1

# 仅在脚本直接执行时进入 CLI 入口。
if __name__ == "__main__":

    # 直接执行脚本时把 CLI 返回码转换成进程退出状态。
    raise SystemExit(main())
