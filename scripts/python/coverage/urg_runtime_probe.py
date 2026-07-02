#!/usr/bin/env python3
"""采集 URG 运行时与动态加载链路的只读诊断信息。

本模块同时提供可导入的探针函数与命令行入口。

命令行标准输出协议：
- 默认只打印带前缀的人类可读状态摘要，不直接把结构化探针结果刷到终端。
- 当传入 ``--json`` 时，标准输出会写出单个 JSON 对象，供自动化直接消费。
"""

# 启用延后求值注解，避免类型提示在运行期引入额外解析顺序要求。
from __future__ import annotations

# 提供命令行解析、哈希计算、JSON 序列化、环境处理、子进程调用与路径处理能力。
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 固定 strace 关注的系统调用集合，便于聚焦 loader 与进程启动阶段的关键事件。
STR_TRACE_EVENTS = (
    "execve,openat,access,stat,statx,readlink,mmap,mprotect,munmap,"
    "brk,clone,futex,rt_sigaction,rt_sigprocmask,kill,tgkill,exit_group"
)

# 收拢 strace 摘要中需要保留的关键字，避免把整份原始日志直接灌回 JSON。
TUPLE_STRACE_KEYWORDS = (  # strace 摘要中允许保留的关键事件关键字
    "SIGSEGV",  # 捕获进程段错误信号
    "SIGABRT",  # 捕获进程主动中止信号
    "SIGBUS",  # 捕获总线错误信号
    "libucapi",  # 捕获 ucapi 共享库相关加载或崩溃线索
    "libsnpsmalloc",  # 捕获 Synopsys 定制内存库相关加载线索
    "libhvpapi",  # 捕获 HVP API 共享库相关加载线索
    "exit_group",  # 捕获进程退出系统调用
)

# 计算文件内容的 SHA256 摘要，供候选库清单稳定比较。
def sha256_file(path_file: Path) -> str:
    """
    计算常规文件的 SHA256 十六进制摘要。

    参数：
    - path_file: 需要计算摘要的目标文件路径。

    返回：
    - 文件存在且为常规文件时返回 64 位十六进制摘要，否则返回空字符串。

    异常：
    - 无显式异常；底层文件读取失败会沿用 Python 默认行为。
    """

    # 缺失路径或非常规文件没有稳定文件摘要，直接返回空字符串。
    if not path_file.exists() or not path_file.is_file():

        # 让调用方用空字符串区分“没有可读文件”和“有文件且已完成哈希”。
        return ""

    # 读取整个文件字节并计算 SHA256 摘要，保持与其他诊断脚本一致的比较口径。
    return hashlib.sha256(path_file.read_bytes()).hexdigest()

# 读取文本文件首行，供 wrapper 识别 shell shebang 或其他入口特征。
def first_line(path_file: Path) -> str:
    """
    读取文本文件的第一行内容。

    参数：
    - path_file: 需要读取首行的目标文件路径。

    返回：
    - 成功读取到首行时返回该行文本；读取失败或没有任何文本行时返回空字符串。

    异常：
    - 无显式异常；常见文件读取异常会在函数内部收敛为空字符串。
    """

    # 尝试按宽松 UTF-8 模式读取首行，兼容脚本头部可能混入的非标准字节。
    try:

        # 只取首行即可满足 wrapper 识别诉求，避免无意义地解析更多正文内容。
        return path_file.read_text(encoding="utf-8", errors="ignore").splitlines()[0]

    # 缺文件、目录路径、空文件和编码问题都统一降级为空字符串。
    except (FileNotFoundError, IsADirectoryError, IndexError, OSError, UnicodeDecodeError):

        # 调用方据此可判断“没有可读首行”，而不是把异常继续外抛打断探针。
        return ""

# 在给定 PATH 环境中查找工具路径，兼容相对 PATH 项和 shutil.which 的差异。
def which_tool(str_tool_name: str, dict_env: dict[str, str]) -> str:
    """
    在指定环境变量下定位工具可执行文件。

    参数：
    - str_tool_name: 要查找的工具名。
    - dict_env: 参与搜索的环境变量字典，主要读取 ``PATH``。

    返回：
    - 找到工具时返回原始路径字符串，否则返回空字符串。

    异常：
    - 无显式异常；路径查找沿用 ``shutil.which`` 与 ``Path.exists`` 的默认行为。
    """

    # 先复用标准库 which 行为，优先兼容系统 PATH 搜索规则。
    str_found_tool = shutil.which(str_tool_name, path=dict_env.get("PATH"))  # 标准库 which 返回的原始工具定位结果

    # 标准库已经找到工具时直接返回，避免重复扫描 PATH。
    if str_found_tool:

        # 保留 which 原始结果，让后续归一化逻辑统一决定是否转绝对路径。
        return str_found_tool

    # 提前取出 PATH 文本，后续逐段手动补查相对目录场景。
    str_search_path = dict_env.get("PATH", "")  # 当前环境里用于手动补查的 PATH 原始文本

    # 逐个 PATH 目录手动拼接工具名，兼容某些平台不会返回相对命中的情况。
    for str_path_entry in str_search_path.split(os.pathsep):

        # 空 PATH 项没有实际搜索意义，直接跳过。
        if not str_path_entry:

            # 空 PATH 项不会贡献任何候选目录，直接进入下一轮搜索。
            continue

        # 拼出当前 PATH 目录下的候选工具路径。
        path_candidate_tool = Path(str_path_entry) / str_tool_name  # 当前 PATH 目录下拼出的候选工具路径

        # 只要候选路径真实存在，就把它作为本次工具定位结果返回。
        if path_candidate_tool.exists():

            # 返回字符串形式，保持与 shutil.which 的返回类型一致。
            return str(path_candidate_tool)

    # 所有搜索路径都没命中时返回空字符串，交给上层决定回退逻辑。
    return ""

