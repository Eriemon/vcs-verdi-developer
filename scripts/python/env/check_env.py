#!/usr/bin/env python3
"""检查 VCS/Verdi 冒烟前的环境就绪状态。

stdout_protocol: json
本模块既可输出机器可读 JSON，也可输出简短文本摘要，
供技能工作流在本地或远端执行前做只读预检。
"""
from __future__ import annotations

# 引入命令行、JSON、环境变量、路径与子进程接口，用于构建环境检查报告。
import argparse
import json
import os
import sys

# 引入映射、路径与系统工具，支撑环境扫描与可执行文件定位。
from collections.abc import Mapping
from pathlib import Path
import shutil
import subprocess

# 引入通用类型标注，约束环境报告中的动态字典结构。
from typing import Any, Callable

# 把动态 JSON 风格对象统一抽象成别名，便于说明报告的层级结构。
JsonDict = dict[str, Any]  # 工具、环境和总体状态等报告节点共用的对象类型

# 这些工具决定编译器、波形查看器与脚本包装器能否被完整拉起。
tuple_tool_names = (  # 按稳定顺序登记需要探测的工具名集合
    "vlogan",  # 首个探针固定为 Verilog 编译入口，便于问题摘要优先暴露核心缺口
    "vhdlan",  # 第二个探针专门覆盖 VHDL 分支，避免只看到 Verilog 路径结论
    "vcs",  # elaboration 与仿真主体都依赖 vcs 本体
    "verdi",  # GUI 波形入口单独列出，便于直接判断可视化链路
    "fsdbreport",  # 非 GUI 的 FSDB 读取能力首先依赖这个命令
    "python3",  # Python3 入口需要单独探测，方便远端脚本选择解释器
    "python",  # 兼容旧环境仍可能只暴露 python 别名
    "bash",  # bash 是部分包装脚本的首选执行壳
    "sh",  # sh 需要单独检查包装器兼容性
)  # 当前环境检查需要探测的工具名集合

# 只有少数 Synopsys 工具具备稳定的快速版本探针，因此单独列成集合。
set_version_probe_tools = {"vlogan", "vhdlan", "vcs", "verdi", "fsdbreport"}  # 允许执行版本探针的工具名集合

# 这些环境变量会左右许可证、GUI、PLI 与 shell 兼容性，因此统一回写到报告。
tuple_env_var_names = (  # 按稳定顺序登记需要显式检查的环境变量名集合
    "VCS_HOME",  # 先检查 VCS 安装根目录，后续工具链就绪判断都会依赖这一项
    "VCS_BIN",  # 允许用户显式覆盖 vcs 可执行文件路径
    "VERDI_HOME",  # Verdi 安装根目录决定 GUI 与 NPI 路径推导
    "VERDI_PYTHON",  # 用户可直接指定 Verdi 自带 Python 解释器
    "NOVAS_HOME",  # PLI 根目录通常从这个变量获得最强线索
    "SNPSLMD_LICENSE_FILE",  # Synopsys 许可证主变量需要优先展示
    "LM_LICENSE_FILE",  # 兼容旧式许可证变量命名
    "DISPLAY",  # GUI 直连显示会话取决于 DISPLAY
    "XAUTHORITY",  # X11 鉴权文件会影响远端 GUI 能否拉起
    "VNC_DISPLAY",  # VNC 会话通常会额外声明一个显示编号
    "SHELL",  # shell 类型会影响包装器参数兼容性
    "PATH",  # PATH 决定多数命令的默认解析结果
    "LD_LIBRARY_PATH",  # 动态库搜索路径可补充 PLI 线索
)  # 当前环境检查需要显式回写的环境变量名集合

# 读取版本探针输出时只关心首行摘要，因此先独立抽成小 helper。
def _first_output_line(str_output: str) -> str:
    """
    从命令输出中提取首条非空摘要行。

    :param str_output: 子进程返回的原始输出文本，dtype=str，unit=text
    :return: 返回去除首尾空白后的首行文本；没有有效行时返回空字符串，dtype=str，unit=text
    """

    # 先按行拆开并去掉整体首尾空白，保证版本摘要不会带尾部换行。
    list_lines = str_output.strip().splitlines()  # 原始输出规整后的逐行文本列表

    # 没有任何有效输出时直接回空字符串，让上层保持“探针无结果”的稳定语义。
    if not list_lines:

        # 当前探针没有返回可展示的版本摘要，因此报告空文本。
        return ""

    # 只取首行即可满足当前环境报告需要，避免把冗长 banner 直接塞进 JSON。
    return list_lines[0].strip()

# 对支持版本探针的 Synopsys 工具执行轻量版本检查。
def detect_tool_version(str_path: str) -> str:
    """
    探测工具的首行版本摘要。

    :param str_path: 工具可执行文件路径；为空时直接返回空字符串，dtype=str，unit=path
    :return: 返回首条可用版本摘要；所有探针都失败时返回空字符串，dtype=str，unit=text
    """

    # 没有可执行路径时不做任何探针，避免把空路径直接交给 subprocess。
    if not str_path:

        # 当前工具没有定位到真实路径，因此不生成版本摘要。
        return ""

    # 把常见版本开关排成固定顺序，便于在不同 Synopsys 工具之间复用同一探测流程。
    tuple_probes = ((str_path, "-ID"), (str_path, "-version"), (str_path, "-V"))  # 当前版本探针候选命令序列

    # 按既定顺序逐个执行探针，首个返回有效输出的结果就可以结束。
    for tuple_cmd in tuple_probes:

        # 为当前版本开关实际拉起一次子进程，避免外层循环把执行结果隐式吞掉。
        try:

            # 保存当前探针进程的完成结果，后面只读取合并后的标准输出摘要。
            obj_completed_process = subprocess.run(  # 当前版本探针的完成结果对象
                tuple_cmd,  # 直接把本轮探针命令元组交给子进程执行
                text=True,  # 统一按文本模式读取工具输出
                encoding="utf-8",  # 版本探针输出一律按 UTF-8 解码
                errors="replace",  # 遇到异常字节时用替换策略保住摘要提取流程
                stdout=subprocess.PIPE,  # 合并输出前先把标准输出留在内存里供摘要提取
                stderr=subprocess.STDOUT,  # 标准错误并入标准输出，保证首行摘要只走一条提取路径
                timeout=10,  # 单个版本探针最长等待十秒，避免异常工具拖死全局预检
            )

        # 单个版本开关执行失败时，不要把工具误判为缺失，直接切换到下一种探针写法。
        except (OSError, subprocess.SubprocessError):

            # 当前版本开关已经证实不可用，因此继续尝试备用版本参数。
            continue

        # 只抽取首条摘要行，避免冗长 banner 污染最终 JSON 报告。
        str_version_line = _first_output_line(obj_completed_process.stdout or "")  # 当前探针返回的首行版本摘要

        # 一旦拿到有效版本摘要，就直接返回给上层工具报告。
        if str_version_line:

            # 当前探针已经得到稳定结果，不再继续后续版本开关。
            return str_version_line

    # 所有探针都没有拿到有效输出时，统一回空字符串。
    return ""

