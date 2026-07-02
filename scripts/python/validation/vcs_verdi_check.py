"""构建并执行最小化的 VCS/Verdi 冒烟计划。

stdout_protocol: json
本模块的 CLI 在 ``--json`` 模式下输出 machine-readable stdout protocol，
供测试、技能工作流和上游脚本直接读取完整 JSON 结果。
"""
from __future__ import annotations

# 引入命令行、JSON、环境变量、可执行文件探测与子进程接口，支撑本脚本的计划构建与执行流程
import argparse
import json
import os
import shutil
import subprocess
import sys

# 补充可调用、路径、轻量配置对象与通用类型标注，供计划构建 helper 共享。
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# 把动态 JSON 风格对象统一抽象成别名，便于后续说明计划、清单和状态字典的结构
JsonDict = dict[str, Any]  # 运行计划、诊断结果和执行摘要使用的通用对象结构

# 用轻量命名空间承接冒烟计划配置，避免在公共入口上继续堆叠几十个显式参数。
class SmokePlanConfig(SimpleNamespace):
    """
    承接 ``build_smoke_plan`` 输入键值的轻量配置对象。

    参数：
    - 构造时可直接传入与 ``build_smoke_plan`` 兼容的关键字，例如 ``sources``、``workdir``、``top``、``coverage`` 和 ``verdi_check``。

    返回：
    - 当前类实例会把这些字段保存为属性，供 ``build_smoke_plan`` 及其内部 helper 统一读取。
    """

# 用具名命名空间承接计划构建阶段共享的基础事实，避免 helper 之间继续传递匿名字典。
class SmokePlanInputs(SimpleNamespace):
    """
    承接 ``build_smoke_plan`` 内部复用的基础事实。

    参数：
    - 构造时传入工作目录、日志目录、源码参数、覆盖率参数、工具映射、PLI 产物等字段。

    返回：
    - 当前类实例会把这些字段保存为属性，供命令构造、步骤组装和最终计划回写统一读取。
    """

# 兼容旧调用方式时，只允许这些字段经由 ``**kwargs`` 写入配置对象，避免拼写错误被静默吞掉。
tuple_smoke_plan_option_names = (  # ``build_smoke_plan`` 允许接收的显式配置键集合
    "source",  # 单个 Verilog/SystemVerilog 源文件入口
    "sources",  # 多个 Verilog/SystemVerilog 源文件入口
    "source_list",  # 单个 filelist 入口
    "source_lists",  # 多个 filelist 入口
    "vhdl_sources",  # VHDL 源文件入口列表
    "include_dirs",  # 编译阶段头文件搜索目录列表
    "defines",  # Verilog/SystemVerilog 宏定义映射
    "libraries",  # 逻辑工作库名称列表
    "timescale",  # 编译时标约束
    "debug",  # ``-debug_access+`` 调试可见性等级
    "kdb",  # 是否启用 ``-kdb`` 调试数据库
    "coverage",  # 覆盖率维度列表
    "sv_libs",  # DPI 共享库路径列表
    "plusargs",  # simulate 阶段 plusargs 列表
    "seed",  # 仿真随机种子
    "workdir",  # 冒烟工程工作目录
    "top",  # 顶层模块名
    "dump_name",  # FSDB 转储文件名
    "tools",  # 工具路径覆盖映射
    "env",  # 执行阶段环境变量覆盖映射
    "vhdlan_args",  # VHDL 分析阶段额外参数
    "vlogan_args",  # Verilog 编译阶段额外参数
    "vcs_args",  # elaboration 阶段额外参数
    "simv_args",  # 直接追加给 simv 可执行文件本体的命令尾参数
    "fsdbreport_args",  # fsdbreport 校验阶段额外参数
    "verdi_args",  # 交给 Verdi GUI 装载阶段的补充开关列表
    "expected_artifacts",  # 额外产物校验规则映射
    "step_timeout",  # 单阶段超时秒数
    "rc_file",  # Verdi RC 文件路径
    "cmd_file",  # simv UCLI 命令文件路径
    "pli_dir",  # 显式指定的 Novas PLI 目录
    "auto_pli",  # 是否允许从环境自动探测 PLI
    "verdi_check",  # 波形加载检查模式
    "report_signal",  # fsdbreport 模式下的目标信号路径
    "clean",  # 执行前是否清理工作目录
    "log_dir",  # 自定义日志目录
)

# 为未知字段校验准备集合视图，避免每次兼容旧接口时都重复临时构造 ``set(...)``。
set_smoke_plan_option_names = set(tuple_smoke_plan_option_names)  # ``build_smoke_plan`` 允许接收的字段名集合

# 统一读取配置对象里的某个字段，让 helper 们不必重复书写 ``getattr(..., default)``。
def _plan_option(obj_plan_config: SmokePlanConfig, str_name: str, obj_default: Any = None) -> Any:
    """
    读取冒烟计划配置对象中的某个字段。

    :param obj_plan_config: 已规整的冒烟计划配置对象，dtype=SmokePlanConfig，unit=object
    :param str_name: 需要读取的字段名，dtype=str，unit=identifier
    :param obj_default: 字段缺失时的兜底值，dtype=Any，unit=object
    :return: 返回配置对象中的实际值或兜底值，dtype=Any，unit=object
    """

    # 所有 helper 都通过同一个读取入口获取字段，保证缺省值语义在整个模块内保持一致。
    return getattr(obj_plan_config, str_name, obj_default)

# 把配置对象和兼容 ``**kwargs`` 合并成统一命名空间，避免旧接口和新接口分叉出两套逻辑。
def _coerce_smoke_plan_config(
    *,
    config: SmokePlanConfig | None = None,
    dict_kwargs: dict[str, Any] | None = None,
) -> SmokePlanConfig:
    """
    合并显式配置对象与兼容关键字参数。

    :param config: 可选的冒烟计划配置对象；为空时仅使用兼容关键字参数构造，dtype=SmokePlanConfig | None，unit=object
    :param dict_kwargs: 兼容旧接口的关键字参数映射；为空时只读取 ``config``，dtype=dict[str, Any] | None，unit=mapping
    :return: 返回统一规整后的冒烟计划配置对象，dtype=SmokePlanConfig，unit=object
    :raises TypeError: 当兼容关键字参数包含未支持字段时抛出异常。
    """

    # 先把兼容关键字参数拷成独立字典，避免后续校验与覆盖流程回写调用方对象。
    dict_request_kwargs = dict(dict_kwargs or {})  # 兼容旧 ``build_smoke_plan(...)`` 调用方式的关键字参数副本

    # 只要出现未知字段就立刻阻断，保持旧显式参数接口对拼写错误的敏感性。
    set_unknown_option_names = set(dict_request_kwargs) - set_smoke_plan_option_names  # 兼容关键字参数里未被支持的字段名集合

    # 未知字段继续放行会把拼写错误静默吞掉，因此这里主动抛出结构化异常。
    if set_unknown_option_names:

        # 把未知字段名按字典序拼起来，便于调用方一次性定位全部错误键名。
        str_unknown_option_names = ", ".join(sorted(set_unknown_option_names))  # 当前请求里出现的未知字段名摘要文本

        # 用统一错误前缀报告未知字段，方便 CLI 与测试按固定协议识别失败来源。
        raise TypeError(f"> ERR: [Python] build_smoke_plan got unexpected options: {str_unknown_option_names}")

    # 显式配置对象存在时，先复制其属性字典，后续再叠加兼容关键字参数覆盖。
    dict_config_values = vars(config).copy() if config else {}  # 当前计划配置对象的属性字典副本

    # 让兼容关键字参数覆盖配置对象中的同名字段，保留“显式传入优先”的旧接口语义。
    dict_config_values.update(dict_request_kwargs)

    # 用合并后的键值重建命名空间，让后续 helper 始终面对统一的数据入口。
    return SmokePlanConfig(**dict_config_values)

# 统一解析可空路径入参，避免 CLI 和 manifest 分支重复做 Path 转换
def _as_path(obj_value: Path | str | None) -> Path | None:
    """
    把可空路径值规整成 Path 对象。

    :param obj_value: 调用方传入的路径参数，允许 Path、str 或 None，dtype=Path | str | None，unit=path
    :return: 返回规整后的 Path；未提供路径时返回 None，dtype=Path | None，unit=path
    """

    # 没有提供实参时直接保留空值，让上层按照默认分支继续决策
    if obj_value is None:

        # 当前参数没有真实路径，因此不生成 Path 实例
        return None

    # 把字符串和现成的 Path 统一封装成 Path，减少后续调用方的类型分支
    return Path(obj_value)

# 汇总 SystemVerilog/Verilog 源文件和 filelist 参数，生成供 vlogan 使用的稳定实参序列
def normalize_sources(
    *,
    source: Path | None = None,
    sources: list[Path] | None = None,
    source_list: Path | None = None,
    source_lists: list[Path] | None = None,
) -> list[str]:
    """
    规范化源码路径与 filelist 选项。

    :param source: 单个源码路径，主要兼容 CLI 单文件入口，dtype=Path | None，unit=path
    :param sources: 多个显式源码路径，按原始声明顺序加入结果，dtype=list[Path] | None，unit=collection
    :param source_list: 单个 filelist 路径，展开为 ``-f <path>``，dtype=Path | None，unit=path
    :param source_lists: 多个 filelist 路径，保持声明顺序逐个展开，dtype=list[Path] | None，unit=collection
    :return: 返回可直接拼入 vlogan 的参数列表，dtype=list[str]，unit=argv
    :raises ValueError: 当所有源码入口都为空时抛出异常。
    """

    # 先写入显式源码路径，保证单文件与多文件输入都能落到统一的参数容器
    list_normalized = [str(Path(path_source).resolve()) for path_source in (sources or [])]  # 已规整的显式源码参数列表

    # 兼容只传入单个源码路径的调用方式，避免 CLI 单文件模式丢失输入
    if source:

        # 把单文件输入追加到显式源码列表末尾，保持与历史行为一致
        list_normalized.append(str(source.resolve()))

    # 补入单个 filelist，保留 vlogan 所需的 ``-f`` 标记和绝对路径
    if source_list:

        # 单个 filelist 与源码参数混排时继续复用统一的字符串列表
        list_normalized.extend(["-f", str(source_list.resolve())])

    # 逐个展开多个 filelist，确保 manifest 中的多 filelist 顺序不会被破坏
    for path_source_list in source_lists or []:

        # 每个 filelist 都以独立的 ``-f`` 对出现，匹配 vlogan 期望的参数形态
        list_normalized.extend(["-f", str(Path(path_source_list).resolve())])

    # 没有任何源码入口时尽早失败，避免生成缺少输入的无效命令计划
    if not list_normalized:

        # 统一使用受管错误前缀，便于上层日志和人工排查按规则识别异常来源
        raise ValueError("> ERR: [Python] at least one --source or --source-list is required")

    # 返回已规整的源码参数序列，供 vlogan 命令构建直接复用
    return list_normalized

# 把一组 Path 统一转成绝对路径字符串，减少重复的 resolve 拼接逻辑
def _resolve_many(list_values: list[Path] | None) -> list[str]:
    """
    将可空路径列表转成绝对路径字符串列表。

    :param list_values: 需要规整的路径列表，允许为空，dtype=list[Path] | None，unit=collection
    :return: 返回解析为绝对路径后的字符串列表，dtype=list[str]，unit=argv
    """

    # 按原顺序解析每个路径，保证上游声明的库和源文件顺序可追踪
    return [str(Path(path_item).resolve()) for path_item in (list_values or [])]

# 把 ``NAME`` 或 ``NAME=VALUE`` 形式的宏定义字典转成 VCS 接受的 ``+define+`` 参数
def _define_args(dict_defines: dict[str, str] | None) -> list[str]:
    """
    把 define 字典转换成编译器参数列表。

    :param dict_defines: 键为宏名、值为宏值的 define 映射；空字符串表示无值宏，dtype=dict[str, str] | None，unit=mapping
    :return: 返回 ``+define+`` 风格的命令行参数列表，dtype=list[str]，unit=argv
    """

    # 使用独立列表保存转换结果，便于按声明顺序稳定输出 define 参数
    list_args: list[str] = []  # define 字典转换后的命令行参数列表

    # 逐项保留宏名和值，确保 manifest 声明顺序在最终命令里可复核
    for str_key, str_value in (dict_defines or {}).items():

        # 空值宏只保留宏名，匹配 ``+define+NAME`` 的 Synopsys 语法
        if str_value == "":

            # 当前 define 没有显式值，因此只输出宏名部分
            list_args.append(f"+define+{str_key}")

        # 非空宏值按 ``NAME=VALUE`` 形式输出，便于工具直接解析
        else:

            # 把宏值一起写入 define 参数，避免调用方额外再拼接一层字符串
            list_args.append(f"+define+{str_key}={str_value}")

    # 返回 define 参数列表，供编译命令拼接时复用
    return list_args

# 把包含目录列表转换成 ``+incdir+`` 语法，保持所有路径都是绝对地址
def _include_args(list_include_dirs: list[Path] | None) -> list[str]:
    """
    将包含目录列表转成 ``+incdir+`` 参数。

    :param list_include_dirs: 需要加入编译搜索路径的目录集合，dtype=list[Path] | None，unit=collection
    :return: 返回格式化后的 ``+incdir+`` 参数列表，dtype=list[str]，unit=argv
    """

    # 直接把每个目录规整成绝对路径，避免相对路径在远程或临时目录下漂移
    return [f"+incdir+{Path(path_include_dir).resolve()}" for path_include_dir in (list_include_dirs or [])]

# 根据覆盖率开关和数据库目录生成 compile/elaborate/simulate 三阶段共用的覆盖率参数
def _coverage_arg(list_coverage: list[str] | None, path_coverage_db: Path | None = None) -> list[str]:
    """
    构建覆盖率命令行参数。

    :param list_coverage: 需要启用的覆盖率维度列表，例如 ``line``、``cond``，dtype=list[str] | None，unit=collection
    :param path_coverage_db: 覆盖率数据库目录；为空时只输出 ``-cm`` 参数，dtype=Path | None，unit=path
    :return: 返回覆盖率相关命令行参数列表，dtype=list[str]，unit=argv
    """

    # 没有声明覆盖率项时直接返回空列表，避免无意义地插入 ``-cm`` 开关
    if not list_coverage:

        # 当前计划未启用覆盖率，因此不生成任何覆盖率参数
        return []

    # 先生成覆盖率维度本身，后续再根据数据库目录决定是否补充 ``-cm_dir``
    list_args = ["-cm", "+".join(list_coverage)]  # 覆盖率维度对应的基础命令行参数

    # 只有显式提供数据库目录时才补 ``-cm_dir``，保持与现有测试契约一致
    if path_coverage_db is not None:

        # 把稳定的覆盖率数据库目录写入参数，便于 compile/elaborate/simulate 三阶段共享
        list_args.extend(["-cm_dir", str(path_coverage_db)])

    # 返回覆盖率参数列表，供多个阶段命令复用
    return list_args

# 从库列表里恢复 ``-work`` 目标库，保持历史上只取第一个库名的行为
def _work_args(list_libraries: list[str] | None) -> list[str]:
    """
    生成 ``-work`` 目标库参数。

    :param list_libraries: 工程声明的逻辑库列表，仅首个元素参与 ``-work`` 构建，dtype=list[str] | None，unit=collection
    :return: 返回 ``-work`` 命令行参数；无库名时返回空列表，dtype=list[str]，unit=argv
    """

    # 未提供逻辑库时不插入 ``-work``，让工具沿用默认工作库
    if not list_libraries:

        # 当前调用没有额外工作库要求，因此保持空参数
        return []

    # 只消费首个逻辑库名称，保持与旧实现和测试假设一致
    return ["-work", list_libraries[0]]

