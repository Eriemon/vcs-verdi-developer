#!/usr/bin/env python3
"""URG 故障排查循环与根因归类辅助。

本模块负责加载 URG 排查尝试矩阵、构建多组排查命令、执行远端诊断循环，并把失败特征归类成稳定的结构化报告。
stdout_protocol: json
当 CLI 使用 `--json` 时，stdout 输出单一 JSON 文本，供上层脚本或 skill 直接消费。
"""

# 延后注解求值，避免脚本级类型提示引入运行期前向定义依赖。
from __future__ import annotations

# 标准库中的命令行、序列化、环境变量与进程控制能力。
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time

# 路径对象与通用类型注解用于约束 CLI 和诊断结果结构。
from pathlib import Path
from typing import Any

# 当前脚本的尝试矩阵配置来自同目录 JSON 文件，方便独立治理排查组合。
ATTEMPT_CONFIG_PATH = Path(__file__).with_name("urg_troubleshoot_attempts.json")  # URG 排查尝试矩阵配置文件路径

# 从同目录 JSON 载入 URG 排查尝试矩阵，供运行循环逐项复用。
def _load_attempt_definitions() -> tuple[dict[str, str], ...]:
    """
    读取 URG 排查尝试矩阵配置。

    参数：
    - 无业务参数；函数只依赖当前脚本同目录配置文件。

    返回：
    - 返回按顺序排列的尝试定义元组；每个元素都是字符串键值构成的尝试配置。

    异常：
    - 无显式异常；JSON 读取失败会沿用底层异常直接暴露，帮助安装或打包阶段尽早发现缺件。
    """

    # 先把 JSON 文本完整读入内存，避免后续逐段解析破坏配置错误的定位上下文。
    str_attempts_json = ATTEMPT_CONFIG_PATH.read_text(encoding="utf-8")  # 尝试矩阵配置文件的原始 JSON 文本

    # 再把 JSON 文本解析成 Python 列表，供后续转成不可变元组结构。
    list_attempts_raw = json.loads(str_attempts_json)  # 尝试矩阵配置解析得到的原始 Python 结构

    # 返回不可变元组，避免运行时意外修改排查组合顺序或字段内容。
    return tuple(dict(item) for item in list_attempts_raw if isinstance(item, dict))

# 导出的排查矩阵需要在模块加载后即可复用，测试也会直接读取它的长度。
ATTEMPTS = _load_attempt_definitions()  # 当前 URG 故障排查循环使用的尝试矩阵定义

# 只保留日志尾部窗口，避免执行结果携带不可控的大体积文本。
def _tail(text: str, limit: int = 4000) -> str:
    """
    截取文本尾部的固定窗口。

    参数：
    - text: 待截断的完整文本。
    - limit: 允许保留的最大字符数。

    返回：
    - 返回原文本尾部的固定窗口；较短文本保持原样返回。

    异常：
    - 无显式异常；长度窗口由调用方自行保证为合理值。
    """

    # 文本超长时只保留尾部窗口，兼顾排障价值和结果体大小控制。
    return text[-limit:] if len(text) > limit else text

# 统计报告目录内的普通文件数，供每次尝试的成功判定和报告摘要复用。
def _count_files(path_target: Path) -> int:
    """
    统计目录树里的普通文件数。

    参数：
    - path_target: 待统计的目录路径。

    返回：
    - 返回目录树下普通文件总数；路径不存在时返回 `0`。

    异常：
    - 无显式异常；缺失路径按空结果处理。
    """

    # 报告目录不存在时，直接把文件数视为零，避免把缺失目录误判成通过。
    if not path_target.exists():

        # 不存在的报告目录不能贡献任何有效文件。
        return 0

    # 目录存在时递归统计普通文件数量，作为“报告是否真正产出”的事实信号。
    return sum(1 for path_item in path_target.rglob("*") if path_item.is_file())

# 读取文本文件首行，供 shell 兼容性摘要与 wrapper 识别逻辑复用。
def _read_first_line(path_target: Path) -> str:
    """
    读取文件首行文本。

    参数：
    - path_target: 待读取的目标文件路径。

    返回：
    - 返回首行文本；文件为空时返回空字符串。

    异常：
    - 无显式异常；读取失败会沿用底层异常直接暴露。
    """

    # 先把文件读成按行列表，后续只消费首行而不重复触发磁盘读取。
    list_lines = path_target.read_text(encoding="utf-8", errors="ignore").splitlines()  # 目标文件读取到的文本行列表

    # 命中首行时返回真实 shebang 或 wrapper 文本，否则回退为空字符串。
    return list_lines[0] if list_lines else ""

