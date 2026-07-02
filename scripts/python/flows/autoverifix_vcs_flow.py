#!/usr/bin/env python3
"""规划并执行 AutoVeriFix 风格的非 GUI VCS 覆盖率流程。

本模块同时提供可导入的规划/执行函数与命令行入口。

命令行标准输出协议：
- 默认输出带前缀的人类可读摘要，不直接把结构化结果打印到终端。
- 当传入 ``--json`` 时，标准输出会写出单个 JSON 对象，供上游自动化直接消费。
"""

# 启用延后求值注解，避免类型提示在运行期引入额外解析顺序要求。
from __future__ import annotations

# 提供命令行参数解析、JSON 序列化、日志正则匹配、子进程执行、标准输出与单调时钟能力。
import argparse
import json
import re
import subprocess
import sys
import time

# 补充路径、轻量配置对象与通用类型标注，供规划和执行 helper 共享。
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# 把动态 JSON 风格对象统一抽象成别名，便于说明计划、步骤与执行结果的结构。
JsonDict = dict[str, Any]  # 计划、步骤、执行结果与诊断摘要共用的通用映射结构

# 用轻量命名空间承接 ``build_plan`` 的输入字段，避免公共入口继续堆叠大量显式参数。
class AutoVeriFixPlanConfig(SimpleNamespace):
    """
    承接 ``build_plan`` 输入键值的轻量配置对象。

    参数：
    - 构造时可直接传入 ``task_root``、``task``、``dry_run``、``fsdb``、``verdi_check`` 等兼容 ``build_plan`` 的关键字。

    返回：
    - 当前类实例会把这些字段保存为属性，供计划构建 helper 统一读取。
    """

# 用具名命名空间承接最终计划输出组装阶段共享的事实，避免 helper 之间来回传递长参数列表。
class AutoVeriFixPlanOutputInputs(SimpleNamespace):
    """
    承接最终计划对象组装阶段共享的输入事实。

    参数：
    - 构造时传入基础计划字段、任务目录、dry-run 状态、各阶段命令、产物列表与可选 Verdi 步骤。

    返回：
    - 当前类实例会把这些字段保存为属性，供最终计划组装 helper 统一读取。
    """

# 用具名命名空间承接可选 Verdi/fsdbreport 步骤共用的输入事实，避免相关 helper 持续传递长参数列表。
class AutoVeriFixVerdiStepInputs(SimpleNamespace):
    """
    承接可选 Verdi/fsdbreport 步骤构造阶段共享的输入事实。

    参数：
    - 构造时传入任务目录、FSDB 文件名、顶层名、目标信号、工具覆盖和额外参数列表。

    返回：
    - 当前类实例会把这些字段保存为属性，供波形检查步骤 helper 统一读取。
    """

# 固定 AutoVeriFix 默认采集的覆盖率维度，保证计划输出与测试断言稳定一致。
COVERAGE_METRICS = ["line", "cond", "fsm"]  # AutoVeriFix 默认启用的覆盖率指标列表

# 固定 AutoVeriFix 规划始终关注的两个源码入口名称，避免不同调用路径漂移。
SOURCE_FILENAMES = ("{task}.v", "testbench.v")  # 每个任务都需要具备的 RTL 与 testbench 文件模板

# 固定清理规则，保持 dry-run 输出与历史脚本约定一致。
CLEANUP_PATTERNS = [  # AutoVeriFix 流程默认清理的中间产物模式列表
    "*.log",  # 编译、仿真与覆盖率阶段日志
    "csrc",  # VCS 生成的 csrc 目录
    "simv*",  # simv 可执行文件及其衍生产物
    "*.key",  # VCS/Verdi 可能生成的 key 文件
    "*.vpd",  # VPD 波形文件
    "DVEfiles",  # DVE/Verdi 可能生成的辅助目录
    "coverage",  # 历史脚本遗留的 coverage 目录
    "*.vdb",  # VCS 覆盖率数据库目录
    "output.txt",  # 历史流程常见输出文件
]

# 兼容旧调用方式时，只允许这些字段经由 ``**kwargs`` 写入配置对象，避免拼写错误被静默吞掉。
tuple_plan_option_names = (  # ``build_plan`` 允许接收的显式配置键集合
    "task_root",  # 当前 AutoVeriFix 任务目录
    "task",  # 当前任务名；为空时回退到目录名
    "dry_run",  # 当前计划是否保持 dry-run 语义
    "fsdb",  # 调用方显式指定的 FSDB 文件名
    "verdi_check",  # 波形检查模式
    "report_signal",  # fsdbreport 模式下的目标信号
    "tools",  # 工具入口覆盖映射
    "compile_args",  # 仅附加给 compile 阶段的补充开关
    "simulate_args",  # 仅附加给 simulate 阶段的运行时参数
    "urg_args",  # 仅附加给覆盖率汇总阶段的 URG 参数
    "verdi_args",  # 波形检查阶段附带的工具级扩展参数
)  # build_plan 兼容旧显式参数接口时允许读取的字段列表

# 为未知字段校验准备集合视图，避免每次兼容 ``**kwargs`` 时都重复构造 ``set(...)``。
set_plan_option_names = set(tuple_plan_option_names)  # ``build_plan`` 允许读取的字段名集合

# 统一读取配置对象里的某个字段，让计划 helper 不必反复书写 ``getattr(..., default)``。
def _plan_option(obj_plan_config: AutoVeriFixPlanConfig, str_name: str, obj_default: Any = None) -> Any:
    """
    读取 AutoVeriFix 计划配置对象中的某个字段。

    参数：
    - obj_plan_config: 已规整的计划配置对象。
    - str_name: 需要读取的字段名。
    - obj_default: 字段缺失时的兜底值。

    返回：
    - 返回配置对象中的实际值或兜底值。
    """

    # 所有 helper 都经由同一个入口读取配置字段，保证缺省值语义在整个模块中保持一致。
    return getattr(obj_plan_config, str_name, obj_default)

# 把配置对象入口和兼容 ``**kwargs`` 入口合并成统一命名空间，避免新旧接口分叉出两套逻辑。
def _coerce_plan_config(
    *,
    config: AutoVeriFixPlanConfig | None = None,
    dict_kwargs: dict[str, Any] | None = None,
) -> AutoVeriFixPlanConfig:
    """
    合并显式配置对象与兼容关键字参数。

    参数：
    - config: 可选的 AutoVeriFix 计划配置对象；为空时只使用兼容关键字参数构造。
    - dict_kwargs: 兼容旧接口的关键字参数映射；为空时只读取 ``config``。

    返回：
    - 返回统一规整后的 AutoVeriFix 计划配置对象。

    异常：
    - 当兼容关键字参数包含未支持字段时抛出 ``TypeError``。
    """

    # 先把兼容关键字参数复制成独立字典，避免校验和覆盖过程回写调用方对象。
    dict_request_kwargs = dict(dict_kwargs or {})  # 兼容旧 ``build_plan(...)`` 调用方式的关键字参数副本

    # 未知字段继续放行会把拼写错误静默吞掉，因此这里先统一做显式阻断。
    set_unknown_option_names = set(dict_request_kwargs) - set_plan_option_names  # 当前请求里不被 ``build_plan`` 支持的字段名集合

    # 发现未知字段时立刻抛出结构化异常，方便测试和 CLI 一次性定位全部错误键名。
    if set_unknown_option_names:

        # 把未知字段按字典序拼成单行文本，保证错误输出稳定可比。
        str_unknown_option_names = ", ".join(sorted(set_unknown_option_names))  # 当前请求中的未知字段名摘要文本

        # 沿用统一错误前缀，方便上游脚本按固定协议识别参数层面的失败来源。
        raise TypeError(f"> ERR: [Python] build_plan got unexpected options: {str_unknown_option_names}")

    # 先复制显式配置对象里的现有字段，保证兼容关键字参数只在需要时覆盖同名字段。
    dict_config_values = dict(getattr(config, "__dict__", {})) if config is not None else {}  # 来自显式配置对象的字段副本

    # 兼容旧接口的关键字参数优先级更高，允许调用方在传入配置对象时局部覆盖个别字段。
    dict_config_values.update(dict_request_kwargs)

    # 返回重新封装后的统一配置对象，供后续计划构建 helper 通过属性方式读取。
    return AutoVeriFixPlanConfig(**dict_config_values)

# 统一解析工具覆盖映射，避免每个命令构造 helper 都重复处理空字典回退逻辑。
def _tool(dict_tool_overrides: dict[str, str] | None, str_name: str, str_default: str) -> str:
    """
    返回某个工具的最终可执行入口。

    参数：
    - dict_tool_overrides: 调用方传入的工具覆盖映射；为空时沿用默认工具名。
    - str_name: 需要读取的工具键名。
    - str_default: 没有覆盖值时使用的默认工具名。

    返回：
    - 返回覆盖后的工具入口字符串；若调用方未覆盖则回退到默认值。

    异常：
    - 无显式异常；字典读取与字符串转换沿用 Python 默认行为。
    """

    # 先把可能为空的工具映射规整成普通字典，避免后续读取逻辑散落在多个 helper 里。
    dict_tool_lookup = dict(dict_tool_overrides or {})  # 当前 helper 实际读取的工具覆盖映射

    # 返回覆盖后的工具入口或默认值，保持命令构造阶段的回退语义稳定一致。
    return str(dict_tool_lookup.get(str_name) or str_default)

