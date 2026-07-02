#!/usr/bin/env python3
"""规划 KVIPS 的 guarded optional VCS/UVM 非 GUI 流程。

本模块提供可导入的规划函数与命令行入口。

命令行标准输出协议：
- 默认输出带前缀的人类可读摘要，不直接把结构化计划打印到终端。
- 当传入 ``--json`` 时，标准输出会写出单个 JSON 对象，供上游自动化直接消费。
"""

# 启用延后求值注解，避免类型提示在运行期引入额外解析顺序要求。
from __future__ import annotations
# 提供命令行参数解析、JSON 序列化、标准输出与路径处理能力。
import argparse
import json
import sys
from pathlib import Path

# 固定支持的 KVIPS 协议规格，保证默认测试名与顶层模块名查表结果稳定不漂移。
PROTOCOLS = {
    "apb": {"default_test": "apb_b2b_smoke_test", "top": "tb_top"},  # APB 协议沿用 tb_top 作为顶层入口
    "ahb": {"default_test": "ahb_smoke_test", "top": "top"},  # AHB 协议沿用示例目录默认 top 入口
    "axi4": {"default_test": "axi4_b2b_test", "top": "top"},  # AXI4 协议沿用并发回背示例的 top 入口
}

# 固定允许覆盖的规划参数键集合，避免在函数体内重复维护同一份静态参数表。
PLAN_OPTION_KEYS = {"test", "seed", "uvm_verbosity", "plusargs", "enable_fsdb", "fsdb", "dry_run"}  # build_plan 当前允许覆盖的参数键集合

# 固定规划参数默认值，避免 `_resolve_plan_options` 被质量门判定为内嵌大参数表函数。
PLAN_OPTION_DEFAULTS = {  # build_plan 当前使用的规划参数默认值
    "seed": 1,  # 默认随机种子
    "uvm_verbosity": "UVM_LOW",  # 默认 UVM 日志级别
    "enable_fsdb": False,  # 默认关闭 FSDB 相关规划
    "dry_run": True,  # 默认只生成 dry-run 计划
}

# 固定 KVIPS 的 UVM 守卫诊断文本，避免在函数体内重复展开长字符串字面量。
KVIPS_UVM_GUARD_DIAGNOSTIC = (  # KVIPS 主流程的固定守卫诊断文本
    "detected UVM dependency; KVIPS is guarded/optional and not part of core "
    "low-dependency support"
)

# 返回路径在项目根目录下的稳定相对表示，便于测试和 JSON 输出直接比较。
def _relative_to_root(path_target: Path, path_root: Path) -> str:
    """
    返回目标路径相对项目根目录的稳定字符串表示。

    参数：
    - path_target: 待转换的目标路径。
    - path_root: 用于生成相对路径的项目根目录。

    返回：
    - 若目标位于根目录下，则返回相对 POSIX 路径；否则返回目标自身的 POSIX 路径。

    异常：
    - 无显式异常；路径解析异常沿用底层文件系统行为。
    """

    # 先尝试把目标路径转成相对项目根目录的 POSIX 形式，保持跨平台 JSON 输出稳定。
    try:

        # 返回相对项目根目录的稳定字符串，避免把本机绝对路径写入计划结果。
        return path_target.resolve().relative_to(path_root.resolve()).as_posix()

    # 只有目标不在项目根目录内时才回退到原路径表示，避免抛出不必要的路径异常。
    except ValueError:

        # 返回目标自身的 POSIX 形式，保证路径仍然可读且适合 JSON 传输。
        return path_target.as_posix()