# 把 vendor 提供的 `urg` wrapper 复制到工作目录并改成 bash shebang。
def _materialize_copied_bash_wrapper(*, workdir: Path, vendor_vcs_home: Path) -> Path:
    """
    生成工作目录内的 bash 兼容 urg wrapper 副本。

    参数：
    - workdir: 本次排查循环的工作目录。
    - vendor_vcs_home: vendor 版 VCS 安装根目录。

    返回：
    - 返回复制后 wrapper 的目标路径。

    异常：
    - 无显式异常；源 wrapper 缺失或不可写会沿用底层异常暴露。
    """

    # 先定位 vendor 安装树里的原始 urg wrapper，后续复制与改写都依赖它。
    path_source_wrapper = vendor_vcs_home / "bin" / "urg"  # vendor 安装树中的原始 urg wrapper 路径

    # 再规划工作目录下的 wrapper 落点，避免直接改写 vendor 安装树内容。
    path_target_dir = workdir / "urg_troubleshoot" / "vendor_copied_bash_wrapper" / "bin"  # 复制版 bash wrapper 的目标目录

    # 目标目录需要提前创建，确保后续写文件与 chmod 都有稳定落点。
    path_target_dir.mkdir(parents=True, exist_ok=True)

    # 最终复制出的 wrapper 文件统一命名为 `urg`，保持命令构造逻辑不变。
    path_target_wrapper = path_target_dir / "urg"  # 复制版 bash wrapper 的最终文件路径

    # 先读取原始 wrapper 的所有文本行，后续只重写 shebang 而不改动其他内容。
    list_wrapper_lines = path_source_wrapper.read_text(encoding="utf-8", errors="ignore").splitlines()  # 原始 urg wrapper 读取到的文本行列表

    # 原始 wrapper 至少有一行时，才把首行 shebang 改成 bash 兼容形式。
    if list_wrapper_lines:

        # 把首行替换成 bash shebang，绕开 `/bin/sh -h` 兼容性问题。
        list_wrapper_lines[0] = "#!/usr/bin/env bash"  # 复制版 wrapper 使用的 bash shebang

    # 把改写后的 wrapper 文本写入工作目录副本，供特定尝试路径调用。
    path_target_wrapper.write_text("\n".join(list_wrapper_lines) + "\n", encoding="utf-8")

    # 再补齐用户/组/其他执行位，确保复制出的 wrapper 可直接执行。
    path_target_wrapper.chmod(
        path_target_wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )

    # 返回复制版 wrapper 路径，让命令构造函数可以直接引用它。
    return path_target_wrapper

# 根据一次尝试的输出文本归类失败原因，供最终摘要与 root cause 诊断复用。
def _classify_failure(output: str, *, attempt: dict[str, str]) -> str:
    """
    把 URG 输出归类成稳定失败标签。

    参数：
    - output: 一次尝试的 stdout 与 stderr 合并文本。
    - attempt: 当前尝试定义字典，用于结合入口模式细化分类。

    返回：
    - 返回稳定的失败标签；未命中已知模式时回退到 `failed`。

    异常：
    - 无显式异常；未知输出只会得到保守的默认分类。
    """

    # 许可证报错优先归类成缺证标签，方便和宿主机问题区分。
    if "Error-[URG-NLC]" in output or "No license key" in output:

        # 当前输出已经明确指向 URG 许可证不可用。
        return "license_missing"

    # `/bin/sh -h` 不兼容是 vendor wrapper 的典型 shell 兼容性问题。
    if "/bin/sh: 0: Illegal option -h" in output:

        # 把 shell 兼容性失败单独拉成稳定分类标签。
        return "vendor_wrapper_shell_incompatible"

    # 缺少 `vcsMsgReport` 或 `VCS_HOME` 错误通常说明 bash wrapper 环境不完整。
    if "Cannot find 'vcsMsgReport' script in /bin" in output or "Please make sure VCS_HOME is set correctly" in output:

        # 当前失败更像 wrapper 环境不完整，而不是缺证或 loader 问题。
        return "vendor_bash_wrapper_env_incomplete"

    # 栈追踪或 ptrace 受阻说明 URG 进入了内部崩溃或宿主限制路径。
    if "Stack trace follows" in output or "ptrace: Operation not permitted" in output:

        # overlay wrapper 崩溃时要保留 wrapper 入口特征，便于后续对比。
        if attempt["entry"] == "overlay_wrapper":

            # 这里仍停留在 overlay wrapper 入口，后续应优先排查 wrapper 到二进制的桥接链路。
            return "overlay_wrapper_internal_crash"

        # 直接调用 `urg1` 崩溃时需要触发后续 gdb 探针。
        if attempt["entry"] == "overlay_urg1_direct":

            # 这里已经绕过 wrapper 仍然崩溃，说明故障更可能位于二进制内部或宿主环境。
            return "direct_urg1_internal_crash"

        # 其余栈追踪失败都先归到 vendor 或宿主机阻断类。
        return "vendor_or_host_blocked"

    # ncurses 缺失是典型的 loader 共享库问题，需要单独落类。
    if "libncursesw.so.5" in output:

        # 当前 loader 问题来自运行时共享库缺失。
        return "loader_missing_dependency"

    # `libucapi` 或 `libsnpsmalloc` 暴露时，同样归到 loader 依赖问题。
    if "libucapi.so" in output or "libsnpsmalloc.so" in output:

        # 当前输出已出现核心共享库名，说明 loader 或运行时装载链异常。
        return "loader_missing_dependency"

    # 兜底处理 shell 报错中的 `not found`，同样按依赖缺失看待。
    if "not found" in output:

        # 当前日志更像动态依赖、解释器或可执行体缺失。
        return "loader_missing_dependency"

    # 未命中任何已知模式时，回退到最保守的 failed 标签。
    return "failed"