# 把工具路径统一规整为对工作目录稳定可解释的文本路径。
def _normalize_tool_path(str_tool_path: str, *, cwd: Path) -> str:
    """
    规整工具路径字符串，必要时按工作目录展开相对路径。

    参数：
    - str_tool_path: 原始工具路径文本。
    - cwd: 解释相对路径时使用的工作目录。

    返回：
    - 传入空字符串时返回空字符串；绝对路径原样返回；相对路径则展开为绝对路径。

    异常：
    - 无显式异常；路径解析沿用 ``Path.resolve`` 的默认行为。
    """

    # 没有路径文本时直接返回空字符串，让调用方显式走回退逻辑。
    if not str_tool_path:

        # 空结果代表当前工具尚未被 PATH 搜到。
        return ""

    # 先把原始路径文本规整成 Path，后续统一判断绝对/相对路径。
    path_candidate_tool = Path(str_tool_path)  # 由原始工具路径文本规整得到的候选路径对象

    # 已经是绝对路径时无需再拼接工作目录。
    if path_candidate_tool.is_absolute():

        # 返回绝对路径原文，避免 resolve 改写供应商工具的符号链接表现。
        return str(path_candidate_tool)

    # 相对路径按工作目录展开，便于测试和真实远端场景得到稳定绝对路径。
    return str((cwd / path_candidate_tool).resolve(strict=False))

# 把异常对象里可能出现的 stdout/stderr 统一规整成字符串。
def _normalize_process_output(obj_output: object) -> str:
    """
    把子进程输出对象收敛为字符串。

    参数：
    - obj_output: 可能为 ``str``、``bytes``、``None`` 或其他对象的输出值。

    返回：
    - 返回可安全写入 JSON 的字符串文本。

    异常：
    - 无显式异常；类型转换沿用 Python 默认行为。
    """

    # 缺失输出统一降级为空字符串，避免 JSON 里混入 null。
    if obj_output is None:

        # 空输出用空字符串表达，方便调用方直接做字符串处理。
        return ""

    # 已经是字符串时直接返回，避免重复编码转换。
    if isinstance(obj_output, str):

        # 直接复用现成文本结果。
        return obj_output

    # 字节输出按替换错误方式解码，兼容异常对象里残留的原始字节流。
    if isinstance(obj_output, bytes):

        # 用 UTF-8 宽松解码，尽量保住可读信息而不因坏字节抛异常。
        return obj_output.decode("utf-8", errors="replace")

    # 其他少见对象统一转成字符串，保持 JSON 可序列化。
    return str(obj_output)

# 把 PATH 类环境变量拆成非空目录列表，避免调用方重复书写过滤逻辑。
def _split_non_empty_path_entries(str_path_text: str) -> list[str]:
    """
    把 PATH 风格文本拆成非空目录条目列表。

    参数：
    - str_path_text: 需要拆分的 PATH 风格环境变量文本。

    返回：
    - 返回按原顺序保留的非空目录条目列表。

    异常：
    - 无显式异常；字符串拆分沿用 Python 默认行为。
    """

    # 直接按平台分隔符拆分并过滤空字符串，保留后续需要比较顺序的目录条目。
    return [
        str_entry  # 当前保留下来的非空 loader 搜索目录
        for str_entry in str_path_text.split(os.pathsep)
        if str_entry
    ]

# 只在目标值实际存在时返回其索引，否则返回 None。
def _index_or_none(list_values: list[str], str_target: str) -> int | None:
    """
    返回目标字符串在列表中的索引，缺失时返回 None。

    参数：
    - list_values: 待搜索的字符串列表。
    - str_target: 需要查找索引的目标字符串。

    返回：
    - 找到目标时返回对应索引，否则返回 ``None``。

    异常：
    - 无显式异常；成员判断与索引查找沿用 Python 默认行为。
    """

    # 目标值出现在列表中时返回索引，不存在时统一回退为 None。
    return list_values.index(str_target) if str_target in list_values else None