# 检查当前系统上的 ``sh`` 是否支持技能脚本会用到的 ``-h`` 包装形式。
def check_sh_compat(which_func: Callable[[str], str | None] | None = None) -> JsonDict:
    """
    检查 ``sh -h -c`` 这一调用约定是否可用。

    :param which_func: 可选的 which 函数注入点；为空时回落到 ``shutil.which``，dtype=Callable[[str], str | None] | None，unit=callable
    :return: 返回包含可用性、路径、``-h`` 支持情况和 stderr 的报告对象，dtype=dict[str, Any]，unit=mapping
    """

    # 明确本次 shell 探测实际使用哪个 which 实现，方便测试注入和生产路径共用一套逻辑。
    func_which = which_func or shutil.which  # 当前 shell 兼容性检查采用的 which 函数

    # 没有显式找到 ``sh`` 时继续尝试 ``/bin/sh``，保持 Linux 远端环境下的常见默认值。
    str_path = func_which("sh") or "/bin/sh"  # 当前用于 shell 兼容性探测的可执行路径

    # 对候选 shell 发起一次真实空命令调用，用返回码确认包装器参数是否兼容。
    try:

        # 保存 shell 探针的完成结果，后面会同时消费返回码和标准错误摘要。
        obj_completed_process = subprocess.run(  # 当前 shell 兼容性探针的完成结果对象
            [str_path, "-h", "-c", "exit 0"],  # 用最小空命令验证 ``sh -h -c`` 约定是否成立
            text=True,  # shell 探针同样统一按文本模式读取 stdout / stderr
            stdout=subprocess.PIPE,  # 保留标准输出，便于后续需要时继续扩展诊断细节
            stderr=subprocess.PIPE,  # 同步捕获标准错误，方便解释 ``-h`` 被拒绝的原因
            timeout=10,  # shell 探针最多等待十秒，避免远端会话把检查流程拖死
        )

    # 如果连 shell 探针进程都拉不起来，就直接回不可用结论并保留已解析的路径。
    except (OSError, subprocess.SubprocessError):

        # 无法执行 ``sh`` 时直接报告不可用，并保留当前探测到的路径。
        return {
            "available": False,
            "path": str_path,
            "supports_dash_h": False,
            "stderr": "",
        }

    # 只要返回码为零，就说明 ``-h`` 包装约定在当前 shell 上真实成立。
    bool_available = obj_completed_process.returncode == 0  # 当前 shell 探针是否成功执行的布尔结论

    # 返回 shell 兼容性报告，供总体 warning 与 dry-run 诊断复用。
    return {
        "available": bool_available,
        "path": str_path,
        "supports_dash_h": bool_available,
        "stderr": obj_completed_process.stderr.strip(),
    }

# 把单个环境变量统一规整成 ``set/value`` 结构，便于 JSON 报告与测试断言复用。
def _env_entry(str_value: str) -> JsonDict:
    """
    构造单个环境变量的报告对象。

    :param str_value: 环境变量的当前字符串值；未设置时传空字符串，dtype=str，unit=text
    :return: 返回包含是否已设置与原始值的对象，dtype=dict[str, Any]，unit=mapping
    """

    # ``set`` 专门表达变量是否存在有效文本值，避免调用方反复手写 ``bool(value)``。
    return {"set": bool(str_value), "value": str_value}

# 解析 PATH 文本并保留实际可读的路径条目顺序。
def _path_entries(str_path_value: str) -> list[str]:
    """
    拆分 PATH 文本为路径条目列表。

    :param str_path_value: 原始 PATH 文本，dtype=str，unit=text
    :return: 返回去掉空条目后的路径列表，dtype=list[str]，unit=collection
    """

    # 默认先使用当前平台路径分隔符，再兼容远端 POSIX 风格 ``:`` 文本。
    str_split_token = os.pathsep  # 当前 PATH 解析采用的分隔符

    # 只有出现 POSIX 风格冒号且没有分号时，才切换成远端 Linux 常见格式。
    if ":" in str_path_value and ";" not in str_path_value:

        # 当前 PATH 文本更像远端 Linux 风格，因此按冒号拆分更符合预期。
        str_split_token = ":"  # 当前 PATH 需要切换到 POSIX 冒号分隔语义

    # 过滤空条目，避免报告里出现无意义的空字符串路径片段。
    return [str_entry for str_entry in str_path_value.split(str_split_token) if str_entry]