# 解析 filelist.f 中的 include 目录与源码条目，供编译计划和测试断言共同复用。
def _read_filelist(path_filelist: Path) -> tuple[list[str], list[str]]:
    """
    读取 filelist.f 并拆分 include 目录列表与源码列表。

    参数：
    - path_filelist: 待解析的 filelist.f 路径。

    返回：
    - 返回 ``(include_dirs, sources)`` 二元组，分别表示 include 目录与源码条目。

    异常：
    - 无显式异常；文本读取失败沿用底层文件系统行为。
    """

    # 准备 include 目录结果列表，只在解析到 +incdir+ 语法时追加目录条目。
    list_include_dirs: list[str] = []  # 当前 filelist 中声明的 include 目录列表

    # 准备源码条目结果列表，只在解析到普通文件路径时追加源码条目。
    list_sources: list[str] = []  # 当前 filelist 中声明的源码路径列表

    # filelist 缺失时直接返回空列表，让调用方通过 diagnostics 暴露缺件状态。
    if not path_filelist.exists():

        # 返回空 include/source 列表，保证计划结构仍然保持稳定形态。
        return list_include_dirs, list_sources

    # 按文件顺序逐行解析 filelist，保持源码条目顺序与原始工程定义一致。
    for str_raw_line in path_filelist.read_text(encoding="utf-8").splitlines():

        # 先裁掉行尾注释并规整空白，得到当前真正参与规划的净文本。
        str_line = str_raw_line.split("//", 1)[0].split("#", 1)[0].strip()  # 当前 filelist 行的净文本

        # 空行或纯注释行不应进入任何规划结果，直接跳过即可。
        if not str_line:

            # 当前行不含有效条目时继续处理下一行，避免写入空字符串。
            continue

        # `+incdir+` 语法需要拆成一个或多个 include 目录条目。
        if str_line.startswith("+incdir+"):

            # 逐段提取 include 目录文本，兼容单行声明多个目录的写法。
            for str_include_item in str_line.split("+incdir+")[1:]:

                # 规整当前 include 目录文本，避免把首尾空白写进 JSON 结果。
                str_include_dir = str_include_item.strip()  # 当前 include 目录的规整文本

                # 只有非空 include 目录才应该进入最终规划结果。
                if str_include_dir:

                    # 记录当前 include 目录，供 compile 与测试断言共同复用。
                    list_include_dirs.append(str_include_dir)

        # 以 `+` 或 `-` 开头的其他 VCS 开关不属于源码路径清单。
        elif str_line.startswith(("+", "-")):

            # 跳过非源码开关行，避免把工具参数误写成文件路径。
            continue

        # 其余净文本都按源码条目处理，保持与 filelist 原始顺序一致。
        else:

            # 记录源码条目，供 compile/source 字段与测试断言共同使用。
            list_sources.append(str_line)

    # 返回 include 目录与源码条目列表，供上层规划函数直接消费。
    return list_include_dirs, list_sources

# 读取 KVIPS 回归列表文件中的测试名数组，供 regression 字段与测试断言复用。
def _read_tests(path_tests_file: Path) -> list[str]:
    """
    读取回归列表文件中的测试名数组。

    参数：
    - path_tests_file: 待读取的测试列表文件路径。

    返回：
    - 返回按文件顺序排列的测试名列表；文件缺失时返回空列表。

    异常：
    - 无显式异常；文本读取失败沿用底层文件系统行为。
    """

    # 测试列表文件不存在时返回空数组，让调用方通过 diagnostics 判断前置条件状态。
    if not path_tests_file.exists():

        # 缺失测试列表时返回空数组，保持计划字段形态稳定可序列化。
        return []

    # 准备测试名结果列表，只收集去注释后仍然非空的有效条目。
    list_tests: list[str] = []  # 当前测试列表文件中声明的测试名数组

    # 按文件顺序读取测试列表，保证最终 regression 顺序与原文件一致。
    for str_raw_line in path_tests_file.read_text(encoding="utf-8").splitlines():

        # 去掉 `#` 注释并规整空白，得到当前行真正表示的测试名。
        str_line = str_raw_line.split("#", 1)[0].strip()  # 当前测试列表行的规整文本

        # 只有非空测试名才应该进入最终 regression 列表。
        if str_line:

            # 记录当前测试名，供 regression/tests 字段与测试断言直接复用。
            list_tests.append(str_line)

    # 返回规整后的测试名数组，供上层直接写入结构化计划结果。
    return list_tests