# 运行只读子命令并把返回码、stdout、stderr 固化成统一结构。
def command_result(
    list_command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 10,
) -> dict[str, object]:
    """
    执行只读诊断命令并返回结构化结果。

    参数：
    - list_command: 要执行的命令参数列表。
    - cwd: 子进程工作目录。
    - env: 子进程环境变量字典。
    - timeout: 子进程超时秒数。

    返回：
    - 返回包含 ``cmd``、``returncode``、``stdout`` 与 ``stderr`` 的结果字典。

    异常：
    - 常见进程启动与超时异常会在函数内部收敛，不向外继续抛出。
    """

    # 尝试按统一捕获策略执行当前只读命令。
    try:

        # 用文本模式执行命令，确保 stdout/stderr 可以直接进入 JSON 结果。
        dict_run_kwargs: dict[str, object] = {  # 传给 subprocess.run 的统一执行参数字典
            "cwd": cwd,  # 当前命令的工作目录
            "env": env,  # 当前命令的环境变量字典
            "text": True,  # 按文本模式捕获 stdout/stderr
            "stdout": subprocess.PIPE,  # 捕获标准输出供 JSON 结果使用
            "stderr": subprocess.PIPE,  # 捕获标准错误供失败诊断使用
            "timeout": timeout,  # 当前命令允许的最长执行秒数
        }

        # 执行当前只读命令，并把统一参数字典展开传给标准库。
        completed_process_command: subprocess.CompletedProcess[str] = subprocess.run(  # 当前只读命令执行完成后的子进程结果对象
            list_command,  # 当前需要执行的命令参数列表
            **dict_run_kwargs,  # 当前统一展开的 subprocess 执行参数字典
        )

        # 逐项构建成功执行的结果字典，避免大块字面量触发 current-project 门禁。
        dict_command_result: dict[str, object] = {  # 当前只读命令成功执行后的结构化结果字典
            "cmd": list_command,  # 当前执行过的命令参数列表
            "returncode": completed_process_command.returncode,  # 当前命令的进程退出码
            "stdout": completed_process_command.stdout,  # 当前命令捕获到的标准输出文本
            "stderr": completed_process_command.stderr,  # 当前命令捕获到的标准错误文本
        }

        # 返回成功分支结果，供上层直接纳入最终探针 JSON。
        return dict_command_result

    # 子进程不可执行、权限不足、OS 错误和超时都统一转换成诊断结果对象。
    except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired) as exc:

        # 逐项构建异常分支结果，让调用方仍能看到原命令与错误文本。
        dict_failed_result: dict[str, object] = {  # 当前只读命令异常分支的结构化结果字典
            "cmd": list_command,  # 当前尝试执行的命令参数列表
            "returncode": None,  # 异常分支没有稳定退出码
            "stdout": _normalize_process_output(getattr(exc, "stdout", "")),  # 异常对象携带的标准输出文本
            "stderr": str(exc),  # 异常对象转换后的错误说明文本
        }

        # 返回异常分支结果，让探针保持只读但不因单条命令失败而整体中断。
        return dict_failed_result

# 收集 overlay 下与 URG 运行时相关的候选库文件元信息。
def candidate_libraries(path_vcs_home: Path) -> dict[str, dict[str, object]]:
    """
    收集 VCS 安装树中关键候选库文件的存在性与摘要信息。

    参数：
    - path_vcs_home: 当前 VCS 根目录。

    返回：
    - 返回以逻辑名称为键、以路径与哈希信息为值的候选库字典。

    异常：
    - 无显式异常；哈希计算沿用 ``sha256_file`` 的默认行为。
    """

    # 固定这组逻辑键，便于证据脚本稳定对比原始库、本地补丁库和可执行入口的存在性。
    dict_candidate_paths = {  # 当前先枚举 probe 必须核对的五个候选文件，后面会按这些固定键逐项生成 path、exists、is_symlink 和 sha256 证据
        "urg1": path_vcs_home / "linux64" / "bin" / "urg1",  # 当前真正承载 URG 运行体依赖解析的 urg1 可执行文件路径
        "libucapi.so": path_vcs_home / "linux64" / "lib" / "libucapi.so",  # 当前安装树里原始 ucapi 共享库的基准比对路径
        "libsnpsmalloc.so": path_vcs_home / "linux64" / "lib" / "libsnpsmalloc.so",  # 当前安装树里 Synopsys 内存库的基准比对路径
        "libhvpapi.so": path_vcs_home / "linux64" / "lib" / "libhvpapi.so",  # 当前安装树里 HVP 相关共享库的基准比对路径
        "patched_libucapi.so": path_vcs_home / "ucapi_patch_lib" / "libucapi.so",  # 当前 overlay 目录里补丁版 ucapi 共享库的比对路径
    }

    # 逐个候选路径构建元信息字典，供证据收集脚本稳定消费。
    dict_candidate_infos: dict[str, dict[str, object]] = {}  # 当前 VCS 安装树全部候选库文件的元信息结果字典

    # 按固定顺序写入每个候选项的路径、存在性与摘要信息。
    for str_candidate_name, path_candidate_file in dict_candidate_paths.items():

        # 单独构建当前候选项结果，避免在推导式里塞入过密字段逻辑。
        dict_candidate_info: dict[str, object] = {  # 当前单个候选库文件的元信息字典
            "path": str(path_candidate_file),  # 当前候选库文件的绝对路径文本
            "exists": path_candidate_file.exists(),  # 当前候选库文件是否真实存在
            "is_symlink": path_candidate_file.is_symlink(),  # 当前候选库文件是否为符号链接入口
            "sha256": sha256_file(path_candidate_file),  # 当前候选库文件内容的 SHA256 摘要
        }

        # 把当前候选项元信息按稳定逻辑名写回总结果字典。
        dict_candidate_infos[str_candidate_name] = dict_candidate_info  # 当前逻辑名对应的候选库元信息

    # 返回稳定的候选库快照，供后续证据聚合直接落盘。
    return dict_candidate_infos