# 在工具选择逻辑里优先尊重 ``VCS_BIN``，保持与现有工作流对 vcs 路径覆盖的契约一致。
def _resolve_tool_path(
    str_tool_name: str,
    which_func: Callable[[str], str | None],
    dict_env_map: Mapping[str, str],
    path_exists_func: Callable[[str], bool],
) -> tuple[str, str]:
    """
    解析单个工具的最终路径与来源。

    :param str_tool_name: 当前需要解析的工具名，dtype=str，unit=identifier
    :param which_func: 工具查找函数，dtype=Callable[[str], str | None]，unit=callable
    :param dict_env_map: 当前环境变量映射，dtype=Mapping[str, str]，unit=mapping
    :param path_exists_func: 路径存在性检查函数，dtype=Callable[[str], bool]，unit=callable
    :return: 第一个返回值是工具路径，第二个返回值是路径来源标签，dtype=tuple[str, str]，unit=collection
    """

    # 先尝试走 PATH 查找常规安装位置，作为后续覆盖逻辑的初始基线。
    str_path = which_func(str_tool_name) or ""  # 当前工具按 PATH 查到的可执行路径

    # 根据 PATH 是否命中先给出一个初始来源标签，后面可能被 VCS_BIN 明确覆盖。
    str_source = "PATH" if str_path else ""  # 当前工具路径的来源标签

    # vcs 允许通过 ``VCS_BIN`` 显式覆盖，因此要在 PATH 之后再执行一次兜底提升。
    if str_tool_name == "vcs" and dict_env_map.get("VCS_BIN") and path_exists_func(dict_env_map["VCS_BIN"]):

        # 显式存在的 VCS_BIN 优先级更高，用它可以避免 PATH 指向旧版本或包装器。
        str_path = dict_env_map["VCS_BIN"]  # 当前工具路径切换为显式声明的 VCS_BIN

        # 把来源标签同步改成 VCS_BIN，方便最终报告解释覆盖来源。
        str_source = "VCS_BIN"  # 当前工具路径来源切换为 VCS_BIN

    # 返回当前工具最终采用的路径和来源标签。
    return str_path, str_source

# 构造工具可用性报告，让版本探针、路径来源和可执行状态都走同一出口。
def _tool_report(
    which_func: Callable[[str], str | None],
    dict_env_map: Mapping[str, str],
    version_func: Callable[[str], str],
    path_exists_func: Callable[[str], bool],
) -> JsonDict:
    """
    构造所有工具的可用性报告。

    :param which_func: 工具查找函数，dtype=Callable[[str], str | None]，unit=callable
    :param dict_env_map: 当前环境变量映射，dtype=Mapping[str, str]，unit=mapping
    :param version_func: 版本探针函数，dtype=Callable[[str], str]，unit=callable
    :param path_exists_func: 路径存在性检查函数，dtype=Callable[[str], bool]，unit=callable
    :return: 返回按工具名分组的报告对象，dtype=dict[str, Any]，unit=mapping
    """

    # 为所有工具准备统一承载容器，保证后续 readiness 只读取这一份事实源。
    json_dict_tool_reports: JsonDict = {}  # 当前环境检查得到的工具可用性映射

    # 按固定顺序逐个构造工具报告，保证 JSON 输出和文本摘要顺序稳定。
    for str_tool_name in tuple_tool_names:

        # 先缓存路径解析函数返回的二元结果，避免解包赋值误伤类型前缀规则。
        tuple_tool_resolution = _resolve_tool_path(str_tool_name, which_func, dict_env_map, path_exists_func)  # 当前工具的路径与来源二元组

        # 从路径解析结果里取出真实可执行路径，供后续版本探针和可用性判断复用。
        str_path = tuple_tool_resolution[0]  # 当前工具最终采用的可执行路径

        # 从同一份解析结果里取出来源标签，便于区分 PATH 与 VCS_BIN 覆盖来源。
        str_source = tuple_tool_resolution[1]  # 当前工具最终采用的路径来源标签

        # 只有支持版本探针且定位到真实路径的工具才会执行探针，避免无谓子进程开销。
        str_version = version_func(str_path) if str_path and str_tool_name in set_version_probe_tools else ""  # 当前工具的首行版本摘要

        # 统一构造单个工具的报告对象，供 readiness 与最终 JSON 摘要复用。
        json_dict_tool_reports[str_tool_name] = {  # 当前工具名对应的结构化可用性报告
            "available": bool(str_path),  # 首个字段固定保留可用性布尔值，便于摘要快速读取
            "path": str_path,  # 第二个字段回写真正解析到的可执行路径
            "source": str_source,  # 第三个字段说明路径来自 PATH 还是 VCS_BIN 覆盖
            "version": str_version,  # 最后一个字段记录首行版本摘要，便于人工核对具体工具版本
        }

    # 返回完整工具报告，供后续环境就绪判断统一消费。
    return json_dict_tool_reports

# 按固定白名单回写关键环境变量，避免调用方自行猜测哪些变量会影响技能行为。
def _environment_report(dict_env_map: Mapping[str, str]) -> JsonDict:
    """
    构造关键环境变量报告。

    :param dict_env_map: 当前环境变量映射，dtype=Mapping[str, str]，unit=mapping
    :return: 返回按环境变量名分组的报告对象，dtype=dict[str, Any]，unit=mapping
    """

    # 准备关键环境变量报告容器，确保所有白名单字段都能以固定结构输出。
    json_dict_env_report: JsonDict = {}  # 当前环境检查生成的关键环境变量报告

    # 所有关键环境变量都统一规整成 ``set/value`` 结构，方便测试和文本摘要读取。
    for str_env_name in tuple_env_var_names:

        # 缺失变量统一回空字符串，避免 None 泄漏到 JSON 结果里。
        str_value = dict_env_map.get(str_env_name, "")  # 当前环境变量对应的原始字符串值

        # 把当前变量规整成统一的 ``set/value`` 结构，避免下游消费字段漂移。
        json_dict_env_report[str_env_name] = _env_entry(str_value)  # 当前环境变量名对应的结构化报告对象

    # 返回完整环境变量报告，供 readiness 计算和最终输出复用。
    return json_dict_env_report

# 汇总 GUI 相关环境，重点检查 DISPLAY、XAUTHORITY 和 VNC_DISPLAY。
def _display_report(dict_env_map: Mapping[str, str]) -> JsonDict:
    """
    构造 GUI 相关显示环境报告。

    :param dict_env_map: 当前环境变量映射，dtype=Mapping[str, str]，unit=mapping
    :return: 返回包含 DISPLAY、XAUTHORITY 与 VNC_DISPLAY 状态的对象，dtype=dict[str, Any]，unit=mapping
    """

    # DISPLAY 是否存在是 GUI Verdi 能否直接启动的最关键前提。
    str_display_value = dict_env_map.get("DISPLAY", "")  # 当前环境里的 DISPLAY 文本

    # 返回 GUI 相关环境摘要，让 overall 与文本报告都能直接消费。
    return {
        "available": bool(str_display_value),
        "value": str_display_value,
        "xauthority_set": bool(dict_env_map.get("XAUTHORITY", "")),
        "vnc_display": dict_env_map.get("VNC_DISPLAY", ""),
    }