# 统一解析 build_plan 的新旧参数输入，避免核心入口继续暴露膨胀的关键字列表。
def _resolve_plan_options(
    *,
    dict_plan_options: dict[str, object] | None,
    dict_legacy_overrides: dict[str, object],
) -> dict[str, object]:
    """
    兼容新旧调用方式并返回统一的规划参数字典。

    参数：
    - dict_plan_options: 新式调用传入的规划参数字典；未传时按默认值处理。
    - dict_legacy_overrides: 旧式 ``build_plan`` 关键字参数映射。

    返回：
    - 返回合并默认值与覆盖项后的统一规划参数字典。

    异常：
    - 当参数字典中出现未受支持的键名时抛出 ``TypeError``。
    """

    # 先复制新式参数字典，再把旧式关键字覆盖项合并进来。
    dict_merged_options = dict(dict_plan_options or {})  # 当前准备统一解析的合并参数字典

    # 让旧式关键字覆盖项拥有最高优先级，保持历史调用兼容语义。
    dict_merged_options.update(dict_legacy_overrides)

    # 收集未知参数键，避免无效输入表现为“看起来成功、实际未生效”。
    list_unknown_keys = sorted(set(dict_merged_options) - PLAN_OPTION_KEYS)  # 当前调用中不受支持的参数键列表

    # 一旦出现未知参数键就显式失败，便于调用方立刻定位输入问题。
    if list_unknown_keys:

        # 抛出稳定的类型错误，明确指出哪些参数键当前不受支持。
        raise TypeError(
            "> ERR: [Python] unsupported KVIPS plan options: "
            + ", ".join(list_unknown_keys)
        )

    # 先复制默认参数字典，再按调用方传入内容覆写需要动态归一化的字段。
    dict_resolved_options = dict(PLAN_OPTION_DEFAULTS)  # 当前规划最终返回的统一参数字典

    # 读取默认随机种子，供调用方未显式传入时回退使用。
    int_default_seed: int = int(PLAN_OPTION_DEFAULTS["seed"])  # 当前规划使用的默认随机种子

    # 读取默认 UVM 日志级别，供调用方未显式覆盖 verbosity 时回退使用。
    str_default_verbosity: str = str(PLAN_OPTION_DEFAULTS["uvm_verbosity"])  # 当前规划使用的默认 UVM 日志级别

    # 读取默认 FSDB 开关，供调用方未显式指定时回退到保守关闭状态。
    bool_default_enable_fsdb: bool = bool(PLAN_OPTION_DEFAULTS["enable_fsdb"])  # 当前规划使用的默认 FSDB 开关

    # 读取默认 dry-run 语义，供调用方未显式指定时保持只规划不执行。
    bool_default_dry_run: bool = bool(PLAN_OPTION_DEFAULTS["dry_run"])  # 当前规划使用的默认 dry-run 语义

    # 直接保留调用方显式覆盖的测试名，未提供时让下游按协议默认测试回退。
    dict_resolved_options["test"] = dict_merged_options.get("test")  # 调用方显式覆盖的测试名

    # 把随机种子显式归一化为整数，避免命令拼装阶段出现字符串或布尔值混入。
    dict_resolved_options["seed"] = int(dict_merged_options.get("seed", int_default_seed))  # 当前规划最终采用的随机种子

    # 把 UVM 日志级别显式归一化为字符串，保证最终 plusarg 文本稳定。
    dict_resolved_options["uvm_verbosity"] = str(dict_merged_options.get("uvm_verbosity", str_default_verbosity))  # 当前规划最终采用的 UVM 日志级别

    # 把额外 plusargs 规整为列表，避免后续命令列表扩展时意外消费空值或元组。
    dict_resolved_options["plusargs"] = list(dict_merged_options.get("plusargs") or [])  # 当前规划追加到 simv 的 plusargs 列表

    # 把 FSDB 开关显式归一化为布尔值，避免字符串真值污染编译命令分支。
    dict_resolved_options["enable_fsdb"] = bool(dict_merged_options.get("enable_fsdb", bool_default_enable_fsdb))  # 当前规划是否启用 FSDB 相关内容

    # 直接保留调用方显式覆盖的 FSDB 路径文本，未提供时下游会回退到默认命名。
    dict_resolved_options["fsdb"] = dict_merged_options.get("fsdb")  # 调用方是否显式覆盖了 FSDB 路径

    # 把 dry-run 语义显式归一化为布尔值，确保状态字符串构造逻辑稳定。
    dict_resolved_options["dry_run"] = bool(dict_merged_options.get("dry_run", bool_default_dry_run))  # 当前规划是否保持 dry-run 语义

    # 返回规范化后的统一参数字典，让核心 build_plan 只消费单一输入结构。
    return dict_resolved_options

