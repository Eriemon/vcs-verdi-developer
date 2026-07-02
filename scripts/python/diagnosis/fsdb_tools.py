#!/usr/bin/env python3
"""FSDB 非 GUI 诊断辅助脚本。

本模块负责检查 FSDB 工件状态、构造 fsdbreport 与波形转换命令、优先生成 Verdi NPI 读取计划，并把 CLI 输出稳定收口为机器可读 JSON 或简短摘要。
stdout_protocol: json
当 CLI 使用 `--json` 时，stdout 输出单一 JSON 文本；否则仅输出带前缀的简短状态摘要。
"""

# 延后注解求值，避免脚本级类型提示在导入阶段带来额外前向依赖。
from __future__ import annotations

# 标准库中的命令行、序列化、宿主环境、子进程、标准输出与计时能力。
import argparse
import json
import os
import subprocess
import sys
import time

# 路径对象与通用类型契约用于组织 FSDB 计划和命令结果。
from pathlib import Path
from typing import Any, Callable, Mapping

# 统一判断文本路径是否存在，供 `find_verdi_python` 在未注入夹具时复用。
def _path_exists(str_path_text: str) -> bool:
    """
    判断字符串形式的路径是否存在。

    参数：
    - str_path_text: 待检测的文件系统路径文本。

    返回：
    - 路径存在时返回 `True`；否则返回 `False`。

    异常：
    - 无显式异常；路径解释失败场景由 `Path.exists()` 自身稳定吸收。
    """

    # 把传入的字符串先转成路径对象，再统一做存在性判断。
    path_candidate = Path(str_path_text)  # 当前待检测的候选路径对象

    # 直接返回存在性结果，供上层路径发现逻辑继续决策。
    return path_candidate.exists()

# 返回 Verdi NPI 启动横幅里常见的版权、版本与许可关键词。
def _npi_banner_keywords() -> tuple[str, ...]:
    """
    提供 NPI 横幅过滤使用的关键词元组。

    参数：
    - 当前函数不接收额外业务参数。

    返回：
    - 返回一组用于识别 Synopsys NPI 版权、版本与许可横幅的固定关键词。

    异常：
    - 无显式异常；函数只返回静态元组常量。
    """

    # 这些词全部来自 Synopsys NPI 启动横幅；命中时说明当前行不是波形采样正文。
    return (
        "NPI -",  # Native Programming Interface 版本标题前缀
        "Version V-",  # 版本号横幅里的固定字样
        "Copyright",  # 版权声明行
        "Synopsys",  # 厂商标识行
        "Licensed Products",  # License 说明行
        "Native Programming",  # NPI 能力说明行
        "solvnetplus",  # 技术支持链接行
        "License Key",  # 许可证键提示行
    )

# 读取 FSDB 工件的存在性与字节数状态，供所有诊断路径共用。
def fsdb_status(path: Path | str) -> dict[str, Any]:
    """
    统计 FSDB 工件的基础状态。

    参数：
    - path: 待检查的 FSDB 文件路径，可以是 `Path` 或字符串。

    返回：
    - 返回包含 `state`、`status`、`path` 与 `bytes` 的状态字典；缺失文件返回 `missing`，零字节文件返回 `zero`，正常文件返回 `present`。

    异常：
    - 无显式异常；路径解析与 `stat()` 调用失败场景由文件系统原生异常处理。
    """

    # 先把调用方输入统一收口成路径对象，避免后续混用字符串路径。
    path_fsdb = Path(path)  # 当前待检查的 FSDB 路径对象

    # 文件缺失时直接返回缺失状态，避免继续触发 `stat()` 异常。
    if not path_fsdb.exists():

        # 缺失工件要明确暴露为 missing，便于上游和 CLI 直接识别。
        return {
            "state": "missing",
            "status": "missing",
            "path": str(path_fsdb),
            "bytes": 0,
        }

    # 读取 FSDB 当前字节数，后续要据此区分 present 与 zero。
    int_size = path_fsdb.stat().st_size  # 当前 FSDB 工件的实际字节数

    # 零字节波形文件通常说明仿真导出失败，因此要独立标成 zero。
    str_state = "present" if int_size > 0 else "zero"  # 当前 FSDB 工件的结构化存在状态

    # 返回完整状态对象，供命令规划层或 CLI 直接消费。
    return {
        "state": str_state,
        "status": str_state,
        "path": str(path_fsdb),
        "bytes": int_size,
    }

# 在基础工件状态上追加“必须非零字节”的严格判定，供执行前守门复用。
def require_nonzero_fsdb(path: Path | str) -> dict[str, Any]:
    """
    断言 FSDB 工件存在且非零字节。

    参数：
    - path: 待检查的 FSDB 文件路径，可以是 `Path` 或字符串。

    返回：
    - 工件有效时返回 `status=passed` 的状态字典；缺失或零字节时返回 `status=failed` 与稳定原因文本。

    异常：
    - 无显式异常；底层文件检查异常由 `fsdb_status()` 负责暴露。
    """

    # 先复用基础状态检查，避免重复拼接路径和字节统计逻辑。
    dict_status = fsdb_status(path)  # 当前 FSDB 工件的基础状态对象

    # 只有真正存在且字节数大于零时，才允许后续读取或转换流程继续执行。
    if dict_status["state"] == "present":

        # 通过场景只补写稳定 passed 标记，其余字段沿用基础状态结果。
        return {**dict_status, "status": "passed"}

    # 缺失与零字节属于两种不同故障面，原因文本需要明确区分。
    str_reason = "FSDB is missing" if dict_status["state"] == "missing" else "FSDB is zero bytes"  # 当前失败场景对应的人类可读原因文本

    # 把失败原因写回结果字典，供 CLI、测试与上游证据层直接消费。
    return {**dict_status, "status": "failed", "reason": str_reason}

# 构造最基础的 fsdbreport 命令，供非 GUI 波形探针直接复用。
def build_fsdbreport_cmd(fsdb: Path | str, signal: str | None = None) -> list[str]:
    """
    构造 `fsdbreport` 命令参数列表。

    参数：
    - fsdb: 待读取的 FSDB 文件路径，shape=scalar，dtype=Path | str，unit=filesystem path。
    - signal: 可选目标信号路径，shape=scalar or none，dtype=str | None，unit=hierarchical signal path；提供时追加 `-s` 读取开关。

    返回：
    - 返回可直接传给 `subprocess.run()` 的命令参数列表，shape=(n_tokens,)，dtype=list[str]，unit=CLI token。

    异常：
    - 无显式异常；路径只被转成字符串，不在本函数内做存在性校验。

    数值风险：
    - 无数值计算风险；本函数只负责保持路径与信号文本的命令行拼接顺序稳定。
    """

    # 先把 FSDB 路径转成稳定字符串，作为 fsdbreport 的第二个位置参数。
    path_fsdb = Path(fsdb)  # 当前 fsdbreport 命令绑定的 FSDB 路径对象

    # 基础命令至少包含工具名和目标 FSDB 路径。
    list_cmd = ["fsdbreport", str(path_fsdb)]  # 当前 fsdbreport 命令的基础参数列表

    # 只有调用方显式指定信号时，才需要把 `-s` 信号查询参数写进命令。
    if signal:

        # 目标信号路径要紧跟 `-s` 进入命令，保持 fsdbreport 原生命令格式。
        list_cmd.extend(["-s", signal])

    # 返回最终命令列表，供 CLI 输出或实际执行阶段直接复用。
    return list_cmd

