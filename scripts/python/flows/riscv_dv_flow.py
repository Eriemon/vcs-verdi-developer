#!/usr/bin/env python3
"""规划或执行 PicoRV32 / RISCV-DV 风格的非 GUI VCS 流程。

本模块同时提供可导入的规划函数、执行函数与命令行入口。

命令行标准输出协议：
- 默认输出带前缀的人类可读摘要，不直接把结构化计划打印到终端。
- 当传入 ``--json`` 时，标准输出会写出单个 JSON 对象，供上游自动化直接消费。
"""

# 启用延后求值注解，避免类型提示在运行期引入额外解析顺序要求。
from __future__ import annotations
# 提供命令行参数解析、JSON 序列化、子进程执行、耗时统计与路径处理能力。
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# 固定 RISCV-DV 流程中使用的覆盖率指标，保证编译与仿真阶段的参数保持一致。
COVERAGE_METRICS = "line+cond+fsm+tgl+branch"  # RISCV-DV VCS 与 URG 统一使用的覆盖率指标字符串

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

# 统一构造单个步骤的结构化表示，避免不同阶段出现字段不一致。
def _build_step(str_name: str, list_cmd: list[str], path_cwd: Path) -> dict[str, object]:
    """
    返回统一格式的单个流程步骤字典。

    参数：
    - str_name: 当前步骤名称。
    - list_cmd: 当前步骤的命令参数列表。
    - path_cwd: 当前步骤执行时使用的工作目录。

    返回：
    - 返回包含 ``name``、``cmd`` 与 ``cwd`` 三个字段的步骤字典。

    异常：
    - 无显式异常；字符串转换与路径字符串化沿用 Python 默认行为。
    """

    # 返回统一字段结构的步骤字典，确保 dry-run 与 execute 共用同一份计划形态。
    return {
        "name": str_name,
        "cmd": [str(item) for item in list_cmd],
        "cwd": str(path_cwd),
    }

# 统一返回未显式传入种子时应使用的默认种子值。
def _seed_value(seed: int | None) -> int:
    """
    返回当前流程应使用的整数随机种子值。

    参数：
    - seed: 调用方传入的可选种子值。

    返回：
    - 若调用方传入种子则返回该值；否则返回默认种子 ``1``。

    异常：
    - 无显式异常；整数返回沿用 Python 默认行为。
    """

    # 缺省种子沿用历史脚本使用的 1，保持测试与参考命令行为一致。
    return seed if seed is not None else 1

# 返回 RISCV-DV 主流程默认声明的外部依赖清单。
def _optional_dependencies() -> list[str]:
    """
    返回 RISCV-DV 主流程的外部依赖列表。

    参数：
    - 无额外业务参数；依赖清单由模块内置规则固定提供。

    返回：
    - 返回 RISCV-DV、工具链、仿真器与覆盖率工具相关的依赖名称列表。

    异常：
    - 无显式异常；静态列表构造沿用 Python 默认行为。
    """

    # 返回当前流程依赖的生成器、编译器、参考模型与 Synopsys 工具清单。
    return ["riscv-dv", "RISC-V GCC toolchain", "Spike", "vcs", "urg"]

# 统一构造 RISCV-DV 主流程会反复引用的路径集合，避免散落的路径拼接难以维护。
def _build_flow_paths(path_project_root: Path, path_dv_root: Path, str_test_name: str) -> dict[str, Path]:
    """
    返回 RISCV-DV 主流程需要反复引用的路径集合。

    参数：
    - path_project_root: 当前 PicoRV32 工程根目录。
    - path_dv_root: 当前 RISCV-DV 工作目录。
    - str_test_name: 当前待执行的测试名称。

    返回：
    - 返回包含 out、build、simv、hex、trace 与 coverage 路径的字典。

    异常：
    - 无显式异常；路径拼接沿用 Python 默认行为。
    """

    # 固定 out 目录位置，供后续构造 build、hex、trace 与 coverage 路径复用。
    path_out_dir = path_dv_root / "out"  # RISCV-DV 工作目录下的统一输出根目录

    # 固定 build 目录位置，供 simv 和 coverage.vdb 的产物路径复用。
    path_build_dir = path_out_dir / "build"  # 编译与覆盖率数据库所在的构建目录

    # 返回后续各阶段会重复使用的全部关键路径，避免不同函数各自重新拼接。
    return {
        "project_root": path_project_root,
        "dv_root": path_dv_root,
        "out_dir": path_out_dir,
        "build_dir": path_build_dir,
        "simv": path_build_dir / "simv",
        "hex": path_out_dir / "picorv32" / str_test_name / "test.hex",
        "trace": path_out_dir / "picorv32" / str_test_name / "trace_core_0.log",
        "coverage_vdb": path_build_dir / "coverage.vdb",
        "coverage_report": path_out_dir / "cov_report",
    }