# 统一派生阶段日志路径，避免各步骤手写日志文件名时出现偏差
def _log_path(path_log_dir: Path, str_step_name: str) -> str:
    """
    计算单个阶段的日志文件路径。

    :param path_log_dir: 日志目录根路径，dtype=Path，unit=path
    :param str_step_name: 阶段名称，例如 ``compile`` 或 ``simulate``，dtype=str，unit=identifier
    :return: 返回阶段日志文件的字符串路径，dtype=str，unit=path
    """

    # 日志文件名始终使用 ``<step>.log`` 规则，便于测试和人工定位
    return str(path_log_dir / f"{str_step_name}.log")

# 把可空阶段额外参数统一拍平成字符串列表，减少各阶段命令构建时的重复判空
def _stage_args(list_value: list[str] | tuple[str, ...] | None) -> list[str]:
    """
    规范化单个阶段的附加参数列表。

    :param list_value: 调用方传入的阶段附加参数，允许 list、tuple 或 None，dtype=list[str] | tuple[str, ...] | None，unit=collection
    :return: 返回字符串化后的参数列表，dtype=list[str]，unit=argv
    """

    # 逐项显式转成字符串，避免 Path 或其他对象混入 subprocess 参数列表
    return [str(obj_item) for obj_item in (list_value or [])]

# 从工具覆盖字典中读取真实可执行文件名，未覆盖时回退到默认工具名
def _tool(dict_tools: dict[str, str] | None, str_name: str, str_default: str) -> str:
    """
    解析某个阶段对应的工具名。

    :param dict_tools: 工具覆盖映射，允许为空，dtype=dict[str, str] | None，unit=mapping
    :param str_name: 需要查询的工具键名，例如 ``vcs`` 或 ``verdi``，dtype=str，unit=identifier
    :param str_default: 未覆盖时采用的默认工具名，dtype=str，unit=identifier
    :return: 返回最终生效的工具名或路径，dtype=str，unit=argv token
    """

    # 优先采用调用方覆盖值；未覆盖时回落到脚本内置默认工具名
    return str((dict_tools or {}).get(str_name) or str_default)

# 根据 simv 可执行名推导本地生成产物路径，兼容默认值和 ``./custom_simv`` 覆盖场景
def _simv_artifact(path_workdir: Path, str_simv_tool: str) -> Path:
    """
    推导 simv 产物在工作目录中的落点。

    :param path_workdir: 当前冒烟工程工作目录，dtype=Path，unit=path
    :param str_simv_tool: simulate 阶段采用的命令名或相对路径，dtype=str，unit=argv token
    :return: 返回预期的 simv 可执行文件路径，dtype=Path，unit=path
    """

    # ``./custom_simv`` 表示把输出程序写在工作目录下的自定义文件名
    if str_simv_tool.startswith("./"):

        # 去掉 ``./`` 前缀后拼回工作目录，保持与旧实现完全一致
        return path_workdir / str_simv_tool[2:]

    # 默认工具名 ``simv`` 时直接映射到工作目录中的标准产物文件名
    if str_simv_tool == "simv":

        # 保留历史默认产物名，避免破坏现有期望路径和测试夹具
        return path_workdir / "simv"

    # 其他覆盖形式仍沿用 ``workdir/simv`` 的既有约定，保持行为兼容
    return path_workdir / "simv"

# 把 expected_artifacts 配置统一规整成绝对路径与最小字节数，方便执行后统一验收
def _normalize_expected_artifacts(
    path_workdir: Path,
    dict_expected: dict[str, Any] | None,
    path_default_dump: Path,
) -> dict[str, JsonDict]:
    """
    规范化预期产物配置。

    :param path_workdir: 冒烟工程工作目录，用于补全相对路径，dtype=Path，unit=path
    :param dict_expected: 用户声明的预期产物映射；值允许字符串或对象，dtype=dict[str, Any] | None，unit=mapping
    :param path_default_dump: 未提供 expected_artifacts 时默认检查的波形文件路径，dtype=Path，unit=path
    :return: 返回键为产物名、值含 ``path`` 与 ``min_bytes`` 的字典，dtype=dict[str, dict[str, Any]]，unit=mapping
    :raises ValueError: 当 expected_artifacts 条目既不是字符串也不是对象时抛出异常。
    """

    # 调用方未自定义期望产物时，至少保证默认 FSDB 转储文件会被检查
    if not dict_expected:

        # 回退到最小默认契约，只要求 dump 文件存在且非空
        return {"dump": {"path": str(path_default_dump), "min_bytes": 1}}

    # 使用新字典承接规整后的绝对路径与字节阈值，避免污染原始配置对象
    dict_normalized: dict[str, JsonDict] = {}  # 规整后的预期产物配置映射

    # 逐个吸收调用方声明的产物规则，兼容字符串与对象两种输入形式
    for str_name, obj_spec in dict_expected.items():

        # 字符串条目默认只提供产物路径，并沿用最小 1 字节阈值
        if isinstance(obj_spec, str):

            # 直接把字符串解释为目标产物路径，保持配置语义简单直观
            path_item = Path(obj_spec)  # 当前产物声明的原始路径对象

            # 字符串简写不携带阈值，因此沿用非空文件这一最低验收要求
            int_min_bytes = 1  # 当前产物需要满足的最小字节数

        # 对象条目允许同时覆盖路径和最小字节数，适合自定义 simv 等产物规则
        elif isinstance(obj_spec, dict):

            # 缺少 path 时退回到当前产物名，让命名与默认文件落点对齐
            path_item = Path(str(obj_spec.get("path", str_name)))  # 当前产物声明的路径对象

            # 把 min_bytes 强制规整成整数，避免 JSON 中的字符串数值泄漏到比较逻辑
            int_min_bytes = int(obj_spec.get("min_bytes", 1))  # 当前产物最小字节阈值

        # 其他输入形态无法稳定解释，应尽早阻断并提示配置错误
        else:

            # expected_artifacts 只接受字符串或对象，其他类型会破坏后续统一校验流程
            raise ValueError("> ERR: [Python] expected_artifacts entries must be strings or objects")

        # 相对路径统一锚定到工作目录，保证远程和本地运行下的解释一致
        path_resolved = path_item if path_item.is_absolute() else path_workdir / path_item  # 当前产物的绝对落盘路径

        # 把路径和阈值写回规整后的映射，供执行后验收逻辑直接消费
        dict_normalized[str_name] = {
            "path": str(path_resolved),  # 当前产物规整后的绝对路径字符串
            "min_bytes": int_min_bytes,  # 当前产物需要满足的最小字节阈值
        }

    # 返回规整后的产物规则，供计划字典与执行阶段共享
    return dict_normalized

# 按照 VERDI_HOME 与 NOVAS_HOME 的常见布局探测 Verdi PLI 目录
def find_pli_dir(dict_env: dict[str, str] | None = None) -> Path | None:
    """
    在环境变量声明的位置查找 Verdi PLI 目录。

    :param dict_env: 可选环境变量映射；为空时读取当前进程环境，dtype=dict[str, str] | None，unit=mapping
    :return: 当目录中同时存在 ``novas.tab`` 与 ``pli.a`` 时返回该目录，否则返回 None，dtype=Path | None，unit=path
    """

    # 优先使用调用方提供的环境映射，便于测试覆盖自定义环境值
    dict_env_map = dict_env or os.environ  # 当前用于探测 PLI 目录的环境变量集合

    # 按优先级累积候选目录，先看 NOVAS_HOME，再看 VERDI_HOME
    list_candidates: list[Path] = []  # 按既定搜索顺序排列的 PLI 目录候选列表

    # 当环境里存在 NOVAS_HOME 时，同时尝试根目录和常见 share/PLI 子目录布局
    if dict_env_map.get("NOVAS_HOME"):

        # 先解析 NOVAS_HOME 根路径，避免后续重复构造 Path 对象
        path_novas_home = Path(dict_env_map["NOVAS_HOME"])  # 环境变量 NOVAS_HOME 指向的根目录

        # 保留历史上的三个搜索位置顺序，以兼容不同安装布局
        list_candidates.extend(
            [
                path_novas_home,
                path_novas_home / "share" / "PLI" / "VCS" / "LINUX64",
                path_novas_home / "share" / "PLI" / "VCS" / "LINUX",
            ]
        )

    # 当环境里存在 VERDI_HOME 时，再补充 Verdi 安装目录下的两个常见 PLI 位置
    if dict_env_map.get("VERDI_HOME"):

        # 先把 VERDI_HOME 锚定成 Path，后续候选目录都从这一个安装根目录向下展开。
        path_verdi_home = Path(dict_env_map["VERDI_HOME"])  # 当前环境声明的 Verdi 安装根目录。

        # 沿用 Linux64 优先于 Linux 的搜索顺序，匹配大多数 Synopsys 安装习惯
        list_candidates.extend(
            [
                path_verdi_home / "share" / "PLI" / "VCS" / "LINUX64",
                path_verdi_home / "share" / "PLI" / "VCS" / "LINUX",
            ]
        )

    # 逐个检查候选目录是否同时具备 novas.tab 和 pli.a 两个关键文件
    for path_candidate in list_candidates:

        # 只有两类文件都存在时才认为该目录可用于 ``-P novas.tab pli.a`` 参数
        if (path_candidate / "novas.tab").exists() and (path_candidate / "pli.a").exists():

            # 返回首个命中的候选目录，保持探测顺序的确定性
            return path_candidate

    # 没有命中任何有效候选目录时返回空值，让上层按无 PLI 分支继续构建计划
    return None

# 把 PLI 目录转换成 ``-P novas.tab pli.a`` 参数，并返回用于计划元数据的产物记录
def pli_args(path_pli_dir: Path | None) -> tuple[list[str], JsonDict]:
    """
    生成 Verdi PLI 参数列表与产物记录。

    :param path_pli_dir: 预期包含 ``novas.tab`` 和 ``pli.a`` 的目录；为空时不启用 PLI，dtype=Path | None，unit=path
    :return: 第一个返回值是 ``-P`` 参数列表，第二个返回值是记录 ``pli_tab`` 与 ``pli_lib`` 的字典，
        dtype=tuple[list[str], dict[str, Any]]，unit=collection
    :raises ValueError: 当目录存在但缺少必需 PLI 文件时抛出异常。
    """

    # 调用方未要求或未探测到 PLI 目录时，直接返回空参数和空产物记录
    if not path_pli_dir:

        # 当前流程不需要附带 Verdi PLI，因此不补任何 ``-P`` 实参
        return [], {}

    # 先解析 novas.tab 的绝对路径，确保命令构建和产物记录都指向同一个文件位置
    path_tab = (path_pli_dir / "novas.tab").resolve()  # Verdi PLI 需要的 novas.tab 文件路径

    # 再解析 pli.a 的绝对路径，让后续存在性检查能明确区分表文件和库文件
    path_lib = (path_pli_dir / "pli.a").resolve()  # Verdi PLI 需要的 pli.a 静态库路径

    # 只要任一关键文件缺失，就阻断计划生成并给出明确的目录定位信息
    if not path_tab.exists() or not path_lib.exists():

        # PLI 目录不完整时继续执行只会在 elaboration 阶段失败，因此提前抛出结构化错误
        raise ValueError(f"> ERR: [Python] novas PLI files not found under {path_pli_dir}")

    # 返回 ``-P`` 所需的三段参数，并同时写入计划产物元数据供后续诊断输出复用
    return ["-P", str(path_tab), str(path_lib)], {"pli_tab": str(path_tab), "pli_lib": str(path_lib)}

# 把当前配置对象里的 Verilog/SystemVerilog 多入口输入规整成统一参数序列，避免主 helper 重复承受多行赋值门禁。
def _configured_source_args(obj_plan_config: SmokePlanConfig) -> list[str]:
    """
    解析配置对象中的 Verilog/SystemVerilog 输入参数。

    :param obj_plan_config: 已规整的冒烟计划配置对象，dtype=SmokePlanConfig，unit=object
    :return: 返回可直接交给 ``vlogan`` 的源码与 filelist 参数列表，dtype=list[str]，unit=argv
    """

    # 这里直接委托给既有的 ``normalize_sources`` 规则，保持 CLI 与 manifest 两条路径的解释完全一致。
    return normalize_sources(
        source=_plan_option(obj_plan_config, "source"),
        sources=_plan_option(obj_plan_config, "sources"),
        source_list=_plan_option(obj_plan_config, "source_list"),
        source_lists=_plan_option(obj_plan_config, "source_lists"),
    )