# 为一次尝试构造最终命令数组，覆盖 wrapper、urg1 direct 与 loader 变体。
def _command_for_attempt(
    attempt: dict[str, str],
    workdir: Path,
    vendor_vcs_home: Path,
    overlay_vcs_home: Path,
    vdb: Path, report_dir: Path,
) -> list[str]:
    """
    根据尝试定义生成可执行命令数组。

    参数：
    - attempt: 当前尝试定义字典。
    - workdir: 本次排查循环的工作目录。
    - vendor_vcs_home: vendor 版 VCS 安装根目录。
    - overlay_vcs_home: overlay 版 VCS 安装根目录。
    - vdb: 当前待检查的覆盖率数据库目录。
    - report_dir: 当前尝试专属的报告输出目录。

    返回：
    - 返回交给 `subprocess.run` 的命令数组。

    异常：
    - 无显式异常；路径缺失或 wrapper 复制失败会沿用底层异常暴露。
    """

    # 先按 entry 选择 vendor 或 overlay 安装树，作为普通 wrapper 路径的基准。
    path_selected_home = vendor_vcs_home if attempt["entry"].startswith("vendor") else overlay_vcs_home  # 当前尝试绑定的安装根目录

    # direct urg1 路径需要跳过 wrapper，直接落到 overlay 的 `linux64/bin/urg1`。
    if attempt["entry"] == "overlay_urg1_direct":

        # 当前尝试显式要求绕开 wrapper，直接使用 urg1 可执行文件。
        str_executable = str(overlay_vcs_home / "linux64" / "bin" / "urg1")  # 当前尝试最终要执行的 urg1 路径文本

    # copied bash wrapper 路径需要先生成工作目录副本，再拿副本路径拼命令。
    elif attempt["shell_mode"] == "copied_bash_wrapper":

        # 当前尝试要求使用复制并改写 shebang 的 vendor wrapper 副本。
        path_copied_wrapper = _materialize_copied_bash_wrapper(workdir=workdir, vendor_vcs_home=vendor_vcs_home)  # 复制版 wrapper 的文件路径

        # 命令数组需要字符串形式的可执行路径，因此在这里完成 `Path -> str` 转换。
        str_executable = str(path_copied_wrapper)  # 当前尝试最终要执行的复制版 wrapper 路径文本

    # 其余路径沿用所选安装树下的标准 `bin/urg` wrapper。
    else:

        # 标准入口路径故意不改写 wrapper，便于对照 vendor 与 overlay 自带脚本差异。
        str_executable = str(path_selected_home / "bin" / "urg")  # 保留安装树自带 urg 包装脚本时的命令入口文本

    # 先写入命令首元素，后续再按尝试属性逐步追加参数。
    list_cmd = [str_executable]  # 当前尝试的基础命令数组

    # force64 模式要求显式追加 `-full64`，避免依赖 `.mode64` 自动判断。
    if attempt["full64"] == "force64":

        # 当前尝试需要强制使用 64 位 urg 路径。
        list_cmd.append("-full64")

    # 格式模式非空时追加 `-format` 参数，覆盖默认输出格式。
    if attempt["format_mode"]:

        # 当前尝试需要指定额外格式参数。
        list_cmd.extend(["-format", attempt["format_mode"]])

    # show 模式非空时追加 `-show` 参数，控制输出摘要形态。
    if attempt["show_mode"]:

        # 当前尝试需要指定额外的展示模式参数。
        list_cmd.extend(["-show", attempt["show_mode"]])

    # 基础命令最终总是带上 metric、vdb 和 report 路径。
    list_cmd.extend(["-metric", attempt["metrics"], "-dir", str(vdb), "-report", str(report_dir)])

    # 返回可直接执行的命令数组，供主循环调用。
    return list_cmd

# 从系统级 gdb 回溯里提取更细的根因签名。
def _classify_gdb_root_cause(output: str) -> str:
    """
    把 gdb 输出归类成稳定的根因标签。

    参数：
    - output: gdb 标准输出与标准错误合并后的文本。

    返回：
    - 返回稳定根因标签；未命中时返回空字符串。

    异常：
    - 无显式异常；未知回溯只会得到空根因标签。
    """

    # 命中 covdb 授权检出栈并伴随 snpsmalloc 时，说明是 ucapi 取证路径崩溃。
    if "covdb_get_license" in output and "scl_lc_checkout" in output and "libsnpsmalloc.so" in output:

        # 当前 gdb 栈清晰暴露了 ucapi 授权检出的 segv 根因。
        return "ucapi_license_checkout_segv"

    # 未命中精确模式时，不伪造根因签名。
    return ""