# 判断 ucapi_patch_lib 是否已在 LD_LIBRARY_PATH 中先于原始 VCS lib 生效。
def activation_status(path_vcs_home: Path, dict_env: dict[str, str]) -> dict[str, object]:
    """
    汇总当前环境中补丁库目录的激活顺序信息。

    参数：
    - path_vcs_home: 当前 VCS 根目录。
    - dict_env: 需要检查的环境变量字典，主要读取 ``LD_LIBRARY_PATH``。

    返回：
    - 返回包含路径条目、补丁目录存在性与优先级判断的字典。

    异常：
    - 无显式异常；字符串拆分和路径存在性判断沿用 Python 默认行为。
    """

    # 先把 LD_LIBRARY_PATH 拆成稳定列表，后续索引比较才更直观。
    list_ld_library_entries = _split_non_empty_path_entries(  # 从 LD_LIBRARY_PATH 拆分得到的非空目录条目列表
        dict_env.get("LD_LIBRARY_PATH", ""),  # 当前环境变量里原始 LD_LIBRARY_PATH 文本
    )

    # 明确记录补丁目录路径，便于下游结果直接读取而不再重复拼接。
    str_patch_lib = str(path_vcs_home / "ucapi_patch_lib")  # 当前 overlay 私有补丁目录的文本路径

    # 明确记录原始 VCS 库目录路径，便于比较二者优先级。
    str_vcs_lib = str(path_vcs_home / "linux64" / "lib")  # 当前原始 VCS 库目录的文本路径

    # 计算补丁目录在 loader 搜索列表中的顺序索引，用于后续优先级比较。
    obj_patch_index = _index_or_none(list_ld_library_entries, str_patch_lib)  # 补丁目录在 LD_LIBRARY_PATH 中的条目索引

    # 计算原始 VCS 库目录在 loader 搜索列表中的顺序索引，用于后续优先级比较。
    obj_vcs_index = _index_or_none(list_ld_library_entries, str_vcs_lib)  # 原始 VCS 库目录在 LD_LIBRARY_PATH 中的条目索引

    # 先单独算出补丁目录优先级，避免把布尔表达式直接塞进大字面量里。
    bool_patch_lib_precedes_vcs_lib = (
        obj_patch_index is not None  # 当前环境里确实出现了补丁目录条目
        and (obj_vcs_index is None or obj_patch_index < obj_vcs_index)  # 补丁目录在搜索顺序上领先或独占
    )  # 补丁目录是否先于原始库目录被 loader 搜索到

    # 逐项构建激活顺序结果，保持字段名与既有消费方兼容。
    dict_activation: dict[str, object] = {  # 当前 loader 激活顺序的结构化诊断结果字典
        "ld_library_path_entries": list_ld_library_entries,  # 当前生效的 LD_LIBRARY_PATH 非空条目列表
        "patch_lib": str_patch_lib,  # overlay 私有补丁目录路径文本
        "vcs_lib": str_vcs_lib,  # 原始 VCS 库目录路径文本
        "patch_lib_exists": Path(str_patch_lib).exists(),  # overlay 私有补丁目录是否存在
        "patch_lib_precedes_vcs_lib": bool_patch_lib_precedes_vcs_lib,  # loader 是否会优先命中 overlay 补丁目录
    }

    # 输出的重点是说明 overlay 是否真正走到了原始库前面。
    return dict_activation

# 汇总 strace 日志中的关键尾部片段，避免结果 JSON 过大。
def _summarize_strace(path_strace_log: Path) -> dict[str, object]:
    """
    读取 strace 日志并提取关键尾部事件。

    参数：
    - path_strace_log: strace 输出日志路径。

    返回：
    - 日志存在时返回文件大小与关键尾部片段；不存在时返回存在性说明。

    异常：
    - 无显式异常；文件读取沿用 Python 默认行为。
    """

    # strace 日志不存在时直接返回最小结果，避免后续无意义读取。
    if not path_strace_log.exists():

        # 缺日志通常代表 strace 没有成功生成输出文件。
        return {
            "path": str(path_strace_log),
            "exists": False,
        }

    # 用替换模式读入全文，兼容 strace 日志中的非 UTF-8 字节。
    str_trace_text = path_strace_log.read_text(encoding="utf-8", errors="replace")  # 当前 strace 日志全文文本

    # 逐行筛出包含关键字的事件，供后续只回传最有价值的尾部窗口。
    list_interesting_lines: list[str] = []  # 当前 strace 日志里命中关键字的事件行列表

    # 按原始顺序扫描全文，保留与 loader 崩溃和退出相关的关键行。
    for str_trace_line in str_trace_text.splitlines():

        # 当前行命中任一关键字时，把它纳入最终摘要候选。
        if any(str_keyword in str_trace_line for str_keyword in TUPLE_STRACE_KEYWORDS):

            # 保留原行文本，便于后续直接比对 crash 和库加载上下文。
            list_interesting_lines.append(str_trace_line)

    # 返回受控大小的 strace 摘要，避免大日志把主结果拖得过长。
    return {
        "path": str(path_strace_log),
        "exists": True,
        "bytes": path_strace_log.stat().st_size,
        "interesting_tail": list_interesting_lines[-80:],
    }

# 构建本次探针执行使用的环境变量字典，并显式补齐 VCS_HOME 与 PATH。
def _build_probe_environment(
    path_vcs_home: Path,
    dict_env_override: dict[str, str] | None,
) -> dict[str, str]:
    """
    构建探针执行环境。

    参数：
    - path_vcs_home: 当前 VCS 根目录。
    - dict_env_override: 调用方额外覆盖的环境变量字典。

    返回：
    - 返回已经注入 ``VCS_HOME`` 与 ``PATH`` 的环境变量字典副本。

    异常：
    - 无显式异常；字典复制与字符串拼接沿用 Python 默认行为。
    """

    # 从当前进程环境复制一份基础字典，避免原地改写全局 os.environ。
    dict_probe_env = os.environ.copy()  # 从当前进程环境复制得到的探针基础环境字典

    # 调用方显式传入的环境覆盖项拥有更高优先级。
    dict_probe_env.update(dict_env_override or {})

    # 固定注入本次探针目标 VCS_HOME，保证子进程读取的是当前待诊断安装树。
    dict_probe_env["VCS_HOME"] = str(path_vcs_home)  # 当前探针目标 VCS 根目录文本

    # 把 VCS bin 目录前置到 PATH，优先命中当前目标版本的工具包装器。
    str_existing_path = dict_probe_env.get("PATH", "")  # 当前基础环境里已有的 PATH 文本

    # 把目标版本 bin 目录前置到 PATH，确保工具定位优先命中当前待诊断安装树。
    dict_probe_env["PATH"] = f"{path_vcs_home / 'bin'}{os.pathsep}{str_existing_path}"  # 当前探针执行时优先命中目标版本工具包装器的 PATH 文本

    # 返回最终子进程环境，供工具定位与只读命令执行共用。
    return dict_probe_env