# 过滤掉 Verdi NPI 输出里的版权与版本横幅，保留真正的波形正文。
def filter_npi_banner(text: str) -> str:
    """
    去除 Verdi NPI 输出中的 banner 噪声。

    参数：
    - text: 原始 NPI 输出文本。

    返回：
    - 返回移除空行与 banner 头部后的纯净正文文本。

    异常：
    - 无显式异常；文本拆分和拼接依赖 Python 字符串内建能力。
    """

    # 先准备一个新列表，后续只追加真正属于波形正文的文本行。
    list_filtered_lines: list[str] = []  # 去掉 banner 后保留下来的有效正文行

    # 先把横幅关键词元组取出来，避免在逐行扫描阶段重复构造同一组静态文本。
    tuple_banner_keywords = _npi_banner_keywords()  # 当前 banner 过滤逻辑逐行比较使用的固定关键词元组

    # 逐行扫描原始文本，分别处理空行、banner 行和正文行。
    for str_line_raw in text.splitlines():

        # 先裁掉两端空白，便于后续统一判断空行与 banner 特征。
        str_line_stripped = str_line_raw.strip()  # 当前待判断文本行的去空白版本

        # 纯空白行不承载任何波形信息，应直接丢弃。
        if not str_line_stripped:

            # 空行对后续采样解析没有帮助，因此这里立即跳过。
            continue

        # 横幅关键字出现时说明这行属于工具头部噪声，不应混入正文。
        if any(str_keyword in str_line_stripped for str_keyword in tuple_banner_keywords):

            # banner 头部只会干扰后续 report 解析，所以这里直接忽略。
            continue

        # 走到这里说明文本行是有效正文，需要原样保留下来。
        list_filtered_lines.append(str_line_raw)

    # 按原有换行重新拼接正文内容，供上层继续解析或展示。
    return "\n".join(list_filtered_lines)

# 从调用方环境中定位 Verdi 自带的 NPI Python 解释器。
def find_verdi_python(
    *,
    env: Mapping[str, str] | None = None,
    path_exists_func: Callable[[str], bool] | None = None,
) -> str:
    """
    定位 Verdi 自带的 Python 解释器。

    参数：
    - env: 可选环境变量映射；缺省时回退到当前进程环境。
    - path_exists_func: 可选存在性判断函数；缺省时使用真实文件系统检查。

    返回：
    - 找到可用解释器时返回其绝对路径字符串；无法定位时返回空字符串。

    异常：
    - 无显式异常；路径探测失败场景统一回退为空字符串。
    """

    # 先把环境映射转成普通字典，便于后续多次读取同一组键值。
    dict_env_map = dict(env or os.environ)  # 当前探测 Verdi Python 时参考的环境快照

    # 路径探测函数允许测试注入夹具；未注入时回退到真实文件系统检查。
    func_path_exists = path_exists_func or _path_exists  # 当前 Verdi Python 探测复用的存在性判断函数

    # 调用方若已显式给出 VERDI_PYTHON，则应优先信任这个直接路径。
    str_verdi_python = str(dict_env_map.get("VERDI_PYTHON", ""))  # 环境里显式声明的 Verdi Python 路径文本

    # 显式路径存在时直接返回，避免再去拼接 `VERDI_HOME` 默认目录。
    if str_verdi_python and func_path_exists(str_verdi_python):

        # 明确返回调用方指定的解释器路径，保持外部覆盖优先级最高。
        return str_verdi_python

    # 若未声明显式解释器，则回退到 `VERDI_HOME` 下的标准安装目录探测。
    str_verdi_home = str(dict_env_map.get("VERDI_HOME", ""))  # 当前环境里声明的 Verdi 安装根目录

    # 完全没有 `VERDI_HOME` 时说明无法继续推导默认解释器位置。
    if not str_verdi_home:

        # 路径锚点缺失时稳定返回空字符串，交给上游走 CLI fallback。
        return ""

    # Synopsys 常见安装既可能带 `python3.6`，也可能直接暴露 `python3`。
    list_candidate_paths = [  # 按优先顺序尝试的 Verdi Python 候选路径列表
        Path(str_verdi_home) / "platform" / "linux64" / "Python" / "bin" / "python3.6",  # 老版本 Verdi 常见的内置 Python 解释器路径
        Path(str_verdi_home) / "platform" / "linux64" / "Python" / "bin" / "python3",  # 新一些安装里直接暴露的 python3 可执行路径
    ]

    # 依次验证候选路径，只要命中一个存在项就可停止搜索。
    for path_candidate in list_candidate_paths:

        # 候选路径最终要交给存在性探测函数，因此这里先转成字符串。
        str_candidate = str(path_candidate)  # 当前待验证的 Verdi Python 候选路径文本

        # 只要候选路径真实存在，就把它作为最终解释器返回给调用方。
        if func_path_exists(str_candidate):

            # 返回首个存在的候选项，保持探测顺序和结果稳定。
            return str_candidate

    # 所有候选路径都不存在时，说明当前环境无法提供 NPI Python。
    return ""

# 把 CLI 风格信号路径规整成 NPI 使用的点号形式。
def normalize_npi_signal_path(signal: str) -> str:
    """
    规范化 NPI 使用的信号路径。

    参数：
    - signal: 原始信号路径文本，shape=scalar，dtype=str，unit=hierarchical signal path；允许包含前导 `/` 与点号混用。

    返回：
    - 返回去掉前导 `/` 且使用点号分隔的 NPI 风格信号路径，shape=scalar，dtype=str，unit=npi signal path。

    异常：
    - 无显式异常；文本规整完全依赖 Python 字符串方法。

    数值风险：
    - 无数值计算风险；只做路径分隔符规整，调用方仍需保证信号层级语义正确。
    """

    # NPI 查询要求点号路径，因此要先去掉首尾空白和前导 `/`。
    str_normalized_signal = signal.strip().strip("/").replace("/", ".")  # 当前信号路径对应的 NPI 风格文本

    # 返回规整后的点号路径，供脚本模板直接插入 `sig_by_name()`。
    return str_normalized_signal

# 把 NPI 或点号风格信号路径规整成 CLI 使用的斜杠形式。
def normalize_cli_signal_path(signal: str) -> str:
    """
    规范化 CLI 使用的信号路径。

    参数：
    - signal: 原始信号路径文本，shape=scalar，dtype=str，unit=hierarchical signal path；允许缺少前导 `/` 或混用点号分隔。

    返回：
    - 返回始终带前导 `/` 且使用斜杠分隔的 CLI 风格信号路径，shape=scalar，dtype=str，unit=cli signal path。

    异常：
    - 无显式异常；文本规整完全依赖 Python 字符串方法。

    数值风险：
    - 无数值计算风险；只做文本规范化，不验证层级路径是否真实存在于波形中。
    """

    # 先裁掉两端空白，保证后续前导斜杠处理不受外部空格影响。
    str_signal_text = signal.strip()  # 当前待规范化信号路径的去空白版本

    # 调用方若已携带前导 `/`，这里先暂时剥掉，避免最终结果出现双斜杠。
    if str_signal_text.startswith("/"):

        # 统一去掉最左侧斜杠，后面再由返回表达式重新补一个标准前缀。
        str_signal_text = str_signal_text[1:]  # 去掉旧前缀后再统一补回标准单个斜杠

    # 返回带标准前导 `/` 的 CLI 风格路径，供 fsdbreport 命令直接使用。
    return "/" + str_signal_text.replace(".", "/")

# 为嵌入 Python 字面量的 FSDB 路径做反斜杠转义，避免 Windows 路径破坏脚本内容。
def _escaped_fsdb_literal(path_fsdb: Path) -> str:
    """
    生成可直接嵌入 NPI 脚本的 FSDB 路径字面量。

    参数：
    - path_fsdb: 待写入内嵌脚本的 FSDB 路径对象。

    返回：
    - 返回解析成绝对路径后、已对反斜杠做双写转义的文本。

    异常：
    - 无显式异常；路径解析失败场景由 `Path.resolve()` 自身处理。
    """

    # NPI 子脚本在独立 Python 进程里执行，因此要先固化成绝对路径文本。
    str_path_text = str(path_fsdb.resolve())  # 当前 FSDB 工件对应的绝对路径文本

    # Windows 反斜杠要额外双写，避免内嵌脚本把它当成转义前缀。
    str_escaped_path = str_path_text.replace("\\", "\\\\")  # 可安全嵌入 Python 字面量的 FSDB 路径文本

    # 返回转义完成的路径文本，供各类 NPI 脚本模板共用。
    return str_escaped_path