# 统一执行并捕获子进程输出，供普通尝试与 gdb 探针复用。
def _run_command_capture(
    *,
    list_cmd: list[str],
    path_cwd: Path,
    dict_env: dict[str, str],
    timeout: int,
    str_timeout_stderr: str,
) -> dict[str, Any]:
    """
    执行一次带超时的子进程并收集文本输出。

    参数：
    - list_cmd: 交给 `subprocess.run` 的命令数组。
    - path_cwd: 子进程运行时使用的工作目录。
    - dict_env: 子进程继承并覆盖后的环境变量字典。
    - timeout: 子进程允许的最大运行时长，单位秒。
    - str_timeout_stderr: 子进程超时时写入结果的标准错误占位文本。

    返回：
    - 返回包含返回码、stdout、stderr 与超时标记的结构化捕获结果。

    异常：
    - 无显式异常；超时会转成结构化结果而不是继续向外抛出。
    """

    # 先创建空的关键字参数字典，后续逐项补齐 subprocess 需要的捕获设置。
    dict_subprocess_kwargs: dict[str, Any] = {}  # 当前子进程执行共享的关键字参数集合

    # 工作目录决定命令解析到的相对路径与输出落点。
    dict_subprocess_kwargs["cwd"] = path_cwd  # 子进程执行工作目录

    # 环境变量字典承载 wrapper、loader 与架构覆盖值。
    dict_subprocess_kwargs["env"] = dict_env  # 子进程执行环境变量字典

    # 文本模式可以避免后续重复处理字节串解码逻辑。
    dict_subprocess_kwargs["text"] = True  # 以文本模式读取 stdout/stderr

    # 标准输出需要完整捕获下来，供日志尾部分析与失败签名识别复用。
    dict_subprocess_kwargs["stdout"] = subprocess.PIPE  # 捕获标准输出供日志尾部分析

    # 标准错误同样需要完整捕获，便于识别许可证、loader 与超时特征。
    dict_subprocess_kwargs["stderr"] = subprocess.PIPE  # 捕获标准错误供失败签名分析

    # 超时时长统一由调用方传入，保持普通尝试和 gdb 探针的门限可控。
    dict_subprocess_kwargs["timeout"] = timeout  # 限制单次子进程的最大运行时长

    # 正常路径直接执行子进程，并把 stdout/stderr 规整成稳定字符串字段。
    try:

        # 调用底层子进程执行器，生成一次完整的完成态或超时态捕获结果。
        process_completed = subprocess.run(list_cmd, **dict_subprocess_kwargs)  # 当前命令执行完成态进程对象

        # 先把标准输出规整成字符串，避免空值扰动后续日志裁剪与模式匹配。
        str_stdout = process_completed.stdout or ""  # 当前命令执行采集到的标准输出文本

        # 再把标准错误规整成字符串，保持返回结构字段类型始终一致。
        str_stderr = process_completed.stderr or ""  # 当前命令执行采集到的标准错误文本

        # 返回统一的结构化捕获结果，供上层诊断逻辑继续组合使用。
        return {
            "returncode": process_completed.returncode,
            "stdout": str_stdout,
            "stderr": str_stderr,
            "timed_out": False,
        }

    # 超时路径同样落成结构化结果，避免一次长阻塞中断整个排查闭环。
    except subprocess.TimeoutExpired as exc_timeout:

        # 超时前残留的标准输出通常仍有价值，因此要原样保留下来。
        str_stdout = exc_timeout.stdout or ""  # 当前超时命令保留下来的标准输出文本

        # 标准错误缺失时注入统一占位文本，帮助调用方识别这是超时而非静默失败。
        str_stderr = exc_timeout.stderr or str_timeout_stderr  # 当前超时命令的标准错误或超时占位文本

        # 超时结果没有稳定返回码，因此统一回填为 `None`。
        return {
            "returncode": None,
            "stdout": str_stdout,
            "stderr": str_stderr,
            "timed_out": True,
        }