# 根据配置对象规整源码、路径、覆盖率和 PLI 等基础事实，供后续命令与报告 helper 统一复用。
def _resolve_smoke_plan_inputs(obj_plan_config: SmokePlanConfig) -> SmokePlanInputs:
    """
    解析冒烟计划构建所需的基础输入事实。

    :param obj_plan_config: 已规整的冒烟计划配置对象，dtype=SmokePlanConfig，unit=object
    :return: 返回工作目录、源码参数、覆盖率参数、工具映射和 PLI 产物等基础事实，dtype=SmokePlanInputs，unit=object
    :raises ValueError: 当 Verilog 与 VHDL 两类源码入口都为空时抛出异常。
    """

    # 先把工作目录规整成绝对路径，避免后续命令和日志路径混入相对目录。
    path_workdir = Path(_plan_option(obj_plan_config, "workdir", Path("build/vcs-verdi-smoke"))).resolve()  # 当前计划采用的绝对工作目录

    # 自定义日志目录只有显式给出时才覆盖默认值，否则沿用 ``workdir/logs``。
    path_requested_log_dir: Path | None = _as_path(_plan_option(obj_plan_config, "log_dir"))  # 当前请求显式给出的日志目录覆盖值

    # 把最终日志目录也规范成绝对路径，保持 dry-run 与 execute 模式定位一致。
    path_log_dir = (path_requested_log_dir if path_requested_log_dir else path_workdir / "logs").resolve()  # 当前计划实际采用的绝对日志目录

    # 覆盖率维度列表需要先单独取出，后面会驱动数据库目录和多个阶段的参数构建。
    list_coverage = _plan_option(obj_plan_config, "coverage")  # 当前请求显式声明的覆盖率维度列表

    # 只有启用覆盖率时才创建稳定的 ``simv.vdb`` 目录定位，避免无覆盖率场景产生伪元数据。
    path_coverage_db = path_workdir / "simv.vdb" if list_coverage else None  # 覆盖率数据库目录

    # compile、elaborate 和 simulate 三个阶段共享同一份覆盖率参数组合。
    list_coverage_args = _coverage_arg(list_coverage, path_coverage_db)  # Verilog 主线三个阶段共用的覆盖率参数

    # vhdlan 目前不接受这里的 ``-cm`` 参数组合，因此显式保留空列表更安全。
    list_vhdl_coverage_args: list[str] = []  # VHDL 分析阶段当前刻意保持为空的覆盖率参数列表

    # 工具覆盖映射先统一复制成独立字典，避免后续 helper 误改调用方对象。
    dict_tool_map = dict(_plan_option(obj_plan_config, "tools", {}) or {})  # 当前计划采用的工具入口覆盖映射

    # 先单独规整 VHDL 输入，后续步骤列表会据此决定是否插入 ``compile-vhdl``。
    list_vhdl_source_args = _resolve_many(_plan_option(obj_plan_config, "vhdl_sources"))  # 当前计划的 VHDL 绝对路径参数列表

    # 只有显式给出 Verilog/SystemVerilog 源或 filelist 时，才进一步展开 vlogan 输入参数。
    if (
        _plan_option(obj_plan_config, "source")
        or _plan_option(obj_plan_config, "sources")
        or _plan_option(obj_plan_config, "source_list")
        or _plan_option(obj_plan_config, "source_lists")
    ):

        # Verilog/SystemVerilog 多入口输入统一交给专门 helper 规整，保持主流程职责只剩事实汇总。
        list_source_args = _configured_source_args(obj_plan_config)  # 当前计划的 Verilog/SystemVerilog 源码与 filelist 参数列表

    # 没有 Verilog 类输入时显式保留空列表，让 VHDL-only 场景还能继续构建完整计划。
    else:

        # 当前请求没有 Verilog/SystemVerilog 源或 filelist，因此不生成 vlogan 输入参数。
        list_source_args = []  # VHDL-only 场景下显式保留空的 vlogan 输入参数序列

    # Verilog 和 VHDL 两类输入都为空时，继续构建计划已经失去意义，应立即阻断。
    if not list_source_args and not list_vhdl_source_args:

        # 冒烟计划至少需要一类源码输入，否则 compile 阶段根本无法形成有效命令。
        raise ValueError(
            "> ERR: [Python] at least one --source, --source-list, --vhdl-source, or manifest source is required"
        )

    # 默认 FSDB 转储文件始终落在工作目录下，便于 simulate 和后续波形检查共享定位。
    path_dump = path_workdir / str(_plan_option(obj_plan_config, "dump_name", "waves.fsdb"))  # 默认波形转储产物路径

    # 先解析 simulate 阶段真实采用的工具入口，再由它推导计划里的 ``simv`` 产物路径。
    str_simv_tool = _tool(dict_tool_map, "simv", "./simv")  # simulate 阶段实际采用的工具入口

    # 计划对象里的 ``simv`` 产物路径必须与 simulate 命令保持一致，避免后续诊断口径漂移。
    path_simv = _simv_artifact(path_workdir, str_simv_tool)  # 当前计划视角下的 simv 产物路径

    # PLI 目录优先尊重显式配置；没有显式目录时再按 ``auto_pli`` 决定是否自动探测。
    path_configured_pli_dir: Path | None = _as_path(_plan_option(obj_plan_config, "pli_dir"))  # 调用方手工锁定的 Novas PLI 目录覆盖值

    # 只有显式关闭时才阻止自动探测，其他场景保持历史默认行为。
    bool_auto_pli = bool(_plan_option(obj_plan_config, "auto_pli", True))  # 当前计划是否允许自动探测 PLI 目录

    # 最终采用的 PLI 目录要么来自显式配置，要么来自自动探测，要么明确保持为空。
    if path_configured_pli_dir:

        # 显式给出 PLI 目录时，优先锁定该绝对路径，避免环境探测结果覆盖调用方意图。
        path_selected_pli_dir = path_configured_pli_dir.resolve()  # 当前计划最终采用的显式 PLI 目录

    # 没有显式目录但允许自动探测时，继续沿用历史环境扫描逻辑。
    elif bool_auto_pli:

        # 自动探测结果可能为空；这里保留原始返回值语义，由后续 ``pli_args`` 决定如何降级。
        path_selected_pli_dir = find_pli_dir()  # 当前计划根据环境探测得到的 PLI 目录

    # 显式关闭自动探测且没有手工目录时，后续 elaboration 不应再拼接任何 PLI 参数。
    else:

        # 用 ``None`` 明确表示当前计划不启用 Novas PLI 挂接。
        path_selected_pli_dir = None  # 当前计划刻意保持为空的 PLI 目录

    # 生成 elaboration 需要的 ``-P`` 参数，并同步拿到可回写的 PLI 产物记录。
    tuple_pli_payload = pli_args(path_selected_pli_dir)  # PLI 参数列表与产物记录组成的二元结果

    # 从 PLI 二元结果中取出命令行参数部分，供 elaboration 阶段直接拼接。
    list_selected_pli_args = tuple_pli_payload[0]  # elaboration 阶段需要附带的 ``-P`` 参数列表

    # 同一份二元结果里的第二项只用于计划元数据和 dry-run 诊断输出。
    dict_pli_artifacts = tuple_pli_payload[1]  # PLI 相关文件的产物记录映射

    # 把基础事实收口到具名对象，后续 helper 直接按属性读取更容易维持类型与职责边界。
    return SmokePlanInputs(
        path_workdir=path_workdir,
        path_log_dir=path_log_dir,
        path_coverage_db=path_coverage_db,

        # 覆盖率参数会被多个阶段重复读取，因此集中放在同一组字段里更利于保持一致。
        list_coverage_args=list_coverage_args,
        list_vhdl_coverage_args=list_vhdl_coverage_args,

        # 工具映射和源码入口共同驱动命令构造，后续 helper 会频繁同时访问这组字段。
        dict_tool_map=dict_tool_map,
        list_vhdl_source_args=list_vhdl_source_args,
        list_source_args=list_source_args,

        # 这些产物路径会被步骤对象和最终计划报告同步回写。
        path_dump=path_dump,
        str_simv_tool=str_simv_tool,
        path_simv=path_simv,

        # PLI 参数和产物元数据都源自同一轮探测，放在结尾便于与前面的路径组区分。
        list_selected_pli_args=list_selected_pli_args,
        dict_pli_artifacts=dict_pli_artifacts,
    )

# 基于基础事实构造 compile、elaborate 和 simulate 三大阶段命令，避免主入口继续承载长流程细节。
def _build_command_block(obj_plan_config: SmokePlanConfig, smoke_plan_inputs: SmokePlanInputs) -> JsonDict:
    """
    构造 VHDL、Verilog、elaborate 与 simulate 四类阶段命令。

    :param obj_plan_config: 已规整的冒烟计划配置对象，dtype=SmokePlanConfig，unit=object
    :param smoke_plan_inputs: 由 ``_resolve_smoke_plan_inputs`` 生成的基础事实对象，dtype=SmokePlanInputs，unit=object
    :return: 返回四类阶段命令参数列表，dtype=dict[str, Any]，unit=mapping
    """

    # DPI 共享库参数只属于 elaboration 阶段，因此单独累计更容易保证阶段边界清晰。
    list_sv_lib_args: list[str] = []  # elaboration 阶段逐个展开的 ``-sv_lib`` 参数对

    # 共享库的声明顺序可能携带依赖语义，因此这里保持调用方原始顺序逐个展开。
    for path_sv_lib in _plan_option(obj_plan_config, "sv_libs") or []:

        # 每个 DPI 共享库都展开成 ``-sv_lib`` 与绝对路径成对出现的参数。
        list_sv_lib_args.extend(["-sv_lib", str(Path(path_sv_lib).resolve())])

    # KDB 是 compile 和 elaborate 共用能力，因此先单独整理成小列表供多个阶段复用。
    list_kdb_args = ["-kdb"] if bool(_plan_option(obj_plan_config, "kdb", True)) else []  # 需要附加到相关阶段的 KDB 参数列表

    # timescale 只影响编译阶段，没有值时显式保持空列表最能贴合历史行为。
    str_timescale = _plan_option(obj_plan_config, "timescale")  # 当前请求显式声明的编译时标约束

    # compile 阶段的 timescale 参数只在有值时才补进命令列表。
    list_timescale_args = [f"-timescale={str_timescale}"] if str_timescale else []  # 编译阶段实际采用的 timescale 参数列表

    # 工作库参数与 KDB 开关会同时影响 VHDL 与 Verilog 编译入口，因此提前合并可减少重复。
    list_common_compile_args = [*_work_args(_plan_option(obj_plan_config, "libraries")), *list_kdb_args]  # 编译阶段共享的基础参数

    # VHDL 命令只承接 VHDL 输入与该阶段专属参数，不混入后续 elaboration 或 simulate 开关。
    list_vhdl_cmd = [  # VHDL 分析阶段最终采用的命令参数列表
        _tool(smoke_plan_inputs.dict_tool_map, "vhdlan", "vhdlan"),  # 以 vhdlan 作为 VHDL 分析入口
        "-full64",  # 固定锁定 64 位模式，和其余 Synopsys 阶段保持一致
        *list_common_compile_args,  # 复用工作库与 KDB 等编译前置参数
        *smoke_plan_inputs.list_vhdl_coverage_args,  # 当前刻意保持空的 VHDL 覆盖率参数占位
        *_stage_args(_plan_option(obj_plan_config, "vhdlan_args")),  # 接入调用方专门追加给 vhdlan 的额外开关
        *smoke_plan_inputs.list_vhdl_source_args,  # 把当前这批 VHDL 源文件路径接到命令尾部
    ]  # VHDL 分析阶段命令参数列表

    # Verilog/SystemVerilog 命令需要统一吸收源码、filelist、incdir、define 与覆盖率开关。
    list_compile_cmd = [  # Verilog/SystemVerilog 编译阶段最终采用的命令参数列表
        _tool(smoke_plan_inputs.dict_tool_map, "vlogan", "vlogan"),  # 以 vlogan 作为 Verilog/SystemVerilog 编译入口
        "-full64",  # 编译阶段同样固定工作在 64 位模式
        "-sverilog",  # 明确启用 SystemVerilog 语法解析能力
        *list_common_compile_args,  # 把工作库和 KDB 前置条件一次性并入 vlogan 编译链路
        *smoke_plan_inputs.list_coverage_args,  # 为 Verilog 编译阶段挂上统一的覆盖率采集开关
        *list_timescale_args,  # 按需追加编译时标约束
        *_include_args(_plan_option(obj_plan_config, "include_dirs")),  # 把头文件搜索目录展开成 ``+incdir+`` 参数
        *_define_args(_plan_option(obj_plan_config, "defines")),  # 把宏定义映射展开成 ``+define+`` 参数
        *_stage_args(_plan_option(obj_plan_config, "vlogan_args")),  # 只把调用方显式声明的 vlogan 扩展参数附加在这里
        *smoke_plan_inputs.list_source_args,  # 把显式源码路径和 filelist 参数接到命令尾部
    ]  # Verilog/SystemVerilog 编译阶段命令参数列表

    # elaboration 命令会连接 PLI、DPI 共享库以及调试/覆盖率设置，确保进入 simv 前的链路完整。
    list_elaborate_cmd = [  # 用于链接顶层设计并产出 simv 的 vcs 命令参数列表
        _tool(smoke_plan_inputs.dict_tool_map, "vcs", "vcs"),  # 以 vcs 作为 elaboration 与链接入口
        "-full64",  # 链接阶段继续沿用 64 位模式，避免和前序编译位宽不一致
        *list_kdb_args,  # 继续透传 KDB 开关，保证后续 Verdi 能看到调试数据库
        "-sverilog",  # 让链接阶段和前序编译保持一致的语言模式
        f"-debug_access+{str(_plan_option(obj_plan_config, 'debug', 'all'))}",  # 注入当前计划要求的调试可见性等级
        *smoke_plan_inputs.list_coverage_args,  # 沿用前序阶段的覆盖率布局，确保 simv.vdb 路径不漂移
        *smoke_plan_inputs.list_selected_pli_args,  # 挂接 ``-P novas.tab pli.a`` 这组 PLI 参数
        *list_sv_lib_args,  # 把 DPI 共享库参数成对拼进链接命令
        *_stage_args(_plan_option(obj_plan_config, "vcs_args")),  # 这里仅吸收调用方明确要求的 elaboration 补充开关
        f"work.{str(_plan_option(obj_plan_config, 'top', 'top'))}",  # 把顶层设计名转换成 ``work.<top>`` 形式
        "-o",  # 明确指定 simv 可执行文件输出参数名
        str(smoke_plan_inputs.path_simv),  # 把 simv 稳定落到当前工作目录中的目标路径
    ]  # elaboration 阶段命令参数列表

    # simulate 命令先放工具入口与 FSDB 输出约束，其他运行态开关稍后按条件附加。
    list_simulate_cmd = [  # simulate 阶段启动时的基础命令骨架
        smoke_plan_inputs.str_simv_tool,  # 首项固定为 simulate 阶段真实采用的可执行入口
        "+fsdbfile+" + str(_plan_option(obj_plan_config, "dump_name", "waves.fsdb")),  # 把 FSDB 输出文件名固定绑定到 simv 启动参数
    ]  # simulate 阶段最小启动命令骨架

    # 覆盖率参数需要继续透传到 simulate 阶段，确保 ``simv.vdb`` 与前面阶段共用同一路径。
    list_simulate_cmd.extend(smoke_plan_inputs.list_coverage_args)

    # 显式种子仅在调用方提供时注入，避免改变未声明种子的历史行为。
    if _plan_option(obj_plan_config, "seed") is not None:

        # 把随机种子转成 ``+ntb_random_seed``，便于远程重放和日志对比。
        list_simulate_cmd.append(f"+ntb_random_seed={_plan_option(obj_plan_config, 'seed')}")

    # plusargs 需要保持业务层声明顺序，因此直接原样追加到 simulate 命令尾部。
    list_simulate_cmd.extend(_plan_option(obj_plan_config, "plusargs") or [])

    # 额外的 simv 参数在 plusargs 之后追加，保证调用方可以同时使用两类开关。
    list_simulate_cmd.extend(_stage_args(_plan_option(obj_plan_config, "simv_args")))

    # UCLI 命令文件只有显式给出时才接入，避免默认计划意外进入交互或脚本模式。
    if _plan_option(obj_plan_config, "cmd_file"):

        # 这里统一把命令文件路径 resolve 后交给 ``-ucli -do``，保持既有接口语义。
        list_simulate_cmd.extend(["-ucli", "-do", str(Path(_plan_option(obj_plan_config, "cmd_file")).resolve())])

    # 四类阶段命令统一回写成字典，供步骤组装和最终计划报告重复复用。
    return {
        "list_vhdl_cmd": list_vhdl_cmd,
        "list_compile_cmd": list_compile_cmd,
        "list_elaborate_cmd": list_elaborate_cmd,
        "list_simulate_cmd": list_simulate_cmd,
    }

# 根据波形校验模式生成最终 load-check 步骤，避免主计划入口继续同时承载 GUI 与非 GUI 两套分支。
def _build_verdi_check_step(obj_plan_config: SmokePlanConfig, smoke_plan_inputs: SmokePlanInputs) -> JsonDict:
    """
    构造最终的 Verdi 或 fsdbreport 校验步骤。

    :param obj_plan_config: 已规整的冒烟计划配置对象，dtype=SmokePlanConfig，unit=object
    :param smoke_plan_inputs: 由 ``_resolve_smoke_plan_inputs`` 生成的基础事实对象，dtype=SmokePlanInputs，unit=object
    :return: 返回可直接附加到 ``steps`` 列表尾部的最终校验步骤对象，dtype=dict[str, Any]，unit=mapping
    """

    # 当前计划支持 ``verdi`` 和 ``fsdbreport`` 两条校验路径，默认仍然走 GUI Verdi 模式。
    str_verdi_check = str(_plan_option(obj_plan_config, "verdi_check", "verdi"))  # 当前计划选择的波形校验模式

    # fsdbreport 模式不启动 GUI，而是对指定信号做非交互波形加载检查。
    if str_verdi_check == "fsdbreport":

        # 未显式指定目标信号时，继续默认观察顶层 ``clk``，保持文档和测试里的历史约定。
        str_signal = (  # fsdbreport 校验阶段实际采用的目标信号路径
            _plan_option(obj_plan_config, "report_signal")  # 优先尊重调用方显式指定的目标信号
            or f"/{str(_plan_option(obj_plan_config, 'top', 'top'))}/clk"  # 未指定时退回到顶层 ``clk`` 的历史默认路径
        )

        # 直接对 dump 文件执行 fsdbreport，让额外参数继续承接时间窗或格式控制类开关。
        list_verdi_cmd = [  # fsdbreport 校验阶段最终采用的命令参数列表
            _tool(smoke_plan_inputs.dict_tool_map, "fsdbreport", "fsdbreport"),  # 以 fsdbreport 作为非 GUI 波形检查入口
            str(smoke_plan_inputs.path_dump),  # 把 simulate 产生的 FSDB 文件直接交给报告工具读取
            "-s",  # 明确告诉 fsdbreport 后一项是需要观测的目标信号
            str_signal,  # 把本轮波形检查真正关注的信号路径传给报告工具
            *_stage_args(_plan_option(obj_plan_config, "fsdbreport_args")),  # 接入调用方专门追加给 fsdbreport 的额外参数
        ]  # fsdbreport 校验阶段命令参数列表

        # 非 GUI 校验路径使用独立步骤名，便于 dry-run 和执行日志直接区分。
        str_verdi_step_name = "verdi-fsdbreport-check"  # 非 GUI 波形报告检查阶段对外使用的步骤名

    # 默认模式下通过 Verdi 自身加载 dump，并在命令尾部带 ``-exit`` 快速退出。
    else:

        # 先构建基础的 Verdi 打开波形命令，后续再按需附加 RC 文件和额外参数。
        list_verdi_cmd = [  # 用于实际打开 FSDB 并快速退出的 GUI Verdi 命令参数列表
            _tool(smoke_plan_inputs.dict_tool_map, "verdi", "verdi"),  # 以 verdi 作为 GUI 波形装载入口
            "-ssf",  # 明确告诉 Verdi 后一项是需要装载的 FSDB 文件
            str(smoke_plan_inputs.path_dump),  # 把当前计划生成的波形文件路径交给 Verdi 装载
        ]  # Verdi 校验阶段基础命令参数列表

        # RC 文件只有显式存在时才追加 ``-sswr``，避免默认 dry-run 绑定不存在的脚本路径。
        if _plan_option(obj_plan_config, "rc_file"):

            # 把 Verdi RC 文件路径加到命令里，支持自动化恢复窗口布局或预置视图。
            list_verdi_cmd.extend(["-sswr", str(Path(_plan_option(obj_plan_config, "rc_file")).resolve())])

        # 始终关闭 logo 并执行后自动退出，让 execute 模式保持非交互冒烟特性。
        list_verdi_cmd.extend(["-nologo", "-exit", *_stage_args(_plan_option(obj_plan_config, "verdi_args"))])

        # GUI 路径沿用历史步骤名，方便文档、测试与诊断脚本继续直接识别。
        str_verdi_step_name = "verdi-load-check"  # GUI 波形装载检查阶段对外使用的步骤名

    # 最终把命令、工作目录和日志路径折叠成单个步骤对象，供步骤列表直接追加。
    return {
        "name": str_verdi_step_name,
        "cmd": list_verdi_cmd,
        "cwd": str(smoke_plan_inputs.path_workdir),
        "log": _log_path(smoke_plan_inputs.path_log_dir, str_verdi_step_name),
    }