# 生成 NPI 直读单个信号的内嵌 Python 脚本文本。
def _build_npi_read_signal_script(path_fsdb: Path, signal: str) -> str:
    """
    构造 NPI 读取单个信号的脚本文本。

    参数：
    - path_fsdb: 目标 FSDB 路径对象。
    - signal: 已规范化为点号风格的 NPI 信号路径。

    返回：
    - 返回可通过 `python -c` 执行的 NPI 波形读取脚本文本。

    异常：
    - 无显式异常；脚本文本仅做字符串拼装，不在本函数内执行。
    """

    # 先把 FSDB 路径转成脚本可安全引用的字符串字面量。
    str_fsdb_literal = _escaped_fsdb_literal(path_fsdb)  # 当前 NPI 读取脚本使用的 FSDB 绝对路径字面量

    # 返回按固定步骤初始化、读取波形并遍历 value change trace 的脚本文本。
    return (
        "from pynpi import npisys, waveform\n"
        "npisys.init([''])\n"
        f"fh = waveform.open('{str_fsdb_literal}')\n"
        f"sig = fh.sig_by_name('{signal}')\n"
        "fh.add_to_sig_list(sig)\n"
        "fh.load_vc_by_range(0, fh.max_time())\n"
        "vct = sig.create_vct()\n"
        "print('Time(1ps) ' + sig.full_name())\n"
        "ret = vct.goto_first()\n"
        "while ret:\n"
        "    print('%d %s' % (vct.time(), vct.value(waveform.VctFormat_e.HexStrVal)))\n"
        "    ret = vct.goto_next()\n"
        "npisys.end()\n"
    )

# 生成 NPI 列出信号范围的内嵌 Python 脚本文本。
def _build_npi_list_signal_script(path_fsdb: Path, scope: str, depth: int) -> str:
    """
    构造 NPI 列举信号范围的脚本文本。

    参数：
    - path_fsdb: 目标 FSDB 路径对象。
    - scope: 已规范化为点号风格的作用域路径。
    - depth: 计划列举的最大层级深度。

    返回：
    - 返回可通过 `python -c` 执行的 NPI 范围枚举脚本文本。

    异常：
    - 无显式异常；脚本文本仅做字符串拼装，不在本函数内执行。
    """

    # 这个范围列举脚本通常脱离当前工作目录执行，因此这里先把波形路径固化成绝对字面量。
    str_fsdb_literal = _escaped_fsdb_literal(path_fsdb)  # 当前 NPI 列举脚本使用的 FSDB 绝对路径字面量

    # 返回最小列举脚本，先打开波形文件，再打印 scope 与 depth 摘要。
    return (
        "from pynpi import npisys, waveform\n"
        "npisys.init([''])\n"
        f"fh = waveform.open('{str_fsdb_literal}')\n"
        f"scope_name = '{scope}'\n"
        f"max_depth = {depth}\n"
        "print('LIST_SIGNALS depth=%d scope=%s' % (max_depth, scope_name))\n"
        "npisys.end()\n"
    )

# 生成 NPI 读取波形概览信息的内嵌 Python 脚本文本。
def _build_npi_info_script(path_fsdb: Path) -> str:
    """
    构造 NPI 读取 FSDB 基础信息的脚本文本。

    参数：
    - path_fsdb: 目标 FSDB 路径对象。

    返回：
    - 返回可通过 `python -c` 执行的 NPI 信息读取脚本文本。

    异常：
    - 无显式异常；脚本文本仅做字符串拼装，不在本函数内执行。
    """

    # 信息探针脚本只靠内嵌文本执行，因此同样要先固化目标波形的绝对路径字面量。
    str_fsdb_literal = _escaped_fsdb_literal(path_fsdb)  # 当前 NPI 信息脚本使用的 FSDB 绝对路径字面量

    # 返回最小信息探测脚本，只输出固定标题和波形最大时间。
    return (
        "from pynpi import npisys, waveform\n"
        "npisys.init([''])\n"
        f"fh = waveform.open('{str_fsdb_literal}')\n"
        "print('FSDB_INFO')\n"
        "print('max_time=%s' % fh.max_time())\n"
        "npisys.end()\n"
    )

# 根据动作类型选择对应的 NPI 脚本文本模板。
def _npi_script(
    action: str,
    fsdb: Path,
    *,
    signal: str | None = None,
    scope: str | None = None,
    depth: int = 2,
) -> str:
    """
    选择当前动作所需的 NPI 脚本文本。

    参数：
    - action: 当前读取动作名。
    - fsdb: 目标 FSDB 路径对象。
    - signal: 可选目标信号路径。
    - scope: 可选作用域路径。
    - depth: 作用域枚举时的层级深度。

    返回：
    - `read-signal` 返回单信号读取脚本，`list-signals` 返回范围列举脚本，其余动作回退到基础信息脚本。

    异常：
    - 无显式异常；动作分派仅基于文本判断，不在本函数内校验合法性。
    """

    # 单信号读取必须把信号名先转成 NPI 点号路径，再交给专用模板生成脚本。
    if action == "read-signal":

        # `sig_by_name()` 只接受点号路径，因此这里先规整传入信号名。
        str_npi_signal = normalize_npi_signal_path(signal or "")  # 当前单信号读取动作实际采用的 NPI 信号路径

        # 返回读取单个信号的 NPI 脚本文本，供后续 `python -c` 直接执行。
        return _build_npi_read_signal_script(fsdb, str_npi_signal)

    # 列举信号范围时要同时带上作用域与深度，便于输出里保留探测边界。
    if action == "list-signals":

        # 作用域路径同样要转成 NPI 点号格式，避免脚本里混入 CLI 风格斜杠。
        str_scope_filter = normalize_npi_signal_path(scope or "")  # 当前范围列举动作实际采用的 NPI 作用域路径

        # 返回列举信号范围的 NPI 脚本文本，供后续 `python -c` 直接执行。
        return _build_npi_list_signal_script(fsdb, str_scope_filter, depth)

    # 其余动作统一回退到基础信息脚本，保持旧行为对 `info` 和未知动作的兼容。
    return _build_npi_info_script(fsdb)

# 构造最小 fsdb2vcd 探针命令，供 CLI fallback 的 `info` 与 `list-signals` 共用。
def _fsdb_probe_cmd(path_fsdb: Path) -> list[str]:
    """
    构造最小 fsdb2vcd 探针命令。

    参数：
    - path_fsdb: 待探测的 FSDB 路径对象。

    返回：
    - 返回以 `fsdb2vcd` 为入口、仅探测时间窗边界的最小命令列表。

    异常：
    - 无显式异常；路径只被转成字符串，不在本函数内做存在性校验。
    """

    # 这个命令只探测最小时间窗，因此不会真正展开全部波形内容。
    return ["fsdb2vcd", str(path_fsdb), "-bt", "0", "-et", "0"]