# 组合出当前 RISCV-DV 流程的全部阶段步骤，供 dry-run 与 execute 共同复用。
def _build_steps(
    *,
    dict_paths: dict[str, Path],
    str_test_name: str,
    int_seed_value: int,
) -> list[dict[str, object]]:
    """
    返回 RISCV-DV 主流程的结构化步骤列表。

    参数：
    - dict_paths: 当前流程需要复用的关键路径字典。
    - str_test_name: 待执行的测试名称。
    - int_seed_value: 当前流程使用的整数随机种子。

    返回：
    - 返回从生成、编译、仿真到 URG 与 trace compare 的步骤字典列表。

    异常：
    - 无显式异常；静态列表构造沿用 Python 默认行为。
    """

    # 为生成阶段构造 --seed 参数，保证调用方未传种子时仍得到稳定的命令文本。
    str_seed_option = f"--seed={int_seed_value}"  # RISCV-DV 生成脚本使用的 seed 参数

    # 读取 RISCV-DV 工作目录路径，供脚本路径与执行 cwd 统一复用。
    path_dv_root = dict_paths["dv_root"]  # 当前流程使用的 RISCV-DV 工作目录

    # 读取 PicoRV32 工程根目录路径，专供 make -C 阶段绑定正确的工程上下文。
    path_project_root = dict_paths["project_root"]  # compile_test 阶段使用的 PicoRV32 工程根目录

    # 返回完整步骤列表，保持步骤名和命令顺序与现有测试断言一致。
    return [
        _build_step(
            "riscv_dv_gen",
            [
                "python3",
                str(path_dv_root / "scripts" / "run_riscv_dv.py"),
                f"--test={str_test_name}",
                str_seed_option,
            ],
            path_dv_root,
        ),
        _build_step(
            "compile_test",
            [
                "make",
                "-C",
                str(path_dv_root),
                "riscv_dv_test",
                f"TEST={str_test_name}",
                f"SEED={int_seed_value}",
            ],
            path_project_root,
        ),
        _build_step(
            "vcs_compile",
            [
                "vcs",
                "-full64",
                "-sverilog",
                "-f",
                "cfg/vcs.f",
                "-o",
                str(dict_paths["simv"]),
                "-debug_access+all",
                "-timescale=1ns/1ps",
                "-cm",
                COVERAGE_METRICS,
                "-cm_dir",
                str(dict_paths["coverage_vdb"]),
            ],
            path_dv_root,
        ),
        _build_step(
            "simv",
            [
                str(dict_paths["simv"]),
                f"+hex={dict_paths['hex']}",
                f"+trace={dict_paths['trace']}",
                "-cm",
                COVERAGE_METRICS,
            ],
            path_dv_root,
        ),
        _build_step(
            "urg_report",
            ["urg", "-full64", "-dir", str(dict_paths["coverage_vdb"]), "-report", str(dict_paths["coverage_report"])],
            path_dv_root,
        ),
        _build_step(
            "trace_compare",
            ["python3", str(path_dv_root / "scripts" / "compare_trace.py"), str(dict_paths["trace"])],
            path_dv_root,
        ),
    ]