# 从 NOVAS_HOME 与 LD_LIBRARY_PATH 中提炼 PLI 相关线索，供用户快速判断 Verdi 非 GUI 能力。
def _pli_report(dict_env_map: Mapping[str, str]) -> JsonDict:
    """
    构造 Verdi PLI 线索报告。

    :param dict_env_map: 当前环境变量映射，dtype=Mapping[str, str]，unit=mapping
    :return: 返回包含 NOVAS_HOME 和 LD_LIBRARY_PATH 线索的对象，dtype=dict[str, Any]，unit=mapping
    """

    # `novas_hint_present` 会把这一项视作最强证据源，所以这里只负责先拿到显式声明的安装根目录。
    str_novas_home = dict_env_map.get("NOVAS_HOME", "")  # 显式声明的 PLI 安装根目录文本

    # 这一项反映的是 loader 现场搜索路径；后面只会在这段文本里检查 `novas` 和 `pli` 关键词是否出现。
    str_ld_library_path = dict_env_map.get("LD_LIBRARY_PATH", "")  # 运行时库搜索路径的原始文本快照

    # 这一份小写副本只为关键词匹配服务，避免后面每次比较都重复做大小写规整。
    str_ld_library_path_lower = str_ld_library_path.lower()  # 当前动态库搜索路径的小写副本

    # 当前报告只关心是否出现可解释的 PLI 线索，不尝试在本地真正探测文件存在性。
    return {
        "novas_home": str_novas_home,
        "novas_hint_present": (
            bool(str_novas_home)
            or "novas" in str_ld_library_path_lower
            or "pli" in str_ld_library_path_lower
        ),
        "ld_library_path_mentions_pli": "pli" in str_ld_library_path_lower,
    }

# 按既有安装布局定位 Verdi 自带的 NPI Python 解释器。
def find_npi_python(dict_env_map: Mapping[str, str], path_exists_func: Callable[[str], bool]) -> str:
    """
    定位 Verdi 自带的 NPI Python 解释器。

    :param dict_env_map: 当前环境变量映射，dtype=Mapping[str, str]，unit=mapping
    :param path_exists_func: 路径存在性检查函数，dtype=Callable[[str], bool]，unit=callable
    :return: 返回可用的 NPI Python 路径；未找到时返回空字符串，dtype=str，unit=path
    """

    # 显式设置的 VERDI_PYTHON 优先级最高，适合用户手动修正多版本安装。
    if dict_env_map.get("VERDI_PYTHON") and path_exists_func(dict_env_map["VERDI_PYTHON"]):

        # 用户显式给出的 Python 路径存在时，直接采用即可。
        return dict_env_map["VERDI_PYTHON"]

    # 两个候选 NPI Python 路径都会从这个根目录向下展开，所以先把唯一的拼接基准单独取出来。
    str_verdi_home = dict_env_map.get("VERDI_HOME", "")  # 用于拼接 NPI Python 候选路径的 Verdi 安装根目录文本

    # 缺少 Verdi 安装根目录时，自动推导候选解释器路径没有继续执行的基础。
    if not str_verdi_home:

        # 缺少 Verdi 安装根目录时，NPI Python 的自动定位也无从继续。
        return ""

    # 这里不是抽象候选集合，而是 Verdi 安装树中两个历史上最常见的 Python 可执行文件位置。
    list_candidates = [  # 当前 NPI Python 自动探测的候选路径列表
        Path(str_verdi_home) / "platform" / "linux64" / "Python" / "bin" / "python3.6",  # 优先尝试历史上更常见的 python3.6 可执行文件
        Path(str_verdi_home) / "platform" / "linux64" / "Python" / "bin" / "python3",  # 再退到更泛化的 python3 文件名
    ]

    # 按既定顺序逐个检查候选路径，首个存在的条目即可作为结果返回。
    for path_candidate in list_candidates:

        # 把候选 Path 规整成字符串，方便测试夹具直接模拟远端路径存在性。
        str_candidate = str(path_candidate)  # 当前候选 NPI Python 的字符串路径

        # 只有真实存在的候选解释器才值得拿来声明为可用的 NPI Python。
        if path_exists_func(str_candidate):

            # 当前候选路径已经存在，因此可以直接作为 NPI Python 结果返回。
            return str_candidate

    # 所有候选路径都不存在时，统一回空字符串。
    return ""

# 汇总非 GUI 波形读取能力，兼顾 fsdbreport、verdi 和 NPI Python 三条入口。
def _fsdb_report(dict_tools: JsonDict, str_npi_python: str) -> JsonDict:
    """
    构造 FSDB 读取能力报告。

    :param dict_tools: 工具可用性报告，dtype=dict[str, Any]，unit=mapping
    :param str_npi_python: 自动定位到的 NPI Python 路径；未找到时为空字符串，dtype=str，unit=path
    :return: 返回包含 readers、fsdbreport、verdi 与 NPI 状态的对象，dtype=dict[str, Any]，unit=mapping
    """

    # 先记录无需 GUI 就能直接读 FSDB 的原生命令入口，后面再按需补上 NPI 通道。
    list_readers = [str_name for str_name in ("fsdbreport", "verdi") if dict_tools[str_name]["available"]]  # 当前可直接读取 FSDB 的原生命令通道列表

    # 只有显式发现 NPI Python 时，才把 Python 读取通道加进最终 reader 列表。
    if str_npi_python:

        # 只有定位到 Verdi 自带 Python 时，才把 NPI 加进 readers 列表。
        list_readers.append("npi")

    # 返回非 GUI 波形读取能力摘要，供 overall readiness 与测试断言复用。
    return {
        "readers": list_readers,
        "fsdbreport_available": dict_tools["fsdbreport"]["available"],
        "verdi_available": dict_tools["verdi"]["available"],
        "npi_python_available": bool(str_npi_python),
        "npi_python": str_npi_python,
    }