# 优先走 Verdi NPI，再在必要时回退到 CLI 工具链，构造 FSDB 读取计划。
def build_fsdb_read_plan(
    fsdb: Path | str,
    *,
    action: str, signal: str | None = None, scope: str | None = None,
    depth: int = 2, output: Path | str | None = None,
    **dict_reader_overrides: Any,
) -> dict[str, Any]:
    """
    构造 FSDB 读取或转换计划。

    参数：
    - fsdb: 目标 FSDB 路径，shape=scalar，dtype=Path | str，unit=filesystem path。
    - action: 读取动作名，shape=scalar，dtype=str，unit=workflow action；支持 `info`、`list-signals`、`read-signal` 与 `convert-vcd`。
    - signal: 可选目标信号路径，shape=scalar or none，dtype=str | None，unit=hierarchical signal path；`read-signal` 时用于指定查询对象。
    - scope: 可选作用域路径，shape=scalar or none，dtype=str | None，unit=hierarchical scope path；`list-signals` 时用于描述探测范围。
    - depth: 可选作用域枚举深度，shape=scalar，dtype=int，unit=scope level。
    - output: `convert-vcd` 时要求提供的输出路径，shape=scalar or none，dtype=Path | str | None，unit=filesystem path。
    - dict_reader_overrides: 兼容旧调用方透传的关键字集合，
      shape=mapping，dtype=dict[str, Any]，unit=reader override map；
      目前识别 `env` 与 `path_exists_func`。

    返回：
    - FSDB 工件不合法时返回失败状态字典；其余场景返回 `status=planned` 的读取或转换计划字典，shape=mapping，dtype=dict[str, Any]，unit=read plan object。

    异常：
    - `convert-vcd` 缺少输出路径时抛出 `ValueError`。
    - CLI fallback 的 `read-signal` 缺少目标信号时抛出 `ValueError`。
    - 不受支持的 CLI fallback 动作也抛出 `ValueError`。

    数值风险：
    - 无矩阵或浮点数值风险；主要风险在于信号路径、作用域和输出路径语义不一致会导致生成错误的工具计划。
    """

    # 先把调用方输入统一收口成路径对象，避免后续重复构造 `Path`。
    path_fsdb = Path(fsdb)  # 当前读取计划绑定的 FSDB 路径对象

    # 兼容旧调用方传入的环境映射关键字，便于继续复用测试夹具和外部脚本。
    map_env_override: Mapping[str, str] | None = dict_reader_overrides.get("env")  # 旧调用口径透传的可选环境变量映射

    # 兼容旧调用方传入的存在性探测关键字，便于继续复用测试注入的路径判断函数。
    func_path_exists_override: Callable[[str], bool] | None = dict_reader_overrides.get("path_exists_func")  # 旧调用口径透传的可选路径存在性判断函数

    # 读取动作开始前必须先确认 FSDB 工件非空，否则后续工具命令没有执行意义。
    dict_status = require_nonzero_fsdb(path_fsdb)  # 当前 FSDB 工件的严格守门状态对象

    # 只要守门失败，就把失败结果原样返回给调用方，不再继续拼命令。
    if dict_status["status"] != "passed":

        # 这里直接回传失败原因，避免上层再自行拼接工件缺失或零字节说明。
        return dict_status

    # `convert-vcd` 是纯转换动作，不需要优先探测 NPI Python。
    if action == "convert-vcd":

        # 输出路径缺失时无法构造转换命令，因此要明确阻断并给出固定错误文本。
        if output is None:

            # 转换命令没有目标文件就无法成立，因此这里抛出稳定的错误消息。
            raise ValueError("> ERR: [Python] output is required for convert-vcd")

        # 转换计划只依赖 CLI 工具链，因此这里直接返回 planned 结果对象。
        return {
            "status": "planned",
            "mode": "cli",
            "action": action,
            "cmd": build_convert_cmd(path_fsdb, output),
            "non_gui": True,
        }

    # 若当前环境存在 Verdi 自带 Python，就优先选择 NPI 路径做非 GUI 读取。
    str_verdi_python = find_verdi_python(env=map_env_override, path_exists_func=func_path_exists_override)  # 当前环境可用的 Verdi Python 解释器路径

    # NPI 解释器可用时，读取动作优先走 NPI，便于获得更稳定的 FSDB 原生访问能力。
    if str_verdi_python:

        # 先生成与动作对应的内嵌 Python 脚本文本，后续直接交给 `python -c`。
        str_script = _npi_script(action, path_fsdb, signal=signal, scope=scope, depth=depth)  # 当前读取计划对应的 NPI 子脚本文本

        # `signal` 有值时同时暴露 NPI 与 CLI 两种风格路径，便于日志与 fallback 对照。
        str_npi_signal = normalize_npi_signal_path(signal or "") if signal else ""  # 当前读取计划对应的 NPI 风格信号路径

        # 结果对象里继续保留斜杠路径字段，后续若要切回 fsdbreport fallback 可以直接复用。
        str_cli_signal = normalize_cli_signal_path(signal or "") if signal else ""  # 供后续可能回落到 fsdbreport 的链路直接复用的斜杠信号路径

        # 返回 NPI 计划对象，供调用方直接执行或持久化为证据。
        return {
            "status": "planned",
            "mode": "npi",
            "action": action,
            "fsdb": str(path_fsdb),
            "cmd": [str_verdi_python, "-c", str_script],
            "script": str_script,
            "npi_signal": str_npi_signal,
            "cli_signal": str_cli_signal,
            "non_gui": True,
        }

    # CLI fallback 读取单个信号时必须有显式信号名，否则 fsdbreport 命令不成立。
    if action == "read-signal":

        # 单信号读取缺少目标路径时无法构造 `fsdbreport -s` 命令。
        if not signal:

            # 没有目标信号时直接抛错，比生成无意义的 fallback 计划更安全。
            raise ValueError("> ERR: [Python] signal is required for read-signal")

        # 把信号路径规整成 CLI 斜杠形式后，再交给 fsdbreport 命令生成函数。
        list_cmd = build_fsdbreport_cmd(path_fsdb, normalize_cli_signal_path(signal))  # 当前 CLI fallback 读取单信号动作的最终命令列表

    # `list-signals` 仅做最小探针，因此回退到统一的 fsdb2vcd 时间窗检查命令即可。
    elif action == "list-signals":

        # 范围列举的 CLI fallback 只能给出最小探针命令，而不是完整层级枚举结果。
        list_cmd = _fsdb_probe_cmd(path_fsdb)  # 当前 CLI fallback 列举动作采用的 fsdb2vcd 探针命令

    # `info` 同样只需要最小探针命令，确认工具链能读取 FSDB 即可。
    elif action == "info":

        # 基础信息读取的 CLI fallback 与信号列举共用同一条最小探针命令。
        list_cmd = _fsdb_probe_cmd(path_fsdb)  # 当前 CLI fallback 信息动作采用的 fsdb2vcd 探针命令

    # 其余动作在 CLI fallback 路径下没有稳定语义，应直接阻断。
    else:

        # 这里显式报出不支持的动作名，便于调用方尽快修正计划输入。
        raise ValueError(f"> ERR: [Python] unsupported FSDB reader action: {action}")

    # 降级模式也保留点号路径字段，方便后续和 NPI 计划对象做同构比对。
    str_npi_signal = normalize_npi_signal_path(signal or "") if signal else ""  # CLI 降级结果里仍保留的点号信号路径镜像字段

    # 这里的斜杠路径字段直接对应 CLI 实际执行时会交给 fsdbreport 的信号表示法。
    str_cli_signal = normalize_cli_signal_path(signal or "") if signal else ""  # CLI 降级路径真正会传给 fsdbreport 的斜杠信号字段

    # `scope` 与 `depth` 无法在 CLI fallback 里完全实现，只能以说明文本形式暴露给调用方。
    str_notes = "CLI fallback; scope=%s depth=%s" % (scope or "", depth)  # 当前 CLI fallback 计划对能力边界的摘要说明

    # 返回 CLI fallback 计划对象，提醒调用方这是能力受限的降级路径。
    return {
        "status": "planned",
        "mode": "cli",
        "action": action,
        "fsdb": str(path_fsdb),
        "cmd": list_cmd,
        "notes": str_notes,
        "npi_signal": str_npi_signal,
        "cli_signal": str_cli_signal,
        "non_gui": True,
    }

# 根据输入和输出后缀构造波形格式转换命令。
def build_convert_cmd(src: Path | str, dst: Path | str) -> list[str]:
    """
    构造波形格式转换命令。

    参数：
    - src: 输入波形文件路径。
    - dst: 输出波形文件路径。

    返回：
    - 返回支持的转换命令参数列表，目前覆盖 `fsdb->vcd`、`vcd->fsdb` 与 `vpd->fsdb`。

    异常：
    - 不支持的后缀组合会抛出 `ValueError`。
    """

    # 先把输入输出统一转成路径对象，后续再稳定读取后缀。
    path_src = Path(src)  # 当前转换命令的输入文件路径对象

    # 目标文件对象不仅决定输出路径，也决定本次转换最终要落到哪一类波形格式。
    path_dst = Path(dst)  # 本次转换最终希望落盘到的目标波形文件路径对象

    # 后缀判断要统一转成小写，避免大小写文件扩展名影响分支选择。
    str_src_suffix = path_src.suffix.lower()  # 当前输入文件的规范化后缀文本

    # 目标扩展名单独抽出来后，后续各个分支就不必重复做 `lower()`。
    str_dst_suffix = path_dst.suffix.lower()  # 输出文件后缀对应的目标波形格式标签

    # FSDB 导出到 VCD 时，应选择 `fsdb2vcd` 作为转换入口。
    if str_src_suffix == ".fsdb" and str_dst_suffix == ".vcd":

        # 返回最直接的 fsdb2vcd 命令列表，供上层立即执行或序列化。
        return ["fsdb2vcd", str(path_src), "-o", str(path_dst)]

    # VCD 回写成 FSDB 时，需要调用 `vcd2fsdb`。
    if str_src_suffix == ".vcd" and str_dst_suffix == ".fsdb":

        # 返回标准 vcd2fsdb 命令列表，保持输出文件通过 `-o` 明确声明。
        return ["vcd2fsdb", str(path_src), "-o", str(path_dst)]

    # VPD 转成 FSDB 时，需要调用 `vpd2fsdb`。
    if str_src_suffix == ".vpd" and str_dst_suffix == ".fsdb":

        # 返回标准 vpd2fsdb 命令列表，供调试计划复用。
        return ["vpd2fsdb", str(path_src), "-o", str(path_dst)]

    # 其余组合都不在当前辅助脚本的稳定支持范围内，应显式阻断。
    raise ValueError(f"> ERR: [Python] unsupported conversion: {str_src_suffix} -> {str_dst_suffix}")