# 按固定阶段顺序组装完整 ``steps`` 列表，让 compile、simulate 和最终波形校验链路显式可读。
def _build_plan_steps(
    smoke_plan_inputs: SmokePlanInputs,
    json_dict_command_block: JsonDict,
    json_dict_verdi_step: JsonDict,
) -> list[JsonDict]:
    """
    组装完整的冒烟执行步骤列表。

    :param smoke_plan_inputs: 由 ``_resolve_smoke_plan_inputs`` 生成的基础事实对象，dtype=SmokePlanInputs，unit=object
    :param json_dict_command_block: 由 ``_build_command_block`` 生成的阶段命令字典，dtype=dict[str, Any]，unit=mapping
    :param json_dict_verdi_step: 由 ``_build_verdi_check_step`` 生成的最终波形校验步骤，dtype=dict[str, Any]，unit=mapping
    :return: 返回按执行顺序排列的步骤对象列表，dtype=list[dict[str, Any]]，unit=collection
    """

    # 使用独立列表承接最终步骤集合，便于按源码类型动态插入 VHDL 和 Verilog 编译阶段。
    list_steps: list[JsonDict] = []  # 当前计划最终输出的步骤对象列表

    # 存在 VHDL 源文件时先加入 ``compile-vhdl``，保证混合语言工程遵守预期顺序。
    if smoke_plan_inputs.list_vhdl_source_args:

        # VHDL 分析阶段复用统一工作目录和日志布局，供 dry-run 与 execute 共用。
        list_steps.append(
            {
                "name": "compile-vhdl",
                "cmd": json_dict_command_block["list_vhdl_cmd"],
                "cwd": str(smoke_plan_inputs.path_workdir),
                "log": _log_path(smoke_plan_inputs.path_log_dir, "compile-vhdl"),
            }
        )

    # 存在 Verilog/SystemVerilog 输入时加入编译阶段；混合语言场景下名称切换成 ``compile-verilog``。
    if smoke_plan_inputs.list_source_args:

        # 同时存在 VHDL 输入时改用更明确的步骤名，便于 dry-run 中区分两个编译阶段。
        str_compile_step_name = "compile-verilog" if smoke_plan_inputs.list_vhdl_source_args else "compile"  # Verilog 编译步骤名

        # Verilog/SystemVerilog 编译步骤同样复用统一工作目录和日志布局策略。
        list_steps.append(
            {
                "name": str_compile_step_name,
                "cmd": json_dict_command_block["list_compile_cmd"],
                "cwd": str(smoke_plan_inputs.path_workdir),
                "log": _log_path(smoke_plan_inputs.path_log_dir, str_compile_step_name),
            }
        )

    # elaboration 与 simulate 无论单语言还是混合语言都必须存在，因此固定追加到后半段。
    list_steps.extend(
        [
            {
                "name": "elaborate",
                "cmd": json_dict_command_block["list_elaborate_cmd"],
                "cwd": str(smoke_plan_inputs.path_workdir),
                "log": _log_path(smoke_plan_inputs.path_log_dir, "elaborate"),
            },
            {
                "name": "simulate",
                "cmd": json_dict_command_block["list_simulate_cmd"],
                "cwd": str(smoke_plan_inputs.path_workdir),
                "log": _log_path(smoke_plan_inputs.path_log_dir, "simulate"),
            },
        ]
    )

    # 最终把波形校验步骤追加到计划末尾，形成 compile -> simulate -> load-check 的完整链路。
    list_steps.append(json_dict_verdi_step)

    # 返回已按执行顺序组装完成的步骤列表，供最终计划对象直接复用。
    return list_steps

# 把基础事实、阶段命令和步骤清单重新收口成最终计划对象，供 dry-run 和 execute 共享同一结构。
def _build_plan_report(
    obj_plan_config: SmokePlanConfig,
    smoke_plan_inputs: SmokePlanInputs,
    list_steps: list[JsonDict],
) -> JsonDict:
    """
    组装最终的冒烟计划输出对象。

    :param obj_plan_config: 已规整的冒烟计划配置对象，dtype=SmokePlanConfig，unit=object
    :param smoke_plan_inputs: 由 ``_resolve_smoke_plan_inputs`` 生成的基础事实对象，dtype=SmokePlanInputs，unit=object
    :param list_steps: 已按执行顺序组装完成的步骤对象列表，dtype=list[dict[str, Any]]，unit=collection
    :return: 返回包含输入、步骤、产物和诊断元数据的最终计划对象，dtype=dict[str, Any]，unit=mapping
    """

    # 单文件源码在最终计划里仍需与多文件入口合并展示，保持历史 ``sources`` 字段语义不变。
    obj_source = _plan_option(obj_plan_config, "source")  # 当前请求显式给出的单文件源码入口

    # ``sources`` 字段始终回写成可直接读懂的绝对路径字符串列表。
    list_sources = _plan_option(obj_plan_config, "sources") or ([obj_source] if obj_source else [])  # 当前计划里需要回写的显式源码路径列表

    # 计划对象中的 ``cmd_file`` 产物字段仍按旧接口规则保留绝对路径或空字符串二选一。
    obj_cmd_file = _plan_option(obj_plan_config, "cmd_file")  # 当前请求显式给出的 UCLI 命令文件路径

    # ``rc`` 产物字段也继续保留绝对路径或空字符串二选一，方便测试和 dry-run 稳定断言。
    obj_rc_file = _plan_option(obj_plan_config, "rc_file")  # 用于恢复 Verdi 布局脚本的 RC 文件路径

    # 这些路径与环境映射会被多处回写到计划对象里，先单独取出可避免长行和重复读取。
    path_source_list = _plan_option(obj_plan_config, "source_list")  # 当前请求显式给出的单个 filelist 路径

    # 多个 filelist 需要保持声明顺序逐个回写，便于测试和 dry-run 对照原输入。
    list_source_lists = _plan_option(obj_plan_config, "source_lists") or []  # 当前请求显式给出的多个 filelist 路径列表

    # 环境变量覆盖映射最终会被序列化进计划对象，因此先单独缓存便于后续规整。
    dict_env_overrides = _plan_option(obj_plan_config, "env", {}) or {}  # 当前请求显式给出的环境变量覆盖映射

    # 最终计划对象会被 dry-run、execute 与测试同时消费，因此这里集中组装全部公开字段。
    return {
        "sources": [str(Path(path_item).resolve()) for path_item in list_sources],
        "vhdl_sources": _resolve_many(_plan_option(obj_plan_config, "vhdl_sources")),
        "source_list": str(Path(path_source_list).resolve()) if path_source_list else "",
        "source_lists": [str(Path(path_item).resolve()) for path_item in list_source_lists],
        "cmd_file": str(Path(obj_cmd_file).resolve()) if obj_cmd_file else "",
        "include_dirs": _resolve_many(_plan_option(obj_plan_config, "include_dirs")),
        "defines": _plan_option(obj_plan_config, "defines") or {},
        "libraries": _plan_option(obj_plan_config, "libraries") or [],
        "timescale": _plan_option(obj_plan_config, "timescale") or "",
        "debug": str(_plan_option(obj_plan_config, "debug", "all")),
        "kdb": bool(_plan_option(obj_plan_config, "kdb", True)),
        "coverage": _plan_option(obj_plan_config, "coverage") or [],
        "coverage_db": (
            str(smoke_plan_inputs.path_coverage_db)
            if smoke_plan_inputs.path_coverage_db
            else ""
        ),
        "coverage_args": {
            "vhdlan": smoke_plan_inputs.list_vhdl_coverage_args,
            "vlogan": smoke_plan_inputs.list_coverage_args,
            "compile": smoke_plan_inputs.list_coverage_args,
            "vhdl_compile": smoke_plan_inputs.list_vhdl_coverage_args,
            "verilog_compile": smoke_plan_inputs.list_coverage_args,
            "elaborate": smoke_plan_inputs.list_coverage_args,
            "simulate": smoke_plan_inputs.list_coverage_args,
        },
        "sv_libs": _resolve_many(_plan_option(obj_plan_config, "sv_libs")),
        "plusargs": _plan_option(obj_plan_config, "plusargs") or [],
        "seed": _plan_option(obj_plan_config, "seed"),
        "tools": smoke_plan_inputs.dict_tool_map,
        "env": {
            str(str_key): str(str_value)
            for str_key, str_value in dict_env_overrides.items()
        },
        "stage_args": {
            "vhdlan": _stage_args(_plan_option(obj_plan_config, "vhdlan_args")),
            "vlogan": _stage_args(_plan_option(obj_plan_config, "vlogan_args")),
            "vcs": _stage_args(_plan_option(obj_plan_config, "vcs_args")),
            "simv": _stage_args(_plan_option(obj_plan_config, "simv_args")),
            "fsdbreport": _stage_args(_plan_option(obj_plan_config, "fsdbreport_args")),
            "verdi": _stage_args(_plan_option(obj_plan_config, "verdi_args")),
        },
        "expected_artifacts": _normalize_expected_artifacts(
            smoke_plan_inputs.path_workdir,
            _plan_option(obj_plan_config, "expected_artifacts"),
            smoke_plan_inputs.path_dump,
        ),
        "step_timeout": _plan_option(obj_plan_config, "step_timeout"),
        "verdi_check": str(_plan_option(obj_plan_config, "verdi_check", "verdi")),
        "report_signal": _plan_option(obj_plan_config, "report_signal") or "",
        "workdir": str(smoke_plan_inputs.path_workdir),
        "log_dir": str(smoke_plan_inputs.path_log_dir),
        "top": str(_plan_option(obj_plan_config, "top", "top")),
        "clean": bool(_plan_option(obj_plan_config, "clean", False)),
        "steps": list_steps,
        "artifacts": {
            "simv": str(smoke_plan_inputs.path_simv),
            "dump": str(smoke_plan_inputs.path_dump),
            "rc": str(Path(obj_rc_file).resolve()) if obj_rc_file else "",
            **smoke_plan_inputs.dict_pli_artifacts,
        },
    }

# 根据 CLI 参数、manifest 默认值或显式配置对象构建 VCS/Verdi 冒烟执行计划。
def build_smoke_plan(*, config: SmokePlanConfig | None = None, **kwargs: Any) -> JsonDict:
    """
    构建最小 VCS/Verdi 冒烟计划。

    :param config: 可选的轻量配置对象；为空时从兼容关键字参数构造，dtype=SmokePlanConfig | None，unit=object
    :param kwargs: 兼容旧接口的关键字参数映射；键名必须来自 ``tuple_smoke_plan_option_names``，dtype=dict[str, Any]，unit=mapping
    :return: 返回包含步骤、产物、诊断元数据的计划对象，dtype=dict[str, Any]，unit=mapping
    :raises TypeError: 当兼容关键字参数包含未支持字段时抛出异常。
    :raises ValueError: 当没有任何源码输入时抛出异常。
    """

    # 先把新配置对象入口和旧关键字参数入口合并成同一种命名空间表示。
    smoke_plan_config_obj_smoke_plan_config = _coerce_smoke_plan_config(config=config, dict_kwargs=kwargs)  # 当前请求统一规整后的冒烟计划配置对象

    # 再解析工作目录、源码、覆盖率和 PLI 等基础事实，避免后续 helper 反复重复计算。
    smoke_plan_inputs_obj_smoke_plan_inputs = _resolve_smoke_plan_inputs(smoke_plan_config_obj_smoke_plan_config)  # 当前计划构建阶段共享的基础事实对象

    # compile、elaborate 和 simulate 三类命令统一在这里生成，保持阶段边界清晰。
    json_dict_command_block = _build_command_block(  # 当前计划的阶段命令字典
        smoke_plan_config_obj_smoke_plan_config,  # 让命令构造阶段读取统一配置
        smoke_plan_inputs_obj_smoke_plan_inputs,  # 让命令构造阶段复用已解析事实
    )

    # 最终波形校验步骤独立构建，避免 GUI 与非 GUI 模式分支挤回主入口函数。
    json_dict_verdi_step = _build_verdi_check_step(  # 当前计划的最终波形校验步骤对象
        smoke_plan_config_obj_smoke_plan_config,  # 让波形校验阶段沿用同一份配置
        smoke_plan_inputs_obj_smoke_plan_inputs,  # 让波形校验阶段复用前面路径解析结果
    )

    # 步骤列表统一按顺序组装，确保 compile -> simulate -> load-check 链路一眼可读。
    list_steps = _build_plan_steps(  # 当前计划最终组装完成的执行步骤列表
        smoke_plan_inputs_obj_smoke_plan_inputs,  # 让步骤组装阶段读取源码与目录事实
        json_dict_command_block,  # 提供前面已经生成好的阶段命令集合
        json_dict_verdi_step,  # 把最终波形校验步骤接到步骤链末尾
    )

    # 最后把基础事实、阶段命令和步骤列表重新收口成最终计划对象。
    return _build_plan_report(
        smoke_plan_config_obj_smoke_plan_config,
        smoke_plan_inputs_obj_smoke_plan_inputs,
        list_steps,
    )

# 把 manifest 中的相对路径列表统一锚定到 manifest 所在目录
def _manifest_path_list(path_base: Path, list_values: list[str]) -> list[Path]:
    """
    解析 manifest 中的相对路径列表。

    :param path_base: manifest 所在目录，作为相对路径的解析根，dtype=Path，unit=path
    :param list_values: manifest 里声明的路径字符串列表，dtype=list[str]，unit=collection
    :return: 返回规整成绝对路径的 Path 列表，dtype=list[Path]，unit=collection
    """

    # 逐项拼接 manifest 目录并 resolve，保证相对路径在不同调用目录下仍然稳定
    return [(path_base / str_item).resolve() for str_item in list_values]