# 汇总 shell 相关上下文，帮助后续判断远端包装器是否能按预期工作。
def _shell_report(
    dict_env_map: Mapping[str, str],
    which_func: Callable[[str], str | None],
    sh_compat_func: Callable[[], JsonDict] | None,
) -> JsonDict:
    """
    构造 shell 环境报告。

    :param dict_env_map: 当前环境变量映射，dtype=Mapping[str, str]，unit=mapping
    :param which_func: 工具查找函数，dtype=Callable[[str], str | None]，unit=callable
    :param sh_compat_func:
        可选的 shell 兼容性探针函数；为空时回落到 ``check_sh_compat``，
        dtype=Callable[[], dict[str, Any]] | None，unit=callable
    :return: 返回包含 SHELL、PATH_entries 与 sh_compat 的对象，dtype=dict[str, Any]，unit=mapping
    """

    # `_path_entries()` 只负责拆分目录条目；这一步先保留 PATH 原文，避免诊断时丢掉远端会话原始顺序。
    str_path_value = dict_env_map.get("PATH", "")  # 拆分 PATH 条目前保留的原始会话路径文本

    # 根据是否注入自定义探针选择 shell 兼容性来源，保证测试和生产路径都能复用。
    if sh_compat_func:

        # 上游已经给出 shell 探针结果时，直接复用这份已知结论。
        json_dict_sh_compat: JsonDict = sh_compat_func()  # 复用外部注入的 shell 兼容性报告

    # 没有注入探针时，回落到默认 ``sh -h -c`` 兼容性检查路径。
    else:

        # 这里显式走默认探针，确保本地与远端都遵守同一条 shell 预检契约。
        json_dict_sh_compat = check_sh_compat(which_func)  # 使用默认 shell 检查流程重新探测兼容性

    # 返回 shell 相关摘要，供 warning 判断与最终 JSON 报告直接消费。
    return {
        "SHELL": dict_env_map.get("SHELL", ""),
        "PATH_entries": _path_entries(str_path_value),
        "sh_compat": json_dict_sh_compat,
    }

# 统一生成许可证提示摘要，避免主流程重复拼接主变量和提示状态。
def _license_report(dict_env_map: Mapping[str, str], json_dict_env_report: JsonDict) -> JsonDict:
    """
    构造许可证提示摘要。

    :param dict_env_map: 当前环境变量映射，dtype=Mapping[str, str]，unit=mapping
    :param json_dict_env_report: 关键环境变量报告，dtype=dict[str, Any]，unit=mapping
    :return: 返回包含 ``hint_present``、``primary_var`` 与 ``value`` 的对象，dtype=dict[str, Any]，unit=mapping
    """

    # 先把许可证提示存在性压成单个布尔值，后续主变量选择和 overall 都复用这一份事实。
    bool_hint_present = (
        json_dict_env_report["SNPSLMD_LICENSE_FILE"]["set"]  # Synopsys 主许可证变量单独就能说明许可链路已暴露
        or json_dict_env_report["LM_LICENSE_FILE"]["set"]  # 旧式 LM 变量同样可以提供可解释的许可入口
    )  # 当前环境是否已经暴露出至少一个可解释的许可证线索

    # 再按主变量优先级选出最终展示字段名，保证人类可读摘要与 JSON 报告口径一致。
    str_primary_var = (
        "SNPSLMD_LICENSE_FILE"  # 优先展示 Synopsys 主许可证字段名
        if json_dict_env_report["SNPSLMD_LICENSE_FILE"]["set"]  # 主许可证变量存在时直接采用这一名称
        else "LM_LICENSE_FILE"  # 主变量缺失时退回兼容字段名
        if json_dict_env_report["LM_LICENSE_FILE"]["set"]  # 只有兼容变量存在时才保留该名称
        else ""  # 两个变量都缺失时统一返回空字段名
    )  # 最终在报告里展示的主许可证变量名

    # 许可证摘要只负责输出主变量、是否存在提示以及对应值，不夹带其他环境判断。
    return {
        "hint_present": bool_hint_present,
        "primary_var": str_primary_var,
        "value": dict_env_map.get(str_primary_var, "") if str_primary_var else "",
    }

# 把四类 readiness 布尔量集中构造成一份子报告，避免主流程散落重复条件拼接。
def _readiness_report(
    json_dict_tool_reports: JsonDict,
    json_dict_env_report: JsonDict,
    json_dict_display_report: JsonDict,
    str_npi_python: str,
) -> JsonDict:
    """
    构造总体 readiness 布尔量摘要。

    :param json_dict_tool_reports: 工具探测报告，dtype=dict[str, Any]，unit=mapping
    :param json_dict_env_report: 关键环境变量报告，dtype=dict[str, Any]，unit=mapping
    :param json_dict_display_report: GUI 显示环境报告，dtype=dict[str, Any]，unit=mapping
    :param str_npi_python: 自动定位到的 NPI Python 路径；未找到时为空字符串，dtype=str，unit=path
    :return: 返回包含四类 readiness 布尔量的对象，dtype=dict[str, Any]，unit=mapping
    """

    # Verilog 路径必须同时满足编译入口、vcs 本体和 VCS_HOME 三个前提。
    bool_ready_for_vcs = (
        json_dict_tool_reports["vlogan"]["available"]  # Verilog 主线先确认前端编译入口已经可见
        and json_dict_tool_reports["vcs"]["available"]  # 编译完成后还必须能继续承接 elaboration 与仿真
        and json_dict_env_report["VCS_HOME"]["set"]  # 包装脚本依赖稳定的 VCS 安装根目录
    )  # Verilog 主线是否具备从编译到仿真的基础通路

    # VHDL 路径与 Verilog 类似，只是首个编译入口换成了 vhdlan。
    bool_ready_for_vhdl = (
        json_dict_tool_reports["vhdlan"]["available"]  # VHDL 主线先确认专用编译入口已经可见
        and json_dict_tool_reports["vcs"]["available"]  # VHDL 编译产物仍要交给 vcs 承接后续阶段
        and json_dict_env_report["VCS_HOME"]["set"]  # VHDL 路径同样依赖统一的安装根目录
    )  # VHDL 主线是否保留进入 elaboration 与仿真的承接能力

    # GUI Verdi 分支只关注 viewer 本体和安装根目录，不依赖编译器可用性。
    bool_ready_for_verdi = (
        json_dict_tool_reports["verdi"]["available"]  # GUI viewer 本体必须先出现在 PATH 或显式路径上
        and json_dict_env_report["VERDI_HOME"]["set"]  # Verdi 安装根目录决定插件与脚本的推导基线
    )  # GUI Verdi 基础入口是否具备启动前提

    # 非 GUI 读取允许 fsdbreport 与 NPI Python 二选一，但仍然依赖 Verdi 根目录。
    bool_ready_for_nongui_verdi = (
        (json_dict_tool_reports["fsdbreport"]["available"] or bool(str_npi_python))  # 波形离线读取至少要保留一个 reader 入口
        and json_dict_env_report["VERDI_HOME"]["set"]  # 任何 reader 入口都要依赖 Verdi 安装根目录
    )  # 非 GUI 波形读取是否至少存在一条可行通路

    # 这一份子报告只输出 readiness 布尔量，让 overall 组装阶段直接复用。
    return {
        "ready_for_vcs": bool_ready_for_vcs,
        "ready_for_vhdl": bool_ready_for_vhdl,
        "ready_for_verdi": bool_ready_for_verdi,
        "ready_for_nongui_verdi": bool_ready_for_nongui_verdi,
        "ready_for_gui_verdi": bool_ready_for_verdi and json_dict_display_report["available"],
    }

