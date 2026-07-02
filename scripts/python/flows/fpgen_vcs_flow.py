#!/usr/bin/env python3
"""规划 FP-Gen 的 Genesis2、VCS、VPD、SAIF 与 guarded gate-level 流程。

本模块同时提供可导入的规划函数与命令行入口。

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

# 固定 FP-Gen 规划阶段使用的三个生成文件名，保证测试与下游脚本的断言目标稳定不漂移。
SOURCE_LIST_FILENAMES = ("genesis_vlog.vf", "genesis_vlog.synth.vf", "genesis_vlog.verif.vf")  # Genesis2 标准输出 filelist 名称集合

# 固定 VCS 编译阶段的统一开关，避免不同调用路径拼出不一致的命令顺序。
COMPILE_FLAGS = [  # VCS 主编译阶段的固定参数列表
    "-sverilog",  # 开启 SystemVerilog 编译模式
    "+cli",  # 保留命令行交互相关兼容开关
    "+memcbk",  # 启用内存回调支持
    "+lint=PCWM",  # 沿用历史脚本的 lint 级别
    "+libext+.v",  # 允许自动解析 .v 扩展名库文件
    "-notice",  # 输出 notice 级工具信息
    "-full64",  # 固定 64 位 VCS 运行模式
    "+v2k",  # 保持 Verilog-2001 兼容语义
    "-debug_pp",  # 启用调试数据库所需的 debug_pp
    "-timescale=1ps/1ps",  # 固定仿真时间精度
    "+noportcoerce",  # 保留历史端口类型处理策略
    "+vcs+lic+wait",  # 遇到授权繁忙时等待许可
    "+notimingcheck",  # 默认关闭时序检查
    "+delay_mode_zero",  # 把门级延迟规划为零延迟模式
    "-licqueue",  # 允许进入许可证排队模式
]

# 固定 simv VPD 运行阶段的基础参数，确保所有调用都沿用同一份 VPD 规划约定。
SIMULATE_FLAGS = [  # simv VPD 运行阶段的固定参数列表
    "-l",  # 指定第一段日志输出开关
    "simv.log",  # 保留历史 simv 编译日志文件名
    "+vcs+lic+wait",  # 运行阶段同样等待许可证
    "+vpdbufsize+100",  # 固定 VPD 缓冲区大小
    "+vpdfileswitchsize+100",  # 固定 VPD 切换阈值
    "-l",  # 指定运行日志输出开关
    "run_bb.log",  # 保留历史 run 日志文件名
]

# 固定 SAIF 运行阶段的附加 plusargs，保证功耗统计规划与历史工艺脚本约定一致。
SAIF_FLAGS = [  # simv SAIF 功耗统计阶段的固定参数列表
    "+vcs+lic+wait",  # 功耗统计运行阶段同样等待许可证
    "+vpdbufsize+100",  # 复用 VPD 缓冲区大小约定
    "+vpdfileswitchsize+100",  # 复用 VPD 切换阈值约定
    "+SAIF",  # 显式开启 SAIF 导出
    "+clk_period=1000",  # 设定 SAIF 运行使用的时钟周期
    "+NumTrans=1000",  # 固定统计转换次数
    "+notimingcheck",  # 功耗统计阶段同样关闭时序检查
    "+SignIsPos_DistWeight=50",  # 保留符号分布权重约定
    "+Random_DistWeight=200",  # 保留随机分布权重约定
    "+Silent",  # 减少 SAIF 运行阶段额外终端噪声
]

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

        # 返回相对项目根目录的稳定字符串，避免把机器相关绝对路径写入计划结果。
        return path_target.resolve().relative_to(path_root.resolve()).as_posix()

    # 只有目标不在项目根目录内时才回退到原路径表示，避免抛出不必要的路径异常。
    except ValueError:

        # 返回目标自身的 POSIX 形式，保证路径仍然可读且适合 JSON 传输。
        return path_target.as_posix()

# 检查 Genesis2 关键 filelist 是否已经生成，缺失时返回稳定诊断字符串。
def _generated_file_diagnostics(path_project_root: Path, str_generated_file: str) -> list[str]:
    """
    检查 Genesis2 生成文件是否存在，并返回缺失诊断列表。

    参数：
    - path_project_root: 当前 FP-Gen 项目的根目录。
    - str_generated_file: 需要检查的生成文件相对路径。

    返回：
    - 若文件存在则返回空列表；若文件缺失则返回单条稳定诊断字符串列表。

    异常：
    - 无显式异常；路径访问异常沿用底层文件系统行为。
    """

    # 只有 filelist 已经存在时才返回空诊断，避免把 Genesis2 前置条件遗漏掉。
    if (path_project_root / str_generated_file).exists():

        # 生成 filelist 已存在时不再重复报告缺失诊断。
        return []

    # 返回稳定缺失诊断，供测试和调用方直接断言当前仍需先跑 Genesis2。
    return [f"generated filelist is absent until Genesis2 generation succeeds: {str_generated_file}"]

# 返回本次 FP-Gen 规划使用的基础外部依赖清单。
def _base_optional_dependencies() -> list[str]:
    """
    返回 FP-Gen 主流程默认声明的可选外部依赖列表。

    参数：
    - 无额外业务参数；依赖清单由模块内置规则固定提供。

    返回：
    - 返回 Genesis2、DesignWare/GTech 与 VCS 相关的外部依赖名称列表。

    异常：
    - 无显式异常；静态列表构造沿用 Python 默认行为。
    """

    # 返回主流程默认依赖，避免在不同构造函数里各自维护一份可漂移的依赖清单。
    return ["Genesis2.pl", "DesignWare/gtech libraries", "vcs"]

# 构造 Genesis2 生成阶段命令，保持旧脚本和测试依赖的参数顺序不变。
def _build_generate_command(str_top_name: str, str_vlog_list: str, str_hierarchy_name: str) -> list[str]:
    """
    返回 Genesis2 生成阶段的命令列表。

    参数：
    - str_top_name: 当前产品对应的顶层模块名。
    - str_vlog_list: Genesis2 主输出 filelist 文件名。
    - str_hierarchy_name: Genesis2 层级 XML 文件名。

    返回：
    - 返回 Genesis2.pl 的完整命令参数列表。

    异常：
    - 无显式异常；静态列表构造沿用 Python 默认行为。
    """

    # 返回 Genesis2 标准生成命令，确保调用方和测试都拿到稳定可断言的参数序列。
    return [
        "Genesis2.pl",
        "-gen",
        "-top",
        str_top_name,
        "-synthtop",
        str_top_name,
        "-depend",
        "depend.list",
        "-product",
        str_vlog_list,
        "-hierarchy",
        str_hierarchy_name,
        "-debug",
        "0",
    ]

# 构造主 RTL VCS 编译命令，保持历史 dry-run 规划兼容。
def _build_compile_command(str_top_name: str, str_vlog_list: str) -> list[str]:
    """
    返回主 RTL 编译阶段的 VCS 命令列表。

    参数：
    - str_top_name: 当前产品对应的顶层模块名。
    - str_vlog_list: Genesis2 主输出 filelist 文件名。

    返回：
    - 返回可直接写入计划结果的 VCS 编译命令列表。

    异常：
    - 无显式异常；静态列表构造沿用 Python 默认行为。
    """

    # 返回主 RTL 编译命令，确保 -top、-y、+incdir 与 -f 约定始终一致。
    return [
        "vcs",
        *COMPILE_FLAGS,
        "-top",
        str_top_name,
        "-y",
        ".",
        "+incdir+.",
        "-f",
        str_vlog_list,
    ]

# 构造仿真阶段 simv 命令，并沿用项目内相对 simv 路径约定。
def _build_simulate_command(str_simv_path: str) -> list[str]:
    """
    返回 VPD 仿真阶段的 simv 命令列表。

    参数：
    - str_simv_path: 计划中使用的 simv 路径字符串。

    返回：
    - 返回带日志与 VPD 缓冲参数的 simv 命令列表。

    异常：
    - 无显式异常；静态列表构造沿用 Python 默认行为。
    """

    # 返回标准 simv VPD 运行命令，保证 run_bb.log 与 VPD 相关 plusargs 始终齐全。
    return [str_simv_path, *SIMULATE_FLAGS]

# 构造 SAIF 运行阶段命令，复用相同 simv 路径和功耗分析 plusargs。
def _build_saif_command(str_simv_path: str) -> list[str]:
    """
    返回 SAIF 运行阶段的 simv 命令列表。

    参数：
    - str_simv_path: 计划中使用的 simv 路径字符串。

    返回：
    - 返回带 SAIF 相关运行参数的 simv 命令列表。

    异常：
    - 无显式异常；静态列表构造沿用 Python 默认行为。
    """

    # 返回 SAIF 运行命令，保持日志命名和功耗统计 plusargs 与旧流程兼容。
    return [str_simv_path, "-l", f"{str_simv_path}.rtl_saif.log", *SAIF_FLAGS]

# 构造 guarded gate-level 编译命令，分别覆盖 DC 与 ICC 两类后端产物场景。
def _build_gate_level_commands(list_compile_cmd: list[str], str_verif_list: str) -> dict[str, object]:
    """
    返回 guarded gate-level 规划需要的 DC / ICC 编译命令集合。

    参数：
    - list_compile_cmd: 主 RTL 编译命令列表。
    - str_verif_list: gate-level 场景使用的验证 filelist 文件名。

    返回：
    - 返回包含 DC 与 ICC 两条 gate-level 编译命令的字典。

    异常：
    - 无显式异常；静态列表构造沿用 Python 默认行为。
    """

    # 提取 VCS 主编译命令去掉可执行名后的公共参数，避免 gate-level 命令重复手写。
    list_shared_compile_args = list_compile_cmd[1:]  # 去掉 vcs 可执行名后的公共编译参数

    # 返回 guarded gate-level 命令集合，明确这两条命令都依赖额外后端工艺环境。
    return {
        "scope": "guarded_optional",
        "dc_compile_cmd": [
            "vcs",
            "+define+GATES",
            *list_shared_compile_args,
            "-f",
            str_verif_list,
            "-o",
            "simv.dc_gate",
        ],
        "icc_compile_cmd": [
            "vcs",
            "+define+GATES",
            *list_shared_compile_args,
            "-f",
            str_verif_list,
            "-o",
            "simv.icc_gate",
        ],
    }

# 为指定产品构造 FP-Gen 非 GUI 规划结果。
def build_plan(
    *,
    project_root: Path,
    product: str = "FPGen",
    include_gate: bool = False,
    dry_run: bool = True,
) -> dict[str, object]:
    """
    为指定 FP-Gen 产品构造 Genesis2 到 VCS 的非 GUI 执行计划。

    参数：
    - project_root: 当前 FP-Gen 项目的根目录。
    - product: 待规划的产品名，默认使用 ``FPGen``。
    - include_gate: 是否额外生成 guarded gate-level DC / ICC 规划。
    - dry_run: 是否只生成规划而不表达真实执行意图，默认保持 ``True``。

    返回：
    - 返回包含生成、编译、仿真、SAIF 与可选 gate-level 阶段的结构化计划字典。

    异常：
    - 无显式异常；路径解析异常沿用底层文件系统行为。
    """

    # 规范化项目根目录路径，确保 JSON 输出里的 project_root 始终稳定可比较。
    path_project_root = project_root.resolve()  # 当前规划使用的标准化项目根目录

    # 按产品名生成当前顶层模块名，保持 top_FPGen 这类历史命名约定不变。
    str_top_name = f"top_{product}"  # 当前产品对应的顶层模块名

    # 解包 Genesis2 约定的三个 filelist 文件名，供后续各阶段分别引用。
    str_vlog_list, str_synth_list, str_verif_list = SOURCE_LIST_FILENAMES  # Genesis2 主、综合与验证 filelist 文件名

    # 固定层级 XML 文件名，保持产品名和历史工具链命名约定一致。
    str_hierarchy_name = f"{product}.xml"  # 当前产品的层级描述 XML 文件名

    # 规划阶段把 simv 写成相对路径，避免输出受本机绝对路径影响。
    str_simv_path = _relative_to_root(path_project_root / "simv", path_project_root)  # 当前计划中使用的 simv 路径字符串

    # 检查 Genesis2 主 filelist 是否已经生成，缺失时保留稳定诊断而不是直接失败。
    list_diagnostics = _generated_file_diagnostics(path_project_root, str_vlog_list)  # 当前计划的输入缺失诊断列表

    # 读取主流程的基础外部依赖清单，后续 gate-level 开关会在此基础上追加依赖。
    list_optional_dependencies = _base_optional_dependencies()  # 当前计划声明的可选外部依赖列表

    # 构造 Genesis2 生成命令，供计划结果和测试统一复用。
    list_generate_cmd = _build_generate_command(str_top_name, str_vlog_list, str_hierarchy_name)  # Genesis2 生成阶段命令

    # 构造主 RTL VCS 编译命令，保持旧脚本 dry-run 语义与命令结构兼容。
    list_compile_cmd = _build_compile_command(str_top_name, str_vlog_list)  # 主 RTL 编译阶段命令

    # 构造标准 VPD 仿真命令，供 simulate 阶段直接写入结构化计划。
    list_simulate_cmd = _build_simulate_command(str_simv_path)  # VPD 仿真阶段命令

    # 构造 SAIF 运行命令，供功耗分析相关自动化直接读取。
    list_saif_cmd = _build_saif_command(str_simv_path)  # SAIF 运行阶段命令

    # 先生成核心计划字典，保持不含 gate-level 时也能独立满足 dry-run 规划需求。
    dict_plan: dict[str, object] = {  # 当前 FP-Gen 规划结果字典
        "status": "dry-run" if dry_run else "planned",  # 当前计划的执行语义状态
        "scope": "supported_local_plan",  # 当前脚本声明的本地支持范围
        "project_root": str(path_project_root),  # 当前计划对应的项目根目录
        "product": product,  # 当前待规划的产品名称
        "top": str_top_name,  # VCS -top 会引用的顶层模块名
        "source_lists": [str_vlog_list, str_synth_list, str_verif_list],  # Genesis2 主、综合与验证 filelist 清单
        "optional_external_dependencies": list_optional_dependencies,  # 调用方需要自行准备的外部工具与库环境
        "diagnostics": list_diagnostics,  # 当前计划暴露给调用方的缺失输入诊断
        "generate": {  # Genesis2 生成阶段的计划块
            "cmd": list_generate_cmd,  # Genesis2.pl 的完整参数序列
            "outputs": [str_vlog_list, str_synth_list, str_verif_list, str_hierarchy_name],  # Genesis2 预期输出工件
        },
        "compile": {"workdir": ".", "cmd": list_compile_cmd, "log": "comp_bb.log"},  # 主 RTL 编译阶段规划
        "simulate": {"workdir": ".", "cmd": list_simulate_cmd, "log": "run_bb.log"},  # 标准 VPD 仿真阶段规划
        "saif": {  # SAIF 功耗统计阶段的计划块
            "scope": "guarded_optional",  # SAIF 导出依赖额外运行上下文
            "simulate_cmd": list_saif_cmd,  # 功耗统计专用的 simv 参数序列
            "expected_artifact": f"{product}.saif",  # SAIF 阶段预期输出工件
        },
        "expected_artifacts": ["simv", "simv.log", "run_bb.log", "vcdplus.vpd"],  # 主流程预期工件集合
    }

    # 只有调用方显式请求 gate-level 规划时，才补充额外依赖与 DC / ICC 命令集合。
    if include_gate:

        # 把后端工具与工艺库依赖追加到可选依赖列表，显式声明 guarded gate-level 前置条件。
        list_optional_dependencies.extend(["dc_shell", "icc_shell", "technology libraries"])

        # 写入 guarded gate-level 命令集合，同时保持核心计划状态仍由 dry_run / execute 决定。
        dict_plan["gate_level"] = _build_gate_level_commands(list_compile_cmd, str_verif_list)  # guarded gate-level 命令集合

    # 返回当前 FP-Gen 结构化计划，供测试、脚本包装器与质量门统一消费。
    return dict_plan

# 解析命令行参数并输出 FP-Gen 规划摘要或 JSON 协议。
def main(argv: list[str] | None = None) -> int:
    """
    运行 FP-Gen 规划命令行入口并返回进程退出码。

    参数：
    - argv: 可选的命令行参数列表；传入 ``None`` 时使用进程默认参数。

    返回：
    - 始终返回 ``0``，表示规划函数已成功生成结构化结果。

    异常：
    - 参数解析失败时由 ``argparse`` 抛出并终止进程；路径解析异常沿用底层行为。
    """

    # 创建命令行参数解析器，统一声明脚本用途与支持的规划开关。
    parser = argparse.ArgumentParser(description="Plan FP-Gen Genesis2 plus VCS non-GUI flows.")  # 当前 CLI 的参数解析器

    # 注册项目根目录参数，要求调用方显式指定待规划的 FP-Gen 项目位置。
    parser.add_argument("--project-root", type=Path, required=True)

    # 注册产品名称参数，默认沿用历史脚本的 FPGen 命名。
    parser.add_argument("--product", default="FPGen")

    # 注册 guarded gate-level 规划开关，启用后补充 DC / ICC 相关命令。
    parser.add_argument("--include-gate", action="store_true")

    # 注册 execute 开关，显式表达希望得到可执行计划而不是默认 dry-run 计划。
    parser.add_argument("--execute", action="store_true")

    # 注册 dry-run 开关，允许调用方显式保留 dry-run 语义。
    parser.add_argument("--dry-run", action="store_true")

    # 注册 JSON 输出开关，启用后按模块文档约定输出单个结构化 JSON 对象。
    parser.add_argument("--json", action="store_true")

    # 解析命令行参数，得到本次规划请求的项目路径、产品与输出模式。
    args = parser.parse_args(argv)  # 当前 CLI 解析得到的参数对象

    # 只有显式 --execute 才切到 planned；其余情况保持 dry-run，兼容历史默认行为。
    bool_dry_run = not args.execute  # 当前 CLI 最终采用的 dry-run 语义

    # 构造当前请求的结构化计划结果，供 JSON 协议和终端摘要共同复用。
    dict_plan = build_plan(  # 当前 CLI 生成的 FP-Gen 结构化计划
        project_root=args.project_root,  # 当前请求指定的项目根目录
        product=args.product,  # 当前请求指定的产品名称
        include_gate=args.include_gate,  # 是否补充 guarded gate-level 规划
        dry_run=bool_dry_run,  # 当前请求最终采用的 dry-run 语义
    )

    # 当调用方显式请求 JSON 协议时，输出单个结构化对象供自动化直接消费。
    if args.json:

        # 按模块文档约定把单个 JSON 对象写到标准输出，避免混入额外终端文本。
        json.dump(dict_plan, sys.stdout, indent=2, sort_keys=True)

        # 为 JSON 协议输出补一个换行，避免 shell 提示符直接接在 JSON 末尾。
        sys.stdout.write("\n")

    # 未请求 JSON 协议时，只输出人类可读摘要，避免直接把完整结构打印到终端。
    else:

        # 只要存在缺失 filelist 或 guarded gate-level 请求，就用 warning 摘要提示调用方查看详情。
        if dict_plan["diagnostics"] or args.include_gate:

            # 输出告警摘要，提示调用方当前计划仍包含输入缺失或 guarded 依赖信息。
            print("> WARNING: [Python] plan generated with diagnostics; rerun with --json for full details")

        # 其余情况输出普通信息摘要，表示计划已按请求正常生成。
        else:

            # 输出普通规划摘要，提示调用方结构化计划已经成功生成。
            print("> INFO: [Python] plan generated successfully")

    # 当前脚本只负责生成规划，因此成功完成参数解析和计划构造后统一返回零退出码。
    return 0

# 只有以脚本方式直接执行时才启动 CLI，避免导入测试模块时立刻退出当前 Python 进程。
if __name__ == "__main__":

    # 把 main 返回值转换为进程退出码，供 shell、CI 与远端 smoke 流程直接判定成败。
    raise SystemExit(main())