# 规范化 manifest 里的环境变量映射，重点处理 LD_LIBRARY_PATH 的相对路径展开
def _manifest_env(path_base: Path, dict_values: dict[str, str]) -> dict[str, str]:
    """
    解析 manifest 的环境变量映射。

    :param path_base: manifest 所在目录，用于补全相对库路径，dtype=Path，unit=path
    :param dict_values: manifest 里声明的环境变量映射，dtype=dict[str, str]，unit=mapping
    :return: 返回适合直接传给 subprocess 的环境变量字典，dtype=dict[str, str]，unit=mapping
    """

    # 新建结果字典承接规整后的环境变量，避免修改调用方传入对象
    dict_env: dict[str, str] = {}  # 解析并补全后的环境变量映射

    # 逐项处理环境变量，让特殊字段和普通字段都能走同一个归一化入口
    for str_key, str_value in dict_values.items():

        # 先显式转成字符串，避免 JSON 数值或 Path 对象直接泄漏到环境变量字典里
        str_text = str(str_value)  # 当前环境变量规整后的字符串值

        # LD_LIBRARY_PATH 需要把相对路径锚定到 manifest 目录，并按本机分隔符重新拼接
        if str_key == "LD_LIBRARY_PATH":

            # 兼容 Windows 风格分号和 POSIX 风格冒号两类输入
            str_separator = ";" if ";" in str_text else ":"  # 当前原始 LD_LIBRARY_PATH 使用的分隔符

            # 使用独立列表保存规整后的库目录，避免中途拼接字符串时丢失空项过滤逻辑
            list_parts: list[str] = []  # 当前 LD_LIBRARY_PATH 解析出的绝对路径片段列表

            # 逐段展开库目录，忽略空字符串，避免生成多余的路径分隔符
            for str_item in str_text.split(str_separator):

                # 空片段既不能指向稳定路径，也会污染最终环境变量，因此直接跳过
                if not str_item:

                    # 当前片段为空，不参与环境变量重建
                    continue

                # 把单个库目录先包装成 Path，后续再决定是否需要锚定到 manifest 目录
                path_item = Path(str_item)  # 当前库目录片段对应的路径对象

                # 相对路径需要落到 manifest 目录下解析，绝对路径则原样保留
                list_parts.append(str(path_item if path_item.is_absolute() else (path_base / path_item).resolve()))

            # 使用当前平台的路径分隔符重新拼接，确保传给 subprocess 的值可直接使用
            dict_env[str(str_key)] = os.pathsep.join(list_parts)  # 规整后回写的 LD_LIBRARY_PATH 文本

        # 普通环境变量不需要路径语义处理，直接保留其字符串值即可
        else:

            # 其他环境变量保持原始文本语义，避免过度解释导致值失真
            dict_env[str(str_key)] = str_text  # 普通环境变量保持的原始字符串值

    # 返回规整后的环境变量映射，供 dry-run 诊断和 execute 环境注入共用
    return dict_env

# 校验 manifest 顶层字段类型，尽早阻断结构错误，避免在计划构建深处出现难懂异常
def validate_manifest(dict_data: JsonDict) -> None:
    """
    校验 manifest 根对象的结构与字段类型。

    :param dict_data: 由 manifest JSON 解析得到的顶层对象，dtype=dict[str, Any]，unit=mapping
    :return: 当前函数只做验证和异常抛出，不返回业务值。
    :raises ValueError: 当 manifest 字段类型与约定不符时抛出异常。
    """

    # 这些字段都要求是列表，因为后续计划构建会按顺序逐项迭代它们
    tuple_list_fields = (
        "sources",  # 显式源码条目列表
        "source_lists",  # filelist 文件列表
        "include_dirs",  # 编译头文件搜索目录列表
        "libraries",  # 逻辑库名称列表
        "coverage",  # 控制 compile/elaborate/simulate 是否携带 ``-cm`` 的维度选择
        "sv_libs",  # DPI 共享库列表
        "plusargs",  # 追加在 simv 命令尾部的业务 plusargs 序列
        "vhdlan_args",  # VHDL 分析阶段额外参数列表
        "vlogan_args",  # Verilog 编译阶段额外参数列表
        "vcs_args",  # elaboration 阶段额外参数列表
        "simv_args",  # 运行 simv 可执行文件时追加的额外参数列表
        "fsdbreport_args",  # 非 GUI fsdbreport 检查阶段追加的额外参数列表
        "verdi_args",  # 最终波形加载检查阶段透传给 Verdi 的额外参数列表
    )  # manifest 中必须是列表的字段集合

    # 逐个验证列表字段，确保 JSON 形状满足后续构建逻辑的消费方式
    for str_field in tuple_list_fields:

        # 字段存在但不是列表时立即报错，避免后续按序遍历时抛出更模糊的异常
        if str_field in dict_data and not isinstance(dict_data[str_field], list):

            # 使用精确字段名反馈类型错误，便于调用方快速定位 manifest 配置问题
            raise ValueError(f"> ERR: [Python] manifest {str_field} must be a list")

    # 这些字段后续都按映射读取键值，因此必须是对象
    for str_field in ("defines", "tools", "env", "expected_artifacts"):

        # 字段存在但不是对象时立即阻断，避免 ``.items()`` 等操作在运行期炸开
        if str_field in dict_data and not isinstance(dict_data[str_field], dict):

            # 精确指出对象字段的类型错误，保持错误信息和测试预期都足够明确
            raise ValueError(f"> ERR: [Python] manifest {str_field} must be an object")

    # ``sources`` 同时支持字符串和对象两类条目，因此需要逐项做二级验证
    for obj_source_item in dict_data.get("sources", []):

        # 条目不是字符串也不是对象时，后续无法稳定解析语言与路径
        if not isinstance(obj_source_item, (str, dict)):

            # sources 条目只支持最小字符串简写或显式对象写法
            raise ValueError("> ERR: [Python] manifest sources entries must be strings or objects")

        # 使用对象写法时，必须显式给出 path 字段，否则无法定位源码文件
        if isinstance(obj_source_item, dict) and "path" not in obj_source_item:

            # 缺少 path 意味着对象来源没有最基本的文件定位信息
            raise ValueError("> ERR: [Python] manifest source objects must include path")

# 从 JSON manifest 构建完整的冒烟计划，并允许调用方对少数字段做运行时覆盖
def build_smoke_plan_from_manifest(
    *,
    manifest: Path,
    workdir: Path | None = None,
    **overrides: Any,
) -> JsonDict:
    """
    根据 manifest 文件构建最小 VCS/Verdi 冒烟计划。

    :param manifest: 描述源码、工具覆盖和执行选项的 manifest JSON 文件路径，dtype=Path，unit=path
    :param workdir: 可选的运行时工作目录覆盖；为空时读取 manifest 内字段或默认值，dtype=Path | None，unit=path
    :param overrides: 需要覆盖 manifest 某些字段的运行时参数，例如 ``auto_pli``，dtype=dict[str, Any]，unit=mapping
    :return: 返回与 ``build_smoke_plan`` 同结构的计划对象，dtype=dict[str, Any]，unit=mapping
    :raises ValueError: 当 manifest 不是对象根节点或字段类型不合法时抛出异常。
    """

    # 先解析 manifest 的绝对路径，保证后续所有相对路径都基于稳定目录展开
    path_manifest = manifest.resolve()  # manifest 文件的绝对路径

    # 记录 manifest 所在目录，供源码、库目录和 RC/UCLI 路径锚定复用
    path_manifest_dir = path_manifest.parent  # manifest 的父目录

    # 读取并解析 manifest JSON 文本，后续会再校验其是否为对象根节点
    obj_data = json.loads(path_manifest.read_text(encoding="utf-8"))  # manifest 解析后的 Python 对象

    # 顶层必须是对象，否则后续字段读取和类型校验都无法成立
    if not isinstance(obj_data, dict):

        # 根节点不是对象时没有可解释的键值结构，因此直接阻断计划构建
        raise ValueError("> ERR: [Python] manifest root must be an object")

    # 在真正构建计划前，先完成顶层字段类型验证，尽早发现结构性错误
    validate_manifest(obj_data)

    # 把源码按语言拆分成 Verilog/SystemVerilog 与 VHDL 两个列表，以适配不同编译阶段
    list_sv_sources: list[Path] = []  # manifest 解析出的 Verilog/SystemVerilog 源文件列表

    # 这份列表最终会直接决定是否生成 compile-vhdl 步骤，因此它的职责比普通分类缓存更强。
    list_vhdl_sources: list[Path] = []  # 只有被识别成 VHDL 的源码才会落进这里，后续据此插入独立分析阶段。

    # 逐项解析 sources 条目，兼容纯字符串和包含 language 字段的对象写法
    for obj_source_item in obj_data.get("sources", []):

        # 字符串条目只提供路径，语言默认根据文件后缀推断
        if isinstance(obj_source_item, str):

            # 把字符串路径锚定到 manifest 所在目录，得到可复用的源码绝对路径
            path_source = (path_manifest_dir / obj_source_item).resolve()  # 当前 sources 条目解析出的源码路径

            # 语言优先根据后缀推断，保持简写形式的最小配置体验
            str_language = path_source.suffix.lower().lstrip(".")  # 当前源码条目推断出的语言标识

        # 对象条目允许显式声明 language，并保留 path 的相对路径语义
        else:

            # 对象条目必须包含 path，这里直接把 path 相对 manifest 目录展开成绝对路径
            path_source = (path_manifest_dir / obj_source_item["path"]).resolve()  # 当前对象式源码条目解析出的路径

            # 显式 language 优先级高于后缀推断；未给出时仍回落到后缀
            str_language = str(obj_source_item.get("language", path_source.suffix.lower().lstrip("."))).lower()  # 当前源码条目的归一化语言标识

        # language 字段或后缀命中 VHDL 族时，交给 vhdlan 阶段处理
        if str_language in {"vhdl", "vhd"} or path_source.suffix.lower() in {".vhd", ".vhdl"}:

            # 把当前源码归入 VHDL 列表，后续会生成 compile-vhdl 步骤
            list_vhdl_sources.append(path_source)

        # 其他语言统一视为 Verilog/SystemVerilog，交给 vlogan 阶段处理
        else:

            # 将当前源码加入 Verilog/SystemVerilog 列表，供 compile 或 compile-verilog 使用
            list_sv_sources.append(path_source)

    # 多 filelist 统一锚定到 manifest 目录，保持与 sources 相同的路径语义
    list_source_lists = _manifest_path_list(path_manifest_dir, obj_data.get("source_lists", []))  # manifest 解析出的多个 filelist 绝对路径

    # 用局部函数统一处理运行时覆盖与 manifest 默认值的优先级，避免散落的判空逻辑
    def option(str_name: str, obj_default: Any = None) -> Any:
        """
        读取某个字段的运行时覆盖值或 manifest 默认值。

        :param str_name: 需要查询的字段名，dtype=str，unit=identifier
        :param obj_default: manifest 中也缺失该字段时采用的兜底值，dtype=Any，unit=object
        :return: 返回运行时覆盖值、manifest 值或兜底值三者之一，dtype=Any，unit=object
        """

        # overrides 中显式给出的非 None 值优先级最高，用于修正 manifest 中的局部开关
        obj_value = overrides.get(str_name)  # 当前字段的运行时覆盖值

        # 覆盖值存在时直接采用，避免 manifest 原值继续生效
        if obj_value is not None:

            # 返回调用方显式指定的覆盖值，让运行时行为可预测
            return obj_value

        # 否则回落到 manifest 字段或函数提供的最终默认值
        return obj_data.get(str_name, obj_default)

    # 这里把 manifest 里的拆解结果重新组装成 build_smoke_plan 所需的显式参数集合。
    json_dict_plan = build_smoke_plan(  # 这个计划对象会统一承接 manifest 模式的所有默认值和覆盖值。
        sources=list_sv_sources,  # 这批源码会走 vlogan 编译路径，而不是 compile-vhdl 分支。
        source_lists=list_source_lists,  # manifest 里声明的 filelist 路径列表。
        vhdl_sources=list_vhdl_sources,  # 这批 VHDL 源文件会单独触发 compile-vhdl 步骤。
        include_dirs=_manifest_path_list(path_manifest_dir, obj_data.get("include_dirs", [])),  # manifest 中声明的头文件搜索目录列表。
        defines={str(str_key): str(str_value) for str_key, str_value in obj_data.get("defines", {}).items()},  # manifest 中声明的 ``+define+`` 宏映射。
        libraries=[str(obj_item) for obj_item in obj_data.get("libraries", [])],  # manifest 中声明的逻辑库名称列表。
        timescale=obj_data.get("timescale"),  # manifest 中声明的可选编译时标约束。
        debug=str(obj_data.get("debug", "all")),  # manifest 中声明的 ``-debug_access+`` 等级。
        kdb=bool(obj_data.get("kdb", True)),  # manifest 中声明是否保留 KDB 调试数据库生成开关。
        coverage=[str(obj_item) for obj_item in obj_data.get("coverage", [])],  # manifest 中声明的覆盖率维度列表。
        sv_libs=_manifest_path_list(path_manifest_dir, obj_data.get("sv_libs", [])),  # manifest 中声明的 DPI 共享库路径列表。
        plusargs=[str(obj_item) for obj_item in obj_data.get("plusargs", [])],  # manifest 中声明的运行期 plusargs 列表。
        seed=obj_data.get("seed"),  # manifest 中声明的可选仿真随机种子。
        workdir=(workdir or (path_manifest_dir / obj_data.get("workdir", "run"))).resolve(),  # manifest 默认工作目录会相对 manifest 目录解析后再规范化。
        top=str(option("top", "top")),  # manifest 或 override 里声明的顶层模块名。
        dump_name=str(option("dump_name", "waves.fsdb")),  # manifest 或 override 里声明的 FSDB 波形文件名。
        tools={str(str_key): str(str_value) for str_key, str_value in obj_data.get("tools", {}).items()},  # manifest 中声明的工具路径覆盖映射。
        env=_manifest_env(path_manifest_dir, obj_data.get("env", {})),  # manifest 中声明并按根目录修正后的环境变量覆盖映射。
        vhdlan_args=[str(obj_item) for obj_item in obj_data.get("vhdlan_args", [])],  # manifest 中声明的额外 vhdlan 参数列表。
        vlogan_args=[str(obj_item) for obj_item in obj_data.get("vlogan_args", [])],  # manifest 在编译 Verilog/SystemVerilog 时额外追加的命令片段。
        vcs_args=[str(obj_item) for obj_item in obj_data.get("vcs_args", [])],  # manifest 在 elaboration 阶段额外追加的链接与调试开关。
        simv_args=[str(obj_item) for obj_item in obj_data.get("simv_args", [])],  # manifest 中声明的额外 simv 运行参数列表。
        fsdbreport_args=[str(obj_item) for obj_item in obj_data.get("fsdbreport_args", [])],  # manifest 在波形报告检查阶段额外追加的查询参数。
        verdi_args=[str(obj_item) for obj_item in obj_data.get("verdi_args", [])],  # manifest 在 Verdi 打开阶段额外追加的交互参数。
        expected_artifacts=obj_data.get("expected_artifacts"),  # manifest 中声明的产物存在性与最小字节数校验规则。
        step_timeout=option("step_timeout"),  # manifest 或 override 里声明的单阶段超时秒数。
        rc_file=(path_manifest_dir / obj_data["rc_file"]).resolve() if obj_data.get("rc_file") else None,  # manifest 中声明的 Verdi RC 文件路径。
        cmd_file=(path_manifest_dir / obj_data["cmd_file"]).resolve() if obj_data.get("cmd_file") else None,  # manifest 中声明的 Verdi 命令脚本路径。
        pli_dir=(path_manifest_dir / obj_data["pli_dir"]).resolve() if obj_data.get("pli_dir") else None,  # manifest 中声明的显式 Novas PLI 目录路径。
        auto_pli=bool(option("auto_pli", True)),  # manifest 或 override 是否允许自动探测 PLI 目录。
        verdi_check=str(option("verdi_check", "verdi")),  # manifest 或 override 选择的 Verdi 校验模式。
        report_signal=option("report_signal"),  # manifest 或 override 指定的 fsdbreport 目标信号名。
        clean=bool(option("clean", False)),  # manifest 或 override 是否要求在运行前清理工作目录。
    )

    # 把 manifest 入口信息附加回计划对象，便于 dry-run 输出和测试回溯来源
    json_dict_plan["manifest"] = str(path_manifest)  # 回写 manifest 绝对路径，供 dry-run 和测试定位来源

    # 额外记录 manifest 目录，方便测试验证相对路径展开的锚点
    json_dict_plan["manifest_dir"] = str(path_manifest_dir)  # 回写 manifest 根目录，供相对路径展开验证使用

    # 返回补齐来源信息后的完整计划对象
    return json_dict_plan