# 把 blocker 与 warning 的累积逻辑折叠成单独 helper，避免主流程承载过多分支。
def _issue_report(
    json_dict_tool_reports: JsonDict,
    json_dict_env_report: JsonDict,
    json_dict_display_report: JsonDict, json_dict_pli_report: JsonDict,
    json_dict_shell_report: JsonDict,
    *,
    bool_license_hint_present: bool,
) -> JsonDict:
    """
    构造 blocker 与 warning 摘要。

    :param json_dict_tool_reports: 工具探测报告，dtype=dict[str, Any]，unit=mapping
    :param json_dict_env_report: 关键环境变量报告，dtype=dict[str, Any]，unit=mapping
    :param json_dict_display_report: GUI 显示环境报告，dtype=dict[str, Any]，unit=mapping
    :param json_dict_pli_report: PLI 线索报告，dtype=dict[str, Any]，unit=mapping
    :param json_dict_shell_report: shell 兼容性报告，dtype=dict[str, Any]，unit=mapping
    :param bool_license_hint_present: 是否至少声明了一个许可证提示变量，dtype=bool，unit=flag
    :return: 返回包含 ``blockers`` 与 ``warnings`` 列表的对象，dtype=dict[str, Any]，unit=mapping
    """

    # 这一份列表只保留会直接卡住流程的环境缺口，顺序就是终端摘要的展示顺序。
    list_blockers: list[str] = []  # 环境硬阻断列表

    # 另一份列表只收纳还能继续执行但解释结果前仍需人工复核的软风险。
    list_warnings: list[str] = []  # 环境软风险列表

    # Verilog 编译入口缺失时，直接把最核心的编译阻断压到 blocker 列表前部。
    if not json_dict_tool_reports["vlogan"]["available"]:

        # 缺少 vlogan 会让 Verilog 主线从第一步就无法继续。
        list_blockers.append("vlogan missing")

    # vcs 缺失时，elaboration 与仿真阶段都无法接续，因此要单独登记。
    if not json_dict_tool_reports["vcs"]["available"]:

        # 缺少 vcs 时，compile 后续的关键阶段全部失效。
        list_blockers.append("vcs missing")

    # 没有 VCS_HOME 时，现有包装器和安装根目录推导都失去稳定基线。
    if not json_dict_env_report["VCS_HOME"]["set"]:

        # VCS_HOME 缺失应直接阻断依赖安装根目录的后续路径构造。
        list_blockers.append("VCS_HOME unset")

    # GUI viewer 自身不存在时，波形加载与若干辅助脚本都没有入口。
    if not json_dict_tool_reports["verdi"]["available"]:

        # verdi 缺失意味着 GUI 路径完全不可走。
        list_blockers.append("verdi missing")

    # 没有 VERDI_HOME 时，Verdi 相关插件路径与 NPI 推导都不再可信。
    if not json_dict_env_report["VERDI_HOME"]["set"]:

        # VERDI_HOME 缺失要单独暴露，方便用户优先补齐安装根目录。
        list_blockers.append("VERDI_HOME unset")

    # DISPLAY 缺失时，GUI 会话无法直连当前终端环境。
    if not json_dict_display_report["available"]:

        # DISPLAY 缺失按历史语义继续保留为 blocker。
        list_blockers.append("DISPLAY unset")

    # 没有任何许可证提示时未必绝对失败，但必须提醒用户显式核对许可来源。
    if not bool_license_hint_present:

        # 许可证环境提示缺失时仍保留 warning，避免误导成绝对 blocker。
        list_warnings.append("license environment hint missing")

    # PLI 线索完全缺失时，Verdi 集成失败概率显著升高，因此给出观察性风险提示。
    if not json_dict_pli_report["novas_hint_present"]:

        # novas/PLI 线索缺失时提醒用户优先核对 PLI 相关安装与运行时环境。
        list_warnings.append("novas/PLI hint missing")

    # shell 可执行却拒绝 ``-h`` 时，包装器兼容性存在真实风险。
    if (
        json_dict_shell_report["sh_compat"].get("available")
        and not json_dict_shell_report["sh_compat"].get("supports_dash_h")
    ):

        # sh 对 ``-h`` 的拒绝需要显式提示给远端脚本包装路径。
        list_warnings.append("POSIX sh rejects -h")

    # 问题摘要只返回 blocker 与 warning 两类列表，不混入其他 readiness 布尔量。
    return {"blockers": list_blockers, "warnings": list_warnings}