# 为指定测试构造 RISCV-DV 非 GUI 规划结果。
def build_plan(
    *,
    project_root: Path | str,
    dv_root: Path | str,
    test: str,
    seed: int | None = None,
    execute: bool = False,
) -> dict[str, object]:
    """
    为指定 PicoRV32 / RISCV-DV 测试构造非 GUI 执行计划。

    参数：
    - project_root: 当前 PicoRV32 项目的根目录。
    - dv_root: 当前 RISCV-DV 工作目录。
    - test: 待执行的测试名称。
    - seed: 可选随机种子；未传入时沿用默认种子 ``1``。
    - execute: 是否表达真实执行意图；未启用时返回 dry-run 计划。

    返回：
    - 返回包含步骤、命名索引、期望工件与外部依赖的结构化计划字典。

    异常：
    - 无显式异常；路径解析异常沿用底层文件系统行为。
    """

    # 规范化 PicoRV32 工程根目录路径，确保 JSON 输出里的 project_root 始终稳定可比较。
    path_project_root = Path(project_root).resolve()  # 当前计划使用的标准化 PicoRV32 根目录

    # 把用户传入的 dv_root 解析成绝对目录，确保 run_riscv_dv.py 与 compare_trace.py 始终从同一树下取文件。
    path_dv_root = Path(dv_root).resolve()  # 生成器脚本与 trace 对比脚本共用的 dv 工作目录

    # 固定缺省随机种子，确保未传参时命令文本、测试与回归行为保持稳定。
    int_seed_value = _seed_value(seed)  # 当前流程最终采用的随机种子值

    # 预先整理当前流程所有关键路径，避免后续步骤构造和工件回写重复拼接。
    dict_paths = _build_flow_paths(path_project_root, path_dv_root, test)  # 当前 RISCV-DV 流程的关键路径集合

    # 统一构造全部步骤，确保 dry-run 与 execute 始终消费完全同形的计划对象。
    list_steps = _build_steps(  # 当前流程的结构化步骤列表
        dict_paths=dict_paths,  # 供各阶段读取 simv、hex、trace 与 coverage 路径
        str_test_name=test,  # 当前待规划或执行的测试名称
        int_seed_value=int_seed_value,  # 同时驱动生成器与 make 阶段的统一种子值
    )

    # 为步骤列表建立名称索引，便于测试与调用方按步骤名直接访问命令内容。
    dict_steps_by_name = {dict_step["name"]: dict_step for dict_step in list_steps}  # 当前步骤列表按名称建立的索引映射

    # 返回结构化计划结果，保持字段与现有测试和包装脚本依赖一致。
    return {
        "status": "planned" if execute else "dry-run",
        "project_root": str(path_project_root),
        "dv_root": str(path_dv_root),
        "test": test,
        "seed": seed,
        "steps": list_steps,
        "steps_by_name": dict_steps_by_name,
        "expected_artifacts": {
            "test.hex": _relative_to_root(dict_paths["hex"], path_project_root),
            "trace_core_0.log": _relative_to_root(dict_paths["trace"], path_project_root),
            "coverage.vdb": _relative_to_root(dict_paths["coverage_vdb"], path_project_root),
            "cov_report": _relative_to_root(dict_paths["coverage_report"], path_project_root),
        },
        "optional_external_dependencies": _optional_dependencies(),
    }

# 返回单步骤执行结果中除基础计划字段以外的动态状态字段。
def _build_result_fields(
    *,
    returncode: int | None,
    str_status: str,
    float_elapsed_sec: float,
    str_stdout: str,
    str_stderr: str,
) -> dict[str, object]:
    """
    返回单步骤执行结果中的动态状态字段字典。

    参数：
    - returncode: 当前步骤的退出码；超时场景下允许为 ``None``。
    - str_status: 当前步骤的执行状态字符串。
    - float_elapsed_sec: 当前步骤已经计算好的运行耗时秒数。
    - str_stdout: 当前步骤捕获到的标准输出文本。
    - str_stderr: 当前步骤捕获到的标准错误文本。

    返回：
    - 返回退出码、状态、耗时与日志文本构成的动态字段字典。

    异常：
    - 无显式异常；字典构造沿用 Python 默认行为。
    """

    # 返回当前步骤的动态结果字段，供 success 与 timeout 两条路径共同复用。
    return {
        "returncode": returncode,  # 当前步骤的退出码
        "status": str_status,  # 当前步骤的执行状态
        "elapsed_sec": float_elapsed_sec,  # 当前步骤的实际耗时秒数
        "stdout": str_stdout,  # 当前步骤捕获到的标准输出
        "stderr": str_stderr,  # 当前步骤捕获到的标准错误
    }

