#!/usr/bin/env python3
"""规划 AISS Phase III 的非 GUI VCS / DC 流程。

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

# 固定 VCS 规划阶段使用的命令行开关，保持各目标生成出来的编译命令形态一致。
COMPILE_FLAGS = ["-sverilog", "-suppress", "-R", "+vcs+vcdpluson"]  # AISS VCS 规划统一使用的编译参数

# 返回 AISS 各个受支持目标的静态规划规格。
def _target_definitions() -> dict[str, dict[str, object]]:
    """
    返回 AISS 流程支持的目标规格集合。

    参数：
    - 无额外业务参数；目标规格由模块内置清单固定提供。

    返回：
    - 返回以目标名称为键的规格字典，每项包含 top、sources 和 scope 信息。

    异常：
    - 无显式异常；静态字典构造沿用 Python 默认行为。
    """

    # 返回各个 AISS 目标的静态规划规格，供 build_plan 在不同目标分支中统一复用。
    return {
        "MCSEtest": {
            "top": "mcse_top_tb",
            "sources": [
                "mcse_top.sv",
                "mcse_control_unit.sv",
                "secure_memory.sv",
                "secure_boot_control.sv",
                "lifecycle_protection.sv",
                "lc_memory.sv",
                "fw_auth.sv",
                "min_security_module.sv",
                "sha_top.sv",
                "sha256_puf_256.v",
                "camellia_top.sv",
                "camellia.v",
                "gpio.v",
                "gpio_regmap.v",
                "oh_dsync.v",
                "io.v",
                "packet2emesh.v",
                "c1908.v",
                "primitives.v",
                "puf.v",
                "bus_translation.sv",
                "data_worker.sv",
                "error_correction.v",
                "mcse_top_tb.sv",
                "lec25dscc25.v",
            ],
            "scope": "rtl",
        },
        "AHBtest": {
            "top": "data_worker_tb",
            "sources": ["data_worker.sv", "data_worker_tb.sv"],
            "scope": "rtl",
        },
        "NETLISTtest": {
            "top": "mcse_top_netlist_tb",
            "sources": ["mcse_netlist.v", "mcse_top_netlist_tb.sv", "lec25dscc25.v"],
            "scope": "guarded_gate_level",
        },
    }

# 收集当前目标缺失的源码文件，供后续 guarded 或 blocked 判断直接复用。
def _missing_sources(project_root: Path, sources: list[str]) -> list[str]:
    """
    返回当前项目根目录下缺失的源码清单。

    参数：
    - project_root: 待检查的项目根目录。
    - sources: 目标规划要求存在的源码相对路径列表。

    返回：
    - 返回缺失源码诊断字符串列表；若全部存在，则返回空列表。

    异常：
    - 无显式异常；路径访问异常沿用底层文件系统行为。
    """

    # 累积当前目标缺失的源码诊断信息，供规划结果直接暴露给调用方。
    list_diagnostics: list[str] = []  # 当前目标缺失源码的诊断列表

    # 逐个检查目标源码是否存在，缺失时登记稳定的诊断消息。
    for str_source_name in sources:

        # 只有当前源码文件缺失时才记录诊断，避免把已满足的前置条件重复写入计划。
        if not (project_root / str_source_name).exists():

            # 记录缺失源码的稳定诊断字符串，便于测试与自动化直接断言。
            list_diagnostics.append(f"missing source: {str_source_name}")

    # 返回缺失源码诊断列表，供上层规划函数决定 guarded 或 dry-run 状态。
    return list_diagnostics

# 为指定 AISS 目标构造非 GUI 规划结果。
def build_plan(*, project_root: Path, target: str = "MCSEtest", dry_run: bool = True) -> dict[str, object]:
    """
    为指定 AISS 目标构造非 GUI VCS / DC 执行计划。

    参数：
    - project_root: 当前 AISS 项目的根目录。
    - target: 待规划的目标名称，默认使用 ``MCSEtest``。
    - dry_run: 是否只生成规划而不声明真实执行，默认保持 ``True``。

    返回：
    - 返回结构化计划字典，包含状态、目标、作用域、命令、诊断与期望工件等字段。

    异常：
    - 无显式异常；路径解析异常沿用底层文件系统行为。
    """

    # 规范化项目根目录路径，确保回传给调用方的 cwd 字段始终稳定可比较。
    path_project_root = project_root.resolve()  # 当前规划使用的标准化项目根目录

    # 读取全部目标规格定义，供后续按目标名查表选择规划路径。
    dict_targets = _target_definitions()  # 当前支持的 AISS 目标规格映射

    # 单独处理综合规划分支，因为它使用 dc_shell 与 Tcl 脚本而不是 VCS 编译命令。
    if target == "synthesis":

        # 固定综合流程要求的 Tcl 入口脚本名称，保持测试与外部调用的命令约定一致。
        str_script_name = "compiledc.tcl"  # 综合流程的 Tcl 入口脚本名

        # 先假定综合脚本存在，只有实际缺失时才补入对应诊断。
        list_diagnostics: list[str] = []  # 综合流程的缺失脚本诊断列表

        # 综合脚本缺失时仍返回 guarded 计划，避免因为外部环境不全而直接崩溃。
        if not (path_project_root / str_script_name).exists():

            # 记录综合脚本缺失诊断，供调用方在 JSON 计划里直接读取。
            list_diagnostics.append(f"missing source: {str_script_name}")

        # 返回综合规划结果，明确其依赖外部 dc_shell 与标准单元库环境。
        return {
            "status": "guarded",
            "target": target,
            "scope": "guarded_synthesis",
            "project_root": str(path_project_root),
            "guarded_external_dependencies": ["dc_shell", "standard-cell .db libraries"],
            "diagnostics": list_diagnostics,
            "synthesis": {"cwd": str(path_project_root), "cmd": ["dc_shell", "-f", str_script_name]},
        }

    # 未知目标直接返回 blocked 计划，避免生成不可信的命令行。
    if target not in dict_targets:

        # 返回未知目标的阻断结果，让调用方用稳定原因码区分输入错误。
        return {
            "status": "blocked",
            "target": target,
            "reason": "unknown_target",
            "diagnostics": [f"unknown AISS target: {target}"],
        }

    # 取出当前目标的静态规格，作为后续源码、scope 与 top 规划的唯一事实来源。
    dict_target_spec = dict_targets[target]  # 当前目标的静态规划规格

    # 复制源码清单，避免后续命令拼装或测试修改时反向污染共享静态规格。
    list_sources = list(dict_target_spec["sources"])  # 当前目标要求参与编译的源码清单

    # 对当前目标做一次目录树扫描，明确本次规划还缺哪些必须参与编译的 RTL 文件。
    list_diagnostics = _missing_sources(path_project_root, list_sources)  # 供 diagnostics 字段直接复用的缺失文件消息列表

    # 读取当前目标的作用域语义，决定是否需要外部依赖守卫。
    str_scope = str(dict_target_spec["scope"])  # 当前目标对应的规划作用域

    # 默认不声明外部守卫依赖，只有特定 gate-level 目标才补充相应说明。
    list_guarded_dependencies: list[str] = []  # 当前目标额外依赖的守卫项列表

    # gate-level 目标需要补充标准单元、门级 testbench 与门级仿真工具等额外说明。
    if str_scope == "guarded_gate_level":

        # 记录 gate-level 目标对外部环境的守卫依赖，避免调用方误把它当作纯 dry-run 流程。
        list_guarded_dependencies = [  # gate-level 目标的外部守卫依赖列表
            "standard-cell library netlist",  # 标准单元库网表依赖
            "gate-level testbench",  # 门级测试平台依赖
            "VCS gate-level simulation",  # 门级 VCS 仿真环境依赖
        ]

    # 拼装 VCS 编译命令列表，保持源码顺序与统一编译开关顺序稳定可预测。
    list_compile_cmd = ["vcs", *list_sources, *COMPILE_FLAGS]  # 当前目标的 VCS 编译命令

    # gate-level 守卫依赖优先决定 guarded 状态，普通 RTL 目标即使缺文件也保留 dry-run / planned 语义。
    str_status = "guarded" if list_guarded_dependencies else ("dry-run" if dry_run else "planned")  # 当前目标的最终规划状态

    # 返回普通 VCS 目标的结构化规划结果，供测试和外部自动化统一消费。
    return {
        "status": str_status,
        "target": target,
        "scope": str_scope,
        "project_root": str(path_project_root),
        "top": dict_target_spec["top"],
        "sources": list_sources,
        "compile": {"cwd": str(path_project_root), "cmd": list_compile_cmd},
        "expected_artifacts": ["simv", "vcdplus.vpd"],
        "diagnostics": list_diagnostics,
        "guarded_external_dependencies": list_guarded_dependencies,
    }

# 解析命令行参数并输出 AISS 规划结果摘要或 JSON 协议。
def main(argv: list[str] | None = None) -> int:
    """
    运行 AISS 规划命令行入口并返回进程退出码。

    参数：
    - argv: 可选的命令行参数列表；传入 ``None`` 时使用进程默认参数。

    返回：
    - 始终返回 ``0``，表示规划函数已成功生成结构化结果。

    异常：
    - 参数解析失败时由 ``argparse`` 抛出并终止进程；路径解析异常沿用底层行为。
    """

    # 创建命令行参数解析器，统一声明脚本用途与各个入口参数。
    parser = argparse.ArgumentParser(description="Plan AISS Phase III non-GUI VCS/DC targets.")  # 当前 CLI 的参数解析器

    # 注册项目根目录参数，要求调用方显式给出待规划项目路径。
    parser.add_argument("--project-root", type=Path, required=True)

    # 注册目标名称参数，默认沿用历史脚本的 MCSEtest 目标。
    parser.add_argument("--target", default="MCSEtest")

    # 注册 dry-run 开关，允许调用方只生成规划而不表达真实执行意图。
    parser.add_argument("--dry-run", action="store_true")

    # 注册 JSON 输出开关，启用后按模块文档声明输出单个结构化 JSON 对象。
    parser.add_argument("--json", action="store_true")

    # 解析命令行参数，得到本次规划需要的项目路径、目标与输出模式。
    args = parser.parse_args(argv)  # 当前 CLI 解析得到的参数对象

    # 构造当前请求对应的结构化计划结果，供 JSON 协议或终端摘要复用。
    dict_plan = build_plan(project_root=args.project_root, target=args.target, dry_run=args.dry_run)  # 当前 CLI 生成的结构化计划

    # 当调用方显式请求 JSON 协议时，输出单个结构化对象供自动化直接消费。
    if args.json:

        # 按模块文档约定把单个 JSON 对象写到标准输出，避免混入额外终端文本。
        json.dump(dict_plan, sys.stdout, indent=2, sort_keys=True)

        # 为 JSON 协议输出补一个换行，避免 shell 提示符直接接在 JSON 末尾。
        sys.stdout.write("\n")

    # 未请求 JSON 协议时，只输出带前缀的摘要信息，避免把完整结构直接打印到终端。
    else:

        # 阻断目标使用错误前缀提醒调用方优先修正输入目标名称。
        if dict_plan["status"] == "blocked":

            # 输出阻断摘要，提示调用方当前目标名称不受支持。
            print("> ERR: [Python] blocked because the requested AISS target is unknown")

        # 受守卫目标使用告警前缀，提示调用方仍需补齐外部依赖或缺失源码。
        elif dict_plan["status"] == "guarded":

            # 输出守卫摘要，提示调用方当前计划仍受外部依赖或缺失文件约束。
            print("> WARNING: [Python] guarded plan generated; rerun with --json for full dependency details")

        # 其余情况输出普通信息前缀，表示计划已按请求正常生成。
        else:

            # 输出普通规划摘要，提示调用方计划已经成功生成。
            print("> INFO: [Python] plan generated successfully")

    # 当前脚本只负责生成规划结果，因此成功完成参数解析和规划后统一返回零退出码。
    return 0

# 只有以脚本方式直接执行时才启动 CLI，避免导入测试模块时立刻退出当前 Python 进程。
if __name__ == "__main__":

    # 把 main 返回值转换为进程退出码，供 shell、CI 与远端 smoke 流程直接判定成败。
    raise SystemExit(main())