# 检查当前环境是否满足 VCS/Verdi 冒烟前提，并返回结构化报告。
def check_environment(
    *,
    which_func: Callable[[str], str | None] | None = None,
    env: Mapping[str, str] | None = None,
    sh_compat_func: Callable[[], JsonDict] | None = None,
    version_func: Callable[[str], str] | None = None,
    path_exists_func: Callable[[str], bool] | None = None,
) -> JsonDict:
    """
    构造当前环境的 VCS/Verdi 就绪报告。

    :param which_func: 可选工具查找函数；为空时回落到 ``shutil.which``，dtype=Callable[[str], str | None] | None，unit=callable
    :param env: 可选环境变量映射；为空时回落到 ``os.environ``，dtype=Mapping[str, str] | None，unit=mapping
    :param sh_compat_func:
        可选 shell 兼容性探针函数；为空时使用 ``check_sh_compat``，
        dtype=Callable[[], dict[str, Any]] | None，unit=callable
    :param version_func: 可选工具版本探针函数；为空时使用 ``detect_tool_version``，dtype=Callable[[str], str] | None，unit=callable
    :param path_exists_func: 可选路径存在性检查函数；为空时使用 ``Path.exists``，dtype=Callable[[str], bool] | None，unit=callable
    :return: 返回包含工具、环境、display、PLI、FSDB、shell、license 与 overall 的报告对象，dtype=dict[str, Any]，unit=mapping
    """

    # 先固定工具查找实现，避免后续 helper 各自重复决定使用哪一套 which 逻辑。
    func_which = which_func or shutil.which  # 当前环境检查采用的 which 函数

    # 再固定版本探针实现，让测试替身和真实子进程路径共享同一调用约定。
    func_version_probe = version_func or detect_tool_version  # 当前环境检查采用的版本探针函数

    # 把环境变量映射统一收敛到一份对象上，避免 helper 混用 os.environ 与测试注入值。
    dict_env_map = env or os.environ  # 当前环境检查采用的环境变量映射

    # 准备路径存在性探针，后续 VCS_BIN 与 NPI Python 都依赖这一条判断通路。
    func_path_exists = path_exists_func or (lambda str_path: Path(str_path).exists())  # 当前环境检查采用的路径存在性函数

    # 先构造工具探测结果，后续所有 readiness 判断都把它当成唯一的工具事实源。
    json_dict_tool_reports = _tool_report(func_which, dict_env_map, func_version_probe, func_path_exists)  # 当前环境检查生成的工具报告

    # 再构造关键环境变量结果，供许可证、安装根目录与 GUI 规则统一消费。
    json_dict_env_report = _environment_report(dict_env_map)  # 当前环境检查生成的环境变量报告

    # 单独整理 GUI 相关事实，后面 ready_for_gui_verdi 会直接读取这一份摘要。
    json_dict_display_report = _display_report(dict_env_map)  # 当前环境里的 GUI 显示条件摘要

    # 提取 PLI 线索摘要，后面 warning 逻辑只读取这一份结果而不重复解析环境变量。
    json_dict_pli_report = _pli_report(dict_env_map)  # 当前环境里的 PLI 线索摘要

    # 自动定位 Verdi 自带的 NPI Python，供非 GUI 读取能力判定复用。
    str_npi_python = find_npi_python(dict_env_map, func_path_exists)  # 当前环境自动定位到的 NPI Python 路径

    # 汇总 FSDB 读取能力，统一折叠 fsdbreport、verdi 与 NPI Python 三条入口。
    json_dict_fsdb_report = _fsdb_report(json_dict_tool_reports, str_npi_python)  # 当前环境里的 FSDB 读取能力摘要

    # 汇总 shell 兼容性与 PATH 上下文，供 warning 判断和后续人工诊断复用。
    json_dict_shell_report = _shell_report(dict_env_map, func_which, sh_compat_func)  # 当前环境里的 shell 兼容性摘要

    # 许可证摘要统一交给 helper 生成，避免主流程重复拼接主变量与取值逻辑。
    json_dict_license_report = _license_report(dict_env_map, json_dict_env_report)  # 当前环境里的许可证提示摘要

    # 四类 readiness 布尔量集中由 helper 构造，主流程只负责收口最终字段布局。
    json_dict_readiness_report = _readiness_report(  # 当前环境里的总体就绪布尔量摘要
        json_dict_tool_reports,  # 传入工具探测事实，供 helper 折叠主线能力
        json_dict_env_report,  # 传入关键环境变量事实，供 helper 判断安装根目录与许可证
        json_dict_display_report,  # 传入 GUI 显示条件，供 helper 评估图形链路
        str_npi_python,  # 传入自动定位出的 NPI Python 路径，供 helper 评估非 GUI reader
    )

    # blocker 与 warning 汇总同样复用 helper，避免在主流程里继续堆叠分支。
    json_dict_issue_report = _issue_report(  # 当前环境里的阻断项与风险提示摘要
        json_dict_tool_reports,  # 传入工具探测事实，供 helper 识别核心可执行缺口
        json_dict_env_report,  # 传入关键环境变量事实，供 helper 识别安装根目录缺口
        json_dict_display_report,  # 传入 GUI 显示条件，供 helper 判断 DISPLAY 阻断
        json_dict_pli_report,  # 传入 PLI 线索摘要，供 helper 生成集成风险提示
        json_dict_shell_report,  # 传入 shell 兼容性摘要，供 helper 判断包装器风险
        bool_license_hint_present=json_dict_license_report["hint_present"],  # 传入许可证提示布尔值，供 helper 选择 warning
    )

    # overall 字段需要把 readiness 与诊断列表合并成固定布局，便于工作流和测试继续复用。
    dict_json_dict_overall_report = {
        **json_dict_readiness_report,  # 先铺开四类 readiness 布尔量
        "license_hint_present": json_dict_license_report["hint_present"],  # 回写许可证提示是否存在
        "blockers": json_dict_issue_report["blockers"],  # 回写会直接阻断流程的硬缺口列表
        "warnings": json_dict_issue_report["warnings"],  # 回写仍需人工复核的软风险列表
    }  # 当前环境检查对外公开的 overall 摘要对象

    # 把各类子报告重新拼成最终输出对象，保持测试和技能工作流依赖的字段布局不变。
    return {
        "tools": json_dict_tool_reports,
        "env": json_dict_env_report,
        "display": json_dict_display_report,
        "pli": json_dict_pli_report,
        "fsdb": json_dict_fsdb_report,
        "shell": json_dict_shell_report,
        "license": json_dict_license_report,
        "overall": dict_json_dict_overall_report,
    }