# 构造“先转 VCD 再用非 GUI 工具核验”的最小调试计划。
def build_vcd_first_debug_plan(vcd: Path | str, fsdb: Path | str, signal: str | None = None) -> dict[str, Any]:
    """
    构造先 VCD 转换再做波形检查的调试计划。

    参数：
    - vcd: 输入 VCD 路径，shape=scalar，dtype=Path | str，unit=filesystem path。
    - fsdb: 中间或输出 FSDB 路径，shape=scalar，dtype=Path | str，unit=filesystem path。
    - signal: 可选目标信号路径，shape=scalar or none，dtype=str | None，unit=hierarchical signal path；提供时会注入 fsdbreport 命令。

    返回：
    - 返回包含输入、输出、命令列表与 `non_gui` 标记的计划字典，shape=mapping，dtype=dict[str, Any]，unit=debug plan object。

    异常：
    - 无显式异常；转换命令是否合法由 `build_convert_cmd()` 负责校验。

    数值风险：
    - 无数值计算风险；主要风险是路径或信号文本错误会让三步调试计划指向错误工件。
    """

    # 先把 VCD 路径规范化成路径对象，保持计划里输入输出表示一致。
    path_vcd = Path(vcd)  # 当前调试计划绑定的输入 VCD 路径对象

    # 目标 FSDB 是第二步与第三步共同引用的锚点，因此也要单独收口成路径对象。
    path_fsdb = Path(fsdb)  # 三步调试计划共享的目标 FSDB 文件路径对象

    # 三步命令顺序固定为转换、报告核验、Verdi 非 GUI 装载检查。
    list_commands = [  # 当前 VCD-first 调试计划的固定命令序列
        build_convert_cmd(path_vcd, path_fsdb),  # 第一步先把输入 VCD 转成可供后续工具复用的 FSDB
        build_fsdbreport_cmd(path_fsdb, signal),  # 第二步用 fsdbreport 对关键信号或基础可读性做非 GUI 检查
        ["verdi", "-ssf", str(path_fsdb), "-nologo", "-exit"],  # 第三步保留 Verdi 非 GUI 装载动作作为最终兼容性探针
    ]

    # 返回完整调试计划，供上游 workflow 直接序列化到证据或执行计划中。
    return {
        "status": "planned",
        "input": str(path_vcd),
        "output": str(path_fsdb),
        "commands": list_commands,
        "non_gui": True,
    }

# 把命令执行结果统一折叠成稳定结构，避免正常返回与超时返回字段漂移。
def _command_result(
    cmd: list[str], cwd: Path | str | None, returncode: int | None, status: str,
    elapsed_sec: float, stdout: str, stderr: str,
) -> dict[str, Any]:
    """
    组装命令执行结果字典。

    参数：
    - cmd: 本次执行的命令列表。
    - cwd: 本次执行使用的工作目录。
    - returncode: 子进程退出码；超时场景允许为 `None`。
    - status: 结构化执行状态，如 `passed`、`failed` 或 `timeout`。
    - elapsed_sec: 已记录的执行耗时，单位秒。
    - stdout: 本次执行捕获到的标准输出文本。
    - stderr: 本次执行捕获到的标准错误文本。

    返回：
    - 返回字段稳定的命令执行结果字典。

    异常：
    - 无显式异常；本函数仅负责结果字典拼装。
    """

    # 工作目录为空时统一回退成空字符串，避免结果对象里混入 `None`。
    str_cwd = str(Path(cwd)) if cwd is not None else ""  # 当前执行结果里要暴露的工作目录文本

    # 返回完整结构化结果对象，供测试、CLI 与证据层统一消费。
    return {
        "cmd": cmd,
        "cwd": str_cwd,
        "returncode": returncode,
        "status": status,
        "elapsed_sec": elapsed_sec,
        "stdout": stdout,
        "stderr": stderr,
    }

# 实际运行外部命令并把 stdout/stderr 折叠成机器可读证据。
def execute_command(cmd: list[str], *, timeout: int = 300, cwd: Path | str | None = None) -> dict[str, Any]:
    """
    执行外部命令并采集机器可读结果。

    参数：
    - cmd: 待执行的命令列表。
    - timeout: 子进程超时时间，单位秒。
    - cwd: 可选工作目录路径。

    返回：
    - 返回包含命令、工作目录、退出码、状态、耗时与 stdout/stderr 的结果字典。

    异常：
    - 无显式异常；命令超时会被折算成 `status=timeout` 的结构化结果对象。
    """

    # 先记录单调时间起点，后续所有结果都用同一计时基准计算耗时。
    float_started = time.monotonic()  # 当前外部命令执行开始时的单调时钟时间

    # 工作目录若存在，这里先转成 `Path`，避免 `subprocess.run()` 参数行继续膨胀。
    path_cwd = Path(cwd) if cwd is not None else None  # subprocess.run 实际接收的工作目录路径对象

    # 正常执行路径要尽量完整保留 stdout/stderr，供上游做后续证据聚合。
    try:

        # 直接调用子进程并捕获标准输出、标准错误与返回码。
        completed_process_run = subprocess.run(cmd, cwd=path_cwd, text=True, capture_output=True, timeout=timeout)  # 当前外部命令执行完成后的子进程结果对象

        # 命令退出码为 0 时记为 passed，其余退出码统一折算成 failed。
        str_status = "passed" if completed_process_run.returncode == 0 else "failed"  # 当前命令执行结果对应的结构化状态值

        # 正常完成场景的耗时单独抽出后，结果组装调用就不必再携带长表达式。
        float_elapsed_sec = round(time.monotonic() - float_started, 3)  # 当前外部命令正常结束时的执行耗时

        # 正常执行完成后，统一经由 `_command_result()` 返回稳定结果结构。
        return _command_result(
            cmd, cwd, completed_process_run.returncode, str_status,
            float_elapsed_sec, completed_process_run.stdout,
            completed_process_run.stderr,
        )

    # 命令超时时也要保留已捕获输出，并通过统一结构返回给上游。
    except subprocess.TimeoutExpired as obj_timeout_error:

        # 超时场景的耗时同样先落成独立变量，避免结果组装行再出现长表达式。
        float_elapsed_sec = round(time.monotonic() - float_started, 3)  # 当前外部命令超时时对应的执行耗时

        # 超时场景同样要经由统一 helper 返回稳定字段集合，避免调用方额外分支判断。
        return _command_result(
            cmd, cwd, None, "timeout",
            float_elapsed_sec, obj_timeout_error.stdout or "",
            obj_timeout_error.stderr or f"timeout after {timeout}s",
        )