# 根据统一字段协议组装单个步骤的执行结果，避免 success/timeout 两条路径各自维护大字典。
def _build_step_result(
    dict_step: dict[str, object],
    dict_result_fields: dict[str, object],
) -> dict[str, object]:
    """
    返回统一字段形态的单步骤执行结果字典。

    参数：
    - dict_step: 当前已执行步骤的静态计划字典。
    - dict_result_fields: 当前步骤新增的动态状态字段字典。

    返回：
    - 返回包含计划字段、退出码、状态、耗时与日志文本的结果字典。

    异常：
    - 无显式异常；字典构造沿用 Python 默认行为。
    """

    # 先复制原始计划字段，确保返回结果天然继承 name、cmd 与 cwd 三个基础字段。
    dict_result = dict(dict_step)  # 当前步骤结果的基础字段副本

    # 再把当前步骤的动态状态字段合并进去，形成最终统一的结构化结果对象。
    dict_result.update(dict_result_fields)

    # 返回当前步骤的统一结构化执行结果，供 execute_plan 直接汇总。
    return dict_result

# 执行单个结构化步骤，并返回可直接写入结果列表的执行记录。
def _run_step(dict_step: dict[str, object], *, timeout: int) -> dict[str, object]:
    """
    执行单个流程步骤并返回结构化执行记录。

    参数：
    - dict_step: 当前待执行的步骤字典。
    - timeout: 当前步骤允许使用的超时时间，单位为秒。

    返回：
    - 返回包含状态、耗时、标准输出与标准错误的结构化步骤结果字典。

    异常：
    - 子进程启动失败等 ``OSError`` 及其子类异常沿用底层行为。
    """

    # 在步骤开始前记录单调时钟，用于后续稳定计算当前步骤的执行耗时。
    float_started_at = time.monotonic()  # 当前步骤开始执行时的单调时钟时间

    # 尝试在给定超时时间内执行当前步骤，并收集完整 stdout/stderr 供上游复盘。
    try:

        # 运行当前步骤命令，保持文本模式与显式 stdout/stderr 捕获策略一致。
        completed_process_completed: subprocess.CompletedProcess[str] = subprocess.run(  # 当前步骤完成后的子进程结果对象
            dict_step["cmd"],  # 当前步骤要执行的命令参数列表
            cwd=dict_step["cwd"],  # 当前步骤的工作目录
            text=True,  # 统一使用文本模式采集输出
            stdout=subprocess.PIPE,  # 捕获标准输出供结果对象回传
            stderr=subprocess.PIPE,  # 捕获标准错误供结果对象回传
            timeout=timeout,  # 为当前步骤施加单步超时限制
        )

        # 先根据当前执行结果组装动态状态字段，供统一结果构造函数复用。
        dict_result_fields = _build_result_fields(  # 正常执行场景下的动态状态字段
            returncode=completed_process_completed.returncode,  # 真实子进程交回的退出码
            str_status="passed" if completed_process_completed.returncode == 0 else "failed",  # 基于退出码归一化后的成功或失败状态
            float_elapsed_sec=round(time.monotonic() - float_started_at, 3),  # 正常执行场景下重新计算得到的实际耗时
            str_stdout=completed_process_completed.stdout,  # 正常执行场景捕获到的标准输出
            str_stderr=completed_process_completed.stderr,  # 正常执行场景捕获到的标准错误
        )

        # 返回正常执行结束后的结构化步骤结果，统一记录退出码、状态与日志文本。
        return _build_step_result(dict_step, dict_result_fields)

    # 只有子进程执行超时才走这里，显式返回 timeout 状态而不是抛出给上层。
    except subprocess.TimeoutExpired as exc:

        # 先根据超时结果组装动态状态字段，保证调用方仍能读取已捕获的部分日志。
        dict_result_fields = _build_result_fields(  # 超时场景下的动态状态字段
            returncode=None,  # 超时场景没有可用退出码
            str_status="timeout",  # 超时场景统一归一化成 timeout 状态
            float_elapsed_sec=round(time.monotonic() - float_started_at, 3),  # 超时场景下截止异常抛出时的实际耗时
            str_stdout=exc.stdout or "",  # 超时场景已捕获到的标准输出片段
            str_stderr=exc.stderr or f"timeout after {timeout}s",  # 超时场景已捕获或补写的错误信息
        )

        # 返回超时场景的结构化步骤结果，保证调用方仍能读取已捕获的部分日志。
        return _build_step_result(dict_step, dict_result_fields)