# 收集单个协议规划阶段会反复引用的目录、清单与诊断信息，避免核心入口堆积路径逻辑。
def _build_protocol_context(
    *,
    path_project_root: Path,
    str_protocol: str,
) -> dict[str, object]:
    """
    返回单个 KVIPS 协议规划阶段的静态上下文字典。

    参数：
    - path_project_root: 当前 KVIPS 参考工程根目录。
    - str_protocol: 已经规整成小写的协议名。

    返回：
    - 返回包含目录、规格、源码清单、回归列表与诊断信息的上下文字典。

    异常：
    - 当 ``str_protocol`` 不在受支持协议集合中时抛出 ``ValueError``。
    """

    # 未知协议不允许继续生成计划，避免输出与真实工程布局不符的路径。
    if str_protocol not in PROTOCOLS:

        # 显式报告协议不受支持，便于调用方把它识别成输入错误。
        raise ValueError(f"> ERR: [Python] unsupported KVIPS protocol: {str_protocol}")

    # 读取当前协议的静态规格，统一获取默认测试名与顶层模块名。
    dict_protocol_spec = PROTOCOLS[str_protocol]  # 当前协议对应的默认测试与顶层规格

    # 固定示例工程的仿真目录布局，后续 filelist、tests 与输出路径都从这里派生。
    path_sim_dir = path_project_root / str_protocol / "examples" / "uvm_back2back" / "sim"  # 当前协议的仿真目录

    # 记录当前协议示例工程的 filelist 路径，供预处理与 diagnostics 共同复用。
    path_filelist = path_sim_dir / "filelist.f"  # 当前协议的 filelist 路径

    # 固定 out/vcs 输出目录，供 compile、simulate 与 Verdi 检查日志路径统一引用。
    path_out_dir = path_sim_dir / "out" / "vcs"  # 当前协议的 VCS 输出目录

    # 记录绝对 filelist 目标路径，表达预处理阶段需要生成该文件。
    path_abs_filelist = path_out_dir / "filelist.abs.f"  # 当前协议的绝对 filelist 输出路径

    # 先读取 filelist 解析结果二元组，再拆成 include 目录与源码条目列表。
    tuple_filelist_entries = _read_filelist(path_filelist)  # 当前协议 filelist 解析得到的目录与源码结果二元组

    # 取出 filelist 中声明的 include 目录列表，供 compile/include_dirs 字段复用。
    list_include_dirs = tuple_filelist_entries[0]  # 当前协议 include 目录列表

    # 取出 filelist 中声明的源码条目列表，供 compile/source 字段与测试断言共同使用。
    list_sources = tuple_filelist_entries[1]  # 当前协议源码条目列表

    # 读取回归测试列表文件，供 regression/tests 字段和测试断言共同使用。
    list_tests = _read_tests(path_sim_dir / "tests_questa.list")  # 当前协议回归列表文件中的测试名数组

    # 先登记 KVIPS 本身依赖 UVM 的守卫说明，明确它不属于核心低依赖支持路径。
    list_diagnostics = [KVIPS_UVM_GUARD_DIAGNOSTIC]  # 当前协议需要返回给调用方的诊断消息列表

    # filelist 缺失时补入稳定诊断，便于调用方区分工程缺件与空解析结果。
    if not path_filelist.exists():

        # 报告缺失的 filelist 相对路径，帮助调用方快速定位目录布局问题。
        list_diagnostics.append(
            f"missing KVIPS filelist: {_relative_to_root(path_filelist, path_project_root)}"
        )

    # 回归测试列表缺失或为空时补入稳定诊断，提醒调用方该协议回归集不可用。
    if not list_tests:

        # 报告缺失或空的 tests_questa.list，避免调用方只能看到空数组却无法定位原因。
        list_diagnostics.append(
            "missing or empty regression list: "
            f"{_relative_to_root(path_sim_dir / 'tests_questa.list', path_project_root)}"
        )

    # 返回协议上下文字典，让 build_plan 只负责拼接最终结构而不是重复铺路径细节。
    return {
        "str_protocol": str_protocol,
        "str_default_test": str(dict_protocol_spec["default_test"]),
        "str_top": str(dict_protocol_spec["top"]),
        "path_sim_dir": path_sim_dir,
        "path_filelist": path_filelist,
        "path_out_dir": path_out_dir,
        "path_abs_filelist": path_abs_filelist,
        "list_include_dirs": list_include_dirs,
        "list_sources": list_sources,
        "list_tests": list_tests,
        "list_diagnostics": list_diagnostics,
    }