# 把 fsdbreport 文本解析成信号头与采样列表，供离线检查和测试断言复用。
def parse_fsdbreport(text: str) -> dict[str, Any]:
    """
    解析 fsdbreport 的表格化输出文本。

    参数：
    - text: 原始 fsdbreport 文本内容。

    返回：
    - 返回包含 `signals` 与 `samples` 的结构化对象；无法识别表头时返回空信号列表与空样本列表。

    异常：
    - 无显式异常；异常格式的文本行会被稳定忽略。
    """

    # 表头在首次命中 `Time(...)` 行之前保持为空，用于后续分支判断。
    list_header_tokens: list[str] | None = None  # 当前 report 表头拆分得到的 token 列表

    # 采样行要按输入顺序逐条积累，供测试和证据读取保持时间序一致。
    list_samples: list[dict[str, Any]] = []  # 当前 report 里成功解析出的采样记录列表

    # 逐行扫描 report 文本，分别过滤空行、分隔线、表头与真正的采样行。
    for str_raw_line in text.splitlines():

        # 先裁掉两端空白，保证后续空行和分隔线判断更加稳定。
        str_line = str_raw_line.strip()  # 当前待解析 report 行的去空白版本

        # 纯空白行不承载表头或采样信息，应直接跳过。
        if not str_line:

            # 空白行只会打断视觉阅读，不参与真实采样解析。
            continue

        # 常见 report 分隔线不属于采样内容，因此也要提前剔除。
        if str_line.startswith("=") or set(str_line) <= {"-", " "}:

            # 纯分隔符行没有业务数据，保留只会干扰后续 token 数量判断。
            continue

        # 当前非空正文行要先拆成 token，后续再区分表头和数据行。
        list_parts = str_line.split()  # 当前 report 行按空白拆分得到的 token 列表

        # 首次命中 `Time` 开头行时，把它登记成后续所有样本共用的表头。
        if list_header_tokens is None and list_parts and list_parts[0].lower().startswith("time"):

            # 表头只需要记录一次，后续采样行都据此做列名对齐。
            list_header_tokens = list_parts  # 当前 report 后续所有采样行共用的列名表头 token 列表

            # 表头登记完成后不再把当前行当成数据样本处理。
            continue

        # 表头尚未出现时，其余正文行都不具备稳定列语义，因此直接忽略。
        if list_header_tokens is None:

            # 没有表头就无法建立列名和数值的映射关系，所以这里继续等待。
            continue

        # token 数少于表头列数时说明这一行不完整，不适合当成有效样本。
        if len(list_parts) < len(list_header_tokens):

            # 列数不足的行通常是残缺或噪声输出，应稳定跳过。
            continue

        # 先准备一个空映射，后续按表头顺序把每个信号的值逐项写进去。
        dict_values: dict[str, str] = {}  # 当前采样时间点各个信号对应的文本数值映射

        # 逐项遍历表头和样本正文，保证信号名与数值按同一顺序配对。
        for str_signal_name, str_signal_value in zip(list_header_tokens[1:], list_parts[1:]):

            # 每条映射记录都代表当前采样时刻某个信号对应的文本数值。
            dict_values[str_signal_name] = str_signal_value  # 当前采样时间点单个信号对应的文本数值

        # 解析成功的样本要保留时间戳和各信号数值，供测试逐项断言。
        list_samples.append({"time": list_parts[0], "values": dict_values})

    # 最终返回信号表头与样本列表；没有表头时回退到空列表保持结构稳定。
    return {
        "signals": list_header_tokens[1:] if list_header_tokens else [],
        "samples": list_samples,
    }

# 给 CLI 解析器补上统一的 `--json` 输出开关。
def _add_json_flag(argument_parser_target: argparse.ArgumentParser) -> None:
    """
    为指定解析器注册 `--json` 输出开关。

    参数：
    - argument_parser_target: 待注册 JSON 开关的 `ArgumentParser` 对象。

    返回：
    - 无返回值；开关定义直接写入传入解析器。

    异常：
    - 无显式异常；参数冲突由 `argparse` 自身处理。
    """

    # 所有子命令都支持 JSON 协议输出，因此这里统一挂同名开关。
    argument_parser_target.add_argument("--json", action="store_true")

# 构造当前脚本的完整 CLI 参数解析器。
def _build_cli_parser() -> argparse.ArgumentParser:
    """
    构造 `fsdb_tools.py` 的 CLI 参数解析器。

    参数：
    - 当前函数不接收额外业务参数。

    返回：
    - 返回已注册全部子命令和选项的 `ArgumentParser` 对象。

    异常：
    - 无显式异常；参数定义冲突由 `argparse` 自身处理。
    """

    # 顶层解析器负责承接脚本用途说明和全部子命令分派。
    argument_parser_cli = argparse.ArgumentParser(description="Build and inspect non-GUI FSDB utility commands.")  # 当前脚本的顶层 CLI 参数解析器

    # 子命令动作决定后续分派路径，因此这里强制要求调用方显式给出。
    action_subparsers: Any = argument_parser_cli.add_subparsers(dest="command", required=True)  # 当前 CLI 顶层解析器挂载的子命令分派动作

    # `status` 子命令负责读取 FSDB 文件是否存在以及是否非零字节。
    argument_parser_status = action_subparsers.add_parser("status")  # FSDB 工件状态检查子命令解析器

    # `status` 至少需要知道待检查的 FSDB 文件路径。
    argument_parser_status.add_argument("fsdb", type=Path)

    # 可选的严格模式会把零字节和缺失结果提升成 failed。
    argument_parser_status.add_argument("--require-nonzero", action="store_true")

    # `status` 同样支持 JSON 协议输出，便于脚本链路消费。
    _add_json_flag(argument_parser_status)

    # `fsdbreport-cmd` 子命令只负责回显 fsdbreport 命令本体，不实际执行。
    argument_parser_report = action_subparsers.add_parser("fsdbreport-cmd")  # fsdbreport 命令构造子命令解析器

    # 构造 fsdbreport 命令时需要目标 FSDB 路径作为位置参数。
    argument_parser_report.add_argument("fsdb", type=Path)

    # 调用方可选指定目标信号，生成 `-s` 参数。
    argument_parser_report.add_argument("--signal")

    # `fsdbreport-cmd` 同样支持 JSON 协议输出。
    _add_json_flag(argument_parser_report)

    # `convert-cmd` 子命令负责构造波形格式转换命令，不实际执行。
    argument_parser_convert = action_subparsers.add_parser("convert-cmd")  # 波形转换命令构造子命令解析器

    # 构造转换命令时必须同时提供输入和输出路径。
    argument_parser_convert.add_argument("src", type=Path)

    # 输出路径决定选用的转换工具，因此这里也定义成必填位置参数。
    argument_parser_convert.add_argument("dst", type=Path)

    # 转换命令构造结果通常要被自动化脚本直接抓取，因此这里也开放 JSON 输出模式。
    _add_json_flag(argument_parser_convert)

    # `vcd-debug-plan` 子命令用于生成“先转 VCD 再核验”的调试计划。
    argument_parser_vcd_plan = action_subparsers.add_parser("vcd-debug-plan")  # VCD-first 调试计划子命令解析器

    # 这个计划的第一步是读取输入 VCD，因此必须先提供 VCD 路径。
    argument_parser_vcd_plan.add_argument("vcd", type=Path)

    # 调试计划的第二步会生成 FSDB，因此输出 FSDB 路径同样必填。
    argument_parser_vcd_plan.add_argument("fsdb", type=Path)

    # 可选目标信号只会影响 fsdbreport 这一跳的命令构造。
    argument_parser_vcd_plan.add_argument("--signal")

    # 调试计划对象字段较多，比起终端摘要更适合直接通过 JSON 返回。
    _add_json_flag(argument_parser_vcd_plan)

    # `execute` 子命令的具体参数解析交给 `_parse_execute_cli_args()`，这里只保留帮助入口。
    argument_parser_execute = action_subparsers.add_parser("execute")  # 外部命令执行子命令解析器

    # 执行模式的帮助文本需要说明支持 `--` 分隔符，便于调用方传递与本脚本选项重名的下游参数。
    argument_parser_execute.description = (
        "Execute an external command and capture machine-readable evidence. "
        "Use `--` to separate wrapper options from downstream command options when needed."
    )

    # `parse-report` 子命令负责把现有 fsdbreport 文本解析成结构化对象。
    argument_parser_parse_report = action_subparsers.add_parser("parse-report")  # report 文本解析子命令解析器

    # 解析动作需要 report 文件路径作为输入位置参数。
    argument_parser_parse_report.add_argument("report", type=Path)

    # report 解析结果里含有样本列表，最适合通过 JSON 结构直接传递。
    _add_json_flag(argument_parser_parse_report)

    # `read-plan` 子命令负责统一规划 NPI 优先、CLI fallback 的读取动作。
    argument_parser_read_plan = action_subparsers.add_parser("read-plan")  # FSDB 读取计划子命令解析器

    # 读取计划的核心输入仍然是目标 FSDB 路径。
    argument_parser_read_plan.add_argument("fsdb", type=Path)

    # 调用方要显式选择计划动作类型，默认回到最保守的 `info`。
    argument_parser_read_plan.add_argument(
        "--action",
        choices=("info", "list-signals", "read-signal", "convert-vcd"),
        default="info",
    )

    # 单信号读取模式下可额外指定目标信号路径。
    argument_parser_read_plan.add_argument("--signal")

    # 范围列举模式下可额外指定 scope 文本。
    argument_parser_read_plan.add_argument("--scope")

    # 范围列举模式下可额外指定递归深度。
    argument_parser_read_plan.add_argument("--depth", type=int, default=2)

    # 仅 `convert-vcd` 动作需要输出路径，因此这里定义成可选参数。
    argument_parser_read_plan.add_argument("--output", type=Path)

    # 读取计划对象字段较多，这里也统一开放 JSON 输出模式供外部工作流消费。
    _add_json_flag(argument_parser_read_plan)

    # 顶层解析器也允许在子命令前直接带 `--json`，兼容旧习惯用法。
    _add_json_flag(argument_parser_cli)

    # 返回完整解析器，供 `main()` 统一解析命令行。
    return argument_parser_cli