# 执行结构化 RISCV-DV 计划，并在首个失败或超时步骤后提前停止。
def execute_plan(plan: dict[str, object], *, timeout: int) -> dict[str, object]:
    """
    执行结构化 RISCV-DV 计划并返回聚合结果。

    参数：
    - plan: 待执行的结构化计划字典。
    - timeout: 每个步骤允许使用的超时时间，单位为秒。

    返回：
    - 返回包含最终状态与逐步执行结果的聚合字典。

    异常：
    - 子进程启动失败等 ``OSError`` 及其子类异常沿用底层行为。
    """

    # 累积所有已执行步骤的结果，供调用方复盘命令执行顺序与失败位置。
    list_results: list[dict[str, object]] = []  # 当前执行过程中已经收集到的步骤结果列表

    # 逐步执行计划中的每个步骤，保持一旦失败就停止后续高成本命令的历史语义。
    for dict_step in plan["steps"]:

        # 执行当前步骤并收集结构化结果，供最终聚合状态与日志回传复用。
        dict_result = _run_step(dict_step, timeout=timeout)  # 本轮循环刚得到的步骤执行记录

        # 把当前步骤结果追加到总列表，保证失败前的全部日志都能回传给调用方。
        list_results.append(dict_result)

        # 只要当前步骤不是 passed，就立刻停止后续步骤执行，避免级联噪声。
        if dict_result["status"] != "passed":

            # 提前结束步骤循环，把首个失败或超时步骤作为本次执行的终止点。
            break

    # 只有全部步骤都被执行且最后一个结果仍为 passed，才视为命令级执行成功。
    bool_all_steps_passed = bool(list_results)  # 当前执行是否至少成功收集到一条步骤结果

    # 只有最后一个结果通过时，才保留“所有已执行步骤仍处于通过态”的判断前提。
    bool_all_steps_passed = bool_all_steps_passed and list_results[-1]["status"] == "passed"  # 当前执行链路的最后一个步骤是否通过

    # 只有已执行步骤数量与计划步骤数量一致，才说明整条流程确实跑完。
    bool_all_steps_passed = bool_all_steps_passed and len(list_results) == len(plan["steps"])  # 当前执行是否完整覆盖了计划中的全部步骤

    # 返回聚合后的执行结果，保持与原脚本一致的 status 与 results 字段结构。
    return {
        **plan,
        "status": "passed" if bool_all_steps_passed else "failed",
        "results": list_results,
    }