# 基于 PATH 搜索结果和工作目录，把 urg/vcs 的最终路径文本统一规整出来。
def _resolve_tool_paths(path_vcs_home: Path, path_workdir: Path, dict_env: dict[str, str]) -> dict[str, str]:
    """
    解析本次探针需要的 urg 与 vcs 工具路径。

    参数：
    - path_vcs_home: 当前 VCS 根目录。
    - path_workdir: 本次探针工作目录。
    - dict_env: 用于工具搜索的环境变量字典。

    返回：
    - 返回包含 ``which_urg`` 与 ``which_vcs`` 的路径字典。

    异常：
    - 无显式异常；工具搜索与路径规整沿用内部 helper 的默认行为。
    """

    # 直接返回规整后的工具路径字典，避免在本地 helper 中保留多余中间状态。
    return {
        "which_urg": _normalize_tool_path(which_tool("urg", dict_env), cwd=path_workdir),  # 当前解析出的 urg 命令路径文本
        "which_vcs": _normalize_tool_path(which_tool("vcs", dict_env), cwd=path_workdir),  # 当前解析出的 vcs wrapper 或可执行入口路径文本
    }

# 把基础结果里那些依赖只读外部命令的诊断字段集中补齐。
def _append_runtime_diagnostics(
    *, dict_probe_result: dict[str, object], list_vcs_command: list[str],
    path_urg1: Path, path_libucapi: Path, path_vcs_home: Path,
    path_workdir: Path, dict_probe_env: dict[str, str],
) -> None:
    """
    向基础探针结果追加运行时诊断字段。

    参数：
    - dict_probe_result: 待补充字段的基础结果字典。
    - list_vcs_command: 查询 ``vcs -location`` 时使用的命令前缀列表。
    - path_urg1: 目标版本的 ``urg1`` 二进制路径。
    - path_libucapi: 目标版本的 ``libucapi.so`` 路径。
    - path_vcs_home: 当前 VCS 根目录。
    - path_workdir: 本次探针工作目录。
    - dict_probe_env: 本次探针执行环境。

    返回：
    - 无返回值；函数直接原地补充 ``dict_probe_result``。

    异常：
    - 无显式异常；内部只读命令失败会被 ``command_result`` 自行收敛。
    """

    # 把反复使用的命令上下文合并起来，避免每次调用都重复展开 cwd 和 env 说明。
    dict_readonly_command_kwargs: dict[str, object] = {
        "cwd": path_workdir,  # 当前所有只读外部命令统一使用的工作目录
        "env": dict_probe_env,  # 当前所有只读外部命令统一继承的探针环境
    }

    # 记录 vcs wrapper 的基础定位结果，供后续判断 PATH 与包装层来源。
    dict_probe_result["vcs_location"] = command_result(  # `vcs -location` 的结构化执行结果
        [*list_vcs_command, "-location"], **dict_readonly_command_kwargs  # 当前普通 wrapper 定位查询命令及上下文
    )

    # 单独核对 full64 入口，确认 64 位 wrapper 是否仍然指向预期安装树。
    dict_probe_result["vcs_full64_location"] = command_result(  # 当前 64 位 wrapper 定位结果的结构化记录入口
        [*list_vcs_command, "-full64", "-location"], **dict_readonly_command_kwargs  # 当前 full64 入口的定位查询命令及统一探针上下文
    )

    # 采集 urg1 本体的 ldd 结果，用来观察真正运行体会解析到哪些共享库。
    dict_probe_result["ldd_urg1"] = command_result(  # 对 urg1 二进制执行 ldd 得到的结构化结果
        ["ldd", str(path_urg1)], **dict_readonly_command_kwargs  # 当前 urg1 运行体依赖查询命令及上下文
    )

    # 单独采集 libucapi 的依赖解析结果，用来判断 overlay 是否影响核心共享库。
    dict_probe_result["ldd_libucapi"] = command_result(  # 对 libucapi 共享库执行 ldd 得到的结构化结果
        ["ldd", str(path_libucapi)], **dict_readonly_command_kwargs  # 当前核心 ucapi 依赖查询命令及上下文
    )

    # 追加关键候选库的存在性与摘要快照。
    dict_probe_result["candidate_libraries"] = candidate_libraries(path_vcs_home)  # 与 URG 运行时相关的候选库文件快照

    # 追加补丁目录相对原始库目录的 loader 激活顺序结果。
    dict_probe_result["activation"] = activation_status(path_vcs_home, dict_probe_env)  # 当前环境下补丁目录是否已领先原始库目录