# 把阶段附加参数统一转成字符串列表，避免命令构造阶段夹杂多种容器类型。
def _stage_args(list_stage_args: list[str] | tuple[str, ...] | None) -> list[str]:
    """
    把阶段附加参数规整成字符串列表。

    参数：
    - list_stage_args: 调用方传入的附加参数序列；允许 ``list``、``tuple`` 或 ``None``。

    返回：
    - 返回适合直接拼进命令列表的字符串列表；未提供参数时返回空列表。

    异常：
    - 无显式异常；字符串转换沿用 Python 默认行为。
    """

    # 逐项把阶段参数转成字符串，确保 Path 或其他对象不会直接混入 subprocess 参数列表。
    return [str(obj_item) for obj_item in (list_stage_args or [])]

# 返回当前任务默认需要存在的源码文件名列表，保证诊断和计划字段共用同一份来源。
def _task_source_filenames(str_task_name: str) -> list[str]:
    """
    返回当前任务需要检查的源码文件名列表。

    参数：
    - str_task_name: AutoVeriFix 任务名称，用于展开 ``{task}.v`` 模板。

    返回：
    - 返回当前任务的 RTL 文件名和 ``testbench.v`` 组成的稳定列表。

    异常：
    - 无显式异常；字符串格式化沿用 Python 默认行为。
    """

    # 按固定模板展开当前任务需要的两个源码入口，避免多处手写字符串漂移。
    return [str_pattern.format(task=str_task_name) for str_pattern in SOURCE_FILENAMES]

# 检查当前任务是否缺失核心源码文件，并返回稳定的缺失诊断列表。
def _source_diagnostics(path_task_root: Path, str_task_name: str) -> list[str]:
    """
    检查任务目录中的核心源码文件是否齐全。

    参数：
    - path_task_root: 当前 AutoVeriFix 任务目录。
    - str_task_name: 当前任务名称，用于展开 RTL 文件名模板。

    返回：
    - 返回所有缺失源码的稳定诊断字符串列表；若文件齐全则返回空列表。

    异常：
    - 无显式异常；路径存在性检查沿用底层文件系统行为。
    """

    # 使用独立列表累计缺失诊断，保证调用方可以稳定断言缺失文件的完整集合。
    list_diagnostics: list[str] = []  # 当前任务缺失源码后生成的诊断字符串列表

    # 按固定顺序逐项检查 RTL 与 testbench，避免诊断顺序在不同平台间漂移。
    for str_source_name in _task_source_filenames(str_task_name):

        # 只有文件缺失时才追加诊断，避免把存在状态重复写进结果。
        if not (path_task_root / str_source_name).exists():

            # 追加稳定的缺失源码文本，供测试与上游技能直接断言。
            list_diagnostics.append(f"missing source: {str_source_name}")

    # 返回当前任务最终收集到的缺失源码诊断列表。
    return list_diagnostics

# 从 ``testbench.v`` 中推断顶层模块名，保持默认任务可以在缺少显式 top 参数时继续规划。
def _infer_top(path_task_root: Path) -> str:
    """
    推断当前任务 testbench 的顶层模块名。

    参数：
    - path_task_root: 当前 AutoVeriFix 任务目录。

    返回：
    - 若 ``testbench.v`` 存在且能找到 ``module`` 声明，则返回对应模块名；否则回退到 ``testbench``。

    异常：
    - 无显式异常；文件读取异常沿用底层文件系统行为。
    """

    # 先固定 ``testbench.v`` 路径，避免后续多次重复拼接目录。
    path_testbench = path_task_root / "testbench.v"  # 当前任务 testbench 文件的绝对路径

    # 缺少 testbench 时直接回退到稳定默认值，避免顶层推断阻塞整个计划构造。
    if not path_testbench.exists():

        # 返回历史默认的顶层模块名，保持缺失 testbench 场景的行为兼容。
        return "testbench"

    # 逐行扫描 testbench 内容，寻找第一条 ``module`` 声明作为顶层模块名来源。
    for str_line in path_testbench.read_text(encoding="utf-8", errors="ignore").splitlines():

        # 先去掉两端空白，避免缩进影响 ``module`` 关键字匹配。
        str_stripped_line = str_line.strip()  # 当前扫描行去掉首尾空白后的文本

        # 只对 ``module`` 开头的语句做顶层名称提取，避免误判注释或其他语句。
        if str_stripped_line.startswith("module "):

            # 返回 ``module`` 后第一个 token 作为顶层名，并去掉结尾可能残留的分号。
            return str_stripped_line.split()[1].split("(")[0].rstrip(";")

    # 没有解析到任何 ``module`` 声明时仍回退到默认 testbench 名称。
    return "testbench"

# 解析仿真日志中的 mismatch 摘要，统一生成通过/失败/未知三类结构化结果。
def parse_simulation_log(str_log_text: str) -> JsonDict:
    """
    解析仿真日志中的 mismatch 统计摘要。

    参数：
    - str_log_text: 需要解析的完整仿真日志文本。

    返回：
    - 若找到 ``Mismatches: <n> in <m> samples`` 摘要，则返回带 ``status``、``mismatches`` 和 ``samples`` 的结果；
      否则返回 ``status=unknown`` 与原因说明。

    异常：
    - 无显式异常；正则匹配与整型转换沿用 Python 默认行为。
    """

    # 用稳定正则抽取 mismatch 与 sample 计数，保持大小写不敏感的日志兼容性。
    obj_match = re.search(r"Mismatches:\s*(\d+)\s+in\s+(\d+)\s+samples", str_log_text, re.IGNORECASE)  # 当前日志匹配到的 scoreboard 摘要对象

    # 没有找到 scoreboard 摘要时直接返回 unknown，避免伪造通过或失败结论。
    if not obj_match:

        # 返回稳定 unknown 结果，提示调用方日志里没有形成可判定的 mismatch 摘要。
        return {"status": "unknown", "reason": "mismatch_summary_not_found"}

    # 第一个正则分组直接对应 mismatch 计数，是最终 passed/failed 判定的核心依据。
    int_mismatches = int(obj_match.group(1))  # 当前仿真日志报告的 mismatch 数量

    # 第二个正则分组记录样本窗口规模，方便后续区分“零 mismatch”与“样本过少”的诊断场景。
    int_samples = int(obj_match.group(2))  # 当前 scoreboard 摘要覆盖的样本窗口大小

    # mismatch 为零视为通过，否则视为失败，保持 AutoVeriFix 回归判定口径稳定。
    str_status = "passed" if int_mismatches == 0 else "failed"  # 当前仿真日志对应的结构化通过状态

    # 返回完整结构化仿真结果，供 execute 阶段和测试直接复用。
    return {
        "status": str_status,
        "mismatches": int_mismatches,
        "samples": int_samples,
    }

# 统计目录产物中的所有普通文件总大小，避免目录分支里反复展开递归求和表达式。
def _directory_bytes(path_directory: Path) -> int:
    """
    返回目录中所有普通文件的累计字节数。

    参数：
    - path_directory: 需要递归统计的目录路径。

    返回：
    - 返回目录下所有普通文件大小求和后的总字节数。

    异常：
    - 无显式异常；目录遍历与 stat 读取沿用底层文件系统行为。
    """

    # 递归遍历目录中的普通文件，并把它们的大小累加成单个整数结果。
    return sum(path_file.stat().st_size for path_file in path_directory.rglob("*") if path_file.is_file())

# 统计任务目录中关键产物的存在性与大小，供执行结果回写统一复用。
def _artifact_status(path_task_root: Path, list_artifacts: list[str]) -> dict[str, JsonDict]:
    """
    收集关键产物的存在性与字节数状态。

    参数：
    - path_task_root: 当前 AutoVeriFix 任务目录。
    - list_artifacts: 需要检查的产物相对路径列表。

    返回：
    - 返回以产物名为键的状态映射，每项包含 ``path``、``exists`` 与 ``bytes`` 字段。

    异常：
    - 无显式异常；路径访问异常沿用底层文件系统行为。
    """

    # 使用独立字典累计各个产物的状态，保证最终结果可以按产物名稳定索引。
    dict_status: dict[str, JsonDict] = {}  # 当前任务关键产物逐项统计后的状态映射

    # 按调用方给定顺序逐项统计产物，避免目录遍历顺序影响 JSON 输出可比性。
    for str_artifact_name in list_artifacts:

        # 先计算当前产物的绝对路径，便于后续统一做存在性与大小检查。
        path_artifact = path_task_root / str_artifact_name  # 当前产物在任务目录中的绝对路径

        # 先记录产物是否存在，后续文件和目录两类大小统计都会复用该结果。
        bool_exists = path_artifact.exists()  # 当前产物路径是否真实存在

        # 默认把字节数初始化为零，缺失产物和空目录都共享这一稳定基线。
        int_bytes_count = 0  # 当前产物最终统计到的字节数

        # 普通文件存在时直接读取文件大小，避免后续目录递归逻辑干扰文件场景。
        if bool_exists and path_artifact.is_file():

            # 文件场景直接取 stat 大小，保持结果和系统报告一致。
            int_bytes_count = path_artifact.stat().st_size  # 当前文件产物的 stat 字节数

        # 目录存在时递归累加其中所有文件的大小，方便评估覆盖率报告目录体积。
        elif bool_exists and path_artifact.is_dir():

            # 把目录下所有普通文件的大小求和，忽略目录节点自身大小噪声。
            int_bytes_count = _directory_bytes(path_artifact)  # 当前目录产物递归累加后的总字节数

        # 把当前产物的路径、存在性与字节数写回状态字典，供执行结果统一复用。
        dict_status[str_artifact_name] = {"path": str(path_artifact), "exists": bool_exists, "bytes": int_bytes_count}  # 当前产物的结构化状态摘要对象

    # 返回本次任务所有关键产物的状态映射。
    return dict_status