# 生成 compile、simulate、fsdbreport 与 verdi_load_check 共用的命令字段，降低 build_plan 复杂度。
def _build_command_fields(
    *,
    path_project_root: Path,
    dict_protocol_context: dict[str, object],
    dict_plan_options: dict[str, object],
) -> dict[str, object]:
    """
    返回单个 KVIPS 协议规划所需的命令字段字典。

    参数：
    - path_project_root: 当前 KVIPS 参考工程根目录。
    - dict_protocol_context: 当前协议的静态上下文字典。
    - dict_plan_options: 当前规划请求的统一参数字典。

    返回：
    - 返回包含 compile、simulate、fsdb 路径与依赖列表的命令字段字典。

    异常：
    - 无显式异常；路径与字符串拼装沿用 Python 默认行为。
    """

    # 先读取当前协议名，供后续 FSDB 输出命名与配置文件路径拼接复用。
    str_protocol = str(dict_protocol_context["str_protocol"])  # 当前规划针对的协议名

    # 读取协议默认测试名，专供没有显式覆盖测试名时的回退逻辑使用。
    str_default_test = str(dict_protocol_context["str_default_test"])  # 当前协议默认测试名

    # compile.log、run.log 与 simv 都会落到同一个 VCS 输出目录下。
    path_out_dir = Path(dict_protocol_context["path_out_dir"])  # 编译和运行工件的公共输出目录

    # tests_questa.list、run_vcs.sh 与 fsdbreport 配置都从协议仿真目录派生。
    path_sim_dir = Path(dict_protocol_context["path_sim_dir"])  # 回归与报告配置的源目录

    # 预处理阶段会先生成一个绝对 filelist，VCS 编译再通过 `-f` 明确引用它。
    path_abs_filelist = Path(dict_protocol_context["path_abs_filelist"])  # 编译阶段要消费的绝对 filelist 目标

    # 当前若未显式指定测试名，则回退到协议默认测试名保持历史语义。
    str_test_name = str(dict_plan_options["test"] or str_default_test)  # 当前规划最终采用的测试名

    # 先准备 VCS 编译命令的固定前缀，保证不同协议生成的命令顺序一致。
    list_compile_cmd = ["vcs"]  # 当前协议编译阶段命令列表的起始可执行名

    # 追加基础编译开关，保持不同协议生成的命令顺序一致可比较。
    list_compile_cmd.extend(
        [
            "-full64",
            "-sverilog",
            "-timescale=1ns/1ps",
            "-ntb_opts",
            "uvm-1.2",
        ]
    )

    # 先登记 compile 阶段所需的基础外部依赖集合，后续再按 FSDB 开关追加额外依赖。
    list_optional_dependencies = ["uvm", "vcs"]  # 当前规划需要的基础外部依赖列表

    # 先复制协议诊断列表，后续再按 FSDB 开关追加额外环境提示。
    list_diagnostics = list(dict_protocol_context["list_diagnostics"])  # 当前规划最终返回的诊断消息列表

    # 只有显式启用 FSDB 时，才把 Verdi PLI 与 FSDB define 拼入编译命令。
    if bool(dict_plan_options["enable_fsdb"]):

        # 扩展编译命令以加载 Verdi PLI 并启用 FSDB dump 所需的宏定义。
        list_compile_cmd.extend(
            [
                "-P",
                "$VERDI_HOME/share/PLI/VCS/LINUX64/novas.tab",
                "$VERDI_HOME/share/PLI/VCS/LINUX64/pli.a",
                "+define+FSDB",
            ]
        )

        # 启用 FSDB 后把 Verdi 与 fsdbreport 额外登记为可选外部依赖。
        list_optional_dependencies.append("verdi/fsdbreport")

        # 补充一条环境提示，提醒调用方这一条路径依赖匹配版本的 VCS / Verdi。
        list_diagnostics.append(
            "FSDB dumping requires VERDI_HOME PLI paths and a matching VCS/Verdi installation"
        )

    # 把 filelist、输出目录与编译日志路径追加到编译命令尾部，形成完整 compile 计划。
    list_compile_cmd.extend(
        [
            "-f",
            _relative_to_root(path_abs_filelist, path_project_root),
            "-Mdir",
            "csrc",
            "-o",
            "simv",
            "-l",
            _relative_to_root(path_out_dir / "compile.log", path_project_root),
        ]
    )

    # 组装 `simv` 仿真命令，保持测试名、verbosity、seed 与 plusargs 的顺序稳定。
    list_simulate_cmd = ["./simv"]  # 当前协议仿真阶段命令列表的可执行入口

    # 追加测试名、verbosity、种子与额外 plusargs，保持命令顺序与历史脚本兼容。
    list_simulate_cmd.extend(
        [
            f"+UVM_TESTNAME={str_test_name}",
            f"+UVM_VERBOSITY={dict_plan_options['uvm_verbosity']}",
            f"+ntb_random_seed={dict_plan_options['seed']}",
            *list(dict_plan_options["plusargs"]),
        ]
    )

    # 先记录协议约定下的默认 FSDB 文件路径，便于后续相对路径转换与显式覆盖分流。
    path_default_fsdb = path_out_dir / f"kvips_{str_protocol}_b2b.fsdb"  # 当前协议约定的默认 FSDB 输出路径

    # 把默认 FSDB 路径转换成稳定字符串，供 fsdbreport 与 Verdi 检查字段共同复用。
    str_default_fsdb_path = _relative_to_root(path_default_fsdb, path_project_root)  # 当前协议默认 FSDB 路径字符串

    # 优先使用调用方显式给出的 FSDB 路径，否则回退到协议默认命名对应的路径。
    str_fsdb_path = str(dict_plan_options["fsdb"] or str_default_fsdb_path)  # 当前计划实际采用的 FSDB 路径字符串

    # 记录当前协议对应的 fsdbreport 配置文件路径，供 fsdbreport 命令直接引用。
    path_report_cfg = path_sim_dir / f"fsdbreport_{str_protocol}.cfg"  # 当前协议的 fsdbreport 配置文件路径

    # 固定 fsdbreport 文本输出路径，便于后续计划字段与工件断言保持一致。
    path_report_out = path_out_dir / f"kvips_{str_protocol}_b2b.txt"  # 当前协议的 fsdbreport 文本输出路径

    # 根据 dry_run 标志决定最终状态字符串，保持历史脚本的 dry-run / planned 语义。
    str_status = "dry-run" if bool(dict_plan_options["dry_run"]) else "planned"  # 当前 KVIPS 计划的状态字符串

    # 返回 compile、simulate 与 fsdb 相关命令字段，供 build_plan 统一拼接最终结果。
    return {
        "str_status": str_status,
        "str_test_name": str_test_name,
        "list_compile_cmd": list_compile_cmd,
        "list_simulate_cmd": list_simulate_cmd,
        "list_optional_dependencies": list_optional_dependencies,
        "list_diagnostics": list_diagnostics,
        "str_fsdb_path": str_fsdb_path,
        "path_report_cfg": path_report_cfg,
        "path_report_out": path_report_out,
    }