# 在 direct urg1 崩溃场景下运行一次系统级 gdb 探针，补充更细粒度根因。
def _system_gdb_probe(*, cmd: list[str], cwd: Path, env: dict[str, str], timeout: int) -> dict[str, Any]:
    """
    执行一次系统级 gdb 探针。

    参数：
    - cmd: 当前 direct urg1 尝试对应的命令数组。
    - cwd: gdb 与 urg1 执行时使用的工作目录。
    - env: gdb 执行时沿用的环境变量字典。
    - timeout: gdb 允许的最大运行时长，单位秒。

    返回：
    - 返回 gdb 是否可用、返回码、日志尾部和根因签名等结构化摘要。

    异常：
    - 无显式异常；gdb 缺失或超时都会转成结构化结果返回。
    """

    # 先在当前 PATH 下查找 gdb，可用性本身就是一条诊断事实。
    str_gdb_path = shutil.which("gdb", path=env.get("PATH")) or ""  # 当前环境解析到的 gdb 可执行文件路径文本

    # 当前 PATH 里没有 gdb 时，直接返回“不可用”结果，避免伪造探针输出。
    if not str_gdb_path:

        # 缺少 gdb 时仍保持字段结构稳定，方便上层统一消费。
        return {"available": False, "root_cause_signature": ""}

    # 先放入 gdb 本体与静默批处理开关，确保诊断输出适合脚本采集。
    list_gdb_cmd = [str_gdb_path, "-q", "-batch"]  # gdb 启动基础参数数组

    # 再追加关闭分页与直接运行目标程序的控制命令，避免等待交互输入。
    list_gdb_cmd.extend(["-ex", "set pagination off", "-ex", "run"])  # gdb 运行控制参数

    # 最后追加全线程回溯与被调程序参数，确保 direct urg1 崩溃时能收集完整栈。
    list_gdb_cmd.extend(["-ex", "thread apply all bt", "--args", *cmd])  # gdb 回溯与目标程序参数

    # 统一执行 gdb 探针并捕获结果，避免普通尝试与 gdb 探针维护两套超时逻辑。
    dict_capture_result = _run_command_capture(  # 当前 gdb 探针的统一捕获结果
        list_cmd=list_gdb_cmd,  # gdb 批处理命令数组
        path_cwd=cwd,  # gdb 探针工作目录
        dict_env=env,  # gdb 探针执行环境变量
        timeout=timeout,  # gdb 探针最大运行时长
        str_timeout_stderr=f"\n[gdb-timeout-after-{timeout}s]",  # gdb 超时场景使用的标准错误标记
    )

    # 合并两路文本，供 gdb 根因签名函数做统一模式匹配。
    str_combined_output = dict_capture_result["stdout"] + dict_capture_result["stderr"]  # 当前 gdb 探针的合并诊断文本

    # 超时场景下只保留可用性与日志尾部事实，不额外伪造根因签名。
    if dict_capture_result["timed_out"]:

        # gdb 虽可用但本次探针超时，只返回尾部证据供上层判断。
        return {
            "available": True,
            "returncode": None,
            "stdout_tail": _tail(dict_capture_result["stdout"]),
            "stderr_tail": _tail(dict_capture_result["stderr"]),
            "root_cause_signature": "",
        }

    # 正常完成时保留 gdb 返回码、日志尾部与精确根因签名。
    return {
        "available": True,
        "returncode": dict_capture_result["returncode"],
        "stdout_tail": _tail(dict_capture_result["stdout"]),
        "stderr_tail": _tail(dict_capture_result["stderr"]),
        "root_cause_signature": _classify_gdb_root_cause(str_combined_output),
    }

# 把单次尝试的结果整理成统一结构，避免主循环重复拼接大字典常量。
def _build_attempt_report(
    dict_attempt: dict[str, str],
    dict_capture_result: dict[str, Any],
    dict_report_context: dict[str, Any],
) -> dict[str, Any]:
    """
    组装单次 URG 尝试的结构化报告。

    参数：
    - dict_attempt: 当前尝试定义字典。
    - dict_capture_result: 当前尝试的统一捕获结果，包含返回码、stdout、stderr 与超时标记。
    - dict_report_context: 当前尝试的补充上下文字典，包含命令数组、耗时起点、报告目录、失败签名与 gdb 结果。

    返回：
    - 返回供主循环直接追加的单次尝试结构化报告。

    异常：
    - 无显式异常；所有输入都应由调用方提前准备完成。
    """

    # 先把单次尝试耗时折算成固定三位小数，便于 JSON 报告稳定对比。
    float_elapsed_seconds = round(time.monotonic() - dict_report_context["float_started_monotonic"], 3)  # 当前尝试的执行耗时秒数

    # 再统一返回单次尝试报告，保持 CLI、门禁与单测消费的数据形状一致。
    return {
        **dict_attempt,
        "cmd": dict_report_context["list_cmd"],
        "returncode": dict_capture_result["returncode"],
        "elapsed_sec": float_elapsed_seconds,
        "report_exists": dict_report_context["path_report_dir"].exists(),
        "report_file_count": dict_report_context["int_report_file_count"],
        "failure_signature": dict_report_context["str_failure_signature"],
        "root_cause_signature": dict_report_context["dict_gdb_probe"].get("root_cause_signature", ""),
        "system_gdb": dict_report_context["dict_gdb_probe"],
        "stdout_tail": _tail(dict_capture_result["stdout"]),
        "stderr_tail": _tail(dict_capture_result["stderr"]),
    }