# 把成功完成的 subprocess 结果规整成统一的步骤执行结果对象。
def _completed_step_result(
    dict_step: JsonDict,
    completed_process: subprocess.CompletedProcess[str],
    float_started_at: float,
) -> JsonDict:
    """
    把成功返回的 subprocess 结果规整成步骤执行结果对象。

    参数：
    - dict_step: 当前执行的步骤对象。
    - completed_process: ``subprocess.run`` 成功返回的结果对象。
    - float_started_at: 当前步骤开始执行时的单调时钟时间戳。

    返回：
    - 返回包含步骤元数据、退出码、状态、耗时与标准输出/错误输出的结构化结果对象。

    异常：
    - 无显式异常；字段读取与字典展开沿用 Python 默认行为。
    """

    # 计算当前步骤实际耗时，供执行摘要和后续诊断统一复用。
    float_elapsed_sec = round(time.monotonic() - float_started_at, 3)  # 当前步骤从启动到结束的耗时秒数

    # 成功返回时仍需根据退出码区分 passed 与 failed，避免把非零退出码误判为通过。
    str_step_status = "passed" if completed_process.returncode == 0 else "failed"  # 当前步骤根据退出码判定出的状态

    # 返回统一结构的步骤执行结果，保持 execute_plan 的结果列表字段稳定。
    return {
        **dict_step,
        "returncode": completed_process.returncode,
        "status": str_step_status,
        "elapsed_sec": float_elapsed_sec,
        "stdout": completed_process.stdout,
        "stderr": completed_process.stderr,
    }

# 把超时异常规整成统一的步骤执行结果对象，避免 execute_plan 里散落超时细节处理。
def _timeout_stream_text(obj_stream: bytes | str | None, str_fallback: str = "") -> str:
    """
    把超时异常里捕获的 stdout/stderr 规整成字符串。

    参数：
    - obj_stream: ``TimeoutExpired`` 中捕获到的输出流对象；可能为 ``bytes``、``str`` 或 ``None``。
    - str_fallback: 输出流为空时使用的兜底文本。

    返回：
    - 返回规整后的字符串输出文本。

    异常：
    - 无显式异常；字节解码与字符串回退沿用 Python 默认行为。
    """

    # bytes 形态需要先按 UTF-8 解码，避免后续结果对象混入二进制内容。
    if isinstance(obj_stream, bytes):

        # 返回解码后的字符串，忽略不可解码字节，保证结果可直接序列化到 JSON。
        return obj_stream.decode("utf-8", errors="ignore")

    # 字符串或空值场景直接回退到稳定文本，避免调用方再区分多种输入类型。
    return obj_stream or str_fallback

# 把超时异常转换成稳定的步骤结果对象，重点补齐 timeout 语义和可序列化输出文本。
def _timeout_step_result(
    dict_step: JsonDict,
    exc_timeout: subprocess.TimeoutExpired,
    float_started_at: float,
    int_timeout: int,
) -> JsonDict:
    """
    把 ``subprocess.TimeoutExpired`` 规整成步骤执行结果对象。

    参数：
    - dict_step: 当前执行的步骤对象。
    - exc_timeout: subprocess 抛出的超时异常对象。
    - float_started_at: 当前步骤开始执行时的单调时钟时间戳。
    - int_timeout: 当前步骤使用的超时秒数。

    返回：
    - 返回 ``status=timeout`` 的结构化结果对象，并附带已捕获的 stdout/stderr 文本。

    异常：
    - 无显式异常；字符串回退与字典展开沿用 Python 默认行为。
    """

    # 计算当前步骤在超时前已消耗的时间，便于上游判断卡住阶段。
    float_elapsed_sec = round(time.monotonic() - float_started_at, 3)  # 当前超时步骤在中止前实际消耗的秒数

    # 超时异常里的 stdout 可能是 ``bytes`` 或 ``None``，这里统一规整成字符串。
    str_stdout = _timeout_stream_text(exc_timeout.stdout)  # 当前超时步骤捕获到的标准输出文本

    # 超时异常里的 stderr 同样可能为空或为 bytes，因此需要统一规整成字符串。
    str_stderr = _timeout_stream_text(exc_timeout.stderr, f"timeout after {int_timeout}s")  # 当前超时步骤捕获到的标准错误文本

    # 返回统一结构的超时步骤结果，避免调用方再区分异常对象字段形态。
    return {
        **dict_step,
        "returncode": None,
        "status": "timeout",
        "elapsed_sec": float_elapsed_sec,
        "stdout": str_stdout,
        "stderr": str_stderr,
    }