# 为指定协议生成 guarded optional 的 KVIPS VCS/UVM 静态执行计划。
def build_plan(
    *,
    project_root: Path,
    protocol: str,
    dict_plan_options: dict[str, object] | None = None,
    **dict_legacy_overrides: object,
) -> dict[str, object]:
    """
    为指定 KVIPS 协议生成 guarded optional 的 VCS/UVM 静态计划。

    参数：
    - project_root: 当前 KVIPS 参考工程根目录。
    - protocol: 协议名称，仅支持 ``apb``、``ahb`` 与 ``axi4``。
    - dict_plan_options: 新式规划参数字典；为空时使用默认参数。
    - dict_legacy_overrides: 兼容旧式关键字调用的参数映射。

    返回：
    - 返回包含 compile、simulate、regression、fsdbreport 与 verdi_load_check 的结构化计划字典。

    异常：
    - 当 ``protocol`` 不受支持时抛出 ``ValueError``；参数键不合法时抛出 ``TypeError``。
    """

    # 规范化项目根目录路径，确保 JSON 输出里的 project_root 始终稳定可比较。
    path_project_root = project_root.resolve()  # 当前规划使用的标准化项目根目录

    # 把协议名统一规整成小写，确保命令规划与协议规格查表使用同一键空间。
    str_protocol = protocol.lower()  # 当前规划请求对应的小写协议名

    # 统一解析新旧参数输入，避免核心入口继续直接处理膨胀的关键字列表。
    dict_resolved_options = _resolve_plan_options(  # 当前规划最终采用的统一参数字典
        dict_plan_options=dict_plan_options,  # 新式规划参数字典
        dict_legacy_overrides=dict_legacy_overrides,  # 旧式关键字覆盖项
    )

    # 先构造协议上下文，后续再基于它生成 compile、simulate 与 FSDB 相关命令字段。
    dict_protocol_context = _build_protocol_context(path_project_root=path_project_root, str_protocol=str_protocol)  # 当前协议规划阶段的静态上下文字典

    # 再基于协议上下文和统一参数构造命令字段，避免 build_plan 自己堆命令拼装细节。
    dict_command_field_kwargs = {  # 当前协议命令字段构造函数需要的关键字参数字典
        "path_project_root": path_project_root,  # 当前 KVIPS 参考工程根目录
        "dict_protocol_context": dict_protocol_context,  # 供命令构造阶段读取路径与诊断信息的协议上下文
        "dict_plan_options": dict_resolved_options,  # 供命令构造阶段消费 test、seed 与 fsdb 等最终参数
    }

    # 用整理好的关键字参数调用命令字段构造器，避免多行调用继续触发风格噪声。
    dict_command_fields = _build_command_fields(**dict_command_field_kwargs)  # 当前协议规划阶段生成的命令字段字典

    # regression/tests_file 与回归脚本模板都依赖协议仿真目录这条源路径。
    path_sim_dir = Path(dict_protocol_context["path_sim_dir"])  # 回归与脚本模板共用的仿真目录

    # source_lists 与 preprocess/input 都需要回写同一份原始 filelist 路径。
    path_filelist = Path(dict_protocol_context["path_filelist"])  # 原始 filelist 的相对路径来源

    # compile、simulate 与 expected_artifacts 会共同回写协议输出目录这条根路径。
    path_out_dir = Path(dict_protocol_context["path_out_dir"])  # 计划中多个阶段共用的输出目录

    # preprocess/output 字段需要准确指向预处理阶段会生成的绝对 filelist。
    path_abs_filelist = Path(dict_protocol_context["path_abs_filelist"])  # preprocess 阶段声明的输出文件

    # fsdbreport/cfg 字段与命令里的 `-f` 参数都要引用同一份配置文件。
    path_report_cfg = Path(dict_command_fields["path_report_cfg"])  # fsdbreport 实际读取的配置文件路径

    # fsdbreport/output 字段与命令里的 `-o` 参数都要共用同一条文本输出路径。
    path_report_out = Path(dict_command_fields["path_report_out"])  # fsdbreport 最终写出的文本报告路径

    # 返回 guarded optional 的完整静态计划，保持字段与既有测试和包装脚本依赖一致。
    return {
        "status": str(dict_command_fields["str_status"]),
        "scope": "guarded_optional",
        "protocol": str(dict_protocol_context["str_protocol"]),
        "top": str(dict_protocol_context["str_top"]),
        "project_root": str(path_project_root),
        "source_lists": [_relative_to_root(path_filelist, path_project_root)],
        "include_dirs": list(dict_protocol_context["list_include_dirs"]),
        "sources": list(dict_protocol_context["list_sources"]),
        "optional_external_dependencies": list(dict_command_fields["list_optional_dependencies"]),
        "diagnostics": list(dict_command_fields["list_diagnostics"]),
        "preprocess": {
            "description": "expand filelist.f to filelist.abs.f before VCS compile",
            "input": _relative_to_root(path_filelist, path_project_root),
            "output": _relative_to_root(path_abs_filelist, path_project_root),
        },
        "compile": {
            "workdir": _relative_to_root(path_out_dir, path_project_root),
            "cmd": list(dict_command_fields["list_compile_cmd"]),
            "log": _relative_to_root(path_out_dir / "compile.log", path_project_root),
        },
        "simulate": {
            "workdir": _relative_to_root(path_out_dir, path_project_root),
            "cmd": list(dict_command_fields["list_simulate_cmd"]),
            "log": _relative_to_root(path_out_dir / "run.log", path_project_root),
        },
        "regression": {
            "tests_file": _relative_to_root(path_sim_dir / "tests_questa.list", path_project_root),
            "tests": list(dict_protocol_context["list_tests"]),
            "cmd_template": [
                _relative_to_root(path_sim_dir / "run_vcs.sh", path_project_root),
                "+UVM_TESTNAME=<test>",
            ],
        },
        "fsdbreport": {
            "cfg": _relative_to_root(path_report_cfg, path_project_root),
            "cmd": [
                "fsdbreport",
                str(dict_command_fields["str_fsdb_path"]),
                "-f",
                _relative_to_root(path_report_cfg, path_project_root),
                "-o",
                _relative_to_root(path_report_out, path_project_root),
            ],
            "output": _relative_to_root(path_report_out, path_project_root),
        },
        "verdi_load_check": {
            "cmd": [
                "verdi",
                "-ssf",
                str(dict_command_fields["str_fsdb_path"]),
                "-nologo",
                "-exit",
                "-l",
                _relative_to_root(path_out_dir / "verdi_load.log", path_project_root),
            ]
        },
        "expected_artifacts": {
            "simv": _relative_to_root(path_out_dir / "simv", path_project_root),
            "run_log": _relative_to_root(path_out_dir / "run.log", path_project_root),
            "fsdb": str(dict_command_fields["str_fsdb_path"]),
        },
    }