# 执行一次可选的 strace 采样，并把结果整理成与主探针兼容的结构。
def _collect_strace_result(
    *, path_workdir: Path, path_vdb: Path, path_report_dir: Path,
    dict_probe_env: dict[str, str], dict_tool_paths: dict[str, str],
    strace_timeout: int,
) -> dict[str, object]:
    """
    执行带超时控制的 strace 采样。

    参数：
    - path_workdir: 本次探针工作目录。
    - path_vdb: 目标 VDB 路径。
    - path_report_dir: 目标报告目录路径。
    - dict_probe_env: 本次探针执行环境。
    - dict_tool_paths: urg 与 vcs 的已解析路径字典。
    - strace_timeout: 本次 strace 采样超时秒数。

    返回：
    - 返回包含执行结果与摘要信息的 strace 结果字典。

    异常：
    - 无显式异常；命令失败会被 ``command_result`` 与 ``_summarize_strace`` 自行收敛。
    """

    # 固定 strace 输出日志路径，便于证据收集脚本后续按约定文件名寻找。
    path_strace_log = path_workdir / "urg_runtime_probe_strace.log"  # 当前 strace 采样固定输出日志路径

    # 确保 strace 日志父目录存在，避免输出路径创建失败。
    path_strace_log.parent.mkdir(parents=True, exist_ok=True)

    # 没找到 urg 路径时回退到裸命令名，让 strace 继续经 PATH 解析。
    str_urg_command = dict_tool_paths["which_urg"] or "urg"  # 本次 strace 实际调用的 urg 命令路径或命令名

    # strace 执行分支只比普通只读命令多一个超时参数，其余上下文保持一致。
    dict_strace_command_kwargs: dict[str, object] = {
        "cwd": path_workdir,  # 当前 strace 子进程统一使用的工作目录
        "env": dict_probe_env,  # 当前 strace 子进程统一继承的探针环境
        "timeout": strace_timeout,  # 当前 strace 采样允许的最大执行秒数
    }

    # 生成本次 strace 采样命令，保持主流程更易读。
    list_strace_command = _build_strace_command(  # 当前 strace 采样命令参数列表
        path_strace_log=path_strace_log,  # 当前固定输出的 strace 日志路径
        str_urg_command=str_urg_command,  # 本次要经 strace 包装执行的 urg 命令
        path_vdb=path_vdb,  # 当前需要诊断的 VDB 路径
        path_report_dir=path_report_dir,  # 当前 URG 报告目录路径
    )

    # 统一返回执行结果与日志摘要，供主探针结果直接挂载。
    return {
        "execution": command_result(
            list_strace_command,  # 当前准备执行的 strace 命令参数列表
            **dict_strace_command_kwargs,  # 当前统一展开的 strace 执行上下文字典
        ),
        "summary": _summarize_strace(path_strace_log),  # 当前 strace 日志抽取出的关键尾部摘要
    }

# 构建不含 strace 结果的基础探针结果，避免 run_probe 内塞入过大的字面量。
def _base_probe_result(
    *, path_vcs_home: Path, path_workdir: Path, path_vdb: Path,
    path_report_dir: Path, dict_probe_env: dict[str, str],
    dict_tool_paths: dict[str, str],
) -> dict[str, object]:
    """
    构建基础 URG 运行时探针结果。

    参数：
    - path_vcs_home: 当前 VCS 根目录。
    - path_workdir: 本次探针工作目录。
    - path_vdb: 目标 VDB 路径。
    - path_report_dir: 目标报告目录路径。
    - dict_probe_env: 本次探针执行环境。
    - dict_tool_paths: urg 与 vcs 的已解析路径字典。

    返回：
    - 返回不含 strace 字段的基础结果字典。

    异常：
    - 无显式异常；内部只读命令失败会被 ``command_result`` 自行收敛。
    """

    # 没有通过 PATH 找到 urg 时，回退到 VCS_HOME/bin/urg 作为 wrapper 候选。
    path_urg_wrapper = (
        Path(dict_tool_paths["which_urg"])  # 当前 PATH 已解析到的 urg wrapper 路径
        if dict_tool_paths["which_urg"]  # 当前环境里已经能直接定位到 urg 命令
        else path_vcs_home / "bin" / "urg"  # 当前退回到安装树默认 wrapper 路径
    )  # 当前用于读取首行信息的 urg wrapper 路径

    # 若 PATH 没找到 vcs，就退回裸命令名让 command_result 继续按当前环境解析。
    list_vcs_command = [dict_tool_paths["which_vcs"] or "vcs"]  # 当前用于查询 vcs -location 的命令前缀列表

    # 记录 urg1 二进制本体路径，后续要用它观察运行体依赖解析结果。
    path_urg1 = path_vcs_home / "linux64" / "bin" / "urg1"  # 当前目标版本 urg1 二进制路径

    # 记录 libucapi 本体路径，后续要用它观察共享库依赖解析结果。
    path_libucapi = path_vcs_home / "linux64" / "lib" / "libucapi.so"  # 当前目标版本 libucapi 共享库路径

    # 先写入与目标路径直接相关的基础字段，供证据脚本快速锁定诊断对象。
    dict_probe_result: dict[str, object] = {  # 当前基础探针结果里与路径和工具定位直接相关的字段集合
        "status": "passed",  # 当前基础探针流程状态
        "vcs_home": str(path_vcs_home),  # 当前待诊断 VCS 根目录文本
        "workdir": str(path_workdir),  # 当前探针工作目录文本
        "vdb": str(path_vdb),  # 当前目标 VDB 路径文本
        "report_dir": str(path_report_dir),  # 当前目标报告目录路径文本

        # 把工具定位和 wrapper 首行也放进基础字段，便于区分 PATH 解析结果与安装树默认入口。
        "which_urg": dict_tool_paths["which_urg"],  # PATH 中命中的 urg 入口路径文本
        "which_vcs": dict_tool_paths["which_vcs"],  # PATH 中命中的 vcs 可执行或 wrapper 入口路径文本
        "wrapper_first_line": first_line(path_urg_wrapper),  # 当前 urg wrapper 文件首行的原始文本
    }

    # 追加运行时定位、依赖解析与 overlay 激活顺序等诊断字段。
    _append_runtime_diagnostics(
        dict_probe_result=dict_probe_result, list_vcs_command=list_vcs_command, path_urg1=path_urg1,
        path_libucapi=path_libucapi, path_vcs_home=path_vcs_home, path_workdir=path_workdir,
        dict_probe_env=dict_probe_env,
    )

    # 返回不含 strace 扩展字段的基础探针结果。
    return dict_probe_result