# 解析 ``NAME`` 或 ``NAME=VALUE`` 形式的 CLI define 参数
def parse_define(str_value: str) -> tuple[str, str]:
    """
    拆解单个 define 文本。

    :param str_value: CLI 传入的单个 define 字符串，形如 ``NAME`` 或 ``NAME=VALUE``，dtype=str，unit=text
    :return: 返回宏名和宏值的二元组；无值宏的第二个元素为空字符串，dtype=tuple[str, str]，unit=collection
    """

    # 含等号时按照首次出现的位置拆分，允许值中继续包含其他等号
    if "=" in str_value:

        # 只按第一个等号分割，保持 ``A=B=C`` 这类值的后半部分完整
        str_key, str_define_value = str_value.split("=", 1)  # define 文本拆出的宏名与宏值

        # 返回拆解后的键和值，供 build_smoke_plan 继续组装 define 映射
        return str_key, str_define_value

    # 纯宏名形式默认返回空值，让上层用 ``+define+NAME`` 方式输出
    return str_value, ""

# 基于 dry-run 计划判断当前机器缺少哪些必须工具
def missing_tools(dict_plan: JsonDict) -> list[str]:
    """
    找出计划执行所需但当前环境缺失的工具。

    :param dict_plan: ``build_smoke_plan`` 生成的计划对象，dtype=dict[str, Any]，unit=mapping
    :return: 返回去重并排序后的缺失工具名列表，dtype=list[str]，unit=collection
    """

    # 先收集每个步骤引用到的命令入口，再统一做存在性判断和去重排序
    list_required: list[str] = []  # 各步骤声明的可执行程序候选列表

    # 按步骤顺序扫描命令入口，兼容相对 simv 路径、绝对路径和 PATH 查找三种情况
    for dict_step in dict_plan["steps"]:

        # 每个步骤的命令第一个元素就是实际的程序入口
        str_exe = dict_step["cmd"][0]  # 当前步骤使用的可执行程序名或路径

        # ``./simv`` 这类相对产物由计划执行阶段在工作目录里生成，不属于预先缺工具范畴
        if str_exe.startswith("./"):

            # 相对 simv 产物不参与 PATH 探测，直接跳过
            continue

        # 绝对路径命令需要检查文件是否存在，而不是走 PATH 查找
        if Path(str_exe).is_absolute():

            # 绝对路径不存在时把它记为缺失工具，帮助调用方直接看到坏路径
            if not Path(str_exe).exists():

                # 当前绝对路径不可达，因此记录到缺失工具列表中
                list_required.append(str_exe)

            # 绝对路径已经做完存在性判断，不需要再进入 PATH 分支
            continue

        # 普通工具名留待 PATH 查找，先放入候选列表
        list_required.append(str_exe)

    # 只返回当前环境确实找不到的工具名，并在结果层做去重与稳定排序
    return sorted({str_tool for str_tool in list_required if shutil.which(str_tool) is None})

# 为某个命令入口生成包装器诊断信息，帮助 dry-run 解释 wrapper 的真实来源
def wrapper_info(list_cmd: list[str]) -> JsonDict:
    """
    收集某个命令入口对应的包装器诊断信息。

    :param list_cmd: 完整命令参数列表，首元素为可执行程序名或路径，dtype=list[str]，unit=argv
    :return: 返回包含路径、存在性和首行文本的诊断对象，dtype=dict[str, Any]，unit=mapping
    """

    # 空命令没有任何诊断价值，直接返回空形态对象
    if not list_cmd:

        # 当前调用没有提供可执行入口，因此返回全空诊断结构
        return {"path": "", "exists": False, "first_line": ""}

    # 提取命令第一个元素，后续据此区分相对产物、PATH 工具名和绝对路径
    str_exe = list_cmd[0]  # 当前命令的可执行程序入口

    # ``./simv`` 这类相对产物在 dry-run 阶段通常还不存在，只返回其文本路径即可
    if str_exe.startswith("./"):

        # 相对产物路径不尝试读取文件内容，避免把工作目录创建责任提前到 dry-run
        return {"path": str_exe, "exists": False, "first_line": ""}

    # 普通工具名优先通过 PATH 解析；找不到时仍保留原文本便于诊断
    str_resolved = shutil.which(str_exe) or str_exe  # 当前工具名解析出的实际路径或原始文本

    # 包装成 Path 后即可统一执行 exists 和文本读取逻辑
    path_exe = Path(str_resolved)  # 当前命令入口对应的路径对象

    # 先构建基础诊断字典，文件首行稍后按存在性和读取情况再补全。
    dict_json_info = {"path": str(path_exe), "exists": path_exe.exists(), "first_line": ""}  # 当前命令入口的基础诊断对象。

    # 只有真实文件存在时才尝试读取首行，避免对纯命令名误做文本打开
    if path_exe.exists():

        # 首行信息用于判断 Synopsys wrapper 是否存在 ``#!/bin/sh -h`` 等兼容性问题
        try:

            # 只读取首行即可满足 wrapper 诊断需要，忽略编码错误以适配厂商脚本
            dict_json_info["first_line"] = path_exe.read_text(encoding="utf-8", errors="ignore").splitlines()[0]  # 包装器脚本首行文本。

        # 空文件、读取失败或编码异常都统一退回空首行，避免诊断流程被次要问题打断
        except (IndexError, OSError, UnicodeDecodeError):

            # 当前入口无法安全读取首行，因此保留空字符串作为占位
            dict_json_info["first_line"] = ""  # 无法安全读取首行时保留的空字符串占位。

    # 返回包装器诊断结果，供 dry-run 输出和 execution_command 逻辑共同使用
    return dict_json_info