# 运行单次 URG 尝试并返回结构化结果，供主循环按固定顺序复用。
def _run_single_attempt(
    dict_attempt: dict[str, str],
    path_workdir: Path,
    path_vdb: Path,
    path_vendor_vcs_home: Path, path_overlay_vcs_home: Path,
    timeout: int,
) -> dict[str, Any]:
    """
    执行一次 URG 尝试并整理结构化报告。

    参数：
    - dict_attempt: 当前尝试定义字典。
    - path_workdir: 排查循环的绝对工作目录。
    - path_vdb: 待检查覆盖率数据库的绝对路径。
    - path_vendor_vcs_home: vendor 版 VCS 安装根目录绝对路径。
    - path_overlay_vcs_home: overlay 版 VCS 安装根目录绝对路径。
    - timeout: 当前尝试允许的最大运行时长，单位秒。

    返回：
    - 返回单次尝试的结构化执行报告。

    异常：
    - 无显式异常；子进程失败、超时与 gdb 缺失都会转成结构化结果。
    """

    # 每个尝试都使用独立报告目录，避免不同组合相互污染报告文件统计。
    path_report_dir = path_workdir / "urg_troubleshoot" / dict_attempt["name"]  # 当前尝试专属的报告输出目录

    # 每次尝试都从当前进程环境复制一份独立副本，再注入本次组合需要的覆盖变量。
    dict_env = os.environ.copy()  # 当前尝试使用的环境变量副本

    # entry 决定本次组合究竟沿用 vendor 原生工具，还是切到 overlay 补丁工具树。
    path_entry_home = path_vendor_vcs_home if dict_attempt["entry"].startswith("vendor") else path_overlay_vcs_home  # 本次尝试最终生效的 VCS 安装根目录

    # `VCS_HOME` 直接决定 wrapper 内部脚本和辅助资源的解析根目录。
    dict_env["VCS_HOME"] = str(path_entry_home)  # 当前尝试导出的 VCS_HOME 文本

    # arch override 非空时，显式把覆盖值注入环境变量，便于逼出额外兼容性路径。
    if dict_attempt["arch_override"]:

        # 当前尝试要求覆盖 `VCS_ARCH_OVERRIDE`，用于观察 loader 与 wrapper 的分支行为。
        dict_env["VCS_ARCH_OVERRIDE"] = dict_attempt["arch_override"]  # 当前尝试导出的架构覆盖值

    # direct urg1 模式需要补齐 overlay 动态库目录，否则二进制无法解析核心共享库。
    if dict_attempt["loader_mode"] == "direct_vcs_lib":

        # 先定位 overlay 的 `linux64/lib`，后续把它前置到 `LD_LIBRARY_PATH`。
        path_direct_lib = path_overlay_vcs_home / "linux64" / "lib"  # direct urg1 模式依赖的 overlay 动态库目录

        # 把 overlay 动态库目录放到搜索链最前面，确保 urg1 优先加载正确版本的共享库。
        dict_env["LD_LIBRARY_PATH"] = str(path_direct_lib) + os.pathsep + dict_env.get("LD_LIBRARY_PATH", "")  # 当前尝试导出的动态库搜索路径

    # 根据当前尝试定义构造最终命令数组，后续统一交给子进程执行器运行。
    list_cmd = _command_for_attempt(  # 当前尝试最终要执行的命令数组
        dict_attempt,  # 当前尝试定义字典
        workdir=path_workdir,  # 排查循环绝对工作目录
        vendor_vcs_home=path_vendor_vcs_home,  # vendor 安装树绝对路径
        overlay_vcs_home=path_overlay_vcs_home,  # 用于 overlay wrapper 与 direct urg1 的安装树绝对路径
        vdb=path_vdb,  # 覆盖率数据库绝对路径
        report_dir=path_report_dir,  # 当前尝试的报告输出目录
    )

    # 单次尝试开始前记录单调时钟，后续统一产出可比较的耗时摘要。
    float_started_monotonic = time.monotonic()  # 当前尝试执行的单调时钟起点

    # 执行当前命令数组并统一收集 stdout/stderr/timeout 结果。
    dict_capture_result = _run_command_capture(  # 当前尝试的统一捕获结果
        list_cmd=list_cmd,  # 当前尝试执行的命令数组
        path_cwd=path_workdir,  # 当前尝试运行使用的工作目录
        dict_env=dict_env,  # 当前尝试执行环境变量
        timeout=timeout,  # 当前尝试最大运行时长
        str_timeout_stderr=f"timeout after {timeout}s",  # 当前尝试超时场景使用的标准错误占位文本
    )

    # 合并两路输出，后续失败归类与 gdb 触发判断都基于这份文本。
    str_combined_output = dict_capture_result["stdout"] + dict_capture_result["stderr"]  # 当前尝试的合并诊断文本

    # 报告目录文件数是判断“真正产出过报告”的关键补充事实。
    int_report_file_count = _count_files(path_report_dir)  # 当前尝试报告目录里的普通文件数量

    # 只有返回码为零且报告目录有实际文件时，才把本次尝试视为真正通过。
    bool_attempt_passed = dict_capture_result["returncode"] == 0 and int_report_file_count > 0  # 当前尝试是否真正通过

    # 通过场景固定写入 passed，否则根据输出文本归类稳定失败签名。
    if bool_attempt_passed:

        # 报告文件与返回码都满足通过条件时，直接写入稳定的 passed 签名。
        str_failure_signature = "passed"  # 当前尝试的通过签名

    # 未通过时需要结合输出文本继续归类稳定失败标签。
    else:

        # 失败签名统一交给输出分类器生成，避免这里复制规则细节。
        str_failure_signature = _classify_failure(str_combined_output, attempt=dict_attempt)  # 当前尝试的失败签名

    # 默认先准备空 gdb 结果，只有 direct urg1 崩溃时才额外执行系统级探针。
    dict_gdb_probe: dict[str, Any] = {}  # 当前尝试附带的 gdb 根因探针结果

    # 只有 direct urg1 内部崩溃时才值得追加 gdb 证据，其他场景不引入额外成本。
    if str_failure_signature == "direct_urg1_internal_crash":

        # 运行一次 gdb 探针，补充 direct urg1 崩溃场景的更细粒度根因证据。
        dict_gdb_probe = _system_gdb_probe(cmd=list_cmd, cwd=path_workdir, env=dict_env, timeout=min(timeout, 60))  # 当前尝试补充得到的 gdb 根因探针结果

    # 先创建空上下文字典，再逐项写入报告构造阶段真正需要的补充事实。
    dict_report_context: dict[str, Any] = {}  # 当前尝试报告需要的补充上下文字典

    # 命令数组用于把结果回填到最终 JSON，便于复现当前排查路径。
    dict_report_context["list_cmd"] = list_cmd  # 当前尝试实际执行的命令数组

    # 起始时钟值用于在报告构造阶段统一折算耗时秒数。
    dict_report_context["float_started_monotonic"] = float_started_monotonic  # 当前尝试的单调时钟起点

    # 报告目录路径决定最终 `report_exists` 字段的事实来源。
    dict_report_context["path_report_dir"] = path_report_dir  # 当前尝试专属报告目录

    # 报告文件数量决定本次尝试是否真正产出可消费的报告结果。
    dict_report_context["int_report_file_count"] = int_report_file_count  # 当前尝试产出的普通文件数量

    # 失败签名决定 JSON 摘要里记录的稳定根因标签。
    dict_report_context["str_failure_signature"] = str_failure_signature  # 当前尝试的失败或通过签名

    # gdb 探针结果只在 direct urg1 崩溃时出现，因此单独放进上下文字典。
    dict_report_context["dict_gdb_probe"] = dict_gdb_probe  # 仅 direct urg1 崩溃时才会填充的 gdb 取证结果

    # 返回单次尝试的统一结构化报告，供主循环顺序收集。
    return _build_attempt_report(dict_attempt, dict_capture_result, dict_report_context)