# 为 `execute` 子命令构造专用解析器，避免下游命令选项被顶层 argparse 误吞。
def _build_execute_cli_parser() -> argparse.ArgumentParser:
    """
    构造 `execute` 子命令的专用参数解析器。

    参数：
    - 当前函数不接收额外业务参数。

    返回：
    - 返回只解析包装层选项的 `ArgumentParser` 对象；下游命令 token 由调用方单独保留。

    异常：
    - 无显式异常；参数冲突由 `argparse` 自身处理。
    """

    # 先准备 `execute` 包装层帮助文本，明确 `--` 分隔符只用于隔离包装层选项与下游命令选项。
    str_execute_description = (  # `execute` 子命令帮助文本
        "Execute an external command and capture machine-readable evidence. "  # 说明当前子命令负责执行外部命令并回收证据
        "Use `--` to separate wrapper options from downstream command options "  # 说明遇到包装层与下游选项重名时应显式分隔
        "when needed."  # 说明分隔符只在存在歧义时才需要使用
    )

    # 专用解析器只负责包装层选项，避免把下游命令参数误判成未知选项。
    argument_parser_execute = argparse.ArgumentParser(prog="fsdb_tools.py execute", description=str_execute_description)  # 保持包装层帮助文本与下游参数分隔约定一致的参数解析器

    # 工作目录是可选参数，未提供时回退到当前进程工作目录。
    argument_parser_execute.add_argument("--cwd", type=Path)

    # 超时时间默认 300 秒，足够覆盖常见小型离线诊断命令。
    argument_parser_execute.add_argument("--timeout", type=int, default=300)

    # 外部命令执行证据通常会被后续步骤回收，因此这里也挂上 JSON 开关。
    _add_json_flag(argument_parser_execute)

    # 返回只解析包装层选项的专用解析器，供 `main()` 在 execute 场景单独调用。
    return argument_parser_execute

# 为 `execute` 子命令解析包装层选项，并稳定保留下游命令 token。
def _parse_execute_cli_args(list_execute_args: list[str], *, bool_global_json: bool) -> argparse.Namespace:
    """
    解析 `execute` 子命令参数，同时保留下游命令 token。

    参数：
    - list_execute_args: `execute` 子命令后面的原始参数 token 列表。
    - bool_global_json: 顶层是否已显式开启全局 `--json`。

    返回：
    - 返回补齐 `command`、`cmd`、`cwd`、`timeout` 与 `json` 字段的命名空间对象。

    异常：
    - 包装层参数非法或命令列表为空时，底层 `argparse` 会按 CLI 约定退出。
    """

    # 先构造专用解析器，确保包装层选项和下游命令 token 走独立通道。
    argument_parser_execute = _build_execute_cli_parser()  # `execute` 子命令的专用参数解析器

    # 显式 `--` 分隔符存在时，前半段只解析包装层选项，后半段完整保留下游命令。
    if "--" in list_execute_args:

        # 找到首个 `--` 的位置后，把包装层选项和下游命令切成两段。
        int_delimiter_index = list_execute_args.index("--")  # `execute` 子命令参数里首个 `--` 分隔符的位置

        # 先切出分隔符前面的包装层参数列表，避免把下游命令 token 混进当前脚本选项解析器。
        list_execute_option_args: list[str] = list_execute_args[:int_delimiter_index]  # `execute` 子命令包装层参数 token 列表

        # 再解析包装层参数列表，得到供后续统一分派复用的命名空间对象。
        namespace_execute_args: argparse.Namespace = argument_parser_execute.parse_args(list_execute_option_args)  # `execute` 子命令包装层选项解析得到的命名空间

        # 分隔符后面的 token 原样交给外部命令执行器，保持下游选项顺序不变。
        list_cmd: list[str] = list_execute_args[int_delimiter_index + 1 :]  # 分隔符后保留下来的外部命令 token 列表

    # 未使用分隔符时，先提取包装层已知选项，其余 token 全部视为下游命令。
    else:

        # 先让 `parse_known_args()` 吞掉本脚本选项，并完整保留其余未知 token 作为下游命令。
        tuple_parse_result = argument_parser_execute.parse_known_args(list_execute_args)  # `execute` 子命令解析得到的包装层命名空间与保留下来的外部命令 token 结果

        # 先取出包装层已识别的命名空间，保持下游命令拆分后的静态类型边界清晰。
        namespace_execute_args: argparse.Namespace = tuple_parse_result[0]  # `parse_known_args()` 返回的包装层命名空间

        # 再取出需要透传给外部命令的 token 列表，供后续空命令检查与统一分派复用。
        list_cmd: list[str] = tuple_parse_result[1]  # `parse_known_args()` 返回的下游命令 token 列表

    # `execute` 至少需要一条外部命令；空命令应按 CLI 约定直接报错。
    if not list_cmd:

        # 明确提示命令列表缺失，避免调用方误以为包装层选项本身会执行任何动作。
        argument_parser_execute.error("execute requires a command to run")

    # 顶层 `--json` 与子命令 `--json` 任一开启时，都要遵循机器可读 stdout 协议。
    namespace_execute_args.json = namespace_execute_args.json or bool_global_json  # 当前命名空间最终采用的 JSON 输出协议开关

    # `execute` 分派依赖这两个字段，因此这里统一补齐供后续分发逻辑复用。
    namespace_execute_args.command = "execute"  # 当前命名空间对应的子命令名

    # 把已经保留下来的外部命令 token 登记回命名空间，供统一分派逻辑直接调用。
    namespace_execute_args.cmd = list_cmd  # 当前命名空间最终要执行的外部命令 token 列表

    # 返回已经补齐分派字段的命名空间对象，供后续命令执行逻辑直接消费。
    return namespace_execute_args

# 统一解析 CLI 参数，兼容 `execute` 子命令对下游命令 token 的透传需求。
def _parse_cli_args() -> argparse.Namespace:
    """
    解析当前脚本的 CLI 参数。

    参数：
    - 当前函数不接收额外业务参数；原始 token 直接来自 `sys.argv`。

    返回：
    - 返回适配普通子命令或 `execute` 子命令的参数命名空间对象。

    异常：
    - 参数非法时，底层 `argparse` 会按 CLI 约定退出。
    """

    # 先复制一份原始参数列表，后续会在本地列表上剥离顶层 `--json` 开关。
    list_raw_args = list(sys.argv[1:])  # 当前 CLI 调用剥离脚本名后的原始参数 token 列表

    # 顶层 `--json` 允许出现在子命令前，因此这里先单独剥离并记住它。
    bool_global_json = False  # 当前 CLI 是否通过顶层参数显式要求 JSON 输出

    # 连续出现多个顶层 `--json` 时也保持幂等，只要记录“已启用”即可。
    while list_raw_args and list_raw_args[0] == "--json":

        # 只要命中一次顶层 `--json`，后续输出就必须遵循 JSON 协议。
        bool_global_json = True  # 顶层 JSON 输出协议已显式开启

        # 消费掉已经识别的顶层 `--json` token，便于后续判断真正的子命令名。
        list_raw_args = list_raw_args[1:]  # 去掉已经消费的顶层 `--json` token 后剩余的原始参数列表

    # `execute` 子命令需要专用解析路径，避免下游命令选项被普通子解析器误吞。
    if list_raw_args and list_raw_args[0] == "execute":

        # 专用解析器只接收 `execute` 之后的 token，并显式继承顶层 JSON 开关。
        return _parse_execute_cli_args(list_raw_args[1:], bool_global_json=bool_global_json)

    # 其余普通子命令仍沿用原有统一解析器，保持既有 CLI 协议不变。
    argument_parser_cli = _build_cli_parser()  # 当前普通子命令场景使用的完整 CLI 参数解析器

    # 顶层 `--json` 被提前剥离后，需要在普通解析路径里再补回去。
    if bool_global_json:

        # 把顶层 JSON 开关放回参数首位，保持现有普通子命令兼容性。
        list_raw_args = ["--json", *list_raw_args]  # 补回顶层 JSON 开关后的普通子命令参数列表

    # 返回普通子命令解析得到的命名空间对象，供后续统一分派逻辑复用。
    return argument_parser_cli.parse_args(list_raw_args)