# 解析命令行参数并输出 RISCV-DV 规划摘要、执行摘要或 JSON 协议。
def main(argv: list[str] | None = None) -> int:
    """
    运行 RISCV-DV 规划命令行入口并返回进程退出码。

    参数：
    - argv: 可选的命令行参数列表；传入 ``None`` 时使用进程默认参数。

    返回：
    - dry-run 或执行成功时返回 ``0``，执行失败时返回 ``1``。

    异常：
    - 参数解析失败时由 ``argparse`` 抛出并终止进程；子进程启动异常沿用底层行为。
    """

    # 创建命令行参数解析器，统一声明脚本用途与当前支持的执行开关。
    parser = argparse.ArgumentParser(description="Plan a non-GUI PicoRV32 riscv-dv VCS workflow.")  # 当前 CLI 的参数解析器

    # 注册 PicoRV32 项目根目录参数，要求调用方显式指定当前工程位置。
    parser.add_argument("--project-root", type=Path, required=True)

    # 注册 RISCV-DV 工作目录参数，要求调用方显式指定 dv 子目录位置。
    parser.add_argument("--dv-root", type=Path, required=True)

    # 注册测试名称参数，要求调用方显式指定当前要规划或执行的测试。
    parser.add_argument("--test", required=True)

    # 注册可选随机种子参数，未传时由脚本回退到默认种子 1。
    parser.add_argument("--seed", type=int)

    # 注册 execute 开关，启用后按计划顺序真实运行各步骤命令。
    parser.add_argument("--execute", action="store_true")

    # 注册 dry-run 开关，仅作为显式表达意图使用；实际默认模式本来就是 dry-run。
    parser.add_argument("--dry-run", action="store_true", help="Print the non-executing plan; this is the default.")

    # 注册单步骤超时参数，供执行模式下限制每个外部命令的最长运行时间。
    parser.add_argument("--timeout", type=int, default=300)

    # 注册 JSON 输出开关，启用后按模块文档约定输出单个结构化 JSON 对象。
    parser.add_argument("--json", action="store_true")

    # 解析命令行参数，得到本次请求的路径、测试名、种子、执行模式与输出模式。
    args = parser.parse_args(argv)  # 当前 CLI 解析得到的参数对象

    # 构造当前请求对应的结构化计划，供 dry-run 或 execute 两条路径共同复用。
    dict_plan = build_plan(  # 当前 CLI 生成的 RISCV-DV 结构化计划
        project_root=args.project_root,  # 当前请求指定的 PicoRV32 项目根目录
        dv_root=args.dv_root,  # 当前请求指定的 RISCV-DV 工作目录
        test=args.test,  # 当前请求指定的测试名称
        seed=args.seed,  # 当前请求传入的可选随机种子
        execute=args.execute,  # 当前请求是否表达真实执行意图
    )

    # 只有显式请求 execute 时才真实运行计划，否则保留 dry-run 结果直接输出。
    dict_result = execute_plan(dict_plan, timeout=args.timeout) if args.execute else dict_plan  # 当前 CLI 最终要输出的结果对象

    # 当调用方显式请求 JSON 协议时，输出单个结构化对象供自动化直接消费。
    if args.json:

        # 按模块文档约定把单个 JSON 对象写到标准输出，避免混入额外终端文本。
        json.dump(dict_result, sys.stdout, indent=2, sort_keys=True)

        # 为 JSON 协议输出补一个换行，避免 shell 提示符直接接在 JSON 末尾。
        sys.stdout.write("\n")

    # 未请求 JSON 协议时，只输出带前缀的人类可读摘要。
    else:

        # dry-run 模式下提示计划已经生成，并引导需要细节时改用 JSON 输出。
        if dict_result["status"] == "dry-run":

            # 输出 dry-run 摘要，说明当前只是生成了结构化计划而未执行命令。
            print("> INFO: [Python] dry-run plan generated; rerun with --json for structured details")

        # 执行成功时输出通过摘要，便于 shell 或 smoke 脚本快速识别。
        elif dict_result["status"] == "passed":

            # 输出成功摘要，说明所有 RISCV-DV 步骤都已执行通过。
            print("> INFO: [Python] RISCV-DV flow executed successfully")

        # 执行失败时输出告警摘要，提示调用方改用 JSON 检查首个失败步骤日志。
        else:

            # 输出失败摘要，提醒调用方查看结构化结果中的首个失败步骤详情。
            print("> WARNING: [Python] RISCV-DV flow failed; rerun with --json for step details")

    # dry-run 或执行成功返回零退出码，执行失败返回一，保持 shell 语义清晰稳定。
    return 0 if dict_result["status"] in {"dry-run", "passed"} else 1

# 只有以脚本方式直接执行时才启动 CLI，避免导入测试模块时立刻退出当前 Python 进程。
if __name__ == "__main__":

    # 把 main 返回值转换为进程退出码，供 shell、CI 与远端 smoke 流程直接判定成败。
    raise SystemExit(main())