# 运行完整 URG 排查循环，尝试多组 wrapper、loader 与 metric 变体。
def run_attempts(
    *,
    workdir: Path,
    vdb: Path,
    vendor_vcs_home: Path,
    overlay_vcs_home: Path,
    timeout: int,
) -> dict[str, Any]:
    """
    执行多组 URG 排查尝试并汇总结构化报告。

    参数：
    - workdir: 排查循环使用的工作目录。
    - vdb: 当前待检查的覆盖率数据库目录。
    - vendor_vcs_home: vendor 版 VCS 安装根目录。
    - overlay_vcs_home: overlay 版 VCS 安装根目录。
    - timeout: 每次单独尝试允许的最大运行时长，单位秒。

    返回：
    - 返回包含整体状态、shell 兼容性摘要和每次尝试结果的结构化报告。

    异常：
    - 无显式异常；子进程失败、超时和 gdb 缺失都转成结构化结果返回。
    """

    # 先把关键路径都规范化成绝对路径，避免后续命令与报告目录混入相对路径歧义。
    path_workdir = workdir.resolve()  # 当前排查循环的绝对工作目录

    # 覆盖率数据库路径要先规范化，后续命令构造与成功判定都依赖它。
    path_vdb = vdb.resolve()  # 当前待排查的覆盖率数据库绝对路径

    # vendor 安装树统一折算成绝对路径，供 wrapper 定位和 shell 兼容性摘要复用。
    path_vendor_vcs_home = vendor_vcs_home.resolve()  # vendor 版 VCS 安装根目录绝对路径

    # overlay 安装树在总循环里同时承担补丁 wrapper 和直连动态库根目录两类职责。
    path_overlay_vcs_home = overlay_vcs_home.resolve()  # overlay 版工具树绝对路径，供补丁 wrapper 与动态库链路复用

    # 先准备空尝试结果列表，后续每完成一次排查就顺序追加结构化记录。
    list_attempt_reports: list[dict[str, Any]] = []  # 当前排查循环累计得到的尝试结果列表

    # shell 兼容性摘要通过读取 vendor 与 overlay 的首行文本来解释 wrapper 差异。
    dict_shell_compat: dict[str, str] = {}  # 当前 vendor 与 overlay urg wrapper 的首行兼容性摘要

    # vendor 首行文本用于确认原始安装树里的 wrapper 仍使用哪种脚本头。
    dict_shell_compat["vendor_urg_first_line"] = _read_first_line(path_vendor_vcs_home / "bin" / "urg")  # vendor wrapper 当前解析到的脚本头

    # overlay 首行文本用于确认补丁安装树里的 wrapper 是否已经切换到预期脚本头。
    dict_shell_compat["overlay_urg_first_line"] = _read_first_line(path_overlay_vcs_home / "bin" / "urg")  # overlay 安装树此刻暴露出来的脚本头文本

    # 按既定顺序执行全部排查尝试，确保最终报告具备稳定的比较基线。
    for dict_attempt in ATTEMPTS:

        # 先执行当前尝试，把矩阵条目转换成单条结构化结果。
        dict_attempt_report = _run_single_attempt(  # 当前尝试生成的结构化报告
            dict_attempt,  # 当前矩阵条目的原始尝试定义
            path_workdir,  # 所有尝试共享的工作根目录
            path_vdb,  # 本轮要验证的覆盖率数据库路径
            path_vendor_vcs_home,  # 保留原生 wrapper 行为的 vendor 工具根目录
            path_overlay_vcs_home,  # 提供补丁 wrapper 与动态库覆盖链路的 overlay 根目录
            timeout,  # 单条尝试允许占用的最长秒数
        )

        # 再把单次尝试结果顺序追加到总结果列表，保持排查矩阵顺序稳定。
        list_attempt_reports.append(dict_attempt_report)

    # 只要任一尝试返回码为零且报告落地，就把整个排查循环视为通过。
    bool_any_passed = any(item["returncode"] == 0 and item["report_file_count"] > 0 for item in list_attempt_reports)  # 当前排查循环是否存在至少一次成功尝试

    # 整体摘要原因只区分 passed 与 vendor/host blocked，保持外层门禁口径稳定。
    str_summary_reason = "passed" if bool_any_passed else "vendor_or_host_blocked"  # 当前排查循环的整体摘要原因

    # 返回完整排查报告，供 CLI、远端门禁和单元测试统一消费。
    return {
        "status": "passed" if bool_any_passed else "failed",
        "summary": {
            "reason": str_summary_reason,
            "attempt_count": len(list_attempt_reports),
            "any_passed": bool_any_passed,
        },
        "shell_compat": dict_shell_compat,
        "attempts": list_attempt_reports,
    }