# 根据子命令类型分派到对应 helper，并返回统一结构化结果。
def _dispatch_cli_command(namespace_cli_args: argparse.Namespace) -> dict[str, Any]:
    """
    分派 CLI 子命令并返回结构化结果。

    参数：
    - namespace_cli_args: `argparse` 解析后的 CLI 参数对象。

    返回：
    - 返回对应子命令生成的结构化结果字典。

    异常：
    - 参数不满足各 helper 契约时，底层 helper 可能抛出 `ValueError`。
    """

    # `status` 命令根据是否要求非零字节，分别走基础状态或严格守门逻辑。
    if namespace_cli_args.command == "status":

        # 严格模式下必须调用非零字节守门逻辑，否则只回显基础工件状态。
        if namespace_cli_args.require_nonzero:

            # 返回严格守门结果，让零字节和缺失工件直接暴露为 failed。
            return require_nonzero_fsdb(namespace_cli_args.fsdb)

        # 非严格模式只读取基础存在状态，保留 missing/zero/present 三态。
        return fsdb_status(namespace_cli_args.fsdb)

    # `fsdbreport-cmd` 命令只回显命令列表本体，不实际执行外部程序。
    if namespace_cli_args.command == "fsdbreport-cmd":

        # 返回稳定字典包装，便于 JSON 模式与测试断言直接消费。
        return {"cmd": build_fsdbreport_cmd(namespace_cli_args.fsdb, namespace_cli_args.signal)}

    # `convert-cmd` 命令只构造转换命令，不实际触发波形格式转换。
    if namespace_cli_args.command == "convert-cmd":

        # 返回稳定字典包装，保持和其他命令的 JSON 输出结构一致。
        return {"cmd": build_convert_cmd(namespace_cli_args.src, namespace_cli_args.dst)}

    # `vcd-debug-plan` 命令返回三步固定命令组成的最小非 GUI 调试计划。
    if namespace_cli_args.command == "vcd-debug-plan":

        # 直接返回结构化调试计划对象，供调用方后续序列化或执行。
        return build_vcd_first_debug_plan(
            namespace_cli_args.vcd,
            namespace_cli_args.fsdb,
            signal=namespace_cli_args.signal,
        )

    # `execute` 命令是唯一会真正触发外部子进程执行的子命令。
    if namespace_cli_args.command == "execute":

        # 返回稳定命令证据对象，供测试和证据层统一消费。
        return execute_command(namespace_cli_args.cmd, timeout=namespace_cli_args.timeout, cwd=namespace_cli_args.cwd)

    # `read-plan` 命令统一规划 NPI 优先、CLI fallback 的读取动作。
    if namespace_cli_args.command == "read-plan":

        # 返回结构化读取计划对象，让上游清晰区分 action、mode 与命令字段。
        return build_fsdb_read_plan(
            namespace_cli_args.fsdb,
            action=namespace_cli_args.action,
            signal=namespace_cli_args.signal,
            scope=namespace_cli_args.scope,
            depth=namespace_cli_args.depth,
            output=namespace_cli_args.output,
        )

    # 剩余合法子命令只可能是 `parse-report`，因此这里直接读取文本并解析。
    str_report_text = namespace_cli_args.report.read_text(encoding="utf-8", errors="replace")  # 当前 parse-report 命令读入的原始 report 文本

    # 返回结构化解析结果，供 JSON 输出与后续测试断言复用。
    return parse_fsdbreport(str_report_text)

# 为非 JSON 模式生成简短前缀摘要，避免把结构化对象直接打印到终端。
def _human_cli_summary(str_command_name: str, dict_cli_result: dict[str, Any]) -> str:
    """
    生成非 JSON CLI 的简短摘要文本。

    参数：
    - str_command_name: 当前执行的 CLI 子命令名。
    - dict_cli_result: 子命令返回的结构化结果对象。

    返回：
    - 返回满足 current-project 终端输出约束的前缀摘要文本。

    异常：
    - 无显式异常；摘要仅从结果对象中提取少量计数字段或状态字段。
    """

    # 状态类结果对象优先输出单值状态，方便终端快速判断通过或失败。
    if "status" in dict_cli_result:

        # 状态摘要只保留命令名和状态值，避免把结构化大对象直接刷到 stdout。
        return f'> INFO: [Python] command={str_command_name} status={dict_cli_result["status"]}'

    # 命令构造类结果对象优先暴露命令长度，证明命令已经被稳定拼装出来。
    if "cmd" in dict_cli_result:

        # 这里只汇报命令条目数，不直接打印完整参数列表以免污染终端输出。
        return f'> INFO: [Python] command={str_command_name} cmd_len={len(dict_cli_result["cmd"])}'

    # report 解析类结果对象更适合回显样本数，让终端快速确认是否解析到正文。
    if "samples" in dict_cli_result:

        # 样本计数足以帮助调用方确认 parser 是否读到有效采样行。
        return f'> INFO: [Python] command={str_command_name} sample_count={len(dict_cli_result["samples"])}'

    # 其余结构统一回退到键数量摘要，避免 stdout 混入大块结构化正文。
    return f"> INFO: [Python] command={str_command_name} key_count={len(dict_cli_result)}"

# 解析 CLI、执行子命令，并按协议输出 JSON 或简短摘要。
def main() -> int:
    """
    运行 `fsdb_tools.py` 的 CLI 入口。

    参数：
    - 当前函数不接收额外业务参数；命令行输入全部通过 `argparse` 解析。

    返回：
    - 成功解析并输出结果时返回 `0`。

    异常：
    - 参数非法或 helper 主动抛错时，异常会继续上传给脚本顶层调用方处理。
    """

    # 统一解析调用方输入，并在 `execute` 场景下保留下游命令 token。
    namespace_cli_args: argparse.Namespace = _parse_cli_args()  # 当前 CLI 调用解析得到的参数对象

    # 子命令分派统一收口到结构化结果对象，便于后续协议化输出。
    dict_cli_result = _dispatch_cli_command(namespace_cli_args)  # 当前 CLI 调用对应的结构化结果对象

    # JSON 协议输出时，stdout 必须只包含一段完整 JSON 文本。
    if namespace_cli_args.json:

        # 机器可读模式直接写 JSON，避免额外提示文本破坏调用方反序列化。
        sys.stdout.write(json.dumps(dict_cli_result, indent=2, sort_keys=True) + "\n")

    # 纯文本模式只允许输出简短摘要，不能把整个结构化对象直接打印到终端。
    else:

        # 人类可读模式下只输出状态摘要，帮助终端使用者快速确认结果。
        sys.stdout.write(_human_cli_summary(namespace_cli_args.command, dict_cli_result) + "\n")

    # 当前脚本只要成功生成输出就返回 0；业务失败态已通过 JSON 字段表达。
    return 0

# 仅在脚本被直接调用时进入 CLI 入口。
if __name__ == "__main__":

    # 顶层统一把 `main()` 返回码交给宿主进程，保持 CLI 退出语义稳定。
    raise SystemExit(main())