# 执行单个步骤命令并返回结构化结果，统一封装成功返回与超时异常两条路径。
def _run_step(dict_step: JsonDict, *, int_timeout: int) -> JsonDict:
    """
    执行单个步骤命令并返回结构化结果。

    参数：
    - dict_step: 当前需要执行的步骤对象，必须包含 ``cmd`` 与 ``cwd`` 字段。
    - int_timeout: 当前步骤允许执行的最大秒数。

    返回：
    - 返回包含退出码、状态、耗时、stdout 与 stderr 的结构化步骤结果对象。

    异常：
    - ``subprocess.TimeoutExpired`` 会在本函数内被规整为 ``status=timeout`` 的返回值，不再继续向外抛出。
    """

    # 在启动子进程前记录单调时钟起点，保证成功和超时两条路径共用同一套耗时口径。
    float_started_at = time.monotonic()  # 当前步骤命令真正启动前的单调时钟时间戳

    # 子进程正常返回时，统一把结果规整到步骤结果对象里。
    try:

        # 直接执行当前步骤命令并把完成结果交给统一规整 helper，避免长行局部变量反而伤害可读性。
        return _completed_step_result(
            dict_step,
            subprocess.run(
                dict_step["cmd"],
                cwd=dict_step["cwd"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=int_timeout,
            ),
            float_started_at,
        )

    # 命令超时是流程层面允许的诊断结果，因此在这里转成结构化 timeout 返回值。
    except subprocess.TimeoutExpired as exc_timeout:

        # 把超时异常规整成统一的步骤执行结果，保持上游状态字段口径一致。
        return _timeout_step_result(dict_step, exc_timeout, float_started_at, int_timeout)

# 构造当前任务共享的基础计划字段，供 blocked/dry-run/planned 三条路径共用。
def _base_plan(path_task_root: Path, str_task_name: str, list_diagnostics: list[str]) -> JsonDict:
    """
    构造当前任务共享的基础计划字段。

    参数：
    - path_task_root: 当前 AutoVeriFix 任务目录。
    - str_task_name: 当前任务名称。
    - list_diagnostics: 当前任务已收集到的缺失源码诊断列表。

    返回：
    - 返回任务名、任务根目录、顶层、源码列表、覆盖率配置与 guarded 依赖等公共字段。

    异常：
    - 无显式异常；字段构造沿用 Python 默认行为。
    """

    # 先推断顶层模块名，确保 base 计划和后续 verdi 默认信号路径始终使用同一口径。
    str_top_name = _infer_top(path_task_root)  # 当前任务自动推断得到的顶层模块名

    # 返回当前任务所有状态路径都会共享的基础计划字段。
    return {
        "task": str_task_name,
        "task_root": str(path_task_root),
        "frontend": "AutoVeriFix",
        "scope": "non-gui scripted VCS coverage loop",
        "top": str_top_name,
        "sources": _task_source_filenames(str_task_name),
        "coverage": {"metrics": COVERAGE_METRICS},
        "diagnostics": list_diagnostics,
        "guarded_external_dependencies": ["LLM/API repair loop"],
    }

# 构造 VCS 编译阶段命令，保持历史参数顺序与测试断言目标稳定一致。
def _build_compile_command(
    str_task_name: str,
    str_metrics_arg: str,
    dict_tool_overrides: dict[str, str] | None,
    list_compile_args: list[str] | None,
) -> list[str]:
    """
    构造 VCS 编译阶段命令。

    参数：
    - str_task_name: 当前任务名称，用于拼接 RTL 文件名。
    - str_metrics_arg: 覆盖率指标字符串，例如 ``line+cond+fsm``。
    - dict_tool_overrides: 调用方传入的工具覆盖映射。
    - list_compile_args: 调用方传入的额外编译参数列表。

    返回：
    - 返回可直接用于 VCS 编译阶段的命令参数列表。

    异常：
    - 无显式异常；静态列表构造沿用 Python 默认行为。
    """

    # 返回固定参数顺序的编译命令，确保现有测试与下游脚本能稳定比较列表内容。
    return [
        _tool(dict_tool_overrides, "vcs", "vcs"),
        *_stage_args(list_compile_args),
        "-full64",
        "-sverilog",
        "+v2k",
        "-timescale=1ns/1ns",
        "-debug_acc+all",
        "-debug_region+cell+encrypt",
        "-l",
        "compile.log",
        "-cm",
        str_metrics_arg,
        f"{str_task_name}.v",
        "testbench.v",
    ]

# 构造 simv 仿真阶段命令，保持覆盖率与日志输出参数顺序稳定。
def _build_simulate_command(
    str_task_name: str,
    str_metrics_arg: str,
    dict_tool_overrides: dict[str, str] | None,
    list_simulate_args: list[str] | None,
) -> list[str]:
    """
    构造 simv 仿真阶段命令。

    参数：
    - str_task_name: 当前任务名称，用于拼接仿真日志文件名。
    - str_metrics_arg: 覆盖率指标字符串，例如 ``line+cond+fsm``。
    - dict_tool_overrides: 调用方传入的工具覆盖映射。
    - list_simulate_args: 调用方传入的额外仿真参数列表。

    返回：
    - 返回可直接用于 simv 仿真阶段的命令参数列表。

    异常：
    - 无显式异常；静态列表构造沿用 Python 默认行为。
    """

    # 返回固定参数顺序的 simv 命令，确保覆盖率参数与日志文件名不会在不同调用路径中漂移。
    return [
        _tool(dict_tool_overrides, "simv", "./simv"),
        *_stage_args(list_simulate_args),
        "-l",
        f"{str_task_name}_sim.log",
        "-cm",
        str_metrics_arg,
    ]

# 构造 URG 覆盖率汇总阶段命令，保持报告目录与输出格式约定一致。
def _build_urg_command(dict_tool_overrides: dict[str, str] | None, list_urg_args: list[str] | None) -> list[str]:
    """
    构造 URG 覆盖率汇总阶段命令。

    参数：
    - dict_tool_overrides: 调用方传入的工具覆盖映射。
    - list_urg_args: 调用方传入的额外 URG 参数列表。

    返回：
    - 返回可直接用于 URG 覆盖率阶段的命令参数列表。

    异常：
    - 无显式异常；静态列表构造沿用 Python 默认行为。
    """

    # 返回固定参数顺序的 URG 命令，确保覆盖率数据库目录和报告目录口径稳定。
    return [
        _tool(dict_tool_overrides, "urg", "urg"),
        *_stage_args(list_urg_args),
        "-dir",
        "simv.vdb",
        "-report",
        "full_report",
        "-format",
        "both",
    ]

# 构造 fsdbreport 模式的非 GUI 信号读取检查步骤，保证计划层和执行层共用同一套元数据结构。
def _build_fsdbreport_step(obj_verdi_step_inputs: AutoVeriFixVerdiStepInputs) -> JsonDict:
    """
    构造 fsdbreport 模式的波形检查步骤。

    参数：
    - obj_verdi_step_inputs: 可选波形检查步骤共享的输入事实对象。

    返回：
    - 返回包含步骤名、命令、FSDB 与目标信号的结构化步骤对象。
    """

    # 未显式指定目标信号时，回退到顶层时钟路径，保持历史脚本默认检查习惯。
    str_default_report_signal = f"/{obj_verdi_step_inputs.str_top_name}/clk"  # 当前 fsdbreport 模式按顶层名推导的默认信号路径

    # 调用方未显式指定信号路径时，回退到默认顶层时钟路径，保持历史脚本默认检查习惯。
    str_effective_signal = obj_verdi_step_inputs.str_report_signal or str_default_report_signal  # 当前 fsdbreport 检查真正使用的信号路径

    # 先解析 fsdbreport 真实采用的工具入口，便于后面把命令列表拆成更短的可读片段。
    str_fsdbreport_tool = _tool(obj_verdi_step_inputs.dict_tool_overrides, "fsdbreport", "fsdbreport")  # 当前 fsdbreport 检查步骤采用的工具入口

    # 调用方透传的扩展参数需要保持顺序，因此先规整成独立列表再拼接进最终命令。
    list_verdi_stage_args = _stage_args(obj_verdi_step_inputs.list_verdi_args)  # 当前 fsdbreport 检查步骤附带的额外参数列表

    # FSDB 文件名在命令列表里会重复出现，因此先收成短变量可以明显改善单行可读性。
    str_fsdb_file = obj_verdi_step_inputs.str_effective_fsdb_name  # 当前 fsdbreport 检查步骤最终使用的 FSDB 文件名

    # 构造 fsdbreport 命令，顺序固定为工具入口、附加参数、FSDB 文件和目标信号。
    list_verdi_cmd = [str_fsdbreport_tool, *list_verdi_stage_args, str_fsdb_file, "-s", str_effective_signal]  # 当前 fsdbreport 检查步骤使用的完整命令参数列表

    # 返回供计划层和执行层共同复用的 fsdbreport 步骤对象。
    return {
        "name": "verdi-fsdbreport-check",
        "mode": "fsdbreport",
        "cwd": str(obj_verdi_step_inputs.path_task_root),
        "cmd": list_verdi_cmd,
        "fsdb": obj_verdi_step_inputs.str_effective_fsdb_name,
        "report_signal": str_effective_signal,
    }

# 构造 Verdi 装载检查步骤，专门承接 ``verdi -ssf`` 非交互验证场景。
def _build_verdi_load_step(obj_verdi_step_inputs: AutoVeriFixVerdiStepInputs) -> JsonDict:
    """
    构造 Verdi GUI 装载检查步骤。

    参数：
    - obj_verdi_step_inputs: 可选波形检查步骤共享的输入事实对象。

    返回：
    - 返回包含步骤名、命令、FSDB 与模式字段的结构化步骤对象。
    """

    # 先解析 Verdi 真实采用的工具入口，避免最终命令行在单行里混入太多职责。
    str_verdi_tool = _tool(obj_verdi_step_inputs.dict_tool_overrides, "verdi", "verdi")  # 当前 Verdi 装载检查步骤采用的工具入口

    # 把调用方透传的 Verdi 额外参数单独规整出来，便于后续保持它们的原始顺序。
    list_verdi_stage_args = _stage_args(obj_verdi_step_inputs.list_verdi_args)  # 当前 Verdi 装载检查步骤附带的额外参数列表

    # FSDB 文件名在 Verdi 装载命令里只是一个位置参数，单独收短变量更容易肉眼扫描。
    str_fsdb_file = obj_verdi_step_inputs.str_effective_fsdb_name  # 当前 Verdi 装载检查步骤最终使用的 FSDB 文件名

    # 构造 Verdi 非交互装载命令，重点确认 FSDB 文件会在启动后立刻被自动载入。
    list_verdi_cmd = [str_verdi_tool, *list_verdi_stage_args, "-ssf", str_fsdb_file, "-nologo", "-exit"]  # 当前 Verdi 装载检查步骤使用的完整命令参数列表

    # 返回专门用于 GUI 装载校验的步骤对象，避免和 fsdbreport 路径混淆。
    return {
        "name": "verdi-load-check",
        "mode": "verdi",
        "cwd": str(obj_verdi_step_inputs.path_task_root),
        "cmd": list_verdi_cmd,
        "fsdb": obj_verdi_step_inputs.str_effective_fsdb_name,
    }

# 构造可选的 fsdbreport 或 Verdi 波形检查步骤；未启用时返回 ``None``。
def _build_verdi_step(
    str_verdi_check: str,
    obj_verdi_step_inputs: AutoVeriFixVerdiStepInputs,
) -> JsonDict | None:
    """
    构造可选的波形检查步骤。

    参数：
    - str_verdi_check: 波形检查模式，允许 ``none``、``fsdbreport`` 或 ``verdi``。
    - obj_verdi_step_inputs: 可选波形检查步骤共享的输入事实对象。

    返回：
    - 若未启用波形检查则返回 ``None``；否则返回结构化步骤对象。

    异常：
    - 当 ``str_verdi_check`` 不是受支持模式时抛出 ``ValueError``。
    """

    # 先统一回退 FSDB 文件名，保证 fsdbreport 和 Verdi GUI 两条路径共享同一份默认值。
    str_effective_fsdb_name = obj_verdi_step_inputs.str_effective_fsdb_name or "waves.fsdb"  # 当前计划最终采用的 FSDB 文件名

    # 先复制当前波形检查输入事实，避免直接改写调用方传入的对象属性。
    dict_verdi_step_input_values = dict(obj_verdi_step_inputs.__dict__)  # 当前波形检查步骤共享输入事实的可写副本

    # 把规整后的 FSDB 文件名写回输入事实副本，保证后续两个具体 helper 读取到同一最终值。
    dict_verdi_step_input_values["str_effective_fsdb_name"] = str_effective_fsdb_name  # 当前输入事实副本里写回的最终 FSDB 文件名

    # 用更新后的字段副本重建输入对象，让后续 helper 始终看到同一份最终配置。
    auto_veri_fix_verdi_step_inputs_obj_resolved_verdi_inputs: AutoVeriFixVerdiStepInputs = (  # 已写回最终 FSDB 文件名的波形检查输入对象
        AutoVeriFixVerdiStepInputs(**dict_verdi_step_input_values)  # 承接已经写回最终 FSDB 文件名的字段副本
    )

    # 未启用波形检查时直接返回空，保持默认 AutoVeriFix 计划只包含 compile/simulate/coverage 三步。
    if str_verdi_check == "none":

        # 明确返回空值，告诉调用方当前计划不需要额外的波形装载步骤。
        return None

    # fsdbreport 路径只做非 GUI 信号读取验证，因此转交给专门 helper 生成步骤对象。
    if str_verdi_check == "fsdbreport":

        # 把 FSDB 报告校验逻辑收口到独立 helper，减少主分派函数的局部复杂度。
        return _build_fsdbreport_step(auto_veri_fix_verdi_step_inputs_obj_resolved_verdi_inputs)

    # Verdi 路径需要构造 ``verdi -ssf`` 非交互装载命令，因此也交给专门 helper 处理。
    if str_verdi_check == "verdi":

        # 专用 helper 会把 ``-ssf``、``-nologo`` 和 ``-exit`` 这些固定开关封装完整。
        return _build_verdi_load_step(auto_veri_fix_verdi_step_inputs_obj_resolved_verdi_inputs)

    # 对未知模式主动阻断，避免生成调用方无法理解的波形检查步骤。
    raise ValueError("> ERR: [Python] verdi_check must be one of: none, fsdbreport, verdi")

# 构造单个计划步骤对象，保证 ``steps`` 列表里的结构在所有阶段都保持一致。
def _plan_step(str_name: str, path_task_root: Path | str, list_cmd: list[str]) -> JsonDict:
    """
    构造单个计划步骤对象。

    参数：
    - str_name: 当前步骤名称。
    - path_task_root: 当前步骤需要执行的工作目录。
    - list_cmd: 当前步骤真正要执行的命令参数列表。

    返回：
    - 返回包含 ``name``、``cwd`` 与 ``cmd`` 的统一步骤对象。
    """

    # 把步骤级三元信息收口到统一对象结构，避免步骤列表在多个 helper 中各写一遍字段名。
    return {"name": str_name, "cwd": str(path_task_root), "cmd": list_cmd}

# 构造 compile/simulate/coverage 三个核心步骤对象列表，避免调用方重复书写同一组三步链路。
def _core_plan_steps(
    path_task_root: Path,
    list_compile_cmd: list[str],
    list_simulate_cmd: list[str],
    list_urg_cmd: list[str],
) -> list[JsonDict]:
    """
    构造计划固定包含的三个核心步骤对象列表。

    参数：
    - path_task_root: 当前 AutoVeriFix 任务目录。
    - list_compile_cmd: 编译阶段命令列表。
    - list_simulate_cmd: 仿真阶段命令列表。
    - list_urg_cmd: 覆盖率阶段命令列表。

    返回：
    - 返回按 compile、simulate、coverage 顺序排列的核心步骤对象列表。
    """

    # compile/simulate/coverage 三步在所有计划里都固定存在，因此由这个 helper 统一生成最稳妥。
    return [
        _plan_step("compile", path_task_root, list_compile_cmd),
        _plan_step("simulate", path_task_root, list_simulate_cmd),
        _plan_step("coverage", path_task_root, list_urg_cmd),
    ]

# 构造核心步骤列表，保证 compile/simulate/coverage 的顺序稳定且便于可选追加 Verdi 检查。
def _build_plan_steps(
    *,
    path_task_root: Path,
    list_compile_cmd: list[str],
    list_simulate_cmd: list[str],
    list_urg_cmd: list[str],
    dict_verdi_step: JsonDict | None,
) -> list[JsonDict]:
    """
    构造最终计划对象里的步骤列表。

    参数：
    - path_task_root: 当前 AutoVeriFix 任务目录。
    - list_compile_cmd: 编译阶段命令列表。
    - list_simulate_cmd: 仿真阶段命令列表。
    - list_urg_cmd: 覆盖率阶段命令列表。
    - dict_verdi_step: 可选的 Verdi/fsdbreport 步骤对象。

    返回：
    - 返回按执行顺序排列的步骤列表。
    """

    # 三个核心阶段在所有计划里都固定存在，因此直接复用统一 helper 生成基础步骤列表。
    list_steps = _core_plan_steps(path_task_root, list_compile_cmd, list_simulate_cmd, list_urg_cmd)  # 当前计划执行阶段的基础步骤列表

    # 启用波形检查时把对应步骤追加到末尾，形成完整的 compile->simulate->coverage->verdi 链路。
    if dict_verdi_step:

        # 只把步骤执行真正需要的字段附加到 ``steps``，避免步骤内部元数据重复暴露到列表层。
        list_steps.append(_plan_step(str(dict_verdi_step["name"]), str(dict_verdi_step["cwd"]), dict_verdi_step["cmd"]))

    # 返回顺序稳定的完整步骤列表，供最终计划对象直接复用。
    return list_steps

# 把 Verdi 或 fsdbreport 步骤里的对外元数据规整到计划级字段，避免最终计划对象里散落条件判断。
def _verdi_plan_metadata(dict_verdi_step: JsonDict) -> JsonDict:
    """
    规整计划层需要回写的 Verdi 元数据。

    参数：
    - dict_verdi_step: 可选 Verdi/fsdbreport 步骤对象。

    返回：
    - 返回计划层公开需要的 Verdi 元数据字典。
    """

    # 只有 fsdbreport 模式才会暴露报告信号路径，因此这里按需补写可选字段。
    if "report_signal" in dict_verdi_step:

        # fsdbreport 路径最关键的是目标信号，因此这里返回带 ``report_signal`` 的扩展元数据对象。
        return {
            "mode": dict_verdi_step["mode"],
            "cwd": dict_verdi_step["cwd"],
            "cmd": dict_verdi_step["cmd"],
            "fsdb": dict_verdi_step["fsdb"],
            "report_signal": dict_verdi_step["report_signal"],
        }

    # Verdi GUI 装载路径只需要共享基础元数据即可，不必附带 fsdbreport 专属字段。
    return {
        "mode": dict_verdi_step["mode"],
        "cwd": dict_verdi_step["cwd"],
        "cmd": dict_verdi_step["cmd"],
        "fsdb": dict_verdi_step["fsdb"],
    }

# 从最终计划输出输入对象中提取步骤列表，避免 `_build_plan_output` 再承受一段长参数调用。
def _plan_output_steps(obj_plan_output_inputs: AutoVeriFixPlanOutputInputs) -> list[JsonDict]:
    """
    构造最终计划对象需要回写的步骤列表。

    参数：
    - obj_plan_output_inputs: 最终计划组装阶段共享的输入事实对象。

    返回：
    - 返回当前计划需要公开的顺序化步骤列表。
    """

    # 这里只负责把具名输入对象重新投影成步骤列表，不额外承载其他计划级拼装职责。
    return _build_plan_steps(
        path_task_root=obj_plan_output_inputs.path_task_root,
        list_compile_cmd=obj_plan_output_inputs.list_compile_cmd,
        list_simulate_cmd=obj_plan_output_inputs.list_simulate_cmd,
        list_urg_cmd=obj_plan_output_inputs.list_urg_cmd,
        dict_verdi_step=obj_plan_output_inputs.dict_verdi_step,
    )

# 构造计划对象里的覆盖率元数据块，避免 `_build_plan_output` 再维护一份局部字典变量。
def _plan_output_coverage(list_urg_cmd: list[str]) -> JsonDict:
    """
    构造最终计划对象里的覆盖率元数据块。

    参数：
    - list_urg_cmd: 覆盖率汇总阶段的命令参数列表。

    返回：
    - 返回包含覆盖率维度、数据库目录、报告目录和 URG 命令的字典。
    """

    # 覆盖率元数据是一个稳定的小对象，因此直接在这里返回最能减少上层组装噪声。
    return {
        "metrics": COVERAGE_METRICS,
        "vdb_dir": "simv.vdb",
        "report_dir": "full_report",
        "urg_cmd": list_urg_cmd,
    }

# 把 build_plan 阶段需要共享的波形检查输入事实收口成具名对象，避免主流程继续维护多行对象构造细节。
def _plan_verdi_step_inputs(
    path_task_root: Path,
    str_fsdb_name: str,
    str_top_name: str,
    str_report_signal: str | None,
    dict_tool_overrides: dict[str, str] | None,
    list_verdi_args: list[str] | None,
) -> AutoVeriFixVerdiStepInputs:
    """
    构造可选波形检查步骤共享的输入事实对象。

    参数：
    - path_task_root: 当前 AutoVeriFix 任务目录。
    - str_fsdb_name: 当前计划显式指定的 FSDB 文件名。
    - str_top_name: 当前任务顶层模块名。
    - str_report_signal: fsdbreport 模式下调用方显式指定的目标信号路径。
    - dict_tool_overrides: 调用方传入的工具覆盖映射。
    - list_verdi_args: 调用方传入的额外 fsdbreport/Verdi 参数列表。

    返回：
    - 返回可供波形检查步骤 helper 共用的输入事实对象。
    """

    # 这组字段只服务于可选的 Verdi/fsdbreport 路径，因此由这个 helper 统一封装最能隔离职责。
    return AutoVeriFixVerdiStepInputs(
        path_task_root=path_task_root,
        str_effective_fsdb_name=str_fsdb_name,
        str_top_name=str_top_name,
        str_report_signal=str_report_signal,
        dict_tool_overrides=dict_tool_overrides,
        list_verdi_args=list_verdi_args,
    )

# 根据是否显式要求生成 FSDB 文件，构造执行完成后需要统一检查的关键产物名称列表。
def _expected_artifacts(str_fsdb_name: str) -> list[str]:
    """
    构造计划需要检查的关键产物列表。

    参数：
    - str_fsdb_name: 调用方显式指定的 FSDB 文件名；为空字符串时表示不额外追加。

    返回：
    - 返回执行完成后需要统一检查的关键产物名称列表。
    """

    # compile/simulate/coverage 三步始终共享这三类核心产物，因此默认先全部纳入检查集合。
    list_expected_artifacts = ["simv", "simv.vdb", "full_report"]  # 当前计划默认需要检查的关键产物名称列表

    # 显式指定 FSDB 文件名时，把它也纳入执行后产物检查集合。
    if str_fsdb_name:

        # 追加调用方显式要求生成的 FSDB 文件，便于 execute 结果统一校验。
        list_expected_artifacts.append(str_fsdb_name)

    # 返回顺序稳定的产物名称列表，方便 dry-run 与 execute 结果直接对比。
    return list_expected_artifacts

# 根据 CLI 解析结果构造轻量计划配置对象，避免 `main` 内部直接承受一段多行配置赋值。
def _cli_plan_config(args: argparse.Namespace, bool_dry_run: bool) -> AutoVeriFixPlanConfig:
    """
    根据 CLI 解析结果构造轻量计划配置对象。

    参数：
    - args: ``argparse`` 解析得到的命令行参数对象。
    - bool_dry_run: 当前 CLI 最终采用的 dry-run 语义。

    返回：
    - 返回与 ``build_plan`` 兼容的轻量配置对象。
    """

    # CLI 入口和可导入接口需要共用同一套计划构造路径，因此这里先把命令行字段统一封装。
    return AutoVeriFixPlanConfig(
        task_root=args.task_root,
        task=args.task,
        dry_run=bool_dry_run,
        fsdb=args.fsdb,
        verdi_check=args.verdi_check,
        report_signal=args.report_signal,
    )

# 把基础计划字段、阶段命令和可选波形检查步骤组装成最终计划对象。
def _build_plan_output(obj_plan_output_inputs: AutoVeriFixPlanOutputInputs) -> JsonDict:
    """
    组装最终的 AutoVeriFix 计划对象。

    参数：
    - obj_plan_output_inputs: 最终计划组装阶段共享的输入事实对象。

    返回：
    - 返回包含步骤、阶段命令、覆盖率与清理规则的完整计划对象。
    """

    # 先从基础计划字段复制一份独立字典，后续再按块追加状态、步骤与阶段命令。
    dict_plan_output = dict(obj_plan_output_inputs.dict_base_plan)  # 当前任务最终返回给调用方的完整计划对象

    # 状态和步骤是最外层协议字段，因此优先写入，方便 CLI 与测试快速判断计划类型。
    dict_plan_output.update(
        {
            "status": "dry-run" if obj_plan_output_inputs.bool_dry_run else "planned",
            "steps": _plan_output_steps(obj_plan_output_inputs),
        }
    )

    # 核心三阶段命令块统一用独立字段回写，便于 dry-run 输出与执行后诊断保持一一对应。
    dict_plan_output.update(
        {
            "compile": _plan_step(
                "compile",
                obj_plan_output_inputs.path_task_root,
                obj_plan_output_inputs.list_compile_cmd,
            ),
            "simulate": _plan_step(
                "simulate",
                obj_plan_output_inputs.path_task_root,
                obj_plan_output_inputs.list_simulate_cmd,
            ),
        }
    )

    # 覆盖率配置和产物清理规则属于计划级元数据，因此在此集中追加到最终对象。
    dict_plan_output.update(
        {
            "coverage": _plan_output_coverage(obj_plan_output_inputs.list_urg_cmd),
            "expected_artifacts": obj_plan_output_inputs.list_expected_artifacts,
            "cleanup": CLEANUP_PATTERNS,
        }
    )

    # 启用波形检查时，把计划级的 Verdi/fsdbreport 元数据同步写回最终计划对象。
    if obj_plan_output_inputs.dict_verdi_step:

        # 仅暴露计划层真正需要的 Verdi 元数据，避免把步骤内部细节泄露到外层接口。
        dict_plan_output["verdi"] = _verdi_plan_metadata(obj_plan_output_inputs.dict_verdi_step)  # 当前计划回写给调用方的波形检查元数据对象

    # 返回当前任务最终组装完成的结构化计划对象。
    return dict_plan_output

# 根据配置对象或兼容关键字参数构造 AutoVeriFix 计划对象。
def build_plan(*, config: AutoVeriFixPlanConfig | None = None, **kwargs: Any) -> JsonDict:
    """
    构造 AutoVeriFix 风格的非 GUI VCS 覆盖率计划。

    参数：
    - config: 可选的轻量配置对象；为空时从兼容关键字参数构造。
    - kwargs: 兼容旧接口的关键字参数映射；键名必须来自 ``tuple_plan_option_names``。

    返回：
    - 返回包含步骤、覆盖率配置、可选波形检查与清理规则的结构化计划对象。

    异常：
    - 当 ``kwargs`` 含有未知字段时抛出 ``TypeError``。
    - 当 ``task_root`` 缺失或 ``verdi_check`` 非法时抛出异常。
    """

    # 先把新配置对象入口和旧关键字参数入口合并成同一种命名空间表示。
    auto_veri_fix_plan_config_obj_plan_config = _coerce_plan_config(config=config, dict_kwargs=kwargs)  # 当前请求统一规整后的 AutoVeriFix 计划配置对象

    # ``task_root`` 是整个计划最基础的锚点；缺失时直接阻断比让 ``Path(None)`` 隐式报错更清晰。
    obj_task_root = _plan_option(auto_veri_fix_plan_config_obj_plan_config, "task_root")  # 当前请求传入的任务目录原始值

    # 任务目录缺失时继续构造计划没有意义，因此这里显式抛出结构化参数错误。
    if obj_task_root is None:

        # 用统一错误前缀提示调用方补齐 ``task_root``，方便测试和 CLI 按协议识别。
        raise TypeError("> ERR: [Python] build_plan requires task_root")

    # 先把任务根目录规整成绝对路径，避免计划对象在不同 cwd 下产生漂移。
    path_task_root = Path(obj_task_root).resolve()  # 当前任务目录的绝对路径

    # 未显式给出任务名时回退到目录名，保持历史 AutoVeriFix 调用方式兼容。
    str_task_name = str(_plan_option(auto_veri_fix_plan_config_obj_plan_config, "task") or path_task_root.name)  # 当前计划实际采用的任务名称

    # 先收集缺失源码诊断，确保 blocked 计划可以在不构造命令的情况下尽早返回。
    list_diagnostics = _source_diagnostics(path_task_root, str_task_name)  # 当前任务缺失源码后生成的诊断列表

    # 构造当前任务各个状态路径都会共享的基础计划字段。
    json_dict_dict_base_plan = _base_plan(path_task_root, str_task_name, list_diagnostics)  # 当前任务的基础公共计划字段

    # 缺少核心源码时直接返回 blocked 计划，避免继续拼接注定不可执行的命令。
    if list_diagnostics:

        # 在基础计划上补充 blocked 状态与原因，供调用方区分输入缺失和执行失败。
        return {**json_dict_dict_base_plan, "status": "blocked", "reason": "missing_sources"}

    # compile 与 simulate 两个阶段共享同一组覆盖率维度，因此先统一串成 ``-cm`` 需要的格式。
    str_metrics_arg = "+".join(COVERAGE_METRICS)  # 当前计划用于 ``-cm`` 参数的覆盖率指标字符串

    # 先读取 dry-run 开关，保证后续最终计划状态字段始终遵循统一语义。
    bool_dry_run = bool(_plan_option(auto_veri_fix_plan_config_obj_plan_config, "dry_run", True))  # 当前计划最终采用的 dry-run 语义

    # FSDB 文件名为空时仍要保留空字符串语义，避免误把未声明文件当成默认产物。
    str_fsdb_name = str(_plan_option(auto_veri_fix_plan_config_obj_plan_config, "fsdb") or "")  # 当前计划显式指定的 FSDB 文件名

    # 波形检查模式会驱动后续分支选择，因此先提取成独立字符串更利于阅读。
    str_verdi_check = str(_plan_option(auto_veri_fix_plan_config_obj_plan_config, "verdi_check", "none"))  # 当前请求要求的波形检查模式

    # fsdbreport 模式下的目标信号路径只在对应分支使用，这里先统一读取保持后续调用口径一致。
    str_report_signal = _plan_option(auto_veri_fix_plan_config_obj_plan_config, "report_signal")  # fsdbreport 模式下调用方显式指定的目标信号路径

    # 先读取后续所有阶段都会共享的工具覆盖映射，确保三类主命令和可选 Verdi 路径使用同一套入口。
    dict_tool_overrides = _plan_option(auto_veri_fix_plan_config_obj_plan_config, "tools")  # 当前计划使用的工具入口覆盖映射

    # 编译阶段经常需要追加脚本入口或 VCS 额外开关，因此先单独保留这组附参加以后续核对。
    list_compile_args = _plan_option(auto_veri_fix_plan_config_obj_plan_config, "compile_args")  # 当前计划 compile 阶段需要拼到 vcs 末尾的附加参数

    # 仿真阶段附加参数往往承载运行时脚本入口，单独抽离后更容易看清 simv 的真实载荷。
    list_simulate_args = _plan_option(auto_veri_fix_plan_config_obj_plan_config, "simulate_args")  # 当前计划 simulate 阶段额外透传的运行参数

    # 覆盖率汇总阶段只消费 URG 自身补充开关，因此把这组参数隔离出来最便于定位报告故障。
    list_urg_args = _plan_option(auto_veri_fix_plan_config_obj_plan_config, "urg_args")  # 当前计划 coverage 阶段补充给 urg 的参数

    # 波形检查附加参数只影响 fsdbreport/Verdi 尾部选项，提前收口后不会污染主三阶段命令逻辑。
    list_verdi_args = _plan_option(auto_veri_fix_plan_config_obj_plan_config, "verdi_args")  # 当前计划波形检查阶段使用的额外工具参数

    # 编译阶段需要把任务名、覆盖率维度与补充开关一起揉成完整的 vcs 命令序列。
    list_compile_cmd = _build_compile_command(str_task_name, str_metrics_arg, dict_tool_overrides, list_compile_args)  # 把 RTL 与 testbench 交给 vcs 编译的命令序列

    # 仿真阶段在 compile 之后独立启动 simv，因此这里先把运行阶段的完整命令稳定下来。
    list_simulate_cmd = _build_simulate_command(str_task_name, str_metrics_arg, dict_tool_overrides, list_simulate_args)  # 运行 simv 并生成日志与覆盖率数据库的命令序列

    # 覆盖率汇总只关心 vdb 目录和报告输出约定，单独构造 urg 命令更利于后续诊断。
    list_urg_cmd = _build_urg_command(dict_tool_overrides, list_urg_args)  # 读取 simv.vdb 并产出 full_report 的汇总命令序列

    # 关键产物集合和可选 Verdi 步骤都依赖前面解析好的任务级事实，因此在这里顺序收口最清晰。
    list_expected_artifacts = _expected_artifacts(str_fsdb_name)  # 当前计划执行完成后需要统一检查的关键产物列表

    # 顶层名会同时驱动默认报告信号和可选 Verdi 步骤，因此先单独收成稳定字符串变量。
    str_top_name = str(json_dict_dict_base_plan["top"])  # 当前计划最终采用的顶层模块名

    # 波形检查步骤共享输入事实先单独收口，避免具体 helper 持续传递过长的参数列表。
    auto_veri_fix_verdi_step_inputs_obj_verdi_step_inputs: AutoVeriFixVerdiStepInputs = _plan_verdi_step_inputs(  # 当前可选波形检查步骤共享的输入事实对象
        path_task_root=path_task_root,  # Verdi/fsdbreport 步骤共用的任务目录
        str_fsdb_name=str_fsdb_name,  # 波形文件最终采用的 FSDB 文件名
        str_top_name=str_top_name,  # 默认报告信号推导依赖的顶层模块名
        str_report_signal=str_report_signal,  # fsdbreport 模式显式指定的目标信号路径
        dict_tool_overrides=dict_tool_overrides,  # 波形检查沿用的工具入口覆盖映射
        list_verdi_args=list_verdi_args,  # 波形检查阶段附带的额外工具参数
    )

    # 波形检查步骤需要同时读取模式与这组共享输入事实，因此在这里集中构造最稳定。
    json_dict_dict_verdi_step_payload: JsonDict | None = _build_verdi_step(  # 当前计划可选的波形检查步骤对象
        str_verdi_check=str_verdi_check,  # 调用方要求采用的波形检查模式
        obj_verdi_step_inputs=auto_veri_fix_verdi_step_inputs_obj_verdi_step_inputs,  # 波形检查 helper 共享的输入事实对象
    )

    # 最终计划对象组装只依赖这一组稳定事实，因此在返回前先收口成具名输入对象最便于复用。
    auto_veri_fix_plan_output_inputs_obj_plan_output_inputs: AutoVeriFixPlanOutputInputs = (  # 当前最终计划组装阶段共享的输入事实对象
        AutoVeriFixPlanOutputInputs(  # 承接 build_plan 末尾统一交给输出组装阶段的全部共享事实
            dict_base_plan=json_dict_dict_base_plan,  # 已确认源码与顶层名的基础计划字段
            path_task_root=path_task_root,  # 所有阶段统一使用的绝对任务目录
            bool_dry_run=bool_dry_run,  # 决定计划最终状态字段的 dry-run 开关

            # 三个固定阶段各自对应的命令序列在此集中封装，保证 dry-run 与 execute 共享同一份事实。
            list_compile_cmd=list_compile_cmd,  # 负责产出 simv 可执行文件的编译命令序列
            list_simulate_cmd=list_simulate_cmd,  # 真正运行测试并落地日志与 vdb 的仿真命令序列
            list_urg_cmd=list_urg_cmd,  # 汇总 simv.vdb 并生成覆盖率报告的收尾命令序列

            # 执行后校验需要的产物与可选波形步骤也一并封装，避免计划输出阶段再回头取散落变量。
            list_expected_artifacts=list_expected_artifacts,  # execute 结束后统一核查的产物名称列表
            dict_verdi_step=json_dict_dict_verdi_step_payload,  # 计划层需要公开的可选波形检查步骤对象
        )
    )

    # 返回按统一结构组装好的完整计划对象，供 dry-run 与 execute 两条路径共同消费。
    return _build_plan_output(auto_veri_fix_plan_output_inputs_obj_plan_output_inputs)

# 读取当前仿真日志并返回结构化摘要，避免 execute_plan 同时维护日志存在性分支和结果字典拼装。
def _simulation_summary(path_sim_log: Path) -> JsonDict:
    """
    读取当前仿真日志并返回结构化摘要。

    参数：
    - path_sim_log: 当前任务预期生成的仿真日志路径。

    返回：
    - 返回 ``parse_simulation_log`` 结果或日志缺失时的稳定 ``unknown`` 摘要对象。
    """

    # 仿真日志存在时直接解析 scoreboard 摘要，让 execute_plan 专注于总体状态汇总。
    if path_sim_log.exists():

        # 读取日志文本并解析 mismatch 摘要，供最终总体状态判断使用。
        return parse_simulation_log(path_sim_log.read_text(encoding="utf-8", errors="ignore"))

    # 日志缺失时仍返回稳定对象，帮助调用方区分“命令失败”与“日志根本没生成”。
    return {
        "status": "unknown",
        "reason": "simulation_log_missing",
        "path": str(path_sim_log),
    }

# 执行 AutoVeriFix 计划中的各个步骤，并回写仿真摘要与关键产物状态。
def execute_plan(dict_plan: JsonDict, *, timeout: int = 300) -> JsonDict:
    """
    执行 AutoVeriFix 计划中的各个步骤，并返回结构化执行结果。

    参数：
    - dict_plan: 由 ``build_plan`` 生成的结构化计划对象。
    - timeout: 每个步骤允许执行的最大秒数。

    返回：
    - 返回在原计划基础上补充 ``results``、``simulation``、``artifacts`` 与最终 ``status`` 的执行结果对象。

    异常：
    - 无显式异常；步骤命令内部异常会被 ``_run_step`` 转成结构化结果对象。
    """

    # 使用独立列表累计步骤执行结果，便于保留每一步的 stdout/stderr 与状态。
    list_results: list[JsonDict] = []  # 当前计划已经执行完成的步骤结果列表

    # 按计划顺序逐步执行命令，只要某一步未通过就立即停止后续阶段。
    for dict_step in dict_plan.get("steps", []):

        # 执行当前步骤命令，并把结果规整成统一结构供后续状态判定使用。
        json_dict_dict_result = _run_step(dict_step, int_timeout=timeout)  # 当前步骤执行完成后的结构化结果对象

        # 先记录当前步骤结果，保证失败步骤的 stdout/stderr 不会丢失。
        list_results.append(json_dict_dict_result)

        # 只要当前步骤不是 passed，就立即终止后续执行，保持失败即停的历史行为。
        if json_dict_dict_result["status"] != "passed":

            # 当前步骤已经失败或超时，后续阶段继续执行会污染问题定位，因此立即退出循环。
            break

    # 从计划对象中恢复任务目录，供仿真日志与产物状态检查复用。
    path_task_root = Path(dict_plan["task_root"])  # 当前计划对应的任务目录绝对路径

    # 按任务名定位当前仿真日志文件，保持和 build_plan 中 ``simulate`` 日志命名规则一致。
    path_sim_log = path_task_root / f"{dict_plan['task']}_sim.log"  # 当前任务预期生成的仿真日志路径

    # 仿真日志摘要统一交给专门 helper 处理，让当前函数聚焦于总体状态与返回对象拼装。
    json_dict_dict_simulation_summary = _simulation_summary(path_sim_log)  # 当前任务仿真日志对应的结构化摘要对象

    # 按计划声明的关键产物清单回写存在性与大小，便于执行后做统一验收。
    dict_artifacts = _artifact_status(path_task_root, dict_plan.get("expected_artifacts", []))  # 当前任务关键产物的状态映射

    # 先读取计划声明的步骤总数，后续命令链路验收需要同时比对数量和每步状态。
    int_expected_step_count = len(dict_plan.get("steps", []))  # 当前计划理论上应该执行完成的步骤数量

    # 已完成步骤数量需要和计划步数完全一致，否则说明流程被中途失败或超时提前截断。
    int_completed_step_count = len(list_results)  # 当前执行结果里实际收集到的步骤数量

    # 只有所有已收集步骤都是 passed 时，命令链路才满足“逐步通过”的必要条件。
    bool_all_step_statuses_passed = all(json_dict_result["status"] == "passed" for json_dict_result in list_results)  # 当前执行结果里的所有步骤状态是否都为 passed

    # 只有实际完成数量与计划步数一致时，命令链路才满足“完整执行”的数量约束。
    bool_step_counts_match = int_completed_step_count == int_expected_step_count  # 当前执行结果里的步骤数量是否与计划完全一致

    # 只有步骤结果完整且全部 passed 时，命令链路才视为真正通过。
    bool_all_steps_passed = bool(list_results) and bool_step_counts_match and bool_all_step_statuses_passed  # 当前任务的命令执行链路是否完整通过

    # 命令链路状态先单独写成文本字段，便于后续总体状态判断和结果对象复用同一口径。
    str_command_status = "passed" if bool_all_steps_passed else "failed"  # 当前任务从命令执行链路角度判定出的总体状态

    # 只有命令链路通过且仿真日志也显示通过时，整个 AutoVeriFix 流程才视为 passed。
    bool_flow_passed = str_command_status == "passed" and json_dict_dict_simulation_summary["status"] == "passed"  # 当前任务是否满足命令链路与仿真摘要同时通过

    # 整体对外状态只区分 passed/failed 两类，把 blocked 与 unknown 细节保留在内层字段即可。
    str_status = "passed" if bool_flow_passed else "failed"  # 当前任务最终对外暴露的总体状态

    # 返回在原计划基础上补充执行结果、仿真摘要和产物状态后的完整对象。
    return {
        **dict_plan,
        "status": str_status,
        "results": list_results,
        "simulation": json_dict_dict_simulation_summary,
        "artifacts": dict_artifacts,
    }

# 输出默认 CLI 模式下的人类可读摘要，避免直接把结构化内容打印到终端。
def _emit_human_summary(dict_output: JsonDict, bool_execute_requested: bool) -> None:
    """
    输出默认 CLI 模式下的人类可读摘要。

    参数：
    - dict_output: 当前 CLI 最终得到的计划或执行结果对象。
    - bool_execute_requested: 当前 CLI 是否显式请求执行计划。

    返回：
    - 无返回值；函数会直接向标准输出打印带前缀的摘要文本。

    异常：
    - 无显式异常；终端输出沿用 Python 默认行为。
    """

    # 缺失源码导致 blocked 时，输出 warning 摘要提示调用方改用 JSON 查看完整诊断。
    if dict_output.get("status") == "blocked":

        # 输出稳定的 blocked 摘要，避免把详细结构化计划直接打印到终端。
        print("> WARNING: [Python] plan blocked by missing sources; rerun with --json for structured details")

    # execute 模式失败时，输出 ERR 摘要提示调用方改用 JSON 查看步骤细节。
    elif bool_execute_requested and dict_output.get("status") != "passed":

        # 输出稳定的失败摘要，帮助调用方快速区分 execute 失败与 dry-run 规划成功。
        print("> ERR: [Python] AutoVeriFix flow failed; rerun with --json for structured details")

    # execute 模式通过时，输出简短成功摘要，确认全链路已经执行完成。
    elif bool_execute_requested:

        # 输出成功摘要，表明 compile、simulate、coverage 与可选波形检查都已通过。
        print("> INFO: [Python] AutoVeriFix flow executed successfully")

    # 默认 dry-run 路径只报告计划已经生成，详细结构仍由 ``--json`` 提供。
    else:

        # 输出稳定的 dry-run 摘要，提醒调用方改用 JSON 查看完整结构化计划。
        print("> INFO: [Python] dry-run plan generated; rerun with --json for structured details")

# 解析命令行参数，构造或执行 AutoVeriFix 流程，并按请求输出 JSON 或人类可读摘要。
def main(argv: list[str] | None = None) -> int:
    """
    运行 AutoVeriFix 流程脚本的命令行入口。

    参数：
    - argv: 可选命令行参数列表；为空时默认从 ``sys.argv`` 读取。

    返回：
    - dry-run 或执行成功时返回 ``0``；blocked 或执行失败时返回 ``1``。

    异常：
    - 参数解析失败时由 ``argparse`` 直接抛出并终止进程；其余异常沿用下层 helper 的默认行为。
    """

    # 创建命令行参数解析器，统一声明 AutoVeriFix 非 GUI 流程支持的参数。
    parser = argparse.ArgumentParser(description="Plan an AutoVeriFix-style non-GUI VCS coverage loop.")  # 当前 CLI 的参数解析器

    # 注册任务根目录参数，要求调用方显式给出待处理的 AutoVeriFix 任务目录。
    parser.add_argument("--task-root", type=Path, required=True)

    # 注册可选任务名参数，允许调用方覆盖默认的目录名推断。
    parser.add_argument("--task")

    # 注册 dry-run 标记，保留历史 CLI 的显式 dry-run 用法。
    parser.add_argument("--dry-run", action="store_true")

    # 注册 execute 标记，启用后真正执行 compile/simulate/coverage 命令。
    parser.add_argument("--execute", action="store_true")

    # 注册每个步骤的统一超时秒数，供执行模式下所有步骤共享。
    parser.add_argument("--timeout", type=int, default=300)

    # 注册可选 FSDB 文件名参数，供 fsdbreport 或 Verdi 波形检查使用。
    parser.add_argument("--fsdb")

    # 注册波形检查模式参数，限制在三种受支持模式内。
    parser.add_argument("--verdi-check", choices=["none", "fsdbreport", "verdi"], default="none")

    # 注册 fsdbreport 检查时的目标信号路径参数。
    parser.add_argument("--report-signal")

    # 注册 JSON 输出开关，启用后按模块文档约定输出单个 JSON 对象。
    parser.add_argument("--json", action="store_true")

    # 解析命令行参数，得到本次 AutoVeriFix 请求的目录、模式与输出通道。
    args = parser.parse_args(argv)  # 当前 CLI 解析得到的参数对象

    # 只有显式请求 execute 时才真正执行命令，其余情况都保持 dry-run 语义。
    bool_dry_run = not args.execute  # 当前 CLI 最终采用的 dry-run 语义

    # 先把 CLI 输入封装成轻量配置对象，保证命令行入口和可导入接口共用同一套计划构造路径。
    auto_veri_fix_plan_config_obj_plan_config = _cli_plan_config(args, bool_dry_run)  # 当前 CLI 请求对应的轻量计划配置对象

    # 先构造结构化计划对象，供 dry-run 与 execute 两条路径共享同一套前置解析结果。
    json_dict_dict_plan_payload = build_plan(config=auto_veri_fix_plan_config_obj_plan_config)  # 当前 CLI 根据请求生成的结构化计划对象

    # 先声明最终输出对象的静态类型，避免分支赋值让质量门禁无法确认类型边界。
    json_dict_dict_output_payload: JsonDict  # 当前 CLI 在各分支最终都会回写到的结构化输出对象

    # 只有 execute 模式且计划未 blocked 时才真正执行步骤，否则直接返回计划对象本身。
    if args.execute and json_dict_dict_plan_payload.get("status") != "blocked":

        # execute 模式下真正运行结构化计划，并回写所有步骤、产物与仿真摘要。
        json_dict_dict_output_payload = execute_plan(json_dict_dict_plan_payload, timeout=args.timeout)  # 当前 CLI 最终输出的执行结果对象

    # dry-run 或 blocked 计划直接复用结构化计划对象本身，不额外触发子进程执行。
    else:

        # 当前请求不需要真正执行命令，因此直接把计划对象作为最终输出。
        json_dict_dict_output_payload = json_dict_dict_plan_payload  # 当前 CLI 最终输出的 dry-run 或 blocked 计划对象

    # 调用方显式请求 JSON 时，输出单个 JSON 对象供自动化直接消费。
    if args.json:

        # 按模块文档约定把单个 JSON 对象写到标准输出，避免混入额外人类可读文本。
        json.dump(json_dict_dict_output_payload, sys.stdout, indent=2, sort_keys=True)

        # 为 JSON 输出补一个换行，避免 shell 提示符直接粘在 JSON 末尾。
        sys.stdout.write("\n")

    # 默认模式下只输出带前缀的摘要文本，避免结构化计划污染终端日志。
    else:

        # 输出当前结果对应的人类可读摘要，详细结构留给 ``--json`` 协议承载。
        _emit_human_summary(json_dict_dict_output_payload, args.execute)

    # dry-run 与执行通过都视为成功退出；blocked 或执行失败则返回非零退出码。
    return 0 if json_dict_dict_output_payload.get("status") in {"dry-run", "passed"} else 1

# 只有以脚本方式直接执行时才触发 CLI，避免导入测试模块时立即退出当前 Python 进程。
if __name__ == "__main__":

    # 把 main 返回值转换为进程退出码，供 shell、CI 与远端 smoke 流程直接判定成败。
    raise SystemExit(main())