# 针对 Synopsys ``#!/bin/sh -h`` 或特定 ``/bin/sh`` wrapper 生成更稳妥的执行命令
def execution_command(
    list_cmd: list[str],
    shell_which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """
    为给定命令构建实际执行时使用的命令行。

    :param list_cmd: 原始计划步骤命令列表，dtype=list[str]，unit=argv
    :param shell_which: 可选的 shell 定位函数，便于测试注入 bash 路径，dtype=Callable[[str], str | None]，unit=function
    :return: 返回最终交给 subprocess 的命令列表，dtype=list[str]，unit=argv
    """

    # 空命令或工作目录内相对产物命令不需要 wrapper 重写，直接原样返回
    if not list_cmd or list_cmd[0].startswith("./"):

        # 当前命令不触发 shell 兼容性包装逻辑，保留原始参数序列
        return list_cmd

    # 先读取包装器诊断信息，后续依赖路径和首行内容判断是否需要切换到 bash
    json_dict_wrapper_info = wrapper_info(list_cmd)  # 当前命令入口的包装器诊断信息。

    # 首行内容是识别 ``#!/bin/sh -h`` 和 Synopsys wrapper 的关键依据
    str_first_line = json_dict_wrapper_info["first_line"].strip()  # 当前可执行脚本首行文本。

    # 路径文本归一化后可用于识别 ``synopsys/...`` 这类 wrapper 安装目录
    str_path_text = json_dict_wrapper_info["path"].replace("\\", "/").lower()  # 当前命令入口路径的归一化文本。

    # 命中已知会被 dash/ksh 误解释的 wrapper 形态时，改为显式用 bash 启动
    if str_first_line == "#!/bin/sh -h" or (str_first_line == "#!/bin/sh" and "synopsys" in str_path_text):

        # 优先通过 shell_which 找真实 bash；找不到时仍保留 ``bash`` 让系统自行解析
        str_shell = shell_which("bash") or "bash"  # 兼容 Synopsys wrapper 的 bash 入口

        # 把原脚本路径作为 bash 的第一个参数，后续命令参数原样透传
        return [str_shell, json_dict_wrapper_info["path"], *list_cmd[1:]]

    # 未命中已知兼容性风险时直接沿用原命令，避免无谓的 shell 包装
    return list_cmd

# 保护性检查工作目录是否位于足够具体的位置，防止 clean 误删危险路径
def _safe_to_clean(path_value: Path) -> bool:
    """
    判断某个目录是否足够安全，可用于递归清理。

    :param path_value: 需要检查的目录路径，dtype=Path，unit=path
    :return: 当目录名非空且路径层级足够深时返回 True，否则返回 False，dtype=bool，unit=flag
    """

    # 先把路径解析成绝对形式，避免 ``.``、``..`` 等相对段掩盖真实层级
    path_resolved = path_value.resolve()  # 待清理目录的绝对路径

    # 同时约束目录名和层级，尽量避免误把根目录或过浅目录交给递归删除
    return path_resolved.name not in {"", ".", ".."} and len(path_resolved.parts) >= 3

# 统计一组产物文件的存在性和字节数，用于 dry-run 诊断与 execute 结果回填
def artifact_status(dict_artifacts: dict[str, Path | str]) -> dict[str, JsonDict]:
    """
    统计产物文件的存在状态与大小。

    :param dict_artifacts: 键为产物名、值为路径文本或 Path 的映射，dtype=dict[str, Path | str]，unit=mapping
    :return: 返回每个产物对应的状态对象，包含 path、state 和 bytes 字段，dtype=dict[str, dict[str, Any]]，unit=mapping
    """

    # 使用独立状态字典承接检查结果，避免修改调用方传入的产物映射
    dict_status: dict[str, JsonDict] = {}  # 当前产物集合的状态摘要映射

    # 逐个检查产物路径，空字符串条目直接跳过，避免人为制造无意义的 missing 记录
    for str_name, obj_value in dict_artifacts.items():

        # 某些产物字段允许为空字符串表示未启用，例如 rc，因此这里直接跳过
        if not obj_value:

            # 当前产物未启用或没有真实路径，不参与状态统计
            continue

        # 统一包装成 Path，便于做 exists 和 stat 检查
        path_item = Path(obj_value)  # 当前产物对应的路径对象

        # 文件不存在时记为 missing，并显式给出 0 字节，方便上层统一展示
        if not path_item.exists():

            # 记录缺失状态，帮助执行摘要快速区分“没产出”和“产出为空文件”
            dict_status[str_name] = {"path": str(path_item), "state": "missing", "bytes": 0}  # 缺失产物的标准状态对象

            # 缺失文件已经确定结果，不需要继续读取大小
            continue

        # 对已存在文件读取字节数，用于区分零字节和正常非空两种状态
        int_size = path_item.stat().st_size  # 当前产物文件的字节大小

        # 把存在状态和大小写入结果，后续可直接用于 summarize 和 artifact gate
        dict_status[str_name] = {
            "path": str(path_item),  # 当前产物文件路径
            "state": "present" if int_size > 0 else "zero",  # 当前产物的存在性状态
            "bytes": int_size,  # 当前产物实际写出的字节数
        }

    # 返回产物状态映射，供 dry-run 诊断和 execute 后摘要复用
    return dict_status

# 按 expected_artifacts 规则检查产物是否满足最小字节阈值
def expected_artifact_status(dict_expected_artifacts: dict[str, JsonDict]) -> dict[str, JsonDict]:
    """
    检查预期产物是否满足存在性与最小字节数要求。

    :param dict_expected_artifacts: 由 ``_normalize_expected_artifacts`` 生成的产物规则映射，
        dtype=dict[str, dict[str, Any]]，unit=mapping
    :return: 返回每个产物对应的校验结果，附带 path、state、bytes、min_bytes 与 status 字段，dtype=dict[str, dict[str, Any]]，unit=mapping
    """

    # 使用独立字典保存校验结果，便于执行阶段在失败时整体序列化输出
    dict_status: dict[str, JsonDict] = {}  # 预期产物逐项校验后的结果映射

    # 逐个检查预期产物，统一处理缺失、零字节和通过三种主路径
    for str_name, dict_spec in dict_expected_artifacts.items():

        # path 与 min_bytes 都已经在归一化阶段完成兜底，这里直接读取即可
        path_item = Path(dict_spec["path"])  # 当前预期产物的目标路径

        # 最小字节阈值转成整数后再比较，避免 JSON 字段类型漂移影响校验结果
        int_min_bytes = int(dict_spec.get("min_bytes", 1))  # 当前预期产物要求的最小字节数

        # 目标文件完全不存在时直接记为 failed，并携带 0 字节结果
        if not path_item.exists():

            # 缺失文件无需再做大小比较，直接写入失败摘要
            dict_status[str_name] = {
                "path": str(path_item),  # 原样回写目标路径，便于日志和失败摘要直接定位缺失文件。
                "state": "missing",  # 缺失场景固定标记为 missing，供后续 artifact gate 直接识别。
                "bytes": 0,  # 缺失文件统一记录为零字节
                "min_bytes": int_min_bytes,  # 当前规则要求的最小字节数
                "status": "failed",  # 缺失文件对应的校验结论
            }

            # 当前产物已经得到最终失败结论，不再进入后续大小检查
            continue

        # 已存在文件继续读取大小，用真实字节数区分 zero 假成功与真正写出的产物。
        int_size = path_item.stat().st_size  # 当前产物在磁盘上的实际字节数。

        # 先把阈值判断提炼成布尔值，后面生成 state/status 摘要时都复用这一份结论。
        bool_passed = int_size >= int_min_bytes  # 当前产物是否已经达到约定的最小字节门槛。

        # 这里记录的是“文件存在”场景下的细分结果，后续执行摘要会直接展示 zero/present 差异。
        dict_status[str_name] = {
            "path": str(path_item),  # 沿用原始目标路径，便于通过结果反查真实产物位置。
            "state": "present" if int_size > 0 else "zero",  # 当前产物存在但是否为空的状态
            "bytes": int_size,  # 把真实字节数回写给调用方，方便区分零字节和小于阈值两类失败。
            "min_bytes": int_min_bytes,  # 保留本条产物契约的最小字节门槛，供报告同时展示期望值。
            "status": "passed" if bool_passed else "failed",  # 当前产物是否满足阈值要求
        }

    # 返回预期产物校验结果，供 simulate 之后的 artifact gate 和 execute 摘要共用
    return dict_status

# 汇总 dry-run 所需的工具缺失、wrapper 信息、执行命令和当前 shell 环境
def plan_diagnostics(dict_plan: JsonDict) -> JsonDict:
    """
    构建计划的辅助诊断信息。

    :param dict_plan: ``build_smoke_plan`` 生成的计划对象，dtype=dict[str, Any]，unit=mapping
    :return: 返回包含缺工具、步骤 wrapper、产物初始状态和 shell 变量的诊断对象，dtype=dict[str, Any]，unit=mapping
    """

    # 直接把多个诊断维度汇总成单一对象，方便 dry-run JSON 一次性输出完整上下文
    return {
        "missing_tools": missing_tools(dict_plan),
        "steps": [
            {
                "name": dict_step["name"],
                "cwd": dict_step.get("cwd", dict_plan["workdir"]),
                "wrapper": wrapper_info(dict_step["cmd"]),
                "execution_cmd": execution_command(dict_step["cmd"]),
                "log": dict_step["log"],
            }
            for dict_step in dict_plan["steps"]
        ],
        "artifacts": artifact_status(dict_plan["artifacts"]),
        "shell": {"SHELL": os.environ.get("SHELL", ""), "COMSPEC": os.environ.get("COMSPEC", "")},
    }

# 统一构造单个已执行步骤的结果对象，避免 run_plan 内部反复堆叠长字典字面量
def _step_result_record(
    *,
    # 先记录步骤身份和退出状态，便于执行摘要快速定位失败点。
    str_name: str,
    int_returncode: int,

    # 这一组字段保留计划声明命令与真正交给 subprocess 的执行命令。
    list_cmd_raw: list[str],
    list_execution_cmd: list[str],

    # 这组路径字段用于回放步骤执行现场与日志落点。
    path_cwd: Path,
    path_log: Path,

    # 最后单独承接本步骤捕获到的合并输出文本。
    str_output: str,
) -> JsonDict:
    """
    构造单个步骤执行结果对象。

    :param str_name: 当前步骤名称，dtype=str，unit=identifier
    :param int_returncode: 当前步骤退出码，dtype=int，unit=exit code
    :param list_cmd_raw: 计划中声明的原始命令列表，dtype=list[str]，unit=collection
    :param list_execution_cmd: 实际交给 subprocess 的执行命令列表，dtype=list[str]，unit=collection
    :param path_cwd: 当前步骤执行目录，dtype=Path，unit=path
    :param path_log: 当前步骤日志文件路径，dtype=Path，unit=path
    :param str_output: 当前步骤捕获到的合并输出文本，dtype=str，unit=text
    :return: 返回标准化步骤结果对象，dtype=dict[str, Any]，unit=mapping
    """

    # 返回统一的步骤结果对象，供上层结果列表直接复用。
    return {
        # 先回写步骤身份和退出状态，方便执行摘要快速筛选失败阶段。
        "name": str_name,
        "returncode": int_returncode,

        # 再保留计划原始命令与实际执行命令，便于 dry-run 与 execute 对照。
        "cmd": list_cmd_raw,
        "execution_cmd": list_execution_cmd,

        # 最后补齐工作目录、日志路径和输出文本，完整保留执行现场。
        "cwd": str(path_cwd),
        "log": str(path_log),
        "output": str_output,
    }

# 统一构造 simulate 后产物守门失败时附加的伪步骤结果对象
def _artifact_check_failure_record(
    *,
    path_cwd: Path,
    str_message: str,
    dict_failed: JsonDict,
) -> JsonDict:
    """
    构造 simulate 后产物守门失败的伪步骤结果对象。

    :param path_cwd: simulate 步骤执行目录，dtype=Path，unit=path
    :param str_message: 守门失败摘要文本，dtype=str，unit=text
    :param dict_failed: 未通过检查的产物状态映射，dtype=dict[str, Any]，unit=mapping
    :return: 返回标准化 artifact-check 失败结果对象，dtype=dict[str, Any]，unit=mapping
    """

    # 返回统一的 artifact-check 失败对象，保持结果结构对调用方友好一致。
    return {
        "name": "artifact-check",
        "returncode": 1,
        "cmd": [],
        "execution_cmd": [],
        "cwd": str(path_cwd),
        "log": "",
        "output": str_message + "\n" + json.dumps(dict_failed, sort_keys=True),
    }

# 按计划顺序执行各阶段命令，并在 simulate 后执行产物守门逻辑
def run_plan(dict_plan: JsonDict, *, step_timeout: int = 300) -> list[JsonDict]:
    """
    执行最小 VCS/Verdi 冒烟计划。

    :param dict_plan: ``build_smoke_plan`` 生成的计划对象，dtype=dict[str, Any]，unit=mapping
    :param step_timeout: 单步骤默认超时秒数；当计划中声明 ``step_timeout`` 时由计划值覆盖，dtype=int，unit=second
    :return: 返回每个已执行步骤的结果列表；一旦某步失败或产物守门失败就提前停止，dtype=list[dict[str, Any]]，unit=collection
    :raises RuntimeError: 当启用 ``clean`` 但工作目录被判定为不安全时抛出异常。
    """

    # 使用列表保存每个阶段的执行结果，确保失败现场和日志路径都能完整带回给上层
    list_results: list[JsonDict] = []  # 已执行步骤的结果对象列表

    # 先拿到工作目录与日志目录的 Path 形式，便于统一做 mkdir 和清理
    path_workdir = Path(dict_plan["workdir"])  # 当前计划的工作目录路径对象

    # 日志目录在执行前必须存在，这里先独立解析出来方便后续统一创建
    path_log_dir = Path(dict_plan["log_dir"])  # 当前计划的日志目录路径对象

    # 调用方要求 clean 且工作目录已存在时，先做安全检查再删除旧内容
    if dict_plan.get("clean") and path_workdir.exists():

        # 清理前先确认目标目录足够具体，避免递归删除误命中过浅或危险路径
        if not _safe_to_clean(path_workdir):

            # 一旦目录不安全就直接阻断执行，避免 destructive 行为发生
            raise RuntimeError(f"> ERR: [Python] refusing to clean unsafe workdir: {path_workdir}")

        # 通过安全检查后再递归删除旧工作目录，保证后续执行从干净状态开始
        shutil.rmtree(path_workdir)

    # 无论 clean 是否触发，执行前都要确保工作目录存在
    path_workdir.mkdir(parents=True, exist_ok=True)

    # 同样预先创建日志目录，避免首个步骤写日志时因父目录不存在而失败
    path_log_dir.mkdir(parents=True, exist_ok=True)

    # 按计划顺序逐步执行 compile/elaborate/simulate/verdi，任何失败都会中止后续步骤
    for dict_step in dict_plan["steps"]:

        # 先根据 wrapper 规则生成真正要交给 subprocess 的命令
        list_cmd = execution_command(dict_step["cmd"])  # 当前步骤最终生效的执行命令

        # 每一步都允许覆盖 cwd；未覆盖时回落到计划工作目录
        path_cwd = Path(dict_step.get("cwd", dict_plan["workdir"]))  # 当前步骤执行时采用的工作目录

        # 某些工具会在步骤专属 cwd 下写中间文件，因此这里也确保步骤目录存在
        path_cwd.mkdir(parents=True, exist_ok=True)

        # 正常路径下执行子进程并捕获标准输出和标准错误到同一缓冲区
        try:

            # 复制进程环境后叠加计划注入变量，避免直接污染当前 Python 进程的 os.environ
            dict_run_env = os.environ.copy()  # 传给 subprocess 的基础环境变量映射

            # 把计划声明的环境变量覆盖到执行环境中，供许可证、LD_LIBRARY_PATH 等场景使用
            dict_run_env.update(dict_plan.get("env", {}))

            # 统一开启 text 模式并合并 stderr，方便日志落盘和后续 JSON 摘要复用同一份文本
            completed_process_step: subprocess.CompletedProcess[str] = subprocess.run(  # 当前步骤实际执行返回的 CompletedProcess 对象
                list_cmd,  # 当前步骤准备执行的真实命令序列。
                cwd=path_cwd,  # 当前步骤约定的工作目录。
                env=dict_run_env,  # 当前步骤实际使用的环境变量映射。
                text=True,  # 统一按文本模式读取 stdout/stderr。
                stdout=subprocess.PIPE,  # 把标准输出完整捕获回来，供日志落盘和结果摘要复用。
                stderr=subprocess.STDOUT,  # 把标准错误并入 stdout，避免日志拆成两路后难以复盘。
                timeout=dict_plan.get("step_timeout") or step_timeout,  # 当前步骤最终采用的超时秒数。
            )

            # 成功返回后直接读取退出码，供步骤级失败判定使用
            int_returncode = completed_process_step.returncode  # 当前步骤子进程退出码

            # 统一使用捕获的 stdout 文本作为步骤输出日志内容
            str_output = completed_process_step.stdout  # 当前步骤合并后的标准输出文本

        # 超时场景需要人为构造失败结果，同时尽量保留已捕获的部分输出
        except subprocess.TimeoutExpired as exc:

            # 使用 -9 作为超时哨兵值，便于上层一眼区分普通工具失败与超时中断
            int_returncode = -9  # 当前步骤超时时采用的约定退出码

            # subprocess 可能返回 bytes 或 str，两者都需要统一成最终文本日志
            obj_captured_raw: str | bytes = exc.stdout or ""  # 超时异常中附带的部分已捕获输出

            # bytes 输出需要显式解码，避免直接拼接到字符串时报类型错误
            if isinstance(obj_captured_raw, bytes):

                # 使用 replace 策略容忍工具输出里的非 UTF-8 字节，确保日志仍可落盘
                str_captured_output = obj_captured_raw.decode("utf-8", errors="replace")  # 解码后的超时残留输出文本

            # 字符串输出可以直接复用，不需要再做额外解码。
            else:

                # 这里的捕获值已经是文本，因此可直接作为最终日志片段使用。
                str_captured_output = obj_captured_raw  # 已确认是字符串的超时残留输出文本

            # 以计划声明超时值优先，回退到函数默认超时值，保持错误提示与真实约束一致
            int_effective_timeout = dict_plan.get("step_timeout") or step_timeout  # 当前步骤实际生效的超时秒数

            # 在已有输出尾部追加标准化超时说明，帮助人工快速识别失败原因
            str_output = str_captured_output + f"\nstep timed out after {int_effective_timeout} seconds\n"  # 追加标准化超时尾注后的输出文本

        # 每一步都按计划声明的日志路径落盘，确保后续失败排查不依赖内存中的结果列表
        path_log = Path(dict_step["log"])  # 当前步骤日志文件路径对象

        # 再次保证日志父目录存在，以容忍调用方自定义到非默认目录的情况
        path_log.parent.mkdir(parents=True, exist_ok=True)

        # 统一以 UTF-8 文本写日志，编码异常时用 replace 保证落盘不被中断
        path_log.write_text(str_output, encoding="utf-8", errors="replace")

        # 把步骤名、退出码、原始命令和执行命令都记录下来，方便后续回放与对比
        list_results.append(
            _step_result_record(
                str_name=dict_step["name"],
                int_returncode=int_returncode,
                list_cmd_raw=dict_step["cmd"],
                list_execution_cmd=list_cmd,
                path_cwd=path_cwd,
                path_log=path_log,
                str_output=str_output,
            )
        )

        # 当前步骤失败后不应继续执行后续阶段，否则容易掩盖首个根因
        if int_returncode != 0:

            # 立即停止后续步骤执行，把首个失败现场完整交回给调用方
            break

        # simulate 成功后必须先检查期望产物，再决定是否继续进入 Verdi/FSDB report 阶段
        if dict_step["name"] == "simulate":

            # 没有显式 expected_artifacts 时，至少仍要强制检查默认 dump 产物存在且非空。
            dict_expected_artifacts = dict_plan.get("expected_artifacts") or {  # 缺省时至少要求 dump 产物存在且非零字节。
                "dump": {"path": dict_plan["artifacts"]["dump"], "min_bytes": 1}  # 默认只强制检查 dump 产物非空。
            }  # simulate 后最少需要成立的产物契约集合。

            # 这里统一计算 simulate 之后的产物校验结果，供后续是否继续加载波形判断。
            dict_artifact_check = expected_artifact_status(dict_expected_artifacts)  # simulate 之后的预期产物检查结果映射。

            # 只保留未通过检查的产物，作为提前终止后续 Verdi 加载的直接证据
            dict_failed = {
                str_name: dict_item  # 当前这个产物条目没有通过最小交付检查。
                for str_name, dict_item in dict_artifact_check.items()  # 逐项筛出 simulate 后仍未达标的关键产物。
                if dict_item["status"] != "passed"  # 只保留 failed 项，后续据此决定是否跳过 Verdi。
            }  # simulate 后未通过检查的产物集合

            # 任一关键产物缺失或零字节都说明加载检查没有意义，应在这里及时截断
            if dict_failed:

                # 用标准化文本解释为何跳过 Verdi，加上 JSON 失败详情便于机器继续消费
                str_message = "FSDB dump is missing or zero bytes after simulate; skipping Verdi load"  # simulate 后产物守门失败的摘要文本

                # 以伪步骤结果形式附加 artifact-check 失败，保持结果结构对调用方友好一致
                list_results.append(
                    _artifact_check_failure_record(
                        path_cwd=path_cwd,  # artifact-check 复用 simulate 所在执行目录。
                        str_message=str_message,  # 当前守门失败的摘要文本。
                        dict_failed=dict_failed,  # 未通过检查的产物状态映射。
                    )
                )

                # 产物守门失败后不再继续 Verdi/fsdbreport，避免把“没有波形”误报成工具加载失败
                break

    # 返回完整的已执行步骤结果列表，供 CLI 或上层调用者生成最终摘要
    return list_results

# 在 execute 模式下根据最终状态和关键产物情况给出通过/失败结论
def summarize_status(dict_output: JsonDict) -> str:
    """
    根据执行结果和产物状态归纳最终状态。

    :param dict_output: CLI 主流程组装的输出对象，dtype=dict[str, Any]，unit=mapping
    :return: 返回 ``passed``、``failed``、``dry-run`` 或 ``skipped`` 等状态文本，dtype=str，unit=identifier
    """

    # 只要当前状态不是 passed，就直接沿用上层已经得出的更具体结论
    if dict_output["status"] != "passed":

        # 当前结果已经被更早的逻辑标记为 failed/dry-run/skipped，无需再次改写
        return dict_output["status"]

    # 只有 passed 候选状态才需要进一步确认 dump 产物是否真实存在且非空
    dict_artifacts = dict_output.get("artifact_status", {})  # execute 模式回填的产物状态映射

    # dump 是最关键的产物，没有它就不能认为 VCS/Verdi 波形链路真正通过
    dict_dump = dict_artifacts.get("dump", {})  # 默认 FSDB dump 产物的状态对象

    # dump 未真正写出时，把原因补回输出对象，供非 JSON 文本模式和调用方诊断使用
    if dict_dump.get("state") != "present":

        # 明确指出失败原因是波形产物缺失或零字节，而不是简单返回 failed
        dict_output["reason"] = "FSDB dump is missing or zero bytes"  # dump 缺失或零字节时回写的失败原因

        # 没有有效 dump 时应整体判定为失败
        return "failed"

    # 关键产物满足要求时，维持 passed 结论
    return "passed"

# 根据 CLI 参数选择 manifest 或显式源码构建路径，减少 main 中的长分支与参数投影噪声
def _build_cli_plan(args: argparse.Namespace) -> JsonDict:
    """
    根据 CLI 参数生成对应的冒烟计划对象。

    :param args: argparse 解析得到的 CLI 参数命名空间，dtype=argparse.Namespace，unit=object
    :return: 返回 manifest 分支或显式源码分支构建出的完整冒烟计划对象，dtype=dict[str, Any]，unit=mapping
    :raises ValueError: 当输入参数结构不合法或 manifest 内容不合法时抛出异常。
    """

    # manifest 模式优先级最高，适用于混合语言、多 filelist 和更多环境注入场景
    if args.manifest:

        # 只有在显式关闭 auto_pli 时才往 overrides 里写值，避免覆盖 manifest 的其他配置
        dict_manifest_overrides: dict[str, Any] = {}  # manifest 构建阶段需要施加的运行时覆盖值

        # 调用方通过 CLI 关闭 auto_pli 时，要显式覆盖 manifest 里的同名字段
        if args.no_auto_pli:

            # 仅对 auto_pli 写入 False，保持其他配置继续来自 manifest 本身
            dict_manifest_overrides["auto_pli"] = False  # CLI 显式关闭 auto_pli 时写入的覆盖值

        # 基于 manifest 文件和可选覆盖值构建完整计划对象
        return build_smoke_plan_from_manifest(
            manifest=args.manifest,
            workdir=args.workdir.resolve(),
            **dict_manifest_overrides,
        )

    # 未提供 manifest 时，回落到显式源码参数构建路径
    return build_smoke_plan(

        # 这一组参数描述源码入口，决定 compile-vhdl 与 compile/verilog 两类步骤如何生成。
        sources=[path_item.resolve() for path_item in args.source],  # CLI 传入的 Verilog/SystemVerilog 源文件列表。
        source_list=_as_path(args.source_list.resolve()) if args.source_list else None,  # CLI 提供的这个 filelist 会补成一组 ``-f`` 参数。
        vhdl_sources=[path_item.resolve() for path_item in args.vhdl_source],  # 这些源码只会进入 compile-vhdl 分支，不会混进 vlogan 输入。
        include_dirs=[path_item.resolve() for path_item in args.include_dir],  # CLI 提供的头文件搜索目录列表。

        # 这里集中描述编译期的宏、库、时标和调试/覆盖率开关。
        defines=dict(parse_define(str_item) for str_item in args.define),  # CLI 传入的宏定义会在这里收口成 ``name -> value`` 映射。
        libraries=[str(obj_item) for obj_item in args.library],  # CLI 传入的逻辑库名称列表。
        timescale=args.timescale,  # CLI 显式指定的编译 timescale 约束。
        debug=args.debug,  # CLI 显式指定的 ``-debug_access+`` 等级。
        kdb=not args.no_kdb,  # CLI 默认开启 KDB，只有显式传入 ``--no-kdb`` 才会关闭。
        coverage=[str(obj_item) for obj_item in args.coverage],  # CLI 传入的覆盖率维度列表。
        sv_libs=[path_item.resolve() for path_item in args.sv_lib],  # CLI 传入的 DPI 共享库路径列表。

        # 这一组参数只影响 simulate 及后续波形校验阶段的运行行为。
        plusargs=[str(obj_item) for obj_item in args.plusarg],  # CLI 传入的运行期 plusargs 列表。
        seed=args.seed,  # CLI 显式指定的可选仿真随机种子。
        workdir=args.workdir.resolve(),  # CLI 指定的工作目录会先标准化成绝对路径。
        top=args.top,  # CLI 显式指定的顶层模块名。
        dump_name=args.dump_name,  # CLI 显式指定的 FSDB 波形文件名。

        # 最后一组参数负责产物约束、辅助脚本路径和波形加载检查模式。
        rc_file=args.rc_file.resolve() if args.rc_file else None,  # CLI 传入的可选 Verdi RC 文件路径。
        cmd_file=args.cmd_file.resolve() if args.cmd_file else None,  # CLI 传入的可选 Verdi 命令脚本路径。
        pli_dir=args.pli_dir.resolve() if args.pli_dir else None,  # CLI 传入的显式 Novas PLI 目录路径。
        auto_pli=not args.no_auto_pli,  # CLI 默认开启自动探测 PLI 目录，除非显式关闭。
        verdi_check=args.verdi_check,  # CLI 选择的 Verdi 波形加载检查模式。
        report_signal=args.report_signal,  # CLI 请求 fsdbreport 验证时要查询的信号名。
        clean=args.clean,  # CLI 是否要求在生成计划前清理既有工作目录。
    )

# 输出非 JSON 模式的人类可读摘要，避免把结构化内容直接铺到终端正文
def _emit_human_summary(dict_output: JsonDict, json_dict_plan: JsonDict) -> None:
    """
    输出当前计划或执行结果的人类可读摘要。

    :param dict_output: main 组装得到的统一输出对象，dtype=dict[str, Any]，unit=mapping
    :param json_dict_plan: 当前 CLI 输入对应的计划对象，dtype=dict[str, Any]，unit=mapping
    :return: 当前函数只负责终端打印摘要，不返回业务值。
    """

    # 先把人类摘要里需要的标量值抽出来，后续打印只消费这些稳定标量。
    int_step_count = len(json_dict_plan["steps"])  # 当前计划总共生成的阶段数量。

    # 先把缺工具数量单独提取出来，后续只输出摘要数量而不直接展开列表。
    int_missing_tool_count = len(dict_output["missing_tools"])  # 当前环境缺失的工具数量。

    # 把最终状态提前转成稳定字符串，方便后续终端摘要统一打印。
    str_status = str(dict_output["status"])  # 当前计划或执行流程最终归纳出的状态标签。

    # 失败或跳过原因只保留可用性标记，详细正文交给 ``--json`` 协议查看。
    obj_reason = dict_output.get("reason")  # 当前流程可能附带的失败、跳过或提示原因。

    # 这里只报告阶段总数和 JSON 协议入口，让人工先判断计划规模，再按需切换机器可读输出。
    print(f"> INFO: [Python] prepared {int_step_count} planned steps; use --json for machine-readable details.")

    # 缺工具时只打印数量摘要，详细工具列表交给 ``--json`` 协议输出。
    if int_missing_tool_count:

        # 这里不直接展开列表，避免把结构化内容当成人类终端正文输出。
        print(
            f"> WARNING: [Python] detected {int_missing_tool_count} missing tools; "
            "rerun with --json for the exact list."
        )

    # 当存在阻塞原因时，只提示可用性，让上游按需读取机器可读协议获取完整原因。
    if obj_reason:

        # 这里保留 WARNING 信号位，但不把原始 reason 字段直接铺到终端。
        print("> WARNING: [Python] a detailed reason is available; rerun with --json to inspect it.")

    # 最后一行固定输出精简状态，便于人工快速确认当前流程结论。
    print(f"> INFO: [Python] status: {str_status}")

# 解析 CLI 参数，执行 dry-run 或实际冒烟流程，并按请求输出 JSON 或人类可读摘要
def main() -> int:
    """
    运行 VCS/Verdi 冒烟计划 CLI 入口。

    :param 无: 当前入口函数不接收显式 Python 参数，全部输入都由命令行解析结果提供。
    :return: 当状态为 ``dry-run``、``passed`` 或 ``skipped`` 时返回 0，其余情况返回 1，dtype=int，unit=exit code
    """

    # 使用 argparse 描述 CLI 入口，让本脚本同时兼容最小 dry-run 和 manifest 驱动的复杂场景
    parser = argparse.ArgumentParser(description="Plan or run a minimal VCS/Verdi smoke flow.")  # 当前 CLI 入口的参数解析器对象

    # manifest 模式适合复杂工程；未提供时继续支持显式源码参数模式
    parser.add_argument(
        "--manifest",
        type=Path,
        help="JSON manifest describing sources and common non-GUI simulation options.",
    )

    # 单个或多个 Verilog/SystemVerilog 源文件都通过可重复的 --source 输入
    parser.add_argument("--source", action="append", type=Path, default=[])

    # VHDL 源文件单独使用 --vhdl-source，方便后续生成 compile-vhdl 阶段
    parser.add_argument("--vhdl-source", action="append", type=Path, default=[])

    # filelist 与 include-dir 分别映射到常见 VCS 编译输入模式
    parser.add_argument("--source-list", type=Path)

    # include-dir 支持重复声明，便于把多个头文件搜索目录按顺序注入到编译阶段
    parser.add_argument("--include-dir", action="append", type=Path, default=[])

    # define、library 和 timescale 控制编译命令的关键配置项
    parser.add_argument("--define", action="append", default=[], help="Verilog define as NAME or NAME=VALUE.")

    # library 参数允许重复声明，用来恢复首个逻辑库并保留历史 CLI 兼容性
    parser.add_argument("--library", action="append", default=[])

    # timescale 作为单值选项存在，只影响 Verilog/SystemVerilog 编译阶段
    parser.add_argument("--timescale")

    # debug、kdb 与 coverage 控制调试能力和覆盖率采集行为
    parser.add_argument("--debug", default="all")

    # no-kdb 以反向布尔开关形式出现，保持历史 CLI 的默认开启策略
    parser.add_argument("--no-kdb", action="store_true")

    # coverage 允许多次声明，从而把多个覆盖率维度拼成统一的 ``-cm`` 参数
    parser.add_argument("--coverage", action="append", default=[], help="Coverage item, for example line or cond.")

    # sv-lib、plusarg 与 seed 支持 DPI 和仿真运行时参数注入
    parser.add_argument(
        "--sv-lib",
        action="append",
        type=Path,
        default=[],
        help="DPI shared library passed to VCS with -sv_lib.",
    )

    # plusarg 支持重复出现，方便把业务层运行开关原样透传给 simv
    parser.add_argument("--plusarg", action="append", default=[])

    # seed 是单个整数值，用于构造可复现的 ``+ntb_random_seed``
    parser.add_argument("--seed", type=int)

    # workdir、top 和 dump-name 决定工程落盘目录、顶层模块和默认转储文件名
    parser.add_argument("--workdir", type=Path, default=Path("build/vcs-verdi-smoke"))

    # top 单独控制 elaboration 阶段拼出的 ``work.<top>`` 顶层入口
    parser.add_argument("--top", default="top")

    # dump-name 单独决定 ``+fsdbfile+`` 和计划元数据中的默认波形文件名
    parser.add_argument("--dump-name", default="waves.fsdb")

    # rc-file、cmd-file 和 pli-dir 分别覆盖波形布局、UCLI 命令文件与显式 PLI 目录
    parser.add_argument("--rc-file", type=Path)

    # cmd-file 让调用方显式指定 UCLI 脚本文件，供 simv 的 ``-ucli -do`` 入口消费
    parser.add_argument("--cmd-file", type=Path)

    # pli-dir 用于强制绑定显式 PLI 目录，绕过环境变量自动探测逻辑
    parser.add_argument("--pli-dir", type=Path, help="Directory containing novas.tab and pli.a.")

    # auto-pli、clean、execute 与 dry-run 控制运行方式和计划执行边界
    parser.add_argument(
        "--no-auto-pli",
        action="store_true",
        help="Do not infer Verdi novas PLI from VERDI_HOME/NOVAS_HOME.",
    )

    # clean 表示执行前先清理工作目录，但仍受 ``_safe_to_clean`` 的安全边界约束
    parser.add_argument("--clean", action="store_true", help="Remove the work directory before executing.")

    # execute 明确要求真正运行工具；未给出时保持 dry-run 只生成计划
    parser.add_argument("--execute", action="store_true", help="Run commands after planning.")

    # dry-run 选项保留给用户显式声明意图，即使它本身与默认行为一致
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command plan without executing. This is the default.",
    )

    # step-timeout、verdi-check、report-signal 和 json 则控制执行时限、校验模式与输出格式
    parser.add_argument(
        "--step-timeout",
        type=int,
        default=300,
        help="Per-step execution timeout in seconds.",
    )

    # verdi-check 决定最终采用 Verdi GUI 加载还是 fsdbreport 非交互校验
    parser.add_argument("--verdi-check", choices=("verdi", "fsdbreport"), default="verdi")

    # report-signal 仅在 fsdbreport 模式生效，用来指定默认或显式观察的信号路径
    parser.add_argument("--report-signal", default=None, help="Signal path for fsdbreport mode, for example /top/clk.")

    # json 开启结构化输出，供测试与其他脚本直接消费计划和执行摘要
    parser.add_argument("--json", action="store_true", help="Print JSON output.")

    # 所有 CLI 参数在这里完成解析，后续统一交给 manifest 或显式源码分支处理
    args = parser.parse_args()  # 当前进程解析得到的 CLI 参数命名空间

    # 先构建计划；输入结构不合法时统一转成 argparse 友好的错误输出
    try:

        # 这里统一把 manifest 分支和显式源码分支折叠成单一计划对象，后续逻辑不再区分入口。
        json_dict_plan = _build_cli_plan(args)  # 当前 CLI 输入最终对应的完整计划对象。

    # 输入验证失败时交给 argparse 输出错误并带退出码 2，保持 CLI 用户体验一致
    except ValueError as exc:

        # 通过 parser.error 统一打印上下文相关的参数错误信息
        parser.error(str(exc))

    # 基础输出对象始终包含计划本体、dry-run 诊断和缺失工具列表，便于 JSON 消费方直接使用
    dict_output = {
        "plan": json_dict_plan,  # 当前 CLI 入口构建得到的完整计划对象
        "diagnostics": plan_diagnostics(json_dict_plan),  # 与计划对应的 dry-run 诊断信息
        "missing_tools": missing_tools(json_dict_plan),  # 当前机器缺失的必需工具列表
    }  # CLI 统一输出对象

    # execute 模式会真正跑子进程；否则默认维持 dry-run，只输出计划和诊断信息
    if args.execute:

        # 缺工具时没必要盲目执行，直接把状态置为 skipped 并给出原因
        if dict_output["missing_tools"]:

            # 以 skipped 明确表示当前不是脚本本身失败，而是环境不具备执行前提
            dict_output["status"] = "skipped"  # 缺工具时采用的跳过状态

            # 把缺工具作为跳过原因，供人类输出和 JSON 消费方统一展示
            dict_output["reason"] = "missing required tools"  # 缺工具导致跳过执行的原因文本

        # 所需工具都存在时，继续尝试执行完整冒烟流程
        else:

            # 计划执行内部可能因为 clean 安全检查而抛 RuntimeError，这里要统一转成 failed 摘要
            try:

                # 真正执行计划，并把每步结果写回输出对象供 JSON 模式完整展示
                dict_output["results"] = run_plan(json_dict_plan, step_timeout=args.step_timeout)  # execute 模式下逐步执行得到的结果列表

            # 执行前安全检查或运行期硬失败统一映射成 failed 状态
            except RuntimeError as exc:

                # 运行时异常被视为执行失败，并把消息直接带给上层
                dict_output["status"] = "failed"  # 运行时异常映射得到的失败状态

                # 把失败原因文本附加到输出对象中，便于非 JSON 模式直接打印
                dict_output["reason"] = str(exc)  # 运行时异常转写得到的失败原因文本

            # 正常完成 run_plan 后，再根据最后一步退出码和产物状态汇总最终结论
            else:

                # 只有结果列表非空且最后一个步骤退出码为 0，才可暂时视为 passed 候选
                bool_last_ok = bool(dict_output["results"]) and dict_output["results"][-1]["returncode"] == 0  # 当前执行结果是否以成功步骤收尾

                # 无论候选状态如何，都先回填一份完整的产物状态快照供后续总结和 JSON 输出
                dict_output["artifact_status"] = artifact_status(json_dict_plan["artifacts"])  # execute 模式回填的产物状态快照

                # 先根据最后一步退出码给出粗粒度通过/失败，再交给 summarize_status 做产物层复核
                dict_output["status"] = "passed" if bool_last_ok else "failed"  # 基于最后一步退出码得出的粗粒度状态

                # dump 缺失等场景会在这里把候选 passed 改写为 failed，并补充原因
                dict_output["status"] = summarize_status(dict_output)  # 结合关键产物状态修正后的最终状态

    # 未要求 execute 时保持 dry-run，只报告计划、诊断和缺工具信息
    else:

        # 默认模式明确标记为 dry-run，方便上层脚本和人工判断这里没有真正执行工具
        dict_output["status"] = "dry-run"  # 未执行工具时返回的默认状态

    # JSON 模式下直接输出结构化结果，供其他脚本、测试和技能工作流继续消费
    if args.json:

        # 使用稳定缩进和排序输出，便于测试断言与人工阅读同时兼顾
        json.dump(dict_output, sys.stdout, indent=2, sort_keys=True)

        # JSON 协议输出末尾补一个换行，避免 shell 提示符直接接到载荷尾部。
        sys.stdout.write("\n")

    # 非 JSON 模式只输出摘要级状态，避免把步骤列表或字典字段逐条直接打印到终端。
    else:

        # 这里显式走人类摘要分支，避免非 JSON 模式把结构化字段直接打印到终端。
        _emit_human_summary(dict_output, json_dict_plan)

    # 只有 dry-run、passed 和 skipped 视为脚本成功完成了自身职责，其余都返回失败退出码
    return 0 if dict_output["status"] in {"dry-run", "passed", "skipped"} else 1

# 作为脚本独立运行时，使用 SystemExit 透传 CLI 退出码
if __name__ == "__main__":

    # 让 main 的整数返回值成为进程退出码，便于外层批处理和测试统一判定
    raise SystemExit(main())