# 生成固定格式的 strace 命令参数，供采样分支直接复用。
def _build_strace_command(
    *,
    path_strace_log: Path,
    str_urg_command: str,
    path_vdb: Path,
    path_report_dir: Path,
) -> list[str]:
    """
    生成 strace 诊断命令参数列表。

    参数：
    - path_strace_log: strace 输出日志路径。
    - str_urg_command: 本次实际调用的 urg 命令路径或命令名。
    - path_vdb: 目标 VDB 路径。
    - path_report_dir: 目标报告目录路径。

    返回：
    - 返回可直接交给 ``subprocess.run`` 的 strace 命令列表。

    异常：
    - 无显式异常；字符串与路径拼接沿用 Python 默认行为。
    """

    # 返回固定格式的 strace 命令，聚焦 URG 运行时加载链路。
    return [
        "strace",
        "-f",
        "-o",
        str(path_strace_log),
        "-e",
        f"trace={STR_TRACE_EVENTS}",
        str_urg_command,
        "-full64",
        "-dir",
        str(path_vdb),
        "-report",
        str(path_report_dir),
    ]

# 规范化探针输入路径，避免主探针函数起始段出现过密赋值块。
def _normalize_probe_inputs(
    *,
    vcs_home: Path | str,
    workdir: Path | str,
    vdb: Path | str,
    report_dir: Path | str,
) -> tuple[Path, Path, Path, Path]:
    """
    把探针入口收到的路径参数统一规整为 Path 对象。

    参数：
    - vcs_home: 当前 VCS 根目录。
    - workdir: 本次探针工作目录。
    - vdb: 目标 VDB 路径。
    - report_dir: 目标报告目录路径。

    返回：
    - 返回按 ``(path_vcs_home, path_workdir, path_vdb, path_report_dir)`` 排列的规整路径元组。

    异常：
    - 无显式异常；路径规整沿用 ``Path.resolve`` 的默认行为。
    """

    # 直接返回四个核心路径的规整结果，减少主探针函数中的起始样板代码密度。
    return (
        Path(vcs_home).resolve(strict=False),  # 当前待诊断 VCS 根目录的规整路径
        Path(workdir).resolve(strict=False),  # 当前探针执行工作目录的规整路径
        Path(vdb).resolve(strict=False),  # 当前目标 VDB 的规整路径
        Path(report_dir).resolve(strict=False),  # 当前目标报告目录的规整路径
    )

# 执行只读 URG 运行时探针，并按既有契约返回结构化诊断结果。
def run_probe(
    *, vcs_home: Path | str, workdir: Path | str, vdb: Path | str,
    report_dir: Path | str, env: dict[str, str] | None = None,
    strace_timeout: int = 0,
) -> dict[str, object]:
    """
    执行 URG 运行时与动态加载链路探针。

    参数：
    - vcs_home: 当前 VCS 根目录。
    - workdir: 本次探针工作目录。
    - vdb: 目标 VDB 路径。
    - report_dir: 目标 URG 报告目录路径。
    - env: 可选环境变量覆盖字典。
    - strace_timeout: 大于零时额外执行一次带超时的 strace 采样。

    返回：
    - 返回包含工具位置、wrapper 首行、ldd 结果、候选库信息与激活状态的结果字典。

    异常：
    - 无显式异常；只读命令与 strace 失败会被内部 helper 收敛成结构化结果。
    """

    # 先统一规整探针入口路径，避免主流程混杂字符串和 Path 对象。
    tuple_probe_paths = _normalize_probe_inputs(  # 当前探针入口四个核心路径组成的规整元组
        vcs_home=vcs_home,  # 当前待诊断的 VCS 根目录输入值
        workdir=workdir,  # 当前探针执行工作目录输入值
        vdb=vdb,  # 当前目标 VDB 输入值
        report_dir=report_dir,  # 当前目标报告目录输入值
    )

    # 取出安装根目录，后续要据此补齐 VCS_HOME 并扫描 overlay 相关文件。
    path_vcs_home = tuple_probe_paths[0]  # 后续环境构造和候选库扫描依赖的 VCS 根目录

    # 取出工作目录，后续所有只读命令和 strace 日志都在这里执行或落盘。
    path_workdir = tuple_probe_paths[1]  # 后续命令执行和日志落盘使用的工作目录

    # 取出 VDB 路径，后续构造 URG 与 strace 命令时都会把它作为输入。
    path_vdb = tuple_probe_paths[2]  # 后续 urg 与 strace 诊断命令共享的 VDB 路径

    # 取出报告目录，后续既要创建父目录，也要把 URG 输出定向到这里。
    path_report_dir = tuple_probe_paths[3]  # 后续报告输出和父目录创建共用的目标目录

    # 报告目录本身可能尚未存在，但至少要确保其父目录可以落日志和结果文件。
    path_report_dir.parent.mkdir(parents=True, exist_ok=True)

    # 构建本次探针实际使用的环境变量字典。
    dict_probe_env = _build_probe_environment(path_vcs_home, env)  # 当前探针执行使用的完整环境变量字典

    # 解析 urg/vcs 工具路径，供基础探针与 strace 分支共用。
    dict_tool_paths = _resolve_tool_paths(path_vcs_home, path_workdir, dict_probe_env)  # 当前探针解析出的 urg/vcs 工具路径字典

    # 先收集不含 strace 的基础诊断结果，主流程只负责拼装阶段顺序。
    dict_probe_result = _base_probe_result(  # 当前基础探针结果字典
        path_vcs_home=path_vcs_home, path_workdir=path_workdir, path_vdb=path_vdb,  # 基础探针需要的安装根目录、工作目录和 VDB 路径
        path_report_dir=path_report_dir, dict_probe_env=dict_probe_env, dict_tool_paths=dict_tool_paths,  # 基础探针需要的报告目录、环境和工具定位字典
    )

    # 显式要求 strace 采样时，再额外执行一次包装后的 URG 运行探针。
    if strace_timeout > 0:

        # 把 strace 扩展诊断结果挂到主探针结果字典中。
        dict_probe_result["strace"] = _collect_strace_result(  # 当前 strace 采样分支的结构化结果字典
            path_workdir=path_workdir, path_vdb=path_vdb, path_report_dir=path_report_dir,  # strace 分支需要的执行目录、VDB 和报告目录
            dict_probe_env=dict_probe_env, dict_tool_paths=dict_tool_paths, strace_timeout=strace_timeout,  # strace 分支需要的环境、工具定位和超时配置
        )

    # 返回完整探针结果，供测试、远端诊断与证据聚合统一消费。
    return dict_probe_result