# 输出人类可读摘要，方便终端直接快速浏览当前环境缺口。
def text_report(dict_report: JsonDict) -> str:
    """
    把结构化环境报告转成简短文本摘要。

    :param dict_report: ``check_environment`` 生成的结构化环境报告，dtype=dict[str, Any]，unit=mapping
    :return: 返回适合终端直接展示的多行文本摘要，dtype=str，unit=text
    """

    # 先准备标题行，后面的各类摘要片段都会按固定顺序拼接到这条总标题之后。
    list_header_lines = ["VCS/Verdi environment check"]  # 文本摘要最前面的标题行列表

    # 把工具状态规整成单独列表，避免在循环里使用重复的 append 裸调用语句。
    list_tool_lines = [  # 每个工具对应的一行 found/missing 文本
        f"- {str_name}: {'found' if dict_item['available'] else 'missing'}"  # 每行先输出工具名与 found/missing 结论
        + (f" at {dict_item['path']}" if dict_item["path"] else "")  # 只有存在真实路径时才在行尾补出定位信息
        for str_name, dict_item in dict_report["tools"].items()  # 保持和工具报告相同的顺序展开每一行
    ]

    # 把环境变量状态规整成另一组列表，便于人工把文本摘要和 JSON 结果逐项对照。
    list_env_lines = [  # 每个关键环境变量对应的一行 set/unset 文本
        f"- {str_name}: {'set' if dict_item['set'] else 'unset'}"  # 每行只保留 set/unset 结论，避免正文重复展开值
        for str_name, dict_item in dict_report["env"].items()  # 顺着 JSON 顺序输出，方便人工逐项比对
    ]

    # 把固定状态摘要折叠成独立列表，后面只需要做一次最终拼接即可。
    list_status_lines = [  # 与 display、PLI、shell 和 overall 布尔量对应的固定摘要行
        f"- DISPLAY available: {dict_report['display']['available']}",  # 首行先给出 GUI 是否具备显示出口
        f"- novas/PLI hint present: {dict_report['pli']['novas_hint_present']}",  # 第二行专门概括 PLI 线索是否已出现
        f"- /bin/sh supports -h: {dict_report['shell']['sh_compat'].get('supports_dash_h')}",  # 第三行只回答 shell 包装约定是否成立
        f"- ready_for_vcs: {dict_report['overall']['ready_for_vcs']}",  # Verilog 路径 readiness 单独展示
        f"- ready_for_vhdl: {dict_report['overall']['ready_for_vhdl']}",  # VHDL 路径 readiness 单独展示并与 Verilog 行对照
        f"- ready_for_verdi: {dict_report['overall']['ready_for_verdi']}",  # GUI Verdi 基础 readiness 单独展示
        f"- ready_for_nongui_verdi: {dict_report['overall']['ready_for_nongui_verdi']}",  # 非 GUI 波形读取 readiness 单独展示
        f"- ready_for_gui_verdi: {dict_report['overall']['ready_for_gui_verdi']}",  # GUI 链路是否真正打通作为最后一行收尾
    ]

    # blocker 列表只在非空时拼接，避免健康环境下的文本摘要出现空壳结尾。
    list_blocker_lines = (
        ["- blockers: " + ", ".join(dict_report["overall"]["blockers"])]  # 把全部阻断合并成一条紧凑摘要
        if dict_report["overall"]["blockers"]  # 没有 blocker 时完全省略这一段
        else []  # 没有 blocker 时直接返回空列表，不额外制造占位行
    )  # 需要附加到摘要末尾的 blocker 段列表

    # warning 摘要作为另一条可选尾段存在，专门表达“还能跑，但你最好再看一眼”的风险信息。
    list_warning_lines = (
        ["- warnings: " + ", ".join(dict_report["overall"]["warnings"])]  # 把全部 warning 合并成单独的风险提示行
        if dict_report["overall"]["warnings"]  # 没有 warning 时不生成额外结尾行
        else []  # 完全没有 warning 时就不追加任何风险尾段
    )

    # 将所有片段按稳定顺序拼接后返回，供需要完整多行摘要的调用方复用。
    return "\n".join(
        list_header_lines
        + list_tool_lines
        + list_env_lines
        + list_status_lines
        + list_blocker_lines
        + list_warning_lines
    )

# 提供一个极简 CLI，让技能和人工都能直接读取 JSON 或文本摘要。
def main() -> int:
    """
    运行环境检查 CLI 并输出报告。

    参数：
    - 无外部业务参数；命令行参数通过 ``argparse`` 解析。

    返回：
    - 成功把报告输出到约定通道时返回 ``0``。

    异常：
    - 无显式业务异常；解析参数、JSON 序列化和标准输出错误沿用 Python 默认行为。
    """

    # 先创建参数解析器，保持 CLI 只公开一条机器可读输出开关。
    parser = argparse.ArgumentParser(description="Check VCS and Verdi tool readiness.")  # 当前环境检查 CLI 使用的参数解析器

    # 再登记 JSON 输出开关，让上游自动化明确选择协议型 stdout。
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    # 解析命令行参数，后续只根据 ``--json`` 决定走哪一条输出通道。
    args = parser.parse_args()  # 当前 CLI 解析得到的参数对象

    # 始终先生成结构化环境报告，避免人类可读与机器可读两条分支各自重复探测环境。
    json_dict_report = check_environment()  # 当前环境检查生成的结构化报告对象

    # 机器可读模式允许完整写出协议型 JSON，供自动化工作流直接消费。
    if args.json:

        # JSON 模式用于技能工作流和测试，保持 sort_keys 便于稳定对比。
        json.dump(json_dict_report, sys.stdout, indent=2, sort_keys=True)

        # JSON 协议输出补一个换行，避免终端提示符直接贴到载荷尾部。
        sys.stdout.write("\n")

    # 人类可读模式只输出带前缀的短摘要，避免把结构化全文直接倾倒到终端。
    else:

        # 先计算 blocker 条数，方便把整体风险强度浓缩进一行摘要里。
        int_blocker_count = len(json_dict_report["overall"]["blockers"])  # 当前环境报告中的 blocker 数量

        # 这个计数只面向后续排查优先级，不参与任何 ready/not-ready 判定。
        int_warning_count = len(json_dict_report["overall"]["warnings"])  # 当前环境里仍待人工复核的软风险数量

        # 只打印一行总览结论，避免把多个字段拼成类似结构化报表的终端输出。
        print(f"> INFO: [Python] VCS/Verdi 环境检查已完成，blockers={int_blocker_count}，warnings={int_warning_count}")

    # 当前 CLI 没有独立失败退出码约定，成功输出报告即返回 0。
    return 0

# 允许脚本被直接调用，同时把退出码统一交给 ``main`` 返回值控制。
if __name__ == "__main__":

    # 直接脚本执行时把最终退出码交还给外层 shell，方便自动化按同一契约读取结果。
    raise SystemExit(main())