# 提供命令行入口，按需输出 JSON 协议结果或人类可读状态摘要。
def main() -> int:
    """
    运行 URG 故障排查 CLI。

    参数：
    - 无显式业务参数；函数直接消费命令行参数。

    返回：
    - 返回进程退出码；整体通过时为 `0`，否则为 `1`。

    异常：
    - 无显式异常；命令行解析失败由 `argparse` 自行处理退出。
    """

    # 先创建参数解析器，统一承载排查循环所需的目录与超时参数。
    parser = argparse.ArgumentParser(description="Run a focused URG troubleshooting loop on a remote Linux EDA host.")  # URG 故障排查 CLI 的参数解析器

    # 工作目录是所有报告目录、副本 wrapper 与执行 cwd 的共同根路径。
    parser.add_argument("--workdir", type=Path, required=True)

    # vdb 路径决定当前排查循环实际要消费的覆盖率数据库目录。
    parser.add_argument("--vdb", type=Path, required=True)

    # vendor VCS_HOME 用于定位原始 wrapper 与 vendor 安装树。
    parser.add_argument("--vendor-vcs-home", type=Path, required=True)

    # overlay VCS_HOME 用于定位 overlay wrapper、urg1 与 linux64/lib。
    parser.add_argument("--overlay-vcs-home", type=Path, required=True)

    # timeout 控制每次单独尝试与 gdb 探针的最大运行时长。
    parser.add_argument("--timeout", type=int, default=120)

    # `--json` 打开时遵循模块声明的 JSON stdout 协议输出完整结构化报告。
    parser.add_argument("--json", action="store_true")

    # 解析命令行参数后，后续所有逻辑都只消费这一份参数快照。
    args = parser.parse_args()  # 当前 CLI 调用解析得到的参数命名空间

    # 先整理传给排查主函数的关键字参数，避免主调用点堆叠过长的多行参数列表。
    dict_run_kwargs = {  # 当前 CLI 调用传给排查主函数的关键字参数集合
        "workdir": args.workdir,  # CLI 指定的排查循环工作目录
        "vdb": args.vdb,  # CLI 指定的待检查覆盖率数据库目录
        "vendor_vcs_home": args.vendor_vcs_home,  # CLI 指定的原始 vendor 安装树目录
        "overlay_vcs_home": args.overlay_vcs_home,  # CLI 指定的 overlay 安装树与动态库根目录
        "timeout": args.timeout,  # CLI 指定的单次尝试最大运行时长
    }

    # 再运行完整排查循环，得到可直接序列化与汇总的结构化报告。
    dict_result_report = run_attempts(**dict_run_kwargs)  # 当前 CLI 生成的 URG 故障排查结构化报告

    # JSON 模式直接输出单一 JSON 载荷，供脚本链路和远端门禁直接消费。
    if args.json:

        # 把完整结构化报告序列化到 stdout，遵循模块级 JSON stdout 协议。
        json.dump(dict_result_report, sys.stdout, indent=2, sort_keys=True)

        # JSON 协议输出末尾补一个换行，避免终端提示符直接贴到载荷末尾。
        sys.stdout.write("\n")

    # 非 JSON 模式只输出人类可读的通过/失败摘要，避免终端刷出结构化载荷。
    else:

        # 先提取状态摘要文本，避免 print 直接消费结构化字典字段。
        str_status_summary = dict_result_report["status"]  # 当前 CLI 要展示的排查整体状态摘要

        # 向终端报告整体排查状态，便于人工快速判断是否需要读取 JSON 细节。
        print(f"> INFO: [Python] urg troubleshoot status: {str_status_summary}")

    # 通过/失败最终统一映射到进程退出码，方便上层 shell 门禁直接判断。
    return 0 if dict_result_report["status"] == "passed" else 1

# 直接脚本执行时，统一从 `main()` 派生最终进程退出状态。
if __name__ == "__main__":

    # 把 CLI 返回码提升成进程退出码，保持脚本调用语义清晰稳定。
    raise SystemExit(main())