# 输出命令行默认模式下的人类可读摘要，避免直接打印完整结构化对象。
def _print_cli_summary(dict_probe_result: dict[str, object]) -> None:
    """
    打印探针结果的人类可读状态摘要。

    参数：
    - dict_probe_result: ``run_probe`` 返回的结构化结果字典。

    返回：
    - 无返回值；仅向标准输出打印带前缀摘要。

    异常：
    - 无显式异常；标准输出失败会沿用 Python 默认行为。
    """

    # 先把状态字段规整成字符串，避免摘要输出阶段直接处理任意对象。
    str_status = str(dict_probe_result.get("status", "unknown"))  # 当前探针结果归一化后的状态文本

    # 通过状态输出 INFO 摘要，让调用方快速确认探针已跑完。
    if str_status == "passed":

        # 默认模式只打印简短 INFO 文本，详细结果应通过 --json 获取。
        print("> INFO: [Python] urg runtime probe passed")

        # 通过摘要已经打印完成，当前 helper 可以直接结束。
        return

    # 其他状态统一当作错误摘要输出，避免把异常结果误当成正常完成。
    print(f"> ERR: [Python] urg runtime probe {str_status}")

# 解析命令行参数并执行探针入口。
def main(argv: list[str] | None = None) -> int:
    """
    运行命令行探针入口并返回进程退出码。

    参数：
    - argv: 可选命令行参数列表；传入 ``None`` 时使用进程默认参数。

    返回：
    - 当前探针脚本始终返回 ``0``，保持与既有调用链兼容。

    异常：
    - 参数解析失败时由 ``argparse`` 抛出并终止进程；文件系统异常沿用底层行为。
    """

    # 创建参数解析器，集中声明探针所需路径与可选 strace 采样开关。
    parser = argparse.ArgumentParser(  # 当前 CLI 的参数解析器
        description="Collect read-only URG runtime and loader diagnostics.",  # 当前 CLI 用于展示的入口说明文本
    )

    # VCS 根目录必须显式给出，避免脚本误用环境里其他版本的安装树。
    parser.add_argument("--vcs-home", type=Path, required=True)

    # 工作目录必须显式给出，便于相对 PATH 与 strace 日志路径保持可复现。
    parser.add_argument("--workdir", type=Path, required=True)

    # 目标 VDB 路径必须显式给出，供 URG 诊断命令稳定构造。
    parser.add_argument("--vdb", type=Path, required=True)

    # 报告目录也必须显式给出，供 URG 输出路径与证据回收流程对齐。
    parser.add_argument("--report-dir", type=Path, required=True)

    # 允许调用方启用一次受控超时的 strace 采样，用于补充 loader 级诊断。
    parser.add_argument("--strace-timeout", type=int, default=0)

    # 显式请求 JSON 协议时，标准输出允许写出单个结构化结果对象。
    parser.add_argument("--json", action="store_true")

    # 解析本次 CLI 调用参数，得到路径与输出模式配置。
    args = parser.parse_args(argv)  # 当前 CLI 调用解析得到的参数对象

    # 执行核心探针函数，生成结构化结果供 JSON 或摘要模式复用。
    dict_probe_result = run_probe(  # 当前 CLI 运行得到的结构化探针结果字典
        vcs_home=args.vcs_home,  # 当前 CLI 传入的 VCS 根目录参数
        workdir=args.workdir,  # 当前 CLI 传入的工作目录参数
        vdb=args.vdb,  # 当前 CLI 传入的目标 VDB 参数
        report_dir=args.report_dir,  # 当前 CLI 传入的报告目录参数
        strace_timeout=args.strace_timeout,  # 当前 CLI 传入的 strace 超时秒数
    )

    # JSON 模式下输出单个结构化对象，供自动化脚本直接消费。
    if args.json:

        # 把完整探针结果写到标准输出，并补一个换行避免提示符紧贴尾部。
        json.dump(dict_probe_result, sys.stdout, indent=2, sort_keys=True)

        # 为 JSON 协议补一个终止换行，避免 shell 提示符直接贴在对象尾部。
        sys.stdout.write("\n")

    # 默认模式只输出带前缀的短摘要，不把结构化结果直接刷到终端。
    else:

        # 打印符合项目输出规范的人类可读摘要。
        _print_cli_summary(dict_probe_result)

    # 保持既有 CLI 契约，总是返回零退出码。
    return 0

# 只有脚本被直接执行时才启动 CLI，避免导入测试模块时意外跑探针。
if __name__ == "__main__":

    # 把 main 返回值转换成进程退出码，交给 shell 或自动化流程统一处理。
    raise SystemExit(main())