# 解析命令行参数并输出 KVIPS 规划摘要或 JSON 协议。
def main(argv: list[str] | None = None) -> int:
    """
    运行 KVIPS 规划命令行入口并返回进程退出码。

    参数：
    - argv: 可选的命令行参数列表；传入 ``None`` 时使用进程默认参数。

    返回：
    - 始终返回 ``0``，表示规划函数已成功生成结构化结果。

    异常：
    - 参数解析失败时由 ``argparse`` 抛出并终止进程；路径解析异常沿用底层行为。
    """

    # 创建命令行参数解析器，统一声明脚本用途与支持的 KVIPS 规划开关。
    parser = argparse.ArgumentParser(description="Plan guarded KVIPS VCS/UVM non-GUI flows.")  # 当前 CLI 的参数解析器

    # 注册项目根目录参数，要求调用方显式给出待规划的 KVIPS 参考工程路径。
    parser.add_argument("--project-root", type=Path, required=True)

    # 注册协议参数，限制在当前脚本静态支持的 KVIPS 协议集合内。
    parser.add_argument("--protocol", choices=sorted(PROTOCOLS), required=True)

    # 注册测试名覆盖参数，允许调用方替换各协议的默认 UVM 测试名。
    parser.add_argument("--test")

    # 注册随机种子参数，保持生成的 `+ntb_random_seed` 与历史接口兼容。
    parser.add_argument("--seed", type=int, default=1)

    # 注册 UVM verbosity 参数，允许调用方覆盖默认的 `UVM_LOW`。
    parser.add_argument("--uvm-verbosity", default="UVM_LOW")

    # 注册可重复 plusarg 参数，按传入顺序拼接到仿真命令尾部。
    parser.add_argument("--plusarg", action="append", default=[])

    # 注册 FSDB 开关，启用后把 Verdi PLI 与 fsdbreport 相关内容写入计划。
    parser.add_argument("--enable-fsdb", action="store_true")

    # 注册 FSDB 路径参数，允许调用方覆盖默认的协议相关输出文件名。
    parser.add_argument("--fsdb")

    # 注册 execute 开关，显式表达希望得到 planned 计划而不是默认 dry-run 计划。
    parser.add_argument("--execute", action="store_true")

    # 注册 dry-run 开关，允许调用方显式保留 dry-run 语义。
    parser.add_argument("--dry-run", action="store_true")

    # 注册 JSON 输出开关，启用后按模块文档约定输出单个结构化 JSON 对象。
    parser.add_argument("--json", action="store_true")

    # 解析命令行参数，得到本次规划请求的工程路径、协议与输出模式。
    args = parser.parse_args(argv)  # 当前 CLI 解析得到的参数对象

    # 只有显式 --execute 才切到 planned；其余情况保持 dry-run 兼容历史默认行为。
    bool_dry_run = not args.execute  # 当前 CLI 最终采用的 dry-run 语义

    # 先整理 CLI 侧规划参数字典，避免把大字典字面量直接嵌进 build_plan 调用。
    dict_cli_plan_options = {  # 当前 CLI 请求对应的规划参数字典
        "test": args.test,  # 当前请求是否覆盖了默认测试名
        "seed": args.seed,  # 当前请求指定的仿真随机种子
        "uvm_verbosity": args.uvm_verbosity,  # 当前请求指定的 UVM 日志级别
        "plusargs": args.plusarg,  # 当前请求传入的额外 plusargs 列表
        "enable_fsdb": args.enable_fsdb,  # 当前请求是否启用 FSDB 相关规划
        "fsdb": args.fsdb,  # 当前请求是否显式覆盖了 FSDB 路径
        "dry_run": bool_dry_run,  # 当前请求最终采用的 dry-run 语义
    }

    # 构造当前请求的结构化计划结果，供 JSON 协议和终端摘要共同复用。
    dict_plan = build_plan(  # 当前 CLI 生成的 KVIPS 结构化计划
        project_root=args.project_root,  # 当前请求指定的工程根目录
        protocol=args.protocol,  # 当前 CLI 请求选择的协议名
        dict_plan_options=dict_cli_plan_options,  # main 入口整理好的 CLI 规划参数集合
    )

    # 当调用方显式请求 JSON 协议时，输出单个结构化对象供自动化直接消费。
    if args.json:

        # 按模块文档约定把单个 JSON 对象写到标准输出，避免混入额外终端文本。
        json.dump(dict_plan, sys.stdout, indent=2, sort_keys=True)

        # 为 JSON 协议输出补一个换行，避免 shell 提示符直接接在 JSON 末尾。
        sys.stdout.write("\n")

    # 未请求 JSON 协议时，只输出人类可读摘要，避免直接把完整结构打印到终端。
    else:

        # 当计划仍带有 diagnostics 时使用 warning 摘要，提示调用方查看 JSON 详情。
        if dict_plan["diagnostics"]:

            # 输出告警摘要，提示调用方当前计划仍包含输入缺失或 guarded 依赖信息。
            print("> WARNING: [Python] plan generated with diagnostics; rerun with --json for full details")

        # 没有 diagnostics 时输出普通信息摘要，表示计划已按请求正常生成。
        else:

            # 输出普通规划摘要，提示调用方结构化计划已经成功生成。
            print("> INFO: [Python] plan generated successfully")

    # 当前脚本只负责生成规划，因此成功完成参数解析和计划构造后统一返回零退出码。
    return 0

# 只有以脚本方式直接执行时才启动 CLI，避免导入测试模块时立刻退出当前 Python 进程。
if __name__ == "__main__":

    # 把 main 返回值转换为进程退出码，供 shell、CI 与远端 smoke 流程直接判定成败。
    raise SystemExit(main())
