#!/usr/bin/env python3
"""把简单 VCS/Verdi 工程描述导入为统一的 smoke manifest。

命令行标准输出协议：
- 默认只打印带前缀的人类可读摘要，不把完整结构化结果直接输出到终端。
- 当传入 ``--json`` 时，标准输出会写出单个 JSON 对象，供上游自动化直接消费。
"""

# 启用前向类型标注，避免运行时提前求值类型对象
from __future__ import annotations

# 命令行参数解析库
import argparse

# 文件名通配匹配库
import fnmatch

# JSON 编解码库
import json

# 环境变量读取库
import os

# 正则表达式库
import re

# Shell 风格分词库
import shlex

# 标准输出流对象，供 JSON 协议直写 stdout
import sys

# 路径对象库
from pathlib import Path

# 需要直接转入 vlogan 参数列表的兼容标志
VLOGAN_FLAG_PREFIXES = ("+v2k",)  # 直接归入 vlogan_args 的 VCS 标志集合

# 需要直接保留到 VCS 参数列表的常用开关
VCS_FLAG_PREFIXES = ("+mindelays", "-negdelay", "+neg_tchk")  # 常见时序与延迟相关的 VCS 标志集合

# 需要视为 HDL 源文件的后缀集合
SOURCE_SUFFIXES = {".v", ".sv", ".vh", ".svh"}  # 参与 HDL 源文件识别的扩展名集合

# Edalize 文件表里允许直接导入的源码类型前缀
EDALIZE_SOURCE_TYPES = ("verilogSource", "systemVerilogSource", "verilog2001Source", "cSource", "cppSource")  # Edalize/CAPI2 输入里认可的源码类型集合

# `$readmemh("...")` 依赖扫描使用的稳定正则
READMEMH_CALL_PATTERN: re.Pattern[str] = re.compile(  # 匹配 `$readmemh("file")` 调用的正则对象
    r'\$readmemh\s*\(\s*"([^"]+)"'  # 捕获 `$readmemh` 首个双引号参数里的数据文件名
)

# token 解析辅助上下文统一用轻量映射承载，避免额外造短生命周期容器类
TokenPathContext = dict[str, Path]  # token 解析共享的路径上下文映射

# VCS token 解析写回的共享缓冲区使用松耦合映射承载
VcsParseBuffers = dict[str, object]  # VCS token 解析共享输出缓冲区

# 单次 VCS token 扫描过程的状态位也通过轻量映射维护
VcsParseState = dict[str, object]  # VCS token 扫描状态映射

# 设计 token 解析需要的路径和输出缓冲区统一收敛到一个上下文映射
DesignParseContext = dict[str, object]  # design token 解析上下文映射

# 主编译入口会复用同一批路径、变量与 token 上下文，统一收敛到一个映射
PrimaryCompileContext = dict[str, object]  # 主编译入口解析上下文映射

# `import_project` 主流程跨阶段共享的可变状态也统一收敛到轻量映射
ImportProjectState = dict[str, object]  # 项目导入主流程共享状态映射

# 折叠 Makefile 续行标记，避免分词时把一条命令误拆成多行
def _join_continuations(text: str) -> str:
    """
    把反斜杠续行语法压平成单行文本。

    :param text: Makefile 或 Tcl 原始文本，dtype=str，unit=文本字符
    :return: 去掉续行换行后的文本，dtype=str，unit=文本字符
    """

    # 先消化 Windows 风格的反斜杠续行，避免 CRLF 版本漏掉后续分词。
    str_without_windows_continuations = text.replace("\\\r\n", " ")  # 先折叠 CRLF 续行后的文本

    # 再处理 Unix 风格的反斜杠续行，最终得到适合统一词法分析的平铺文本。
    str_flattened_text = str_without_windows_continuations.replace("\\\n", " ")  # 再折叠 LF 续行后的文本

    # 返回供后续词法分析继续使用的平铺文本
    return str_flattened_text

# 删除 Makefile 行内注释，但保留字符串字面量中的井号
def _strip_make_inline_comment(value: str) -> str:
    """
    去掉一行 Make 变量定义里的行尾注释。

    :param value: 单行 Make 变量右值，dtype=str，unit=文本字符
    :return: 删除真实注释后的变量右值，dtype=str，unit=文本字符
    """

    # 单引号上下文会屏蔽其中井号的注释语义。
    bool_in_single_quote = False  # 当前扫描位置是否处于单引号上下文

    # 双引号状态单独维护，避免把单引号字符串里的 `"` 误判成上下文切换。
    bool_in_double_quote = False  # 当前字符是否已经落入双引号保护区间

    # 转义状态只影响紧随其后的一个字符，不需要跨越更长的文本区间。
    bool_is_escaped = False  # 当前字符是否应按被转义语义处理

    # 逐字符扫描整行文本，避免误删字符串里的井号
    for int_index, str_character in enumerate(value):

        # 当前字符若承接转义，则只清空状态并跳过特殊判定
        if bool_is_escaped:

            # 本轮字符已经消费掉转义语义，恢复默认状态
            bool_is_escaped = False  # 下一字符重新按普通字符处理

            # 继续检查下一个字符
            continue

        # 反斜杠会让下一个字符失去特殊含义
        if str_character == "\\":

            # 标记下一个字符需要按转义后文本处理
            bool_is_escaped = True  # 后续单个字符进入转义语义

            # 当前反斜杠自身无需再参与其他判定
            continue

        # 单引号只在当前不处于双引号时切换语义
        if str_character == "'" and not bool_in_double_quote:

            # 切换单引号上下文，保护其中的井号不被误判
            bool_in_single_quote = not bool_in_single_quote  # 单引号包围区间的开闭状态

            # 当前字符处理结束，继续读取后续文本
            continue

        # 双引号只在当前不处于单引号时切换语义
        if str_character == '"' and not bool_in_single_quote:

            # 双引号区间里的 `#` 应保留为正文，因此这里只切换双引号状态。
            bool_in_double_quote = not bool_in_double_quote  # 双引号包围区间的开闭状态

            # 当前双引号字符只承担状态切换职责，不再参与后续注释判定。
            continue

        # 井号仅在未处于任何引号上下文时才表示真实注释起点
        if str_character == "#" and not bool_in_single_quote and not bool_in_double_quote:

            # 返回注释起点之前的有效文本
            return value[:int_index].rstrip()

    # 整行没有真实注释时，返回去除首尾空白后的原始右值
    return value.strip()

# 读取指定 Make 变量，并按 shell 词法拆成 token 列表
def _read_make_var(makefile: Path, name: str) -> list[str]:
    """
    提取 Makefile 中某一个变量定义的 token 序列。

    :param makefile: 待读取的 Makefile 路径，dtype=Path，unit=filesystem path
    :param name: 目标变量名，dtype=str，unit=identifier
    :return: 变量右值按 shell 词法拆分后的 token 列表，dtype=list[str]，unit=token
    """

    # 先把 Makefile 续行折叠掉，后续变量匹配才能按单行语义稳定进行。
    str_make_text = _join_continuations(makefile.read_text(encoding="utf-8", errors="replace"))  # 压平续行后供变量扫描的完整 Makefile 文本

    # 保留变量名前缀，避免循环里重复格式化
    str_prefix = f"{name}"  # 当前目标变量名前缀

    # 按行遍历 Makefile，定位完全匹配的变量定义
    for str_line in str_make_text.splitlines():

        # 去掉行首尾空白，统一变量匹配形态
        str_stripped_line = str_line.strip()  # 当前待匹配的规范化行文本

        # 非目标变量行直接跳过
        if not str_stripped_line.startswith(str_prefix):

            # 继续检查下一行变量定义
            continue

        # 拆分变量定义的左右两侧文本
        str_left, str_separator, str_right = str_stripped_line.partition("=")  # 当前变量定义的左右半区

        # 仅在变量名完全匹配时返回分词结果
        if str_separator and str_left.strip() == name:

            # 返回去注释后的变量 token 列表
            return shlex.split(_strip_make_inline_comment(str_right), posix=True)

    # 变量不存在时返回空列表，交由上层走兼容分支
    return []

# 读取 Makefile 中出现的全部普通变量定义，供后续展开使用
def _read_make_vars(makefile: Path) -> dict[str, str]:
    """
    收集 Makefile 顶层可直接识别的变量定义。

    :param makefile: 待解析的 Makefile 路径，dtype=Path，unit=filesystem path
    :return: 变量名到原始字符串右值的映射，dtype=dict[str, str]，unit=Make variable map
    """

    # 这里做的是全量变量盘点，因此更不能让跨行 `+=` 声明在预处理阶段漏掉。
    str_make_text = _join_continuations(makefile.read_text(encoding="utf-8", errors="replace"))  # 折叠续行后的 Makefile 全文

    # 初始化变量映射表，后续会按出现顺序覆盖或追加
    dict_variables: dict[str, str] = {}  # 当前 Makefile 中可见的变量定义表

    # 逐行扫描普通变量声明，跳过命令行和注释行
    for str_line in str_make_text.splitlines():

        # 去掉两端空白，统一变量定义的匹配入口
        str_stripped_line = str_line.strip()  # 当前行的规范化文本

        # 纯空行、注释行和命令配方行不参与变量表构建
        if (
            not str_stripped_line
            or str_stripped_line.startswith("#")
            or str_stripped_line.startswith("\t")
        ):

            # 继续扫描下一条候选变量定义
            continue

        # 匹配 `=`, `:=`, `?=` 与 `+=` 这几类常见变量声明
        match_variable = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::=|\?=|\+=|=)\s*(.*)$", str_stripped_line)  # 当前行的变量声明匹配结果

        # 成功命中变量定义时，提取名称和右值
        if match_variable:

            # 读取当前变量名
            str_variable_name = match_variable.group(1)  # 当前变量的标识符名称

            # 读取当前变量原始右值
            str_variable_value = match_variable.group(2)  # 当前变量的原始右值文本

            # 去除右值里的行尾注释，避免注释泄漏进参数列表
            str_clean_value = _strip_make_inline_comment(str_variable_value)  # 删除真实注释后的变量右值

            # `+=` 需要承接旧值，其余赋值方式直接覆盖
            if "+=" in str_stripped_line:

                # 先取回旧值，保证 `+=` 的行为是追加而不是覆盖。
                str_existing_value = dict_variables.get(str_variable_name, "")  # 当前变量在字典中的历史值

                # 写回拼接后的追加结果
                dict_variables[str_variable_name] = f"{str_existing_value} {str_clean_value}".strip()  # 承接旧值后的变量右值

            # 非追加赋值则直接用当前右值覆盖
            else:

                # 写入当前变量的最新右值
                dict_variables[str_variable_name] = str_clean_value  # 当前变量最终右值

    # 返回供变量展开阶段复用的变量映射表
    return dict_variables

# 读取 Makefile 命令配方行，后续用于识别 vcs、verdi、urg 与 simv 调用
def _make_commands(makefile: Path) -> list[str]:
    """
    提取 Makefile 中以制表符开头的命令配方行。

    :param makefile: 待解析的 Makefile 路径，dtype=Path，unit=filesystem path
    :return: 去掉前导制表符后的命令列表，dtype=list[str]，unit=shell command
    """

    # 初始化命令列表，按出现顺序保留 Make 配方
    list_commands: list[str] = []  # 当前 Makefile 中提取到的命令配方

    # 遍历折叠续行后的每一行文本，筛出命令配方
    for str_line in _join_continuations(
        makefile.read_text(encoding="utf-8", errors="replace")
    ).splitlines():

        # Make 命令配方必须以制表符开头
        if str_line.startswith("\t"):

            # 这里只保留真正会交给 shell 执行的命令体，不保留 Makefile 的缩进语义。
            list_commands.append(str_line.strip())  # 去掉制表符缩进后的真实 shell 命令体

    # 返回按源文件顺序收集的命令配方列表
    return list_commands

# 读取命令片段展开后的工具基名，兼容 `$(SIMV)` 一类变量包装命令
def _command_tool_name(
    command_piece: str,
    *,
    variables: dict[str, str],
    make_dir: Path,
    project_root: Path,
) -> str:
    """
    返回命令片段展开后的工具基名。

    :param command_piece: 单个 shell 命令片段，dtype=str，unit=shell command
    :param variables: 已解析出的 Make 变量表，dtype=dict[str, str]，unit=Make variable map
    :param make_dir: 当前 Makefile 所在目录，dtype=Path，unit=filesystem path
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :return: 小写工具基名；无法识别时返回空字符串，dtype=str，unit=executable basename
    """

    # 先展开变量再分词，这样像 `$(SIMV)` 这样的包装命令也能正确暴露工具名。
    list_tokens = _expanded_tokens(  # 当前命令片段展开后的 token 列表
        command_piece,  # 当前待识别工具名的命令片段文本
        variables=variables,  # 展开命令片段时可用的 Make 变量映射
        make_dir=make_dir,  # 命令片段内相对路径使用的 Makefile 目录
        project_root=project_root,  # 命令片段里绝对路径相对化所参考的工程根目录
    )

    # 空命令片段不可能携带工具名
    if not list_tokens:

        # 返回空字符串，交由调用方继续匹配其他片段
        return ""

    # 返回可执行文件路径最后一段的小写形式
    return Path(list_tokens[0]).name.lower()

# 把绝对路径或外部路径压缩成相对 project_root 的稳定表示
def _rel(path: Path, project_root: Path) -> str:
    """
    把路径转换成尽量相对 project_root 的 POSIX 字符串。

    :param path: 待转换的路径，dtype=Path，unit=filesystem path
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :return: 相对或原样保留的 POSIX 路径字符串，dtype=str，unit=filesystem path
    """

    # 优先输出相对工程根的路径，保证 manifest 更稳定
    try:

        # 返回相对于工程根的 POSIX 风格路径
        return path.resolve().relative_to(project_root.resolve()).as_posix()

    # 位于工程根外部时，退回到原始 POSIX 路径
    except ValueError:

        # 返回无法相对化时的绝对或原样路径字符串
        return path.as_posix()

# 以给定基目录解析一个 token 路径
def _resolve_from(base: Path, token: str, project_root: Path) -> str:
    """
    相对于指定基目录解析一个路径 token。

    :param base: 当前 token 的解析基目录，dtype=Path，unit=filesystem path
    :param token: 待解析的路径 token，dtype=str，unit=filesystem path fragment
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :return: 规范化后的相对路径字符串，dtype=str，unit=filesystem path
    """

    # 把 token 先转换成 Path，统一后续处理入口
    path_candidate = Path(token)  # 当前待解析的原始路径对象

    # 相对路径需要拼接到基目录下再做相对化
    if not path_candidate.is_absolute():

        # 以基目录还原 token 的真实文件系统位置
        path_candidate = base / path_candidate  # 相对 token 对应的绝对语义路径

    # 返回面向 manifest 的稳定相对路径字符串
    return _rel(path_candidate, project_root)

# 解析源码 token 时，优先尊重 project_root 下真实存在的相对路径
def _resolve_source_token(base: Path, token: str, project_root: Path) -> str:
    """
    解析 HDL 源文件 token，优先复用工程根下真实存在的相对路径。

    :param base: 当前源码 token 的解析基目录，dtype=Path，unit=filesystem path
    :param token: 待解析的源码路径 token，dtype=str，unit=filesystem path fragment
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :return: 规范化后的源码相对路径字符串，dtype=str，unit=filesystem path
    """

    # 把源码 token 先封装为 Path，便于判断绝对与相对形式
    path_candidate = Path(token)  # 当前源码 token 对应的路径对象

    # 绝对路径可以直接相对化到工程根
    if path_candidate.is_absolute():

        # 返回绝对源码路径相对化后的 manifest 表示
        return _rel(path_candidate, project_root)

    # 构造 project_root 视角下的相对路径候选
    path_project_relative = project_root / path_candidate  # 以工程根解释 token 后的候选路径

    # 若工程根下真实存在该路径，则优先保留这层语义
    if path_project_relative.exists():

        # 返回工程根视角更稳定的源码相对路径
        return _rel(path_project_relative, project_root)

    # 其余情况回退到调用方给出的基目录解析
    return _resolve_from(base, token, project_root)

# 规范化命令行参数中的路径值，统一压缩成相对 project_root 的形式
def _normalize_arg_path(value: str, *, base: Path, project_root: Path) -> str:
    """
    解析命令参数携带的路径值，并输出相对工程根的稳定表示。

    :param value: 原始路径参数值，dtype=str，unit=filesystem path fragment
    :param base: 当前参数的解析基目录，dtype=Path，unit=filesystem path
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :return: 规范化后的路径字符串，dtype=str，unit=filesystem path
    """

    # 把字符串路径转成 Path，便于统一解析
    path_candidate = Path(value)  # 当前命令参数里携带的路径对象

    # 相对路径需要先和基目录拼接，再参与相对化
    if not path_candidate.is_absolute():

        # 把相对路径解释到当前命令基目录下
        path_candidate = base / path_candidate  # 参数值对应的真实文件系统路径

    # 返回供 manifest 记录的相对路径字符串
    return _rel(path_candidate, project_root)

# 提取受控 `find` 命令里的根目录与名字过滤规则
def _parse_shell_find_filters(
    tokens: list[str],
    *,
    project_root: Path,
) -> tuple[Path | None, list[str], list[str]]:
    """
    解析 `find` 命令 token，提取根目录、包含模式和排除模式。

    :param tokens: `find` 命令分词后的 token 列表，dtype=list[str]，unit=shell token
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :return: 根目录、包含模式与排除模式三元组，dtype=tuple[Path | None, list[str], list[str]]，unit=find filter pack
    """

    # 只接受 `find <root> ...` 这类受控形态。
    if len(tokens) < 2 or tokens[0] != "find":

        # 返回空根目录，通知调用方当前命令不受支持。
        return None, [], []

    # 先还原 `find` 指定的扫描根目录。
    path_root = Path(tokens[1])  # `find` 命令声明的根目录

    # 相对路径要映射到工程根目录之下再继续扫描。
    if not path_root.is_absolute():

        # 组合出实际参与递归遍历的根目录路径。
        path_root = project_root / path_root  # 转成工程根语义后的扫描根目录

    # include 规则决定候选文件的准入集合。
    list_include_patterns: list[str] = []  # 允许进入候选集的文件名模式

    # exclude 规则只在命中 include 后才参与淘汰。
    list_exclude_patterns: list[str] = []  # 需要后置排除的文件名模式

    # 从 `<root>` 后面的第一个条件 token 开始顺序解析。
    int_index = 2  # 当前正在检查的条件 token 下标

    # 这里只支持 `-name pattern` 与 `! -name pattern` 两种过滤片段。
    while int_index < len(tokens):

        # 取出当前条件 token，方便逐分支识别。
        str_token = tokens[int_index]  # 当前待识别的 `find` 条件 token

        # 命中 `-name pattern` 时，把模式记入 include 集合。
        if str_token == "-name" and int_index + 1 < len(tokens):

            # 保存当前新增的 include 文件名模式。
            list_include_patterns.append(tokens[int_index + 1])  # 追加到 include 规则集的模式

            # `-name` 片段会连同后面的模式一起消费掉两个 token。
            int_index += 2  # 跳到当前 include 条件片段之后的下一个位置

            # 当前 include 过滤条件已经完整落库，继续扫描后续条件。
            continue

        # 命中 `! -name pattern` 时，要把否定模式登记到后置排除列表。
        if (
            str_token == "!"
            and int_index + 2 < len(tokens)
            and tokens[int_index + 1] == "-name"
        ):

            # 把紧跟在 `! -name` 后面的文件名模式写入排除规则集。
            list_exclude_patterns.append(tokens[int_index + 2])  # 当前新增的排除文件名模式

            # 否定条件会跨过 `!`、`-name` 和模式本身三个 token。
            int_index += 3  # 越过整个 `! -name pattern` 片段后的下一扫描位置

            # 当前排除条件已经处理完毕，继续扫描剩余过滤片段。
            continue

        # 不支持的条件不报错，只单步越过，避免卡在当前位置。
        int_index += 1  # 未识别条件统一按单步推进

    # 返回供调用方执行实际文件扫描的过滤条件。
    return path_root, list_include_patterns, list_exclude_patterns

# 在受控范围内模拟少量 `$(shell find ...)` 展开能力
def _shell_find_result(command: str, *, project_root: Path) -> list[str]:
    """
    执行受限的 `find` 风格展开，用于兼容 Make 变量中的简单 shell 片段。

    :param command: `$(shell ...)` 内部命令文本，dtype=str，unit=shell command
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :return: 命中路径的相对字符串列表，dtype=list[str]，unit=filesystem path
    """

    # `pwd` 直接映射为工程根的 POSIX 路径
    if command.strip() == "pwd":

        # 返回供变量展开使用的当前工程根路径
        return [project_root.as_posix()]

    # 先按 shell 词法拆分命令，再提取受控的 `find` 过滤条件元组。
    tuple_find_filters = _parse_shell_find_filters(  # 当前 `find` 命令解析出的过滤条件元组
        shlex.split(command, posix=True),  # 当前 `find` 命令按 shell 词法拆分后的 token 序列
        project_root=project_root,  # 用工程根解释 `find` 里的相对扫描路径
    )  # 受限 `find` 解析出的根目录与过滤规则三元组

    # 依次拆出扫描根目录、include 规则和排除规则，供后续文件筛选复用。
    path_root = tuple_find_filters[0]  # 当前 `find` 命令解析出的扫描根目录

    # include 模式用于筛出允许保留的候选路径。
    list_include_patterns = tuple_find_filters[1]  # 当前 `find` 命令解析出的 include 模式列表

    # exclude 模式只负责在候选路径上做末端裁剪。
    list_exclude_patterns = tuple_find_filters[2]  # 当前 `find` 命令留给末端裁剪阶段使用的排除模式列表

    # 没有任何 include 模式或根目录不存在时，不做实际扫描
    if (
        path_root is None
        or not list_include_patterns
        or not path_root.exists()
    ):

        # 返回空列表表示没有可展开结果
        return []

    # 初始化 find 命中结果列表
    list_matches: list[str] = []  # 当前 find 命令命中的相对路径集合

    # 递归遍历根目录下所有候选文件
    for path_candidate in sorted(path_root.rglob("*")):

        # 只对普通文件做模式匹配
        if not path_candidate.is_file():

            # 目录与特殊文件不纳入返回结果
            continue

        # 取出候选文件名，供后续 fnmatch 判断
        str_file_name = path_candidate.name  # 当前候选文件的基名

        # 未命中任何包含模式时直接忽略
        if not any(
            fnmatch.fnmatch(str_file_name, str_pattern)
            for str_pattern in list_include_patterns
        ):

            # 当前文件不满足 include 规则
            continue

        # 命中任一排除模式时不加入结果
        if any(
            fnmatch.fnmatch(str_file_name, str_pattern)
            for str_pattern in list_exclude_patterns
        ):

            # 当前文件被 exclude 规则过滤掉
            continue

        # 保存相对于工程根的稳定路径表示
        list_matches.append(_rel(path_candidate, project_root))  # 当前命中的相对路径

    # 返回所有命中的相对路径列表
    return list_matches

# 展开 Make 变量、`$(shell ...)` 与 `$(PWD)` 这类受控引用
def _expand_make_value(
    raw: str,
    *,
    variables: dict[str, str],
    make_dir: Path,
    project_root: Path,
    seen: set[str] | None = None,
) -> str:
    """
    在受控范围内递归展开 Make 变量和简单 shell 片段。

    :param raw: 待展开的原始变量文本，dtype=str，unit=Make expression
    :param variables: 已解析出的 Make 变量表，dtype=dict[str, str]，unit=Make variable map
    :param make_dir: 当前 Makefile 所在目录，dtype=Path，unit=filesystem path
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :param seen: 当前递归链路里已访问过的变量名集合，dtype=set[str] | None，unit=variable name set
    :return: 完成受控展开后的字符串，dtype=str，unit=expanded command text
    """

    # 初始化递归访问集合，防止循环引用导致无限展开
    set_seen_variables = seen or set()  # 当前展开链路已经访问过的变量名集合

    # 先替换常见的工作目录占位符
    str_value = raw.replace("$(PWD)", make_dir.as_posix()).replace("$(CURDIR)", make_dir.as_posix())  # 完成 PWD/CURDIR 直替换后的文本

    # 处理 `$(shell ...)` 形式的受控 shell 展开
    def shell_replacer(match_shell: re.Match[str]) -> str:
        """
        展开 `$(shell ...)` 片段里的受控命令。

        :param match_shell: `$(shell ...)` 的正则匹配对象，dtype=re.Match[str]，unit=regex match
        :return: shell 片段的替换结果，dtype=str，unit=expanded command text
        """

        # 读取 shell 片段内部的命令体
        str_command = match_shell.group(1).strip()  # 当前待展开的 shell 命令文本

        # `pwd` 在 Makefile 里通常代表当前 Make 目录
        if str_command == "pwd":

            # 返回 Makefile 所在目录的 POSIX 路径
            return make_dir.as_posix()

        # 其余受控命令按模拟 find 结果拼成空格分隔文本
        return " ".join(_shell_find_result(str_command, project_root=project_root))

    # 把 `$(shell ...)` 逐段替换为受控展开结果
    str_value = re.sub(r"\$\(shell\s+([^)]*)\)", shell_replacer, str_value)  # 完成 shell 片段替换后的文本

    # 处理 `$(VAR)` 与 `${VAR}` 形式的变量引用
    def variable_replacer(match_variable: re.Match[str]) -> str:
        """
        递归展开普通 Make 变量引用。

        :param match_variable: 变量引用的正则匹配对象，dtype=re.Match[str]，unit=regex match
        :return: 当前变量引用的展开结果，dtype=str，unit=expanded variable text
        """

        # 读取当前变量引用里的变量名
        str_name = match_variable.group(1)  # 当前需要展开的变量名

        # 避免循环引用导致无限递归
        if str_name in set_seen_variables:

            # 循环引用分支返回空字符串，保持展开过程可终止
            return ""

        # 变量表里存在该变量时，继续递归展开其右值
        if str_name in variables:

            # 返回该变量右值的递归展开结果
            return _expand_make_value(
                variables[str_name],
                variables=variables,
                make_dir=make_dir,
                project_root=project_root,
                seen={*set_seen_variables, str_name},
            )

        # 缺失 Make 变量时，退回读取同名环境变量
        return os.environ.get(str_name, "")

    # 先处理圆括号写法，兼容 Makefile 里最常见的 `$(VAR)` 形式。
    str_value = re.sub(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)", variable_replacer, str_value)  # 完成圆括号变量替换后的文本

    # 最后补齐花括号写法，兼容某些工程同时混用 `${VAR}` 和 `$(VAR)`。
    str_value = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", variable_replacer, str_value)  # 连同 `${VAR}` 写法一起展开后的最终文本

    # 返回供 shell 分词继续使用的最终展开文本
    return str_value

# 把展开后的 Make 变量值按 shell 词法拆成 token 列表
def _expanded_tokens(
    raw: str,
    *,
    variables: dict[str, str],
    make_dir: Path,
    project_root: Path,
) -> list[str]:
    """
    展开 Make 表达式后，再按 shell 语义分词。

    :param raw: 待展开的原始 Make 文本，dtype=str，unit=Make expression
    :param variables: 已解析出的 Make 变量表，dtype=dict[str, str]，unit=Make variable map
    :param make_dir: 当前 Makefile 所在目录，dtype=Path，unit=filesystem path
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :return: 展开并分词后的 token 列表，dtype=list[str]，unit=shell token
    """

    # 先得到不含受控变量引用的展开文本
    str_expanded_value = _expand_make_value(raw, variables=variables, make_dir=make_dir, project_root=project_root)  # 完成受控变量展开后的命令文本

    # 返回供参数解析阶段使用的 shell token 列表
    return shlex.split(str_expanded_value, posix=True)

# 从 token 列表中筛出 HDL 源文件路径
def _source_tokens(tokens: list[str]) -> list[str]:
    """
    从一组命令 token 中提取 HDL 源文件项。

    :param tokens: 待筛选的命令 token 列表，dtype=list[str]，unit=shell token
    :return: 满足 HDL 后缀且非 UVM 依赖的源码 token 列表，dtype=list[str]，unit=filesystem path
    """

    # 初始化筛选后的源码 token 列表
    list_sources: list[str] = []  # 当前命中的 HDL 源文件 token 集合

    # 逐项检查 token 是否属于 HDL 源文件
    for str_token in tokens:

        # 读取 token 后缀，判断是否为源码文件
        str_suffix = Path(str_token).suffix.lower()  # 当前 token 的小写文件后缀

        # 命中 HDL 后缀且不是 UVM 依赖时，保留该源码 token
        if str_suffix in SOURCE_SUFFIXES and not _is_uvm_token(str_token):

            # 记录一个有效源码 token
            list_sources.append(str_token)  # 当前保留下来的 HDL 源文件 token

    # 返回按输入顺序提取的源码 token 列表
    return list_sources

# 按首次出现顺序去重列表内容
def _dedupe(items: list[str]) -> list[str]:
    """
    保留列表项首次出现的顺序并去重。

    :param items: 待去重的字符串列表，dtype=list[str]，unit=generic string list
    :return: 顺序稳定的去重结果，dtype=list[str]，unit=generic string list
    """

    # 返回按首次出现顺序保留的去重列表
    return list(dict.fromkeys(items))

# 识别一个 token 是否指向 UVM 相关依赖
def _is_uvm_token(token: str) -> bool:
    """
    判断 token 是否包含 UVM 依赖线索。

    :param token: 待检测的命令或路径 token，dtype=str，unit=shell token
    :return: 是否命中 UVM 相关关键字，dtype=bool，unit=boolean
    """

    # 返回 token 是否包含标准化后的 uvm 关键字
    return "uvm" in token.replace("\\", "/").lower()

# 记录当前工程依赖了 UVM，但本导入器只把它标记为可选外部依赖
def _mark_uvm_dependency(
    diagnostics: list[str],
    optional_deps: list[str],
) -> None:
    """
    把 UVM 标记为可选依赖，并补充一条诊断说明。

    :param diagnostics: 诊断消息列表，dtype=list[str]，unit=diagnostic message
    :param optional_deps: 可选外部依赖列表，dtype=list[str]，unit=dependency name
    :return: 无业务返回值，直接原位更新传入列表
    """

    # 仅在列表里还没有 UVM 时才追加依赖名
    if "uvm" not in optional_deps:

        # 记录当前工程需要额外的 UVM 运行时支持
        optional_deps.append("uvm")  # 当前 manifest 的可选外部依赖集合

    # 统一使用稳定诊断文案，避免重复告警
    str_message = "detected UVM dependency; skipped UVM_FLAGS for core non-GUI import"  # UVM 被降级为可选依赖时的标准诊断消息

    # 仅在尚未记录该诊断时才追加消息
    if str_message not in diagnostics:

        # 保存一条 UVM 降级导入的诊断说明
        diagnostics.append(str_message)  # 当前 manifest 的 UVM 兼容性诊断

    # 该函数只承担状态更新职责，不返回额外结果
    return None

# 从变量表里找出可能承载 UVM 依赖的变量名
def _optional_dependency_var_names(variables: dict[str, str]) -> set[str]:
    """
    提取可能携带 UVM 路径或源码的变量名集合。

    :param variables: 已解析出的 Make 变量表，dtype=dict[str, str]，unit=Make variable map
    :return: 疑似 UVM 依赖变量名集合，dtype=set[str]，unit=variable name set
    """

    # 初始化疑似 UVM 变量名集合
    set_variable_names: set[str] = set()  # 当前工程中承载 UVM 依赖的变量名集合

    # 逐项检查变量名与变量值里的 UVM 线索
    for str_name, str_value in variables.items():

        # 变量名统一升成大写，便于做 `UVM_*` 前缀判定
        str_upper_name = str_name.upper()  # 当前变量名的大写形式

        # 变量值统一转成小写，便于做源码路径关键字匹配
        str_lower_value = str_value.lower()  # 当前变量值的小写形式

        # 命中 UVM 命名习惯或典型源码路径时，记为可选依赖变量
        if (
            str_upper_name.startswith("UVM")
            or "uvm.sv" in str_lower_value
            or "uvm_dpi.cc" in str_lower_value
        ):

            # 保存当前疑似 UVM 变量名
            set_variable_names.add(str_name)  # 承载 UVM 内容的变量名

    # 返回后续命令过滤阶段要关注的变量名集合
    return set_variable_names

# 判断一个 token 是否引用了疑似 UVM 变量
def _references_optional_var(token: str, var_names: set[str]) -> bool:
    """
    检查 token 是否引用了某个可选依赖变量。

    :param token: 待检测的 token，dtype=str，unit=shell token
    :param var_names: 疑似可选依赖变量名集合，dtype=set[str]，unit=variable name set
    :return: token 是否引用了这些变量，dtype=bool，unit=boolean
    """

    # 返回 token 是否包含任意变量引用形式
    return any(
        f"$({str_name})" in token or f"${{{str_name}}}" in token
        for str_name in var_names
    )

# 从一条 vcs 命令片段里剥离 UVM 相关 token
def _strip_optional_dependency_tokens(
    command_piece: str,
    *,
    variables: dict[str, str],
    diagnostics: list[str],
    optional_deps: list[str],
) -> str:
    """
    删除命令片段中显式或间接引用 UVM 的 token。

    :param command_piece: 原始命令片段，dtype=str，unit=shell command
    :param variables: 已解析出的 Make 变量表，dtype=dict[str, str]，unit=Make variable map
    :param diagnostics: 诊断消息列表，dtype=list[str]，unit=diagnostic message
    :param optional_deps: 可选外部依赖列表，dtype=list[str]，unit=dependency name
    :return: 剥离可选依赖 token 后的命令片段，dtype=str，unit=shell command
    """

    # 先枚举所有可能间接展开成 UVM 依赖的变量名，后面才能连变量引用一起剥离。
    set_optional_var_names = _optional_dependency_var_names(  # 需要从命令中剥离的可选依赖变量名集合
        variables  # 当前 Makefile 已知的变量全集
    )

    # 初始化保留下来的命令 token 列表
    list_kept_tokens: list[str] = []  # 过滤 UVM 后仍保留的命令 token

    # 记录本条命令是否真的剥离了可选依赖内容
    bool_skipped_dependency = False  # 当前命令是否命中过 UVM 依赖

    # 按 shell 词法逐项检查每个 token
    for str_token in shlex.split(command_piece, posix=True):

        # 直接命中 UVM 或间接引用 UVM 变量的 token 一律剥离
        if _references_optional_var(str_token, set_optional_var_names) or _is_uvm_token(
            str_token
        ):

            # 记下本条命令发生过 UVM 剥离
            bool_skipped_dependency = True  # 当前命令确实包含 UVM 依赖

            # 跳过该 token，不让它进入核心导入结果
            continue

        # 其余 token 原样保留
        list_kept_tokens.append(str_token)  # 当前仍属于核心导入范围的 token

    # 只要发生过剥离，就记录 UVM 降级依赖诊断
    if bool_skipped_dependency:

        # 把 UVM 记为可选外部依赖并写入标准诊断
        _mark_uvm_dependency(diagnostics, optional_deps)

    # 返回过滤后的命令字符串，供后续再做变量展开
    return " ".join(list_kept_tokens)

# 把 `+define+NAME=VALUE` 风格宏定义写入字典
def _add_define(defines: dict[str, str], define: str) -> None:
    """
    解析一个宏定义 token，并写入 `defines` 映射。

    :param defines: 宏定义映射表，dtype=dict[str, str]，unit=macro definition map
    :param define: 去掉 `+define+` 前缀后的宏定义文本，dtype=str，unit=macro expression
    :return: 无业务返回值，直接原位更新传入映射
    """

    # 带 `=` 的宏定义需要拆出名称和值
    if "=" in define:

        # 把拆分结果保存在二元元组中，便于后续分别取名和值
        tuple_name_value = define.split("=", 1)  # 当前宏定义拆分出的名称和值元组

        # 读取宏定义名称
        str_name = tuple_name_value[0]  # 当前宏定义的名称

        # 读取宏定义值
        str_value = tuple_name_value[1]  # 当前宏定义的显式取值

    # 未显式给值的宏默认记为 1
    else:

        # 保存缺省值分支里的宏定义名称
        str_name = define  # 未显式赋值的宏定义名称

        # 为布尔式宏定义补上缺省值 1
        str_value = "1"  # 未显式赋值时的宏定义默认值

    # 空宏名无意义，因此只在有名称时才写入映射
    if str_name:

        # 把解析结果写回宏定义字典
        defines[str_name] = str_value  # 当前宏名对应的宏值

    # 该函数只承担原位更新职责，不返回额外结果
    return None

# 把 `-cm line+cond+...` 字符串拆成覆盖率指标列表
def _coverage_metrics(value: str) -> list[str]:
    """
    解析 VCS 覆盖率指标串。

    :param value: `-cm` 后面的原始指标串，dtype=str，unit=coverage metric expression
    :return: 去掉空项后的覆盖率指标列表，dtype=list[str]，unit=coverage metric
    """

    # 返回按 `+` 拆分且不含空字符串的覆盖率指标列表
    return [str_item for str_item in value.split("+") if str_item]

# 消费一条 filelist token，并把结构化结果写回共享缓冲区
def _consume_filelist_detail_token(
    list_tokens: list[str],
    *,
    int_index: int,
    path_base: Path,
    project_root: Path,
    dict_filelist_buffers: dict[str, object],
) -> int:
    """
    解析单个 filelist token，并返回下一个待处理下标。

    :param list_tokens: 当前 filelist 行拆分后的 token 列表，dtype=list[str]，unit=shell token
    :param int_index: 当前待处理 token 的下标，dtype=int，unit=index
    :param path_base: filelist 相对路径的解析基目录，dtype=Path，unit=filesystem path
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :param dict_filelist_buffers: filelist 解析共享缓冲区，dtype=dict[str, object]，unit=parse buffer
    :return: 下一轮应该处理的 token 下标，dtype=int，unit=index
    """

    # 先读取当前 token 文本，后续各分支都围绕这一个入口判断。
    str_token = list_tokens[int_index]  # 当前待处理的 filelist token

    # UVM 依赖不纳入核心 non-GUI manifest。
    if _is_uvm_token(str_token):

        # 当前 UVM token 不属于核心导入结果，因此只单步跳过即可。
        return int_index + 1

    # `+define+` 会补充宏定义映射。
    if str_token.startswith("+define+"):

        # 把当前宏定义写入共享的 defines 字典。
        _add_define(dict_filelist_buffers["dict_defines"], str_token.split("+define+", 1)[1])

        # 宏定义已经并入共享映射，因此索引只需前进一个 token。
        return int_index + 1

    # `+incdir+` 会补充 include 目录。
    if str_token.startswith("+incdir+"):

        # 保存相对于工程根的 include 目录，便于后续统一回放。
        dict_filelist_buffers["list_include_dirs"].append(
            _resolve_from(
                path_base,
                str_token.split("+incdir+", 1)[1],
                project_root,
            )
        )

        # include 目录已经记入共享列表，因此继续检查下一个 token 即可。
        return int_index + 1

    # `-sverilog` 需要归入 vlogan 参数。
    if str_token == "-sverilog":

        # 保留显式的 SystemVerilog 前端解析开关。
        dict_filelist_buffers["list_vlogan_args"].append(str_token)

        # 语言前端开关已经入列，当前 token 不再需要额外展开。
        return int_index + 1

    # `-cm` 后面承接覆盖率指标串。
    if str_token == "-cm":

        # 取出当前 `-cm` 携带的覆盖率指标表达式。
        str_metric_spec = _take_next(list_tokens, int_index)  # 当前 `-cm` 对应的指标字符串

        # 把指标串拆成单个覆盖率维度并追加到共享列表。
        dict_filelist_buffers["list_coverage_metrics"].extend(_coverage_metrics(str_metric_spec))

        # 保留 `-cm` 与原始参数文本，维持后续 VCS 回放兼容。
        dict_filelist_buffers["list_vcs_args"].extend(["-cm", str_metric_spec])

        # 当前覆盖率参数占用了两个 token，因此要跨过 `-cm` 与指标串。
        return int_index + 2

    # 普通 HDL 源文件路径需要落入 entries。
    if Path(str_token).suffix.lower() in SOURCE_SUFFIXES and not str_token.startswith(("+", "-")):

        # 保存相对于工程根的源码路径，供最终 manifest 汇总源码视图。
        dict_filelist_buffers["list_entries"].append(
            _resolve_from(path_base, str_token, project_root)
        )

        # 源码条目已经写入结果列表，因此继续扫描后续 token。
        return int_index + 1

    # 其余显式参数前缀统一保留到 vcs_args 里。
    if str_token.startswith("-") or str_token.startswith("+"):

        # 记录一个未进一步结构化的 filelist 参数，保证后续仍可原样回放。
        dict_filelist_buffers["list_vcs_args"].append(str_token)

    # 默认按单步推进索引，继续扫描后续 token。
    return int_index + 1

# 解析 filelist 文件，提取源码、包含目录、宏定义与覆盖率参数
def parse_filelist_details(
    filelist: Path,
    *,
    project_root: Path,
    base: Path | None = None,
) -> dict[str, object]:
    """
    解析 VCS filelist，并输出结构化明细。

    :param filelist: 待解析的 filelist 路径，dtype=Path，unit=filesystem path
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :param base: filelist 内相对路径的解析基目录，dtype=Path | None，unit=filesystem path
    :return: 包含 entries、include_dirs、defines、vlogan_args、vcs_args
        与 coverage_metrics 的结构化结果，dtype=dict[str, object]，unit=manifest fragment
    """

    # 未显式提供基目录时，默认用 filelist 所在目录解析相对路径
    path_base = base or filelist.parent  # filelist 内相对路径的实际解析基目录

    # 初始化 filelist 源文件条目列表
    list_entries: list[str] = []  # filelist 中命中的 HDL 源文件路径

    # 初始化 include 目录列表
    list_include_dirs: list[str] = []  # filelist 中收集到的 include 目录

    # 初始化宏定义映射表
    dict_defines: dict[str, str] = {}  # filelist 中收集到的宏定义

    # `-sverilog` 影响前端解析方式，因此不和后端 VCS 开关混放。
    list_vlogan_args: list[str] = []  # filelist 中需要保留的 vlogan 参数

    # 其余暂时无法细分语义的编译开关，统一留给 manifest 回放阶段再透传。
    list_vcs_args: list[str] = []  # filelist 中需要延后到 VCS 后端回放的编译开关

    # 覆盖率指标先按字符串列表缓存，最后再统一去重输出。
    list_coverage_metrics: list[str] = []  # filelist 中解析出的覆盖率指标

    # token 处理器会跨分支同时更新源码、宏和覆盖率结果，因此先打包一份可变视图。
    dict_filelist_buffers = dict(  # filelist 逐 token 解析共享缓冲区
        list_entries=list_entries,  # 最终需要写入 manifest 的源码路径集合
        list_include_dirs=list_include_dirs,  # `+incdir+` 解析出的头文件目录集合
        dict_defines=dict_defines,  # `+define+` 累积得到的宏定义映射
        list_vlogan_args=list_vlogan_args,  # 需要交给 vlogan 的语言前端开关
        list_vcs_args=list_vcs_args,  # 仍按原样透传的 VCS 编译开关
        list_coverage_metrics=list_coverage_metrics,  # `-cm` 拆出的覆盖率维度暂存区
    )

    # 逐行读取 filelist，解析受支持的参数与源码项
    for str_raw_line in filelist.read_text(encoding="utf-8", errors="replace").splitlines():

        # 先去掉当前记录首尾空白，避免后续把无意义空格误判成真实 token。
        str_line = str_raw_line.strip()  # 去除首尾空白后的 filelist 记录文本

        # 空行和注释行不参与 filelist 导入
        if not str_line or str_line.startswith("#") or str_line.startswith("//"):

            # 继续处理下一条 filelist 记录
            continue

        # 把一行 filelist 片段按 shell 词法拆成 token
        list_tokens = shlex.split(str_line, posix=True)  # 当前 filelist 行拆分后的 token 列表

        # 逐个扫描 token，提取受支持的参数和源码
        int_index = 0  # 当前正在处理的 token 下标

        # 顺序处理当前行中的所有 token
        while int_index < len(list_tokens):

            # 把当前 token 交给专门处理器，同时拿回下一轮应该继续扫描的位置。
            int_index = _consume_filelist_detail_token(  # 当前 token 消费后的下一轮扫描下标
                list_tokens,  # 当前 filelist 行按 shell 规则拆出的 token 序列
                int_index=int_index,  # 当前行内正在处理的 token 下标
                path_base=path_base,  # filelist 相对路径解释所依赖的基目录
                project_root=project_root,  # filelist 条目统一相对化时参考的工程根目录
                dict_filelist_buffers=dict_filelist_buffers,  # 当前 filelist 行共享写回的结果缓冲区
            )

    # 返回去重后的 filelist 结构化解析结果
    return {
        "entries": _dedupe(list_entries),
        "include_dirs": _dedupe(list_include_dirs),
        "defines": dict_defines,
        "vlogan_args": _dedupe(list_vlogan_args),
        "vcs_args": _dedupe(list_vcs_args),
        "coverage_metrics": _dedupe(list_coverage_metrics),
    }

# 为兼容旧调用方，保留只返回 filelist 源文件条目的简化入口
def parse_filelist(filelist: Path, *, project_root: Path) -> list[str]:
    """
    解析 filelist，并只返回源码条目列表。

    :param filelist: 待解析的 filelist 路径，dtype=Path，unit=filesystem path
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :return: filelist 中命中的源码路径列表，dtype=list[str]，unit=filesystem path
    """

    # 返回 filelist 明细中的源码 entries 列表
    return parse_filelist_details(filelist, project_root=project_root)["entries"]

# 读取一个二元参数后的下一个 token；缺失时返回空字符串
def _take_next(tokens: list[str], index: int) -> str:
    """
    读取某个 token 的下一个位置内容。

    :param tokens: 当前参数 token 列表，dtype=list[str]，unit=shell token
    :param index: 当前参数所在下标，dtype=int，unit=index
    :return: 下一个 token，缺失时返回空字符串，dtype=str，unit=shell token
    """

    # 返回当前参数后继 token；越界时回退为空字符串
    return tokens[index + 1] if index + 1 < len(tokens) else ""

# 读取一个二元参数的值，并过滤掉缺失值或下一个参数标志
def _take_value(tokens: list[str], index: int) -> str:
    """
    读取参数值，并排除缺失值或下一个开关本身。

    :param tokens: 当前参数 token 列表，dtype=list[str]，unit=shell token
    :param index: 当前参数所在下标，dtype=int，unit=index
    :return: 合法的参数值；不存在时返回空字符串，dtype=str，unit=shell token
    """

    # 先取出当前参数后继 token
    str_value = _take_next(tokens, index)  # 当前参数候选值

    # 缺失值或后继仍是参数开关时，都视作没有有效取值
    if not str_value or str_value.startswith("-") or str_value.startswith("+"):

        # 返回空字符串表示当前参数缺少合法值
        return ""

    # 正常情况下，后继 token 就是当前参数的合法取值。
    return str_value

# 把 `-f value` 这类 filelist 入口写入共享缓冲区
def _consume_vcs_source_list_token(
    tokens: list[str],
    str_token: str,
    *,
    state: VcsParseState,
    buffers: VcsParseBuffers,
    path_context: TokenPathContext,
) -> bool:
    """
    处理显式 `-f` filelist 开关。

    :param tokens: 当前命令 token 列表，dtype=list[str]，unit=shell token
    :param str_token: 当前待判断的 token，dtype=str，unit=shell token
    :param state: 当前 VCS token 扫描状态，dtype=VcsParseState，unit=parse state
    :param buffers: 当前 VCS 解析共享缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :param path_context: 当前 token 的路径解释上下文，dtype=TokenPathContext，unit=path context
    :return: 当前 token 是否已被消费，dtype=bool，unit=boolean
    """

    # 只有 `-f` 才会触发 filelist 采集。
    if str_token != "-f":

        # 其余 token 交给后续处理器继续识别。
        return False

    # 取出 filelist 的原始路径 token，后续同时用于相对化与回放。
    str_filelist_token = _take_next(  # `-f` 后面的原始 filelist token
        tokens,  # 当前 VCS 命令的完整 token 序列
        state["int_index"],  # 当前 `-f` 标记所在的扫描位置
    )

    # 把 filelist 解析成稳定的工程相对路径后并入结果。
    buffers["list_source_lists"].append(
        _resolve_from(
            path_context["path_make_dir"],
            str_filelist_token,
            path_context["path_project_root"],
        )
    )

    # 为命令回放保留未规范化的 filelist 取值。
    buffers["list_original_vcs_args"].append(str_filelist_token)

    # 向后跳过已经消费掉的 filelist 参数对。
    state["int_index"] += 2  # 当前扫描位置前进两个 token

    # 这一组二元参数已经被完整消费。
    return True

# 处理 `-o` 输出路径并同步更新状态与回放参数
def _consume_vcs_output_token(
    tokens: list[str],
    str_token: str,
    *,
    state: VcsParseState,
    buffers: VcsParseBuffers,
    path_context: TokenPathContext,
) -> bool:
    """
    处理显式 `-o` 输出路径参数。

    :param tokens: 当前命令 token 列表，dtype=list[str]，unit=shell token
    :param str_token: 当前待判断的 token，dtype=str，unit=shell token
    :param state: 当前 VCS token 扫描状态，dtype=VcsParseState，unit=parse state
    :param buffers: 当前 VCS 解析共享缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :param path_context: 当前 token 的路径解释上下文，dtype=TokenPathContext，unit=path context
    :return: 当前 token 是否已被消费，dtype=bool，unit=boolean
    """

    # 只在命中 `-o` 时才需要改写输出路径状态。
    if str_token != "-o":

        # 不是输出路径参数时直接退出当前处理器。
        return False

    # 抽出 `-o` 后面的原始路径文本，后续一份用于规范化，一份用于回放。
    str_output_token = _take_next(  # `-o` 携带的原始输出路径 token
        tokens,  # 当前 VCS token 序列
        state["int_index"],  # `-o` 标记所在的扫描位置
    )

    # 输出可执行文件路径需要先规范化到工程根语义。
    state["str_output"] = _normalize_arg_path(  # 规范化后的输出可执行文件路径
        str_output_token,  # 调用方传入的原始输出路径 token
        base=path_context["path_make_dir"],  # 输出路径相对解释时依赖的 Makefile 目录
        project_root=path_context["path_project_root"],  # 输出路径最终要相对化到的工程根目录
    )

    # manifest 里保留规范化后的 `-o` 参数对。
    buffers["list_vcs_args"].extend(["-o", state["str_output"]])

    # 原始命令回放仍需看到调用方最初传入的路径文本。
    buffers["list_original_vcs_args"].append(str_output_token)

    # `-o` 分支已经读完 flag 和输出路径两个位置。
    state["int_index"] += 2  # 让扫描器越过这组输出参数

    # 这一组输出参数已经处理完成。
    return True

# 处理 `-cm` 与 `-cm_dir` 这两类覆盖率控制参数
def _consume_vcs_coverage_token(
    tokens: list[str],
    str_token: str,
    *,
    state: VcsParseState,
    buffers: VcsParseBuffers,
    path_context: TokenPathContext,
) -> bool:
    """
    处理覆盖率指标和覆盖率目录相关 token。

    :param tokens: 当前命令 token 列表，dtype=list[str]，unit=shell token
    :param str_token: 当前待判断的 token，dtype=str，unit=shell token
    :param state: 当前 VCS token 扫描状态，dtype=VcsParseState，unit=parse state
    :param buffers: 当前 VCS 解析共享缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :param path_context: 当前 token 的路径解释上下文，dtype=TokenPathContext，unit=path context
    :return: 当前 token 是否已被消费，dtype=bool，unit=boolean
    """

    # `-cm` 需要同步补充指标列表和原始参数回放值。
    if str_token == "-cm":

        # 抽出调用方写下的覆盖率指标串。
        str_metric_spec = _take_next(  # `-cm` 后面的覆盖率指标表达式
            tokens,  # 当前待解析的 VCS 命令 token 序列
            state["int_index"],  # 当前 `-cm` 标记所在的扫描游标
        )

        # 覆盖率元数据存在时，再把指标并入 `metrics` 字段。
        if buffers["dict_coverage"] is not None:

            # 先读取历史累计过的指标列表，避免把旧值覆盖掉。
            obj_existing_metrics_payload = buffers["dict_coverage"].get(  # 已经积累过的覆盖率指标载荷
                "metrics",  # 覆盖率元数据里的指标列表字段名
                [],
            )

            # 仅在旧载荷确实是列表时才参与合并。
            if isinstance(obj_existing_metrics_payload, list):

                # 把旧载荷逐项字符串化，避免静态类型把元素误推断成未知对象。
                list_existing_metrics = [  # 已完成字符串化的历史覆盖率指标列表
                    str(obj_metric)  # 当前历史指标对应的字符串形式
                    for obj_metric in obj_existing_metrics_payload  # 历史覆盖率载荷中的单个指标成员
                ]

                # 把旧指标和新指标合并后再统一去重。
                list_merged_metrics = _dedupe(  # 合并去重后的覆盖率指标列表
                    [*list_existing_metrics, *_coverage_metrics(str_metric_spec)]  # 当前命令补入的新旧覆盖率指标全集
                )

            # 异常载荷不可信时，只保留这次新解析出的指标。
            else:

                # 回退到当前命令独立贡献出来的指标集合。
                list_merged_metrics = _coverage_metrics(str_metric_spec)  # 仅由当前 `-cm` 产生的指标列表

            # 把整理好的指标列表写回覆盖率元数据。
            buffers["dict_coverage"]["metrics"] = list_merged_metrics  # 供后续 manifest 复用的覆盖率指标列表

        # `vcs_args` 里保留原始 `-cm value` 参数对。
        buffers["list_vcs_args"].extend(["-cm", str_metric_spec])

        # 命令回放也需要记住未改写的指标串。
        buffers["list_original_vcs_args"].append(str_metric_spec)

        # `-cm` 分支已经读完覆盖率标记和指标串两个位置。
        state["int_index"] += 2  # 让扫描器越过当前覆盖率指标参数

        # 当前 `-cm` 参数对已经完整消费。
        return True

    # `-cm_dir` 需要同时写回规范化目录和覆盖率元数据。
    if str_token == "-cm_dir":

        # 先取到调用方传入的原始覆盖率目录 token。
        str_coverage_dir_token = _take_next(tokens, state["int_index"])  # `-cm_dir` 后面的原始目录 token

        # 覆盖率目录要转换成稳定的工程相对路径。
        str_compile_dir = _normalize_arg_path(  # 规范化后的覆盖率目录
            str_coverage_dir_token,  # 调用方传入的原始覆盖率目录 token
            base=path_context["path_make_dir"],  # 解释覆盖率目录时依赖的 Makefile 所在目录
            project_root=path_context["path_project_root"],  # 覆盖率目录最终要相对化到的工程根目录
        )

        # 只有调用方要求收集覆盖率元数据时才写入 compile_dir。
        if buffers["dict_coverage"] is not None:

            # compile_dir 用于后续定位编译阶段生成的覆盖率数据库。
            buffers["dict_coverage"]["compile_dir"] = str_compile_dir  # 编译阶段覆盖率数据库目录

        # `vcs_args` 里保留规范化后的覆盖率目录参数对。
        buffers["list_vcs_args"].extend(["-cm_dir", str_compile_dir])

        # 原始目录文本继续保留给命令回放通路使用。
        buffers["list_original_vcs_args"].append(str_coverage_dir_token)

        # 覆盖率目录分支到这里已经读完目录开关和目录值两个位置。
        state["int_index"] += 2  # 继续扫描 `-cm_dir` 参数对之后的下一个 token

        # 当前覆盖率目录参数已经处理结束。
        return True

    # 不是覆盖率相关 token 时让主调度器继续往下匹配。
    return False

# 处理 `-Mdir=`、`+define+` 与 `+incdir+` 这类就地结构化 token
def _consume_vcs_inline_structured_token(
    str_token: str,
    *,
    state: VcsParseState,
    buffers: VcsParseBuffers,
    path_context: TokenPathContext,
) -> bool:
    """
    处理无需额外消费后继值的结构化 VCS token。

    :param str_token: 当前待判断的 token，dtype=str，unit=shell token
    :param state: 当前 VCS token 扫描状态，dtype=VcsParseState，unit=parse state
    :param buffers: 当前 VCS 解析共享缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :param path_context: 当前 token 的路径解释上下文，dtype=TokenPathContext，unit=path context
    :return: 当前 token 是否已被消费，dtype=bool，unit=boolean
    """

    # `-Mdir=` 这类内联目录参数只需要规范化后原样保留。
    if str_token.startswith("-Mdir="):

        # 拆出等号右侧的原始目录文本。
        str_mdir_value = str_token.split("=", 1)[1]  # `-Mdir=` 对应的原始目录文本

        # 把目录规范化后重新拼回 `-Mdir=...` 形式。
        buffers["list_vcs_args"].append(
            "-Mdir="
            + _normalize_arg_path(
                str_mdir_value,
                base=path_context["path_make_dir"],
                project_root=path_context["path_project_root"],
            )
        )

        # 当前内联目录 token 自身已经足够表达这次更新。
        state["int_index"] += 1  # 当前扫描位置前进一个 token

        # 当前内联目录参数已经被消费。
        return True

    # 只有在调用方请求结构化宏定义时才处理 `+define+`。
    if str_token.startswith("+define+") and buffers["dict_defines"] is not None:

        # 把 `+define+` 后面的宏定义文本写入映射。
        _add_define(buffers["dict_defines"], str_token.split("+define+", 1)[1])

        # 当前 `+define+` token 只占用一个位置，消费后前进一格即可。
        state["int_index"] += 1  # 处理完当前宏定义 token 后的下一个扫描位置

        # 当前宏定义 token 已经完成沉淀。
        return True

    # `+incdir+` 总是只负责补充 include 目录。
    if str_token.startswith("+incdir+"):

        # 去掉前缀后，把 include 目录解释成稳定的工程相对路径。
        buffers["list_include_dirs"].append(
            _resolve_from(
                path_context["path_make_dir"],
                str_token.split("+incdir+", 1)[1],
                path_context["path_project_root"],
            )
        )

        # `+incdir+` 也是单 token 形式，因此同样只需要前进一步。
        state["int_index"] += 1  # 处理完当前 include 目录 token 后的下一个扫描位置

        # 当前 include 目录 token 已被完整处理。
        return True

    # 不是内联结构化 token 时直接返回未命中。
    return False

# 汇总调度所有会直接写入结构化缓冲区的 VCS token 处理器
def _consume_vcs_structured_token(
    tokens: list[str],
    str_token: str,
    *,
    state: VcsParseState,
    buffers: VcsParseBuffers,
    path_context: TokenPathContext,
) -> bool:
    """
    调度结构化 VCS token 的各类处理器。

    :param tokens: 当前命令 token 列表，dtype=list[str]，unit=shell token
    :param str_token: 当前待判断的 token，dtype=str，unit=shell token
    :param state: 当前 VCS token 扫描状态，dtype=VcsParseState，unit=parse state
    :param buffers: 当前 VCS 解析共享缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :param path_context: 当前 token 的路径解释上下文，dtype=TokenPathContext，unit=path context
    :return: 当前 token 是否已被消费，dtype=bool，unit=boolean
    """

    # 先处理会额外吞掉一个值 token 的 filelist 入口。
    if _consume_vcs_source_list_token(
        tokens,
        str_token,
        state=state,
        buffers=buffers,
        path_context=path_context,
    ):

        # 当前 token 已被 filelist 处理器消费。
        return True

    # 输出路径和回放参数也属于结构化元数据的一部分。
    if _consume_vcs_output_token(
        tokens,
        str_token,
        state=state,
        buffers=buffers,
        path_context=path_context,
    ):

        # 当前 token 已被输出路径处理器接管。
        return True

    # 覆盖率相关 token 需要同步写入参数列表和覆盖率元数据。
    if _consume_vcs_coverage_token(
        tokens,
        str_token,
        state=state,
        buffers=buffers,
        path_context=path_context,
    ):

        # 当前 token 已由覆盖率处理器消费完毕。
        return True

    # 最后再处理不需要读取后继值的内联结构化 token。
    return _consume_vcs_inline_structured_token(
        str_token,
        state=state,
        buffers=buffers,
        path_context=path_context,
    )

# 处理 timescale 与 debug 一类会直接更新解析状态的 token
def _consume_vcs_state_token(
    tokens: list[str],
    str_token: str,
    *,
    state: VcsParseState,
    buffers: VcsParseBuffers,
) -> bool:
    """
    处理直接修改 timescale、debug 等状态位的 token。

    :param tokens: 当前命令 token 列表，dtype=list[str]，unit=shell token
    :param str_token: 当前待判断的 token，dtype=str，unit=shell token
    :param state: 当前 VCS token 扫描状态，dtype=VcsParseState，unit=parse state
    :param buffers: 当前 VCS 解析共享缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :return: 当前 token 是否已被消费，dtype=bool，unit=boolean
    """

    # `-timescale=...` 可以直接以内联值刷新状态。
    if str_token.startswith("-timescale="):

        # 等号右侧就是需要保留的 timescale 文本。
        state["str_timescale"] = str_token.split("=", 1)[1]  # 从内联 `-timescale=` 抽出的值

        # 内联 timescale 取值已经完全编码在当前这个 token 里。
        state["int_index"] += 1  # 继续扫描 timescale 开关后面的下一个 token

        # 当前内联 timescale token 已经处理完成。
        return True

        # `-timescale value` 需要额外保留原始取值 token。
        if str_token == "-timescale":

            # 读取调用方显式传入的 timescale 文本。
            state["str_timescale"] = _take_next(tokens, state["int_index"])  # `-timescale` 对应的原始值

            # 原始取值要继续留在命令回放参数中。
            buffers["list_original_vcs_args"].append(state["str_timescale"])

            # 这一支已经读完 `-timescale` 标记和它的文本值两个位置。
            state["int_index"] += 2  # 继续扫描 timescale 参数对后面的下一个 token

            # 当前二元 timescale 参数已经完整消费。
            return True

    # 两种 debug 开关都只会更新调试访问级别状态。
    if str_token.startswith("-debug_access+") or str_token.startswith("-debug_acc+"):

        # `+` 后面的片段就是需要写回的 debug 访问级别。
        state["str_debug"] = str_token.split("+", 1)[1]  # 从 debug token 提取出的访问级别

        # debug 访问级别已经完全编码在当前这个单 token 开关里。
        state["int_index"] += 1  # 继续扫描这类调试访问开关后的下一个 token

        # 当前 debug 开关处理完成后即可返回。
        return True

    # 不是状态型 token 时交由其他处理器继续判断。
    return False

# 处理属于 vlogan/vcs 参数列表的透传型控制开关
def _consume_vcs_passthrough_token(
    tokens: list[str],
    str_token: str,
    *,
    state: VcsParseState,
    buffers: VcsParseBuffers,
) -> bool:
    """
    处理需要原样保留到参数列表的控制型 token。

    :param tokens: 当前命令 token 列表，dtype=list[str]，unit=shell token
    :param str_token: 当前待判断的 token，dtype=str，unit=shell token
    :param state: 当前 VCS token 扫描状态，dtype=VcsParseState，unit=parse state
    :param buffers: 当前 VCS 解析共享缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :return: 当前 token 是否已被消费，dtype=bool，unit=boolean
    """

    # `-sverilog` 与 `+v2k` 一类标志应沉淀到 vlogan 参数列表。
    if str_token in VLOGAN_FLAG_PREFIXES or str_token == "-sverilog":

        # 直接把当前语法兼容开关并入 vlogan 参数列表。
        buffers["list_vlogan_args"].append(str_token)

        # 这类语法兼容开关不带额外参数，只消费当前位置。
        state["int_index"] += 1  # 继续扫描下一个编译控制 token

        # 这一类 vlogan 标志已经处理结束。
        return True

    # 常见 VCS 时序与运行控制标志需要原样保留到 vcs_args。
    if (
        str_token in VCS_FLAG_PREFIXES
        or str_token.startswith("+vcs+dumpvars+")
        or str_token in {"-full64", "-R", "-lca"}
        or str_token.startswith("-debug_region+")
    ):

        # 这些开关的原始文本就是 manifest 里最稳定的记录形式。
        buffers["list_vcs_args"].append(str_token)

        # 单 token 的运行控制开关到这里就已经完整入账。
        state["int_index"] += 1  # 继续扫描下一个透传控制 token

        # 透传型单 token 开关已经消费完成。
        return True

    # `-l` 与 `-assert` 这类二元开关要额外保留一个值 token。
    if str_token in {
        "-l",
        "-notice",
        "-lca",
        "+notimingcheck",
        "+nospecify",
        "-assert",
    }:

        # 先把控制开关自身写入 VCS 参数列表。
        buffers["list_vcs_args"].append(str_token)

        # `-l` 和 `-assert` 还要继续收下一个值 token。
        if str_token in {"-l", "-assert"}:

            # 抽出二元开关对应的值，供参数列表和回放通路共用。
            str_flag_value = _take_next(  # `-l` 或 `-assert` 对应的原始值
                tokens,  # 用来补取透传开关后继值的整条命令 token 序列
                state["int_index"],  # 当前二元透传开关所在的扫描位置
            )

            # 把二元开关的取值继续追加到参数列表。
            buffers["list_vcs_args"].append(str_flag_value)

            # 命令回放通路也要看到这份原始取值。
            buffers["list_original_vcs_args"].append(str_flag_value)

            # 这一支已经消费完控制开关和它的取值两个位置。
            state["int_index"] += 2  # 让扫描器越过当前二元透传参数

            # 当前二元开关已经被完整处理。
            return True

        # 其余透传开关只消费当前 token 自身。
        state["int_index"] += 1  # 继续扫描剩余的单 token 透传开关

        # 当前单 token 控制开关已经处理完成。
        return True

    # 不属于透传型控制开关时直接返回未命中。
    return False

# 处理 `-msg_config` 这类路径敏感的控制参数
def _consume_vcs_msg_config_token(
    tokens: list[str],
    str_token: str,
    *,
    state: VcsParseState,
    buffers: VcsParseBuffers,
    path_context: TokenPathContext,
) -> bool:
    """
    处理 `-msg_config` 相关 token。

    :param tokens: 当前命令 token 列表，dtype=list[str]，unit=shell token
    :param str_token: 当前待判断的 token，dtype=str，unit=shell token
    :param state: 当前 VCS token 扫描状态，dtype=VcsParseState，unit=parse state
    :param buffers: 当前 VCS 解析共享缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :param path_context: 当前 token 的路径解释上下文，dtype=TokenPathContext，unit=path context
    :return: 当前 token 是否已被消费，dtype=bool，unit=boolean
    """

    # `-msg_config=...` 可以在当前 token 内直接拿到路径值。
    if str_token.startswith("-msg_config="):

        # 拆出等号右侧的原始配置路径文本。
        str_msg_config_value = str_token.split(  # `-msg_config=` 对应的原始路径文本
            "=",  # 只在第一个等号处分割，保留路径文本里的其余内容
            1,  # 最多切一刀，避免后续值里的等号被继续拆散
        )[1]  # 去掉 `-msg_config=` 前缀后的原始配置路径

        # 参数列表里保留规范化后的 `-msg_config value` 参数对。
        buffers["list_vcs_args"].extend(
            [
                "-msg_config",
                _normalize_arg_path(
                    str_msg_config_value,
                    base=path_context["path_make_dir"],
                    project_root=path_context["path_project_root"],
                ),
            ]
        )

        # 内联 `-msg_config=...` 只占一个 token，因此只前进一步。
        state["int_index"] += 1  # 处理完当前内联 msg_config token 后的下一个扫描位置

        # 当前内联 msg_config 参数已经处理完成。
        return True

    # `-msg_config value` 需要同时保留原始路径 token。
    if str_token == "-msg_config":

        # 读取紧随其后的原始 msg_config 路径 token。
        str_msg_config_token = _take_next(  # `-msg_config` 对应的原始路径 token
            tokens,  # 当前 `-msg_config` 所在命令的完整 token 序列
            state["int_index"],  # 当前 `-msg_config` flag 的位置，用于向后读取路径值
        )  # 与当前 `-msg_config` 成对出现的原始路径 token

        # 参数列表里写入规范化后的配置路径。
        buffers["list_vcs_args"].extend(
            [
                "-msg_config",
                _normalize_arg_path(
                    str_msg_config_token,
                    base=path_context["path_make_dir"],
                    project_root=path_context["path_project_root"],
                ),
            ]
        )

        # 原始命令重放依旧要看到调用方传入的路径文本。
        buffers["list_original_vcs_args"].append(str_msg_config_token)

        # 这一支会一次性消费开关和路径值两个位置。
        state["int_index"] += 2  # `-msg_config` 及其路径值合计占用的两个 token

        # 当前二元 msg_config 参数对已经处理结束。
        return True

    # 不是 msg_config 相关 token 时退出当前处理器。
    return False

# 处理 `-top` 这一类会更新顶层模块名的控制参数
def _consume_vcs_top_token(
    tokens: list[str],
    str_token: str,
    *,
    state: VcsParseState,
    buffers: VcsParseBuffers,
) -> bool:
    """
    处理顶层模块名相关控制 token。

    :param tokens: 当前命令 token 列表，dtype=list[str]，unit=shell token
    :param str_token: 当前待判断的 token，dtype=str，unit=shell token
    :param state: 当前 VCS token 扫描状态，dtype=VcsParseState，unit=parse state
    :param buffers: 当前 VCS 解析共享缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :return: 当前 token 是否已被消费，dtype=bool，unit=boolean
    """

    # 只有 `-top` 会在这里更新顶层模块名。
    if str_token != "-top":

        # 不是顶层控制参数时让其他处理器继续识别。
        return False

    # 用 `_take_value` 避免把后继开关误当成顶层模块名。
    str_top_value = _take_value(tokens, state["int_index"])  # `-top` 解析出的合法候选值

    # 成功拿到顶层模块名时，直接更新状态并保留原始取值。
    if str_top_value:

        # 顶层模块名需要写回状态，供返回值和 manifest 复用。
        state["str_top"] = str_top_value  # 从 `-top` 提取出的顶层模块名

        # 命令回放也要保留调用方原始给出的顶层模块名。
        buffers["list_original_vcs_args"].append(str_top_value)

        # 顶层模块分支到这里已经读完 `-top` 标记和模块名两个位置。
        state["int_index"] += 2  # 继续扫描顶层控制参数之后的下一个 token

        # 当前顶层控制参数已经完整消费。
        return True

    # 缺值时追加诊断，避免静默吞掉调用方错误。
    if buffers["list_diagnostics"] is not None:

        # 这条诊断用于解释为什么没有更新 top。
        buffers["list_diagnostics"].append("ignored -top without a value")

    # 即便缺少顶层值，也要至少越过当前 `-top` 标记一次。
    state["int_index"] += 1  # 避免扫描器停在缺值的顶层开关上

    # 当前 `-top` token 的处理到此结束。
    return True

# 汇总调度 timescale、debug、top 和透传型控制 token
def _consume_vcs_switch_token(
    tokens: list[str],
    str_token: str,
    *,
    state: VcsParseState,
    buffers: VcsParseBuffers,
    path_context: TokenPathContext,
) -> bool:
    """
    调度控制型 VCS token 的各类处理器。

    :param tokens: 当前命令 token 列表，dtype=list[str]，unit=shell token
    :param str_token: 当前待判断的 token，dtype=str，unit=shell token
    :param state: 当前 VCS token 扫描状态，dtype=VcsParseState，unit=parse state
    :param buffers: 当前 VCS 解析共享缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :param path_context: 当前 token 的路径解释上下文，dtype=TokenPathContext，unit=path context
    :return: 当前 token 是否已被消费，dtype=bool，unit=boolean
    """

    # 优先处理会直接改写状态位的 timescale 和 debug token。
    if _consume_vcs_state_token(
        tokens,
        str_token,
        state=state,
        buffers=buffers,
    ):

        # 当前 token 已被状态处理器消费。
        return True

    # `-msg_config` 需要独立做路径规范化，不能混入普通透传分支。
    if _consume_vcs_msg_config_token(
        tokens,
        str_token,
        state=state,
        buffers=buffers,
        path_context=path_context,
    ):

        # 当前 token 已由 msg_config 处理器接管。
        return True

    # `-top` 会更新顶层模块名，也要单独保持缺值诊断逻辑。
    if _consume_vcs_top_token(
        tokens,
        str_token,
        state=state,
        buffers=buffers,
    ):

        # 当前 token 已由顶层处理器消费。
        return True

    # 其余控制型开关统一走透传保留分支。
    return _consume_vcs_passthrough_token(
        tokens,
        str_token,
        state=state,
        buffers=buffers,
    )

# 解析一组 VCS/vlogan 风格 token，并把识别出的信息写入共享缓冲区
def _parse_vcs_tokens(
    tokens: list[str],
    *,
    path_context: TokenPathContext,
    buffers: VcsParseBuffers,
) -> tuple[str, str, str, str]:
    """
    解析 VCS/vlogan token 序列，并提取核心编译语义。

    :param tokens: 待解析的命令 token 列表，dtype=list[str]，unit=shell token
    :param path_context: 当前 token 的路径解释上下文，dtype=TokenPathContext，unit=path context
    :param buffers: 待原位更新的共享缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :return: timescale、debug、top 与 output 四元组，dtype=tuple[str, str, str, str]，unit=parsed VCS metadata
    """

    # 解析状态沿用历史默认值，确保旧工程没有显式参数时仍能保持兼容行为。
    vcs_parse_state_state_vcs_tokens: VcsParseState = {
        "str_timescale": "",  # 默认未解析出 timescale
        "str_debug": "all",  # 默认调试访问级别沿用历史 `all`
        "str_top": "top",  # 默认顶层模块名保留为 `top`
        "str_output": "",  # 默认未显式声明输出路径
        "int_index": 0,  # 从 token 列表头部开始顺序扫描
    }

    # 主循环只负责调度各类小处理器，不在这里堆叠业务分支。
    while vcs_parse_state_state_vcs_tokens["int_index"] < len(tokens):

        # 每一轮都先取出当前游标位置对应的 token。
        str_token = tokens[vcs_parse_state_state_vcs_tokens["int_index"]]  # 当前待解析的 VCS token

        # UVM 相关 token 不进入核心 non-GUI manifest。
        if _is_uvm_token(str_token):

            # UVM token 只需要越过，不参与其他结构化处理。
            vcs_parse_state_state_vcs_tokens["int_index"] += 1  # 跳过当前 UVM token 后的下一个扫描位置

            # 这类 token 不产生结构化输出，直接进入下一轮扫描。
            continue

        # 原始参数顺序对诊断和回放都很关键，因此先落一份原文。
        buffers["list_original_vcs_args"].append(str_token)

        # 工具名只是命令入口标签，不应计入真实编译参数。
        if str_token in {"vcs", "vlogan", "iverilog", "vlog"}:

            # 跳过工具名前缀，继续读取真正携带语义的参数 token。
            vcs_parse_state_state_vcs_tokens["int_index"] += 1  # 越过命令名后指向首个真实编译参数

            # 工具名本身不属于 manifest 参数载荷，因此直接继续扫描。
            continue

        # 先尝试消费会直接沉淀到结构化缓冲区的 token。
        if _consume_vcs_structured_token(
            tokens,
            str_token,
            state=vcs_parse_state_state_vcs_tokens,
            buffers=buffers,
            path_context=path_context,
        ):

            # 当前 token 已完成结构化处理，主循环无需重复介入。
            continue

        # 再尝试消费 timescale、debug、top 等控制型 token。
        if _consume_vcs_switch_token(
            tokens,
            str_token,
            state=vcs_parse_state_state_vcs_tokens,
            buffers=buffers,
            path_context=path_context,
        ):

            # 当前 token 已被控制参数处理器消费。
            continue

        # 未识别 token 保持忽略策略，但游标必须稳步前进。
        vcs_parse_state_state_vcs_tokens["int_index"] += 1  # 未识别 token 对应的单步游标推进结果

    # 返回当前命令流里抽取出的核心编译元数据。
    return (
        vcs_parse_state_state_vcs_tokens["str_timescale"],
        vcs_parse_state_state_vcs_tokens["str_debug"],
        vcs_parse_state_state_vcs_tokens["str_top"],
        vcs_parse_state_state_vcs_tokens["str_output"],
    )

# 从 Make 配方里提取第一条核心 vcs 命令并完成 UVM 过滤
def _extract_vcs_command_tokens(
    *,
    makefile: Path,
    variables: dict[str, str],
    make_dir: Path,
    project_root: Path,
    diagnostics: list[str], optional_deps: list[str],
) -> list[str]:
    """
    提取 Make 配方中的第一条核心 `vcs` 命令 token 序列。

    :param makefile: 待扫描的 Makefile 路径，dtype=Path，unit=filesystem path
    :param variables: 已解析出的 Make 变量表，dtype=dict[str, str]，unit=Make variable map
    :param make_dir: 当前 Makefile 所在目录，dtype=Path，unit=filesystem path
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :param diagnostics: 诊断消息列表，dtype=list[str]，unit=diagnostic message
    :param optional_deps: 可选外部依赖列表，dtype=list[str]，unit=dependency name
    :return: 过滤 UVM 并展开变量后的命令 token 列表，dtype=list[str]，unit=shell token
    """

    # 顺序扫描所有 Make 配方命令，寻找真正的 vcs 调用入口
    for str_command in _make_commands(makefile):

        # 不含 `vcs` 文本的命令无需进一步处理
        if "vcs" not in str_command:

            # 继续寻找下一个可能的 vcs 命令
            continue

        # `cmd1 && cmd2` 风格配方需要拆成独立片段处理
        list_pieces = [str_piece.strip() for str_piece in str_command.split("&&")]  # 当前配方拆出的命令片段

        # 找到真正以 `vcs ` 开头的命令片段
        for str_piece in list_pieces:

            # 只接受明确的核心 vcs 调用，不把变量名或其他命令误判进来
            if not str_piece.startswith("vcs "):

                # 当前片段不是核心 vcs 调用
                continue

            # 先剥离 UVM 相关 token，避免核心导入结果混入可选依赖
            str_cleaned_piece = _strip_optional_dependency_tokens(  # 仅保留核心非 GUI 导入真正需要的命令片段
                str_piece,  # 当前配方里疑似核心 vcs 调用的命令片段
                variables=variables,  # 剥离可选依赖时使用的 Make 变量映射
                diagnostics=diagnostics,  # 剥离过程中可追加说明的诊断消息列表
                optional_deps=optional_deps,  # 发生剥离时登记可选依赖的输出列表
            )  # 去掉可选依赖 token 后的 vcs 命令片段

            # 返回变量展开并 shell 分词后的 token 列表
            return _expanded_tokens(
                str_cleaned_piece,
                variables=variables,
                make_dir=make_dir,
                project_root=project_root,
            )

    # 未找到核心 vcs 配方时返回空列表
    return []

# 从 `cd ... && vcs ...` 风格命令中推断编译工作目录
def _vcs_workdir_from_commands(
    makefile: Path,
    *,
    project_root: Path,
    variables: dict[str, str],
) -> str:
    """
    从 Make 配方里的 `cd ... && vcs ...` 模式推断工作目录。

    :param makefile: 待扫描的 Makefile 路径，dtype=Path，unit=filesystem path
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :param variables: 已解析出的 Make 变量表，dtype=dict[str, str]，unit=Make variable map
    :return: 相对于工程根的工作目录路径，dtype=str，unit=filesystem path
    """

    # 顺序扫描所有命令配方，寻找带 `cd` 的 vcs 执行链
    for str_command in _make_commands(makefile):

        # 按 `&&` 拆开串联命令，便于识别首段 `cd`
        list_pieces = [str_piece.strip() for str_piece in str_command.split("&&")]  # 当前配方拆出的串联命令片段

        # 展开后若仍然找不到核心编译工具，则该配方无法提供工作目录线索。
        bool_has_following_vcs = any(  # 当前串联命令后续片段里是否出现核心编译工具
            _command_tool_name(  # 把后续片段展开成工具基名后再判断是不是核心编译器
                str_piece,  # 当前正在检查的后继命令片段
                variables=variables,  # 识别工具名时使用的 Make 变量映射
                make_dir=makefile.parent,  # 后继命令片段继承的 Makefile 目录
                project_root=project_root,  # 后继命令片段相对路径统一对齐到的工程根目录
            ) in {"vcs", "vcs.exe"}  # 只把核心 VCS 编译器视作有效命中
            for str_piece in list_pieces[1:]  # 只扫描首段之后的候选命令片段
        )  # 当前命令链后续片段里是否存在核心编译工具

        # 单段 `vcs ...` 命令也应视作在 Makefile 目录下直接编译。
        bool_single_piece_vcs = len(list_pieces) == 1 and _command_tool_name(  # 当前命令链是否本身就是单段核心编译命令
            list_pieces[0],  # 当前命令链唯一的命令片段
            variables=variables,  # 识别单段命令工具名时使用的 Make 变量映射
            make_dir=makefile.parent,  # 单段命令相对路径默认继承的 Makefile 目录
            project_root=project_root,  # 单段命令相对路径统一对齐到的工程根目录
        ) in {"vcs", "vcs.exe"}  # 当前命令链是否为单段核心编译命令

        # 当前配方没有提供可靠的编译工作目录线索
        if not bool_has_following_vcs and not bool_single_piece_vcs:

            # 当前命令配方不提供可靠工作目录线索
            continue

        # 读取串联命令的第一段，期望它是 `cd ...`
        str_first_piece = list_pieces[0]  # 当前命令链的首个片段

        # 仅 `cd` 开头的首段可以用于工作目录推断
        if str_first_piece.startswith("cd "):

            # 读取 `cd` 命令携带的目录参数
            str_raw_dir = shlex.split(str_first_piece, posix=True)[1]  # `cd` 命令中的目标目录文本

            # 展开目录参数里的变量引用
            str_expanded_dir = _expand_make_value(  # 展开 `cd` 目录参数中可能存在的 Make 变量引用
                str_raw_dir,  # `cd` 命令里提取出的原始目录文本
                variables=variables,  # 展开工作目录文本时可用的 Make 变量映射
                make_dir=makefile.parent,  # `cd` 相对目录默认继承的 Makefile 目录
                project_root=project_root,  # 工作目录相对路径最终对齐到的工程根目录
            )  # 变量展开后的工作目录文本

            # 把目录文本转成 Path，便于后续相对化
            path_workdir = Path(str_expanded_dir)  # `cd` 命令指向的工作目录候选

            # 相对路径按 Makefile 所在目录解释
            if not path_workdir.is_absolute():

                # 还原工作目录的真实文件系统位置
                path_workdir = makefile.parent / path_workdir  # `cd` 命令的实际工作目录

            # 返回相对于工程根的工作目录表示
            return _rel(path_workdir, project_root)

        # 单段编译命令默认在 Makefile 所在目录执行。
        if bool_single_piece_vcs:

            # 返回 Makefile 同目录作为工作目录
            return _rel(makefile.parent, project_root)

    # 无法推断时回退到历史默认值 `run`
    return "run"

# 展开可能包含通配符的源码 token，并对空匹配补充诊断
def _expand_source_glob(
    base: Path,
    token: str,
    *,
    project_root: Path,
    diagnostics: list[str],
) -> list[str]:
    """
    展开一个可能带通配符的源码路径 token。

    :param base: 当前源码 token 的解析基目录，dtype=Path，unit=filesystem path
    :param token: 待展开的源码 token，dtype=str，unit=filesystem path fragment
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :param diagnostics: 诊断消息列表，dtype=list[str]，unit=diagnostic message
    :return: 命中的源码相对路径列表，dtype=list[str]，unit=filesystem path
    """

    # 把 token 先转换成路径对象
    path_pattern = Path(token)  # 当前源码 token 对应的模式路径对象

    # 相对路径需要依附到基目录下再做 glob
    if not path_pattern.is_absolute():

        # 还原 token 的真实搜索路径
        path_pattern = base / path_pattern  # 当前源码 token 的实际搜索路径

    # 预先构造诊断里要展示的相对模式文本
    str_pattern_for_diag = _rel(path_pattern, project_root).replace(  # 供缺失诊断复用的稳定相对模式文本
        "\\",  # Windows 分隔符统一改写成 POSIX 斜杠
        "/",  # 诊断里统一使用 POSIX 风格路径分隔符
    )  # 当前 glob 模式的诊断展示文本

    # 仅在文件名部分包含 glob 符号时才执行真正的模式匹配
    if any(str_mark in path_pattern.name for str_mark in "*?["):

        # 执行通配匹配，保留稳定排序的命中结果
        list_candidates = sorted(  # 统一排序 glob 命中的候选路径，保证输出稳定
            path_pattern.parent.glob(path_pattern.name)  # 当前路径模式在父目录下的原始 glob 结果
        )  # 当前 glob 模式命中的候选文件路径

    # 不含 glob 符号时，把 token 本身当作唯一候选
    else:

        # 构造单元素候选列表，沿用后续统一过滤逻辑
        list_candidates = [path_pattern]  # 当前非 glob 源码 token 的唯一候选路径

    # 初始化成功命中的源码路径列表
    list_resolved: list[str] = []  # 当前 glob 模式最终命中的源码相对路径

    # 逐个检查候选路径是否为存在的 HDL 源文件
    for path_candidate in list_candidates:

        # 仅保留真实存在且后缀受支持的普通文件
        if (
            path_candidate.exists()
            and path_candidate.is_file()
            and path_candidate.suffix.lower() in SOURCE_SUFFIXES
        ):

            # 保存当前命中的源码相对路径
            list_resolved.append(
                _rel(path_candidate, project_root)
            )  # 当前 glob 扩展得到的有效源码路径

    # 带通配符却没有任何命中时，需要补充一条诊断消息
    if not list_resolved and any(str_mark in token for str_mark in "*?["):

        # 记录当前 glob 没有命中真实文件的诊断
        diagnostics.append(
            f"source glob matched no files: {str_pattern_for_diag}"
        )  # 当前 glob 模式的空匹配诊断

    # 返回命中的源码路径列表
    return list_resolved

# 解析 vlog / iverilog / vcs 风格的源码与编译参数 token
def _parse_design_tokens(
    tokens: list[str],
    *,
    context: DesignParseContext,
) -> str:
    """
    解析源码导入相关 token，提取顶层、宏定义、include 与源码列表。

    :param tokens: 待解析的命令 token 列表，dtype=list[str]，unit=shell token
    :param context: 待原位更新的设计解析上下文，dtype=DesignParseContext，unit=design parse context
    :return: 解析出的顶层模块名；缺失时返回空字符串，dtype=str，unit=top module name
    """

    # 默认未解析出顶层模块名，只有命中 `-s` 才会刷新它。
    str_top = ""  # 当前设计 token 流解析出的顶层模块名

    # 用游标顺序扫描 token 流，避免丢掉会额外吞掉后继值的参数。
    int_index = 0  # 当前正在检查的设计 token 下标

    # 每轮只处理一个 token，再由具体分支决定是否额外跨过后继值。
    while int_index < len(tokens):

        # 先抽出当前 token，后面的分支都围绕它做判定。
        str_token = tokens[int_index]  # 当前待解析的设计 token

        # 工具名本身不携带真实源码语义，直接跳过
        if str_token in {"vlog", "vcs", "iverilog"}:

            # 工具名前缀只负责指示命令类型，本身不写入导入结果。
            int_index += 1  # 跳过不携带源码语义的工具名前缀

            # 当前 token 只承担命令类型提示作用，直接继续检查后续参数。
            continue

        # `+v2k`、`-vlog01compat` 与 `-g2005` 都等价于 Verilog-2001 兼容模式。
        if str_token in {"+v2k", "-vlog01compat", "-g2005"}:

            # 统一把这些别名折叠成 manifest 使用的 `+v2k` 记录。
            context["list_vlogan_args"].append("+v2k")

            # 语法兼容别名已经被折叠进结果，下一轮继续处理后续 token。
            int_index += 1  # 跳过当前 Verilog-2001 兼容别名

            # 当前兼容模式 token 已经完成归一化，不需要再落入其它分支。
            continue

        # `-s` 明确声明顶层模块名，需要额外读取一个值 token。
        if str_token == "-s":

            # 顶层模块名直接来自 `-s` 的后继 token。
            str_top = _take_next(tokens, int_index)  # `-s` 对应的顶层模块名

            # `-s` 及其取值已经完整消费，下一轮从后继 token 继续。
            int_index += 2  # 跳过 `-s` 及其紧随的顶层模块名

            # 顶层模块名已经提取完成，本轮无需再走后续分支判定。
            continue

        # `-DNAME=VALUE` 形式可以直接沉淀成宏定义映射。
        if str_token.startswith("-D") and str_token != "-D":

            # 去掉 `-D` 前缀后即可得到完整的宏定义文本。
            _add_define(context["dict_defines"], str_token[2:])

            # 当前内联宏定义已经写入映射，继续检查下一个 token。
            int_index += 1  # 跳过当前 `-DNAME=VALUE` 宏定义 token

            # 这类内联宏定义不需要额外读取后继值，直接进入下一轮。
            continue

        # `-D NAME=VALUE` 形式需要把后继 token 作为宏定义值读取出来。
        if str_token == "-D":

            # 后继 token 就是需要落入映射的宏定义文本。
            _add_define(context["dict_defines"], _take_next(tokens, int_index))

            # `-D` 和它的取值都已经消耗掉了。
            int_index += 2  # 跳过 `-D` 以及它绑定的宏定义文本

            # 成对宏定义参数已经消费完毕，直接开始下一轮 token 判定。
            continue

        # `+define+` 是另一种常见的宏定义来源。
        if str_token.startswith("+define+"):

            # 只保留前缀之后的宏定义正文。
            _add_define(context["dict_defines"], str_token.split("+define+", 1)[1])

            # plus 风格宏定义已经写回结果，游标前移一位即可消费当前 token。
            int_index += 1  # 消费当前 `+define+` 形式的宏定义 token

            # 当前 plus 风格宏定义不会再命中其它解析分支。
            continue

        # `-Ipath` 形式把 include 目录和开关写在同一个 token 里。
        if str_token.startswith("-I") and str_token != "-I":

            # 去掉 `-I` 前缀后，再按设计解析基目录做相对化。
            context["list_include_dirs"].append(
                _resolve_from(
                    context["path_base"],
                    str_token[2:],
                    context["path_project_root"],
                )
            )

            # 内联 include 目录已经归一化入表，下一轮继续处理剩余 token。
            int_index += 1  # 跳过当前 `-Ipath` 形式的 include 参数

            # 目录参数已经落入结果，避免再进入默认推进之外的其它分支。
            continue

        # `-I path` 形式需要从后继 token 读取目录值。
        if str_token == "-I":

            # 把后继目录值规范化为工程相对路径后再收集。
            context["list_include_dirs"].append(
                _resolve_from(
                    context["path_base"],
                    _take_next(tokens, int_index),
                    context["path_project_root"],
                )
            )

            # `-I` 及其取值已消耗完成，直接跳到后继 token。
            int_index += 2  # 跳过 `-I` 与它后面的 include 目录值

            # 成对 include 参数已经完成收集，本轮不再继续判定其它分支。
            continue

        # `+incdir+path` 同样会贡献 include 目录。
        if str_token.startswith("+incdir+"):

            # 去掉前缀后，把目录转换成稳定的工程相对路径。
            context["list_include_dirs"].append(
                _resolve_from(
                    context["path_base"],
                    str_token.split("+incdir+", 1)[1],
                    context["path_project_root"],
                )
            )

            # plus 风格 include 目录已经标准化写入结果，继续扫描后续 token。
            int_index += 1  # 跳过当前 `+incdir+` 目录参数

            # 该目录 token 已经完成解析，直接进入下一轮循环。
            continue

        # 源码路径和 glob 模式都要展开成真实可用的文件列表。
        if Path(str_token).suffix.lower() in SOURCE_SUFFIXES or any(
            str_mark in str_token for str_mark in "*?["
        ):

            # 把当前 token 展开出的全部源码条目并入共享结果。
            context["list_sources"].extend(
                _expand_source_glob(
                    context["path_base"],
                    str_token,
                    project_root=context["path_project_root"],
                    diagnostics=context["list_diagnostics"],
                )
            )

            # 当前 glob token 已全部展开，继续读取后续设计 token。
            int_index += 1  # 消费掉当前 glob token 后的下一个扫描位置

            # glob 展开结果已经写回共享上下文，无需落到默认分支。
            continue

        # 其余 token 没有导入语义时，只做单步推进。
        int_index += 1  # 默认忽略 token 后的下一个扫描位置

    # 返回当前 token 流里解析出的顶层模块名。
    return str_top

# 从 Icarus 风格 Make 变量里提取源码、include 与顶层信息
def _parse_icarus_makefile(
    *,
    variables: dict[str, str],
    make_dir: Path,
    project_root: Path,
    context: DesignParseContext,
) -> tuple[str, str]:
    """
    解析 Icarus 风格的 `IVARG` 变量，并映射成 VCS manifest 语义。

    :param variables: 已解析出的 Make 变量表，dtype=dict[str, str]，unit=Make variable map
    :param make_dir: 当前 Makefile 所在目录，dtype=Path，unit=filesystem path
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :param context: 待原位更新的设计解析上下文，dtype=DesignParseContext，unit=design parse context
    :return: 解析出的顶层模块名和工作目录，dtype=tuple[str, str]，unit=top module name plus workdir path
    """

    # 没有 `IVARG` 时，说明当前工程并非 Icarus 风格入口
    if not variables.get("IVARG"):

        # 返回空 top 与空 workdir，交由上层保留原状
        return "", ""

    # 展开 `IVARG` 变量，并按 shell 词法拆分成 token
    list_tokens = _expanded_tokens(  # 先把 Icarus 风格入口展开成统一设计解析器可消费的 token 序列
        variables["IVARG"],  # Icarus 入口聚合出的原始参数文本
        variables=variables,  # 参与 `IVARG` 展开的 Make 变量映射
        make_dir=make_dir,  # Icarus 参数流里相对路径解释使用的 Makefile 目录
        project_root=project_root,  # Icarus 参数流统一相对化时参考的工程根目录
    )  # 当前 Icarus 风格参数流展开后的 token 列表

    # Icarus 风格入口默认在 `sim` 子目录下解释相对源码与 include 路径。
    design_parse_context_ctx_icarus_design: DesignParseContext = {
        "path_base": make_dir / "sim",  # Icarus 风格源码相对路径的解析基目录
        "path_project_root": project_root,  # 当前工程根目录
        # 以下结果容器继续复用上层上下文，避免 Icarus 子解析分支写回到临时副本。
        "list_sources": context["list_sources"],  # 与上层共享的源码结果列表
        "list_include_dirs": context["list_include_dirs"],  # 与上层共享的 include 目录列表
        "dict_defines": context["dict_defines"],  # 与上层共享的宏定义映射
        "list_vlogan_args": context["list_vlogan_args"],  # 与上层共享的 vlogan 参数列表
        "list_diagnostics": context["list_diagnostics"],  # 与上层共享的诊断消息列表
    }

    # 解析 `IVARG` 中的源码、顶层与 include 信息
    str_top = _parse_design_tokens(  # 复用统一设计 token 解析流程抽取顶层与编译副作用
        list_tokens,  # 当前 Icarus 参数流展开后的 token 序列
        context=design_parse_context_ctx_icarus_design,  # Icarus 参数流使用的设计解析上下文
    )  # 当前 Icarus 风格参数流导入出的顶层模块名

    # Icarus 兼容分支同样约定在 `sim` 子目录下运行。
    return str_top, _rel(make_dir / "sim", project_root)

# 从 ModelSim Tcl 脚本里提取源码、include、宏定义与顶层模块
def _parse_modelsim_tcl(
    modelsim_tcl: Path,
    *,
    project_root: Path,
    sources: list[str], include_dirs: list[str],
    defines: dict[str, str], vlogan_args: list[str],
    diagnostics: list[str],
) -> tuple[str, str]:
    """
    解析 ModelSim Tcl 脚本，并映射成 VCS manifest 语义。

    :param modelsim_tcl: 待解析的 ModelSim Tcl 路径，dtype=Path，unit=filesystem path
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :param sources: 待原位更新的源码路径列表，dtype=list[str]，unit=filesystem path
    :param include_dirs: 待原位更新的 include 目录列表，dtype=list[str]，unit=filesystem path
    :param defines: 待原位更新的宏定义映射表，dtype=dict[str, str]，unit=macro definition map
    :param vlogan_args: 待原位更新的 vlogan 参数列表，dtype=list[str]，unit=shell token
    :param diagnostics: 待原位更新的诊断消息列表，dtype=list[str]，unit=diagnostic message
    :return: 解析出的顶层模块名和工作目录，dtype=tuple[str, str]，unit=top module name plus workdir path
    """

    # 初始化 Tcl 局部变量映射表，供后续替换 `$var`
    dict_tcl_variables: dict[str, str] = {}  # 当前 Tcl 脚本中可见的局部变量映射

    # 默认未解析出顶层模块名
    str_top = ""  # 当前 Tcl 脚本导入出的顶层模块名

    # 读取 Tcl 脚本全文，便于逐行解析 `set`、`vlog` 与 `vsim`
    str_tcl_text = modelsim_tcl.read_text(  # 把 Tcl 文件整体读入内存，后续按行扫描
        encoding="utf-8",  # 按 UTF-8 读取工程内 Tcl 文本
        errors="replace",  # 异常字符用替换策略兜底，避免整份 Tcl 因编码问题中断
    )  # 当前 ModelSim Tcl 文件的全文文本

    # 顺序扫描 Tcl 脚本中的所有语句
    for str_raw_line in str_tcl_text.splitlines():

        # Tcl 行首尾空白对命令语义没有影响，这里统一收敛成规范形态。
        str_line = str_raw_line.strip()  # 去掉首尾空白后的 Tcl 语句文本

        # 空行与注释行不参与导入逻辑
        if not str_line or str_line.startswith("#"):

            # 继续检查下一条 Tcl 语句
            continue

        # `set` 语句为后续 `vlog` 命令提供变量替换来源
        if str_line.startswith("set "):

            # 按 shell 词法拆分 `set` 语句
            list_parts = shlex.split(str_line, posix=True)  # 当前 `set` 语句拆分后的 token 列表

            # 至少要有 `set NAME VALUE` 三个 token 才构成有效赋值
            if len(list_parts) >= 3:

                # 记录 Tcl 变量名
                str_variable_name = list_parts[1]  # 当前 Tcl 变量的名称

                # 记录 Tcl 变量值文本
                str_variable_value = " ".join(  # 还原 `set` 语句右侧的完整文本值
                    list_parts[2:]  # `set NAME VALUE` 里从第三个 token 开始的值片段序列
                )  # 当前 Tcl 变量对应的文本值

                # 写入 Tcl 局部变量映射表
                dict_tcl_variables[str_variable_name] = str_variable_value  # 当前 Tcl 脚本中可复用的变量值

            # 当前行已经作为 `set` 语句处理完成
            continue

        # `vlog` 语句会承载源码、include 与宏定义信息
        if str_line.startswith("vlog "):

            # 先复制原始 `vlog` 语句文本，后续再做变量替换
            str_expanded_line = str_line  # 当前待替换变量的 `vlog` 语句文本

            # 用已知 Tcl 变量替换 `$name` 形式的引用
            for str_name, str_value in dict_tcl_variables.items():

                # 先构造当前 Tcl 变量在 `vlog` 语句里的引用文本。
                str_variable_reference = f"${str_name}"  # 当前 Tcl 变量在 `vlog` 行中的 `$name` 引用文本

                # 把当前 Tcl 变量引用替换成它的已解析文本值。
                str_expanded_line = str_expanded_line.replace(  # 完成一轮变量替换后的 `vlog` 语句文本
                    str_variable_reference,  # 当前 Tcl 变量在 `vlog` 行里的引用占位文本
                    str_value,  # 当前 Tcl 变量已经解析出的替换文本
                )

            # 这里按 shell 词法重切一次，是为了让带空格的 Tcl 展开结果重新回到 `vlog` 参数粒度。
            list_tokens = shlex.split(str_expanded_line, posix=True)  # 变量展开后重新按参数边界切开的 `vlog` token 序列

            # 解析 `vlog` 语句中的源码和编译参数
            _parse_design_tokens(
                list_tokens,
                context={
                    "path_base": modelsim_tcl.parent / "sim",
                    "path_project_root": project_root,
                    "list_sources": sources,
                    "list_include_dirs": include_dirs,
                    "dict_defines": defines,
                    "list_vlogan_args": vlogan_args,
                    "list_diagnostics": diagnostics,
                },
            )

            # 当前 `vlog` 语句携带的源码和编译参数已经全部沉淀到共享缓冲区
            continue

        # `vsim` 入口更像运行命令，这里只借它恢复顶层模块名。
        if str_line.startswith("vsim "):

            # `vsim` 行也可能带引号，因此继续沿用 shell 分词规则。
            list_parts = shlex.split(str_line, posix=True)  # 带引号语义保真的 `vsim` 命令 token 列表

            # 至少要有 `vsim <target>` 两个 token 才能提取顶层
            if len(list_parts) >= 2:

                # 记录 `work.top` 风格目标名最后一段作为顶层模块
                str_top = list_parts[1].split(".")[-1]  # 当前 `vsim` 语句导入出的顶层模块名

    # ModelSim 分支统一约定在 `sim` 子目录下运行。
    return str_top, _rel(modelsim_tcl.parent / "sim", project_root)

# 从源码里扫描 `$readmemh("...")`，识别仿真前应存在的外部数据文件
def _readmemh_artifacts(
    sources: list[str],
    *,
    project_root: Path,
) -> list[str]:
    """
    提取源码中 `$readmemh` 依赖的预置数据文件名。

    :param sources: 待扫描的源码相对路径列表，dtype=list[str]，unit=filesystem path
    :param project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :return: 去重后的 `$readmemh` 数据文件名列表，dtype=list[str]，unit=artifact filename
    """

    # 初始化预仿真数据文件列表
    list_artifacts: list[str] = []  # 当前源码中引用到的 `$readmemh` 数据文件名

    # 逐个扫描 manifest 里的源码文件
    for str_rel_source in sources:

        # 还原源码文件的真实路径
        path_source = project_root / str_rel_source  # 当前待扫描的源码文件路径

        # 缺失文件或非普通文件时无法继续扫描
        if not path_source.exists() or not path_source.is_file():

            # 当前源码文件不可读，跳过其 `$readmemh` 检查
            continue

        # 读取源码文本并查找所有 `$readmemh` 调用
        for match_readmemh in READMEMH_CALL_PATTERN.finditer(
            path_source.read_text(encoding="utf-8", errors="replace")
        ):

            # 仅保留被引用文件名，避免把绝对路径写入 manifest
            list_artifacts.append(
                Path(match_readmemh.group(1)).name
            )  # 当前这一次 `$readmemh` 调用引用到的数据文件名

    # 返回去重后的 `$readmemh` 文件名列表
    return _dedupe(list_artifacts)

# 从工具链变量里识别外部交叉编译工具依赖
def _external_tool_dependencies(variables: dict[str, str]) -> list[str]:
    """
    提取 Make 变量里声明的外部工具链依赖。

    :param variables: 已解析出的 Make 变量表，dtype=dict[str, str]，unit=Make variable map
    :return: 去重后的外部工具依赖列表，dtype=list[str]，unit=tool executable name
    """

    # 初始化外部工具依赖列表
    list_dependencies: list[str] = []  # 当前工程声明的外部工具依赖

    # 只检查已知常用的工具链变量名
    for str_tool_name in ("CC", "LD", "OC", "OD", "SZ", "GDB", "CROSS_COMPILE"):

        # 读取当前工具链变量值，并去掉两端空白
        str_tool_value = variables.get(str_tool_name, "").strip()  # 当前工具链变量的可执行文件名

        # 仅把 MIPS 工具链前缀识别为外部依赖
        if str_tool_value.startswith("mips-mti-elf-"):

            # 记录当前需要的外部交叉编译工具
            list_dependencies.append(str_tool_value)  # 当前工程依赖的 MIPS 工具链可执行文件

    # 返回去重后的外部工具依赖列表
    return _dedupe(list_dependencies)

# 把任意对象尽量收敛成字典；失败时回退为空映射
def _mapping_or_empty(value: object) -> dict[str, object]:
    """
    把任意对象清洗成可安全读取 `.get()` 的映射。

    :param value: 待清洗的任意对象，dtype=object，unit=generic value
    :return: 合法映射或空映射，dtype=dict[str, object]，unit=mapping
    """

    # 只有真实映射才能继续参与后续字段抽取。
    if isinstance(value, dict):

        # 保留原始映射对象，避免丢失其中的字段内容。
        return value

    # 非映射形态统一退回到空字典。
    return {}

# 统一读取 Edalize 参数里的 plusarg、vlogdefine 与 vlogparam 三类分组
def _edalize_param_groups(edam: dict[str, object]) -> dict[str, dict[str, object]]:
    """
    合并 Edalize 顶层与 `parameters` 字段中的参数分组。

    :param edam: Edalize/CAPI2 工程描述对象，dtype=dict[str, object]，unit=project metadata map
    :return: 三类参数分组映射，dtype=dict[str, dict[str, object]]，unit=Edalize parameter groups
    """

    # 先把 `parameters` 节点清洗成稳定可读的映射。
    dict_parameters = _mapping_or_empty(  # 清洗后的 `parameters` 映射
        edam.get("parameters", {})  # 缺省时回退到空 `parameters` 节点
    )  # 供参数分组合并阶段复用的 `parameters` 映射快照

    # 初始化三类参数分组结果容器
    dict_groups: dict[str, dict[str, object]] = {
        "plusarg": {},  # 汇总 plusarg 参数
        "vlogdefine": {},  # 汇总 vlogdefine 宏定义
        "vlogparam": {},  # 汇总 vlogparam 形式的参数覆盖
    }  # 当前 Edalize 描述的三类参数分组映射

    # 按组名遍历统一合并 `parameters.<group>` 与顶层 `<group>`。
    for str_group_name in dict_groups:

        # 先读取 `parameters` 节点下的当前分组并清洗成映射。
        dict_parameter_group = _mapping_or_empty(  # `parameters` 节点下的当前分组映射
            dict_parameters.get(str_group_name)  # 当前分组在 `parameters` 节点下的原始载荷
        )  # 已清洗成映射的 `parameters.<group>` 分组

        # 先把 `parameters` 节点下的同名分组合并进结果。
        dict_groups[str_group_name].update(dict_parameter_group)

        # 再把 Edalize 顶层同名分组也清洗后并入结果。
        dict_top_level_group = _mapping_or_empty(  # Edalize 顶层的当前参数分组
            edam.get(str_group_name)  # Edalize 顶层同名分组的原始载荷
        )  # 已清洗成映射的 Edalize 顶层同名分组

        # 顶层分组用于覆盖或补充 `parameters` 节点里缺失的条目。
        dict_groups[str_group_name].update(dict_top_level_group)

    # 返回合并后的三类参数分组映射
    return dict_groups

# 把 Edalize 参数值统一转换成 manifest 中使用的字符串形式
def _edalize_value(value: object) -> str:
    """
    把 Edalize 参数值转换成 manifest 内部统一使用的字符串表示。

    :param value: 待转换的 Edalize 参数值，dtype=object，unit=generic JSON-like scalar
    :return: 适合写入 manifest 的字符串值，dtype=str，unit=serialized parameter value
    """

    # 布尔值在 VCS 宏与参数里通常需要映射成 1 或 0
    if isinstance(value, bool):

        # 返回布尔值对应的数字字符串
        return "1" if value else "0"

    # 其余标量统一退化为字符串表示
    return str(value)

# 提取 Edalize `tool_options` 中的 `vcs` 子映射
def _edalize_tool_options(edam: dict[str, object]) -> dict[str, object]:
    """
    读取 Edalize 描述中的 VCS 工具选项映射。

    :param edam: Edalize/CAPI2 工程描述对象，dtype=dict[str, object]，unit=project metadata map
    :return: VCS 工具选项映射；缺失时返回空映射，dtype=dict[str, object]，unit=tool option map
    """

    # 先保留顶层 `tool_options` 快照，缺少 `vcs` 子节点时就直接回退到它。
    dict_tool_options = _mapping_or_empty(  # 当前 Edalize 顶层工具选项映射快照
        edam.get("tool_options", {})  # Edalize 顶层声明的全部工具选项节点
    )

    # 再尝试提取 `tool_options.vcs` 子映射。
    dict_vcs_options = _mapping_or_empty(  # `tool_options.vcs` 对应的 VCS 选项映射
        dict_tool_options.get("vcs")  # 顶层 `tool_options` 中声明的 `vcs` 子节点
    )  # 清洗后的 `tool_options.vcs` 选项映射

    # 只有显式声明了 `vcs` 子映射时，才优先返回它。
    if "vcs" in dict_tool_options and dict_vcs_options == dict_tool_options.get("vcs"):

        # 这条分支保留更明确的 VCS 子选项集合。
        return dict_vcs_options

    # 否则兼容历史写法，直接把顶层映射本身当作选项映射
    return dict_tool_options

# 把单个 Edalize 文件条目并入源码与 include 结果列表
def _consume_edalize_file_entry(
    obj_file_entry: object,
    *,
    path_project_root: Path,
    list_sources: list[str],
    list_include_dirs: list[str],
) -> tuple[bool, bool]:
    """
    解析单个 Edalize 文件条目，并把其副作用写回共享列表。

    :param obj_file_entry: 单个 `files` 条目，dtype=object，unit=Edalize file entry
    :param path_project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :param list_sources: 待原位更新的源码路径列表，dtype=list[str]，unit=filesystem path
    :param list_include_dirs: 待原位更新的 include 目录列表，dtype=list[str]，unit=filesystem path
    :return: 是否命中过 SystemVerilog 与 Verilog-2001 的布尔二元组，
        dtype=tuple[bool, bool]，unit=language-detected flags
    """

    # 字符串条目按最小形态解释成 `verilogSource`，映射条目则保留结构化字段。
    if isinstance(obj_file_entry, str):

        # 直接复用字符串条目本身作为文件名。
        str_name = obj_file_entry  # 当前 Edalize 文件条目的文件名

        # 未显式提供类型时，沿用普通 Verilog 的兼容解释。
        str_file_type = "verilogSource"  # 当前字符串条目的缺省文件类型

        # 字符串条目没有结构化 include_path，因此回退为空载荷。
        obj_include_path_payload: object = []  # 当前字符串条目的 include_path 载荷

    # 结构化条目需要先清洗字段，再做名称与 include_path 提取。
    else:

        # 非字符串条目统一先清洗成稳定映射，避免裸对象访问失败。
        dict_file_entry = _mapping_or_empty(obj_file_entry)  # 当前文件条目的结构化映射

        # 优先读取 `name`，兼容老格式时再回退到 `file` 字段。
        str_name = str(dict_file_entry.get("name", dict_file_entry.get("file", "")))  # 优先取 `name`、回退 `file` 后得到的文件名

        # `file_type` 决定语言和后续参数补充策略。
        str_file_type = str(dict_file_entry.get("file_type", ""))  # 当前文件条目的类型标识

        # `include_path` 与 `include_paths` 两种写法都要兼容。
        obj_include_path_payload = dict_file_entry.get(  # 当前结构化条目的 include_path 载荷
            "include_path",  # 优先读取当前条目使用的单数键
            dict_file_entry.get("include_paths", []),  # 兼容旧版复数键 `include_paths`
        ) or []

    # 缺失文件名时没有可导入实体，直接返回未命中语言标记。
    if not str_name:

        # 上层只需要知道本条目没有贡献任何语言信息。
        return False, False

    # SystemVerilog 与 Verilog-2001 标记稍后会决定是否补语法兼容开关。
    bool_saw_systemverilog = str_file_type.startswith("systemVerilog")  # 当前条目是否声明为 SystemVerilog

    # 这一个标记专门记录条目是否显式声明老式 Verilog-2001 类型。
    bool_saw_verilog2001 = str_file_type == "verilog2001Source"  # 当前条目是否明确要求 Verilog-2001 兼容模式

    # 命中受支持的源码类型或常见源码后缀时，把路径写入 `sources`。
    if str_file_type.startswith(EDALIZE_SOURCE_TYPES) or Path(str_name).suffix.lower() in {
        ".v",
        ".sv",
        ".c",
        ".cc",
        ".cpp",
    }:

        # 源码路径要区分绝对与相对形式，避免丢掉原始工程语义。
        path_source = Path(str_name)  # 当前源码条目的路径对象

        # 相对路径保留原始相对文本，绝对路径则相对化到工程根。
        if not path_source.is_absolute():

            # 把相对源码路径按 POSIX 形式写入 manifest。
            list_sources.append(path_source.as_posix())  # 当前导入出的相对源码路径

        # 绝对源码路径改写成工程根相对形式后再写入 manifest。
        else:

            # 绝对源码路径统一压缩成工程根相对路径。
            list_sources.append(
                _rel(path_source, path_project_root)
            )  # 当前导入出的绝对源码相对路径

    # 单个 include 路径包装成单元素列表，序列载荷则逐项字符串化。
    if isinstance(obj_include_path_payload, str):

        # 这里把单字符串规范成列表，后续可以复用统一循环逻辑。
        list_include_path_values = [obj_include_path_payload]  # 当前条目规范化后的 include_path 列表

    # 列表或元组载荷要逐项字符串化，避免混入 Path 等对象。
    elif isinstance(obj_include_path_payload, (list, tuple)):

        # 把结构化 include 集合扁平化成纯字符串列表。
        list_include_path_values = [str(obj_include_item) for obj_include_item in obj_include_path_payload]  # 由序列载荷逐项字符串化得到的 include_path 列表

    # 其余异常载荷不可信，因此显式退回空 include 集合。
    else:

        # 异常载荷不可信时，直接退回空 include 列表。
        list_include_path_values = []  # 当前条目在异常载荷下的空 include_path 列表

    # 把 include 路径逐项归一化后写回共享结果。
    for str_include_dir in list_include_path_values:

        # 每个 include 项都先转成 Path，再判断绝对或相对语义。
        path_include_dir = Path(str_include_dir)  # 当前 include_path 条目的路径对象

        # 相对 include 目录按原样保留；绝对目录再相对化到工程根。
        if not path_include_dir.is_absolute():

            # 记录相对 include 目录，保持原始工程写法。
            list_include_dirs.append(
                path_include_dir.as_posix()
            )  # 当前导入出的相对 include 目录

        # 绝对 include 目录则相对化到工程根后再写回共享列表。
        else:

            # 记录工程根相对化后的绝对 include 目录。
            list_include_dirs.append(
                _rel(path_include_dir, path_project_root)
            )  # 当前导入出的绝对 include 目录相对路径

    # 把语言探测结果返回给上层，用于补语法兼容开关。
    return bool_saw_systemverilog, bool_saw_verilog2001

# 构造 Edalize 导入路径最终返回的统一 manifest
def _build_edalize_manifest(
    edam: dict[str, object],
    *,
    dict_manifest_sections: dict[str, object],
) -> dict[str, object]:
    """
    组装 Edalize 导入分支最终返回的 manifest。

    :param edam: Edalize/CAPI2 工程描述对象，dtype=dict[str, object]，unit=project metadata map
    :param dict_manifest_sections: 已整理好的 Edalize manifest 组装输入包，
        dtype=dict[str, object]，unit=manifest section map
    :return: 统一的 non-GUI smoke manifest，dtype=dict[str, object]，unit=manifest
    """

    # 直接把上游已经整理好的字段拼成最终返回载荷。
    return {
        # 源码入口与 include 目录。
        "sources": _dedupe(dict_manifest_sections["list_sources"]),
        "source_lists": [],
        "include_dirs": _dedupe(dict_manifest_sections["list_include_dirs"]),
        "defines": dict(sorted(dict_manifest_sections["dict_defines"].items())),

        # 顶层工程元数据与编译前端配置。
        "libraries": ["work"],
        "top": str(edam.get("toplevel", edam.get("top", "top"))),
        "timescale": str(edam.get("timescale", "")),
        "debug": "all",
        "kdb": True,
        "vlogan_args": _dedupe(dict_manifest_sections["list_vlogan_args"]),
        "vcs_args": _dedupe(dict_manifest_sections["list_vcs_args"]),
        "coverage": {"metrics": []},

        # 运行时参数与波形工件默认约定。
        "output": str(edam.get("name", "simv")),
        "simv_args": dict_manifest_sections["list_simv_args"],
        "plusargs": dict_manifest_sections["list_plusargs"],
        "original_vcs_args": [],
        "tools": {},
        "dump_name": "waves.fsdb",
        "workdir": "run",
        "expected_artifacts": {"dump": {"path": "waves.fsdb", "min_bytes": 1}},

        # Verdi 校验约定与补充诊断。
        "verdi_check": "fsdbreport",
        "report_signal": "/top/clk",
        "filelist_entries": [],
        "pre_sim_artifacts": [],
        "optional_external_dependencies": dict_manifest_sections["list_optional_deps"],
        "diagnostics": dict_manifest_sections["list_diagnostics"],
    }

# 导入 Edalize/CAPI2 风格工程描述
def import_edalize_project(
    edam: dict[str, object],
    *,
    project_root: Path | None = None,
) -> dict[str, object]:
    """
    把 Edalize/CAPI2 风格工程描述转换成统一的 manifest。

    :param edam: Edalize/CAPI2 工程描述对象，dtype=dict[str, object]，unit=project metadata map
    :param project_root: 用于相对化绝对路径的工程根目录，dtype=Path | None，unit=filesystem path
    :return: 统一的 non-GUI smoke manifest，dtype=dict[str, object]，unit=manifest
    """

    # 未显式提供工程根时，默认使用当前工作目录
    path_project_root = (project_root or Path.cwd()).resolve()  # Edalize 导入使用的工程根目录

    # 初始化导入后的源码路径列表
    list_sources: list[str] = []  # Edalize 导入出的源码路径

    # 初始化导入后的 include 目录列表
    list_include_dirs: list[str] = []  # Edalize 导入出的 include 目录

    # 初始化导入后的 vlogan 参数列表
    list_vlogan_args: list[str] = []  # Edalize 导入出的 vlogan 参数

    # 汇总 Edalize 里直接送给 VCS 编译阶段的原始选项，后面会原样写回 manifest。
    list_vcs_args: list[str] = []  # Edalize 导入出的 VCS 编译参数

    # 暂存需要调用方自行准备的额外依赖项，避免 manifest 假设环境已内置它们。
    list_optional_deps: list[str] = []  # Edalize 导入出的可选外部依赖

    # 累积导入阶段想反馈给调用方的诊断文本，便于后续解释缺失字段或降级行为。
    list_diagnostics: list[str] = []  # Edalize 导入过程中的诊断消息

    # 追踪文件清单里是否出现过 SystemVerilog 源，命中后才需要补编译语法开关。
    bool_saw_systemverilog = False  # 当前 Edalize 工程是否包含 SystemVerilog 输入

    # 追踪文件清单里是否出现过 Verilog-2001 源，避免无条件追加 `+v2k`。
    bool_saw_verilog2001 = False  # 当前工程是否至少出现过一次需要 `+v2k` 的 Verilog-2001 源

    # 逐个处理 `files` 字段里的文件项
    for obj_file_entry in edam.get("files", []):

        # 把单条文件记录的源码、副语言标记和 include 副作用写回共享列表。
        tuple_detected_languages = _consume_edalize_file_entry(  # 先消费单条文件记录，再汇总语言命中状态
            obj_file_entry,  # 当前待并入结果集的单条 Edalize 文件记录
            path_project_root=path_project_root,  # 当前工程根目录，供绝对路径相对化时复用
            list_sources=list_sources,  # 汇总后的源码路径输出列表
            list_include_dirs=list_include_dirs,  # 汇总后的 include 目录输出列表，供 manifest 继续复用
        )  # 当前文件条目探测出的语言命中标记二元组

        # 任一文件命中 SystemVerilog 后就一直保留该结论，供末尾补 `-sverilog` 使用。
        bool_saw_systemverilog = (
            bool_saw_systemverilog or tuple_detected_languages[0]  # 当前文件是否新增命中 SystemVerilog 输入
        )  # 工程范围内是否已经确认存在 SystemVerilog 源

        # 这里累计的是工程里是否出现过老式 Verilog 源，末尾再统一决定要不要补 `+v2k`。
        bool_saw_verilog2001 = (
            bool_saw_verilog2001 or tuple_detected_languages[1]  # 当前条目是否首次暴露需要 Verilog-2001 兼容的源
        )  # 后续生成 manifest 时是否需要考虑补上 `+v2k`

    # 只要工程里出现过 SystemVerilog 输入，就把显式语法模式写回 manifest。
    if bool_saw_systemverilog:

        # 将 SystemVerilog 模式沉淀进 vlogan 参数，避免后续回放时再依赖文件后缀猜测。
        list_vlogan_args.append("-sverilog")  # 当前 manifest 需要的 SystemVerilog 语法开关

    # Verilog-2001 文件一旦出现，就把兼容模式显式沉淀进导入结果。
    if bool_saw_verilog2001:

        # 将 Verilog-2001 兼容标记写入 vlogan 参数列表，避免后续重新猜测语法模式。
        list_vlogan_args.append("+v2k")  # 当前 manifest 需要的 Verilog-2001 开关

    # 统一读取 Edalize 中的 plusarg、define 与 param 三类参数分组
    dict_param_groups = _edalize_param_groups(edam)  # 当前 Edalize 描述的合并参数分组

    # 初始化 vlogdefine 导入后的宏定义映射表
    dict_defines: dict[str, str] = {}  # Edalize 导入出的宏定义映射

    # 将 vlogdefine 分组转换成 manifest 所需的字符串宏定义
    for str_define_name, obj_define_value in dict_param_groups["vlogdefine"].items():

        # 写入当前宏定义名称及其字符串化后的值。
        dict_defines[str(str_define_name)] = _edalize_value(  # 当前宏定义条目的规范化字符串值
            obj_define_value  # 当前宏定义条目对应的原始参数值
        )  # 当前宏名称对应的字符串化宏值

    # 把 plusarg 分组折叠成 manifest 统一使用的 `+NAME=VALUE` 文本列表。
    list_plusargs: list[str] = []  # 供 manifest 直接回放的 plusarg 文本列表

    # 逐项保留 plusarg 名称和值，避免列表推导式吞掉局部语义注释。
    for str_plusarg_name, obj_plusarg_value in dict_param_groups["plusarg"].items():

        # 把当前 plusarg 条目转换成调用 VCS 时直接可复用的文本参数。
        list_plusargs.append(  # 当前导入出的单条 plusarg 参数
            f"+{str_plusarg_name}={_edalize_value(obj_plusarg_value)}"
        )

    # 将 vlogparam 分组转成 `-pvalue+` 风格参数
    for str_param_name, obj_param_value in dict_param_groups["vlogparam"].items():

        # 追加一个 `-pvalue+NAME=VALUE` 参数
        list_vcs_args.append(
            f"-pvalue+{str_param_name}={_edalize_value(obj_param_value)}"
        )  # 当前导入出的 vlogparam 对应 VCS 参数

    # 读取 VCS 工具选项映射
    dict_tool_options = _edalize_tool_options(edam)  # 当前 Edalize 描述里的 VCS 工具选项映射

    # 追加 `vcs_options` 中的原始编译参数
    list_vcs_args.extend(
        str(obj_option)
        for obj_option in (dict_tool_options.get("vcs_options", []) or [])
    )  # 当前导入出的额外 VCS 选项

    # 把 `run_options` 原样整理成 simv 运行参数，避免导入阶段提前解释运行时语义。
    list_simv_args: list[str] = []  # Edalize 导入出的仿真运行参数列表

    # 按原始顺序保留 run_options 条目，保证回放命令行时不打乱仿真参数。
    for obj_option in (dict_tool_options.get("run_options", []) or []):

        # 将单个 run option 统一字符串化后写入 simv 参数列表。
        list_simv_args.append(str(obj_option))  # 当前导入出的单条仿真运行参数

    # 把 manifest 组装阶段需要复用的列表和映射打成一个输入包。
    dict_manifest_sections = {
        "list_sources": list_sources,  # Edalize 导入出的源码路径列表
        "list_include_dirs": list_include_dirs,  # Edalize 导入出的 include 目录列表
        "dict_defines": dict_defines,  # 后续写入 manifest `defines` 字段的宏定义映射
        "list_vlogan_args": list_vlogan_args,  # Edalize 导入出的 vlogan 语法参数
        "list_vcs_args": list_vcs_args,  # 后续直接透传给 VCS 的编译参数
        "list_simv_args": list_simv_args,  # Edalize 导入出的仿真运行参数
        "list_plusargs": list_plusargs,  # Edalize 导入出的 plusarg 文本列表
        "list_optional_deps": list_optional_deps,  # Edalize 导入出的可选依赖集合
        "list_diagnostics": list_diagnostics,  # Edalize 导入过程累计的诊断消息
    }  # 供 manifest 构造器消费的 Edalize 中间结果快照

    # 返回 Edalize 工程对应的统一 manifest
    return _build_edalize_manifest(
        edam,
        dict_manifest_sections=dict_manifest_sections,
    )

# 解析 Verdi token，提取工具路径、波形文件与 filelist 补充信息
def _parse_verdi_tokens(
    tokens: list[str],
    *,
    path_make_dir: Path, path_project_root: Path,
    list_source_lists: list[str], dict_tools: dict[str, str],
    str_dump_name: str, bool_trust_invocation: bool = False,
) -> str:
    """
    解析单条 Verdi token 流，提取波形文件与 filelist 元数据。

    :param tokens: 已完成变量展开的 Verdi token 列表，dtype=list[str]，unit=shell token
    :param path_make_dir: Makefile 所在目录，dtype=Path，unit=filesystem path
    :param path_project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :param list_source_lists: 待原位更新的 source list 列表，dtype=list[str]，unit=filesystem path
    :param dict_tools: 待原位更新的工具路径映射，dtype=dict[str, str]，unit=tool path map
    :param str_dump_name: 当前已知的波形文件名，dtype=str，unit=artifact filename
    :param bool_trust_invocation: 当前 token 流是否来自显式 `VERDI` 变量，dtype=bool，unit=bool
    :return: 合并当前 token 流后的波形文件名，dtype=str，unit=artifact filename
    """

    # 空 token 流不可能携带有效的 Verdi 元数据。
    if not tokens:

        # 沿用调用方给出的既有波形文件名。
        return str_dump_name

    # 命令首 token 的基名决定这是不是 Verdi 调用。
    str_tool_name = Path(tokens[0]).name.lower()  # 当前 token 流的工具基名

    # 运行时命令片段只有在明确识别出 Verdi 时才参与推导；显式 `VERDI` 变量则可信。
    if not bool_trust_invocation and str_tool_name not in {"verdi", "verdi.exe"}:

        # 不是 Verdi 时直接返回现有波形文件名。
        return str_dump_name

    # 显式工具路径需要保留下来，便于 manifest 后续重放。
    if tokens[0].lower() not in {"verdi", "verdi.exe"}:

        # 记录当前工程使用的显式 Verdi 可执行文件路径。
        dict_tools["verdi"] = tokens[0]  # 当前工程声明的 Verdi 可执行文件

    # 从第二个 token 开始顺序扫描 Verdi 参数。
    int_index = 1  # 当前正在检查的 Verdi 参数下标

    # 这里只关心 `-ssf` 与 `-f` 这两类会影响 manifest 的参数。
    while int_index < len(tokens):

        # 取出当前 Verdi 参数 token，供分支逐一判定。
        str_token = tokens[int_index]  # 当前待解析的 Verdi 参数

        # `-ssf` 指定波形文件名，只保留文件名本身。
        if str_token == "-ssf":

            # 波形输出文件名来自 `-ssf` 的后继 token。
            str_dump_name = Path(_take_next(tokens, int_index)).name  # 从 Verdi 参数提取出的波形文件名

            # `-ssf` 与其取值都已经消费完成，下一轮应直接跳到后继参数。
            int_index += 2  # 跳过 `-ssf` 及其紧随的文件名参数

            # 当前分支已经完整处理完波形文件参数，直接开始下一轮 token 判定。
            continue

        # `-f` 可作为 source list 的补充来源，但只在尚未确定时使用。
        if str_token == "-f" and not list_source_lists:

            # 把 Verdi token 里的 filelist 规范化成工程相对路径后写入结果。
            list_source_lists.append(
                _resolve_from(
                    path_make_dir,
                    _take_next(tokens, int_index),
                    path_project_root,
                )
            )

            # `-f` 与 filelist 路径都已写入结果，下一轮从后继 token 继续。
            int_index += 2  # 跳过 `-f` 及其对应的 filelist 参数

            # filelist 补充路径已经落盘到结果列表，本轮无需再走默认推进逻辑。
            continue

        # 其余 Verdi 参数不影响当前导入结果，按单步推进即可。
        int_index += 1  # 跳到下一个待判定的 Verdi 参数

    # 返回当前 token 流融合后的波形文件名。
    return str_dump_name

# 从单条 urg token 流里提取 coverage 数据库与报告目录
def _apply_urg_coverage_tokens(
    tokens: list[str],
    *,
    path_project_root: Path,
    str_workdir: str,
    dict_coverage: dict[str, object],
) -> None:
    """
    解析 urg token 流，并补充 coverage 目录元数据。

    :param tokens: 已完成变量展开的 urg token 列表，dtype=list[str]，unit=shell token
    :param path_project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :param str_workdir: 当前导入推导出的工作目录，dtype=str，unit=filesystem path
    :param dict_coverage: 待原位更新的覆盖率元数据映射，dtype=dict[str, object]，unit=coverage metadata
    :return: 当前辅助函数只做原位更新，不返回业务值
    """

    # 空 token 流或非 urg 调用都不可能携带 coverage 目录信息。
    if not tokens or Path(tokens[0]).name.lower() not in {"urg", "urg.exe"}:

        # 非 urg 场景不具备 coverage 目录语义，直接保持调用方传入映射不变。
        return

    # 没有 `-dir` 时就无法推导 VDB 目录。
    if "-dir" not in tokens:

        # 缺少 `-dir` 的 urg 命令无法可靠定位 VDB，宁可提前退出也不伪造目录。
        return

    # 取出 `-dir` 后面的原始 VDB 路径文本。
    str_raw_vdb = _take_next(tokens, tokens.index("-dir"))  # urg 命令里的原始 VDB 目录文本

    # 覆盖率目录需要规范化到工程相对路径语义。
    str_vdb_dir = _normalize_arg_path(  # 规范化后的 coverage 数据库目录
        str_raw_vdb,  # urg 命令中 `-dir` 携带的原始 VDB 路径
        base=path_project_root / str_workdir,  # VDB 相对路径默认解释到当前工作目录下
        project_root=path_project_root,  # coverage 目录统一相对化时参考的工程根目录
    )

    # VDB 目录是后续报告和验收步骤的核心输入。
    dict_coverage["vdb_dir"] = str_vdb_dir  # 当前 coverage 数据库目录

    # URG 报告目录固定相对于 VDB 父目录推导。
    dict_coverage["report_dir"] = _rel(  # 当前 coverage HTML 报告目录
        (path_project_root / str_vdb_dir).parent / "urgReport",  # VDB 父目录下约定的 urg HTML 报告路径
        path_project_root,  # 报告目录统一转换成工程相对路径时参考的根目录
    )

# 顺序扫描全部 Make 配方，提取仿真参数、检查脚本、coverage 与 Verdi 信息
def _scan_make_runtime_commands(
    *,
    commands: list[str],
    dict_variables: dict[str, str],
    path_make_dir: Path,
    path_project_root: Path, str_workdir: str,
    list_source_lists: list[str], dict_tools: dict[str, str],
) -> tuple[list[str], list[str], list[str], dict[str, object], str]:
    """
    扫描 Make 配方命令，提取运行期元数据。

    :param commands: 需要扫描的 Make 配方命令列表，dtype=list[str]，unit=shell command
    :param dict_variables: 已解析出的 Make 变量表，dtype=dict[str, str]，unit=Make variable map
    :param path_make_dir: Makefile 所在目录，dtype=Path，unit=filesystem path
    :param path_project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :param str_workdir: 当前推导出的工作目录，dtype=str，unit=filesystem path
    :param list_source_lists: 待原位更新的 source list 列表，dtype=list[str]，unit=filesystem path
    :param dict_tools: 待原位更新的工具路径映射，dtype=dict[str, str]，unit=tool path map
    :return: 仿真参数、编译后检查、仿真后检查、coverage 元数据与波形文件名五元组，
        dtype=tuple[list[str], list[str], list[str], dict[str, object], str]，
        unit=runtime metadata pack
    """

    # 初始化仿真运行参数列表。
    list_simv_args: list[str] = []  # 从 Make 配方里提取到的仿真参数列表

    # 初始化编译后检查脚本列表。
    list_post_compile_checks: list[str] = []  # 从 Make 配方里收集到的编译后检查命令

    # 初始化仿真后检查脚本列表。
    list_post_sim_checks: list[str] = []  # 从 Make 配方里收集到的仿真后检查命令

    # 运行期覆盖率元数据只在命中 urg 时补充路径字段。
    dict_runtime_coverage: dict[str, object] = {}  # 从运行命令扫描得到的 coverage 路径信息

    # 默认波形文件名沿用历史约定值。
    str_dump_name = "waves.fsdb"  # 运行命令扫描阶段的当前波形文件名

    # 顺序检查每一条 Make 配方命令。
    for str_command in commands:

        # 这里拆的是运行期命令链，目的是分别识别 simv、urg 与 Verdi 片段。
        list_pieces = [str_piece.strip() for str_piece in str_command.split("&&")]  # 当前命令拆分出的独立片段

        # 逐个检查拆分后的命令片段。
        for str_piece in list_pieces:

            # 每个片段都先完成变量展开，再交给后续工具识别。
            list_piece_tokens = _expanded_tokens(  # 当前运行命令片段展开后的 token 序列
                str_piece,  # 当前待识别的单个运行命令片段文本
                variables=dict_variables,  # 运行期命令变量展开所复用的 Make 变量映射
                make_dir=path_make_dir,  # 运行期相对路径默认继承的 Makefile 目录
                project_root=path_project_root,  # 运行期命令里的路径统一按工程根目录相对化
            )  # 供 simv、urg 与 Verdi 三路识别共同复用的展开结果

            # 只有本地 `./simv` 风格调用才会贡献仿真运行参数。
            if list_piece_tokens and list_piece_tokens[0].startswith("./"):

                # 记录除命令名本身外的全部仿真参数。
                list_simv_args = list_piece_tokens[1:]  # 当前 `./simv` 调用携带的全部运行期参数

            # 当前片段若是 urg 调用，则补充 coverage 路径元数据。
            _apply_urg_coverage_tokens(
                list_piece_tokens,
                path_project_root=path_project_root,
                str_workdir=str_workdir,
                dict_coverage=dict_runtime_coverage,
            )

            # 当前片段若是 Verdi 调用，则补充工具路径、波形文件名和 filelist。
            str_dump_name = _parse_verdi_tokens(  # 合并当前片段后得到的最新波形文件名
                list_piece_tokens,  # 专门送给 Verdi 参数解析器的当前片段 token 流
                path_make_dir=path_make_dir,  # Verdi 相对路径默认继承的 Makefile 目录
                path_project_root=path_project_root,  # Verdi 路径统一相对化时参考的工程根目录
                list_source_lists=list_source_lists,  # 供 Verdi `-f` 补写 source list 的共享结果列表
                dict_tools=dict_tools,  # 记录显式 Verdi 工具路径时共用的工具映射
                str_dump_name=str_dump_name,  # 当前已知波形文件名，供本轮 Verdi 片段继续覆盖或沿用
            )

        # 编译错误检查脚本需要保留为 post_compile_checks。
        if "check_compile_error.sh" in str_command:

            # 记录一条编译后检查命令。
            list_post_compile_checks.append(str_command)

        # 仿真错误检查脚本需要保留为 post_sim_checks。
        if "check_sim_error.sh" in str_command:

            # 记录一条仿真后检查命令。
            list_post_sim_checks.append(str_command)

    # 返回运行命令扫描汇总出的全部元数据。
    return (
        list_simv_args,
        list_post_compile_checks,
        list_post_sim_checks,
        dict_runtime_coverage,
        str_dump_name,
    )

# 解析 ModelSim Tcl 或 Icarus 风格入口，补充顶层模块名和工作目录
def _resolve_secondary_frontend_metadata(
    *,
    modelsim_tcl: Path | None,
    dict_variables: dict[str, str],
    path_make_dir: Path,
    path_project_root: Path,
    context: DesignParseContext,
) -> tuple[str, str]:
    """
    根据 ModelSim Tcl 或 Icarus 入口补充顶层模块名和工作目录。

    :param modelsim_tcl: 调用方显式传入的 ModelSim Tcl 路径，dtype=Path | None，unit=filesystem path
    :param dict_variables: 已解析出的 Make 变量表，dtype=dict[str, str]，unit=Make variable map
    :param path_make_dir: Makefile 所在目录，dtype=Path，unit=filesystem path
    :param path_project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :param context: 待原位更新的设计解析上下文，dtype=DesignParseContext，unit=design parse context
    :return: 前端补充入口解析出的顶层模块名和工作目录，dtype=tuple[str, str]，unit=frontend metadata
    """

    # Icarus 入口足够明确时，不再自动探测同目录 ModelSim Tcl。
    bool_has_icarus_entry = bool(dict_variables.get("IVARG"))  # 当前工程是否显式声明了 Icarus 风格入口

    # 未显式指定 Tcl 且同目录存在默认脚本时，自动采用该脚本。
    if (
        modelsim_tcl is None
        and not bool_has_icarus_entry
        and (path_make_dir / "modelsim_script.tcl").exists()
    ):

        # 默认脚本与 Makefile 同目录，适合作为二级补充入口。
        path_modelsim_tcl = path_make_dir / "modelsim_script.tcl"  # 自动发现的 ModelSim Tcl 路径

    # 其余情况下沿用调用方给出的 Tcl 路径或保持为空。
    else:

        # 显式传入的路径优先级最高；缺失时这里保持空值。
        path_modelsim_tcl = modelsim_tcl  # 最终采用的 ModelSim Tcl 路径

    # 可读的 ModelSim Tcl 优先于 Icarus 入口。
    if path_modelsim_tcl is not None and path_modelsim_tcl.exists():

        # 直接解析 Tcl 脚本，提取 top 与 workdir。
        return _parse_modelsim_tcl(
            path_modelsim_tcl,
            project_root=path_project_root,
            sources=context["list_sources"],
            include_dirs=context["list_include_dirs"],
            defines=context["dict_defines"],
            vlogan_args=context["list_vlogan_args"],
            diagnostics=context["list_diagnostics"],
        )

    # 没有 Tcl 时，若存在 Icarus 入口就退回到 Icarus 导入路径。
    if dict_variables.get("IVARG"):

        # 复用 Icarus 解析器提取顶层模块名与工作目录。
        return _parse_icarus_makefile(
            variables=dict_variables,
            make_dir=path_make_dir,
            project_root=path_project_root,
            context=context,
        )

    # 两种补充入口都缺失时，返回空元数据让调用方保持原状。
    return "", ""

# 根据显式 filelist 或自动发现的 source list 决定最终 filelist 路径
def _effective_filelist_path(
    filelist: Path | None,
    *,
    path_project_root: Path,
    list_source_lists: list[str],
) -> Path | None:
    """
    计算当前导入流程应采用的 filelist 路径。

    :param filelist: 调用方显式传入的 filelist 路径，dtype=Path | None，unit=filesystem path
    :param path_project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :param list_source_lists: 已自动发现的 source list 列表，dtype=list[str]，unit=filesystem path
    :return: 最终采用的 filelist 路径，dtype=Path | None，unit=filesystem path
    """

    # 调用方显式传入 filelist 时，始终优先复用它。
    if filelist is not None:

        # 显式 filelist 的优先级最高，当前辅助函数无需再尝试自动推断。
        return filelist

    # 没有自动发现任何 source list 时，当前流程就没有可用 filelist。
    if not list_source_lists:

        # 既没有显式 filelist，也没有自动发现结果时只能返回空值。
        return None

    # 否则采用第一条自动发现的 source list 作为有效 filelist。
    return path_project_root / list_source_lists[0]

# 读取并合并 filelist 里的源码、include、宏定义与覆盖率信息
def _merge_filelist_details(
    *,
    path_filelist: Path | None,
    path_project_root: Path,
    str_workdir: str,
    buffers: VcsParseBuffers,
) -> list[str]:
    """
    合并 filelist 结构化明细到现有解析缓冲区。

    :param path_filelist: 最终采用的 filelist 路径，dtype=Path | None，unit=filesystem path
    :param path_project_root: 工程根目录路径，dtype=Path，unit=filesystem path
    :param str_workdir: 当前导入推导出的工作目录，dtype=str，unit=filesystem path
    :param buffers: 待原位更新的 VCS 解析共享缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :return: filelist 中解析出的源码条目列表，dtype=list[str]，unit=filesystem path
    """

    # 没有 filelist 或 filelist 不存在时，不做任何合并。
    if path_filelist is None or not path_filelist.exists():

        # 缺少可用 filelist 时直接返回空源码条目列表。
        return []

    # `run` 工作目录沿用 filelist 自身目录，其余情况按推导工作目录语义解释相对路径。
    if str_workdir == "run":

        # `run` 场景保留 filelist 文件所在目录作为相对路径锚点。
        path_filelist_base = path_filelist.resolve().parent  # filelist 内相对路径默认解析基目录

    # 非 `run` 工作目录要改用推导出来的运行目录解释 filelist 相对路径。
    else:

        # 其余场景按推导出来的工作目录拼出 filelist 解析基准。
        path_filelist_base = path_project_root / str_workdir  # 非 `run` 场景下 filelist 相对路径的解析基目录

    # 先拿到 filelist 的结构化解析结果。
    dict_filelist_details = parse_filelist_details(  # 当前 filelist 解析得到的源码与参数明细
        path_filelist.resolve(),  # 当前 filelist 对应的绝对路径
        project_root=path_project_root,  # filelist 内绝对路径相对化所依据的工程根目录
        base=path_filelist_base,  # filelist 相对路径在本轮导入里的解释基目录
    )  # filelist 导入出的结构化明细

    # include 目录直接并入共享缓冲区。
    buffers["list_include_dirs"].extend(dict_filelist_details["include_dirs"])

    # 宏定义与参数列表也沿用 filelist 明细继续补充。
    buffers["dict_defines"].update(dict_filelist_details["defines"])

    # 把 filelist 里导出的 vlogan 参数继续并入现有编译参数列表。
    buffers["list_vlogan_args"].extend(dict_filelist_details["vlogan_args"])

    # 把 filelist 里导出的 VCS 运行参数继续并入共享缓冲区。
    buffers["list_vcs_args"].extend(dict_filelist_details["vcs_args"])

    # 把旧 metrics 和当前 filelist 新暴露的 coverage 维度合并后做稳定去重。
    buffers["dict_coverage"]["metrics"] = _dedupe(  # 回写到共享 coverage 缓冲区的 filelist 覆盖率指标列表
        [
            *buffers["dict_coverage"].get("metrics", []),  # 共享缓冲区里已有的覆盖率指标
            *dict_filelist_details["coverage_metrics"],  # 当前 filelist 额外贡献的 coverage 维度
        ]
    )

    # 返回 filelist 中解析出的源码条目列表，供主流程后续继续合并。
    return dict_filelist_details["entries"]

# 解析显式 `VCS` 变量，恢复核心编译入口的参数语义
def _resolve_explicit_vcs_compile_metadata(
    list_vcs_tokens: list[str],
    *,
    dict_tools: dict[str, str],
    dict_ctx_make_tokens: TokenPathContext,
    buffers: VcsParseBuffers,
) -> dict[str, object]:
    """
    从显式 `VCS` 变量里提取核心编译元数据。

    :param list_vcs_tokens: Makefile 中 `VCS` 变量拆分后的 token 列表，dtype=list[str]，unit=shell token
    :param dict_tools: 当前工程导入出的工具路径映射，dtype=dict[str, str]，unit=tool path map
    :param dict_ctx_make_tokens: 显式 `VCS` 变量使用的路径上下文，dtype=TokenPathContext，unit=path context
    :param buffers: VCS token 解析共享输出缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :return: 包含 timescale、debug、top 与 output 的结构化结果，dtype=dict[str, object]，unit=compile metadata
    """

    # `VCS` 变量首 token 若不是 `vcs`，说明工具路径被显式重写过
    if list_vcs_tokens and list_vcs_tokens[0] != "vcs":

        # 记录显式指定的 VCS 可执行文件
        dict_tools["vcs"] = list_vcs_tokens[0]  # 当前工程使用的 VCS 工具路径

    # 首 token 只是 `vcs` 命令名时，需要把真正的参数区间向后平移一位。
    if list_vcs_tokens and list_vcs_tokens[0] == "vcs":

        # 去掉命令名本体后，只保留会影响导入结果的真实编译参数。
        list_vcs_body = list_vcs_tokens[1:]  # 当前 `VCS` 变量里真正的参数 token 列表

    # 首 token 已经是显式工具路径时，整段 token 都属于有效参数流。
    else:

        # 保留完整 token 序列，避免误丢掉显式路径形式的第一个参数位。
        list_vcs_body = list_vcs_tokens  # 显式工具路径形式下需要完整保留的参数 token 列表

    # 解析 `VCS` 变量承载的核心编译元数据
    tuple_vcs_metadata = _parse_vcs_tokens(  # 当前 `VCS` 变量导入出的核心编译元数据
        list_vcs_body,  # 待解析的核心 VCS 参数 token 列表
        path_context=dict_ctx_make_tokens,  # 显式 `VCS` 变量使用的路径解释上下文
        buffers=buffers,  # 用于吸收显式 `VCS` 变量副作用结果的共享缓冲区
    )

    # 把 `VCS` 变量解析结果转换成 recipe 导入阶段复用的统一字段名。
    dict_vcs_metadata = {  # 显式 `VCS` 变量解析结果对应的结构化元数据包
        "str_timescale": tuple_vcs_metadata[0],  # `VCS` 变量解析出的 timescale
        "str_debug": tuple_vcs_metadata[1],  # `VCS` 变量解析出的 debug 级别
        "str_top": tuple_vcs_metadata[2],  # `VCS` 变量解析出的顶层模块名
        "str_output": tuple_vcs_metadata[3],  # `VCS` 变量解析出的 simv 输出路径
        "list_sources": [],  # 显式 `VCS` 变量分支不在这里单独回填源码列表
    }  # 供 recipe 导入分支继续复用的 `VCS` 元数据结果

    # 返回显式 `VCS` 变量导入得到的结构化结果。
    return dict_vcs_metadata

# 把 `VCS_FLAGS` 与 `UVM_FLAGS` 的补充语义并回 recipe 导入结果
def _apply_recipe_vcs_flag_overrides(
    *,
    dict_primary_context: PrimaryCompileContext,
    bool_parsed_expanded_vcs_command: bool,
    dict_recipe_metadata: dict[str, object],
    list_diagnostics: list[str], list_optional_deps: list[str],
    buffers: VcsParseBuffers,
) -> dict[str, object]:
    """
    把 `VCS_FLAGS` 与 `UVM_FLAGS` 的补充语义并回 recipe 导入结果。

    :param dict_primary_context: 主编译入口解析上下文，dtype=PrimaryCompileContext，unit=parse context
    :param bool_parsed_expanded_vcs_command: 是否已经通过命令配方提取过核心 vcs 命令，dtype=bool，unit=boolean
    :param dict_recipe_metadata: recipe 分支已经收集到的编译元数据，dtype=dict[str, object]，unit=compile metadata
    :param list_diagnostics: 当前工程导入过程中的诊断消息，dtype=list[str]，unit=diagnostic
    :param list_optional_deps: 当前工程导入出的可选外部依赖，dtype=list[str]，unit=dependency name
    :param buffers: VCS token 解析共享输出缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :return: 合并补充语义后的 recipe 编译元数据，dtype=dict[str, object]，unit=compile metadata
    """

    # 读取 `VCS_FLAGS` 展开时要复用的变量映射，避免后续重复索引主上下文。
    dict_variables = dict_primary_context["dict_variables"]  # 当前 Makefile 解析出的变量映射表

    # 读取 `VCS_FLAGS` 相对路径换算所依赖的 Makefile 目录。
    path_make_dir = dict_primary_context["path_make_dir"]  # 当前 Makefile 的父目录

    # 读取所有 recipe 相对路径最终对齐到的统一工程根目录。
    path_project_root = dict_primary_context["path_project_root"]  # 当前导入使用的工程根目录

    # 读取核心命令与 `VCS_FLAGS` 共用的路径锚点，保证 flag token 的路径语义一致。
    dict_ctx_flag_tokens = dict_primary_context["dict_ctx_flag_tokens"]  # 核心命令和 `VCS_FLAGS` 的路径上下文

    # `VCS_FLAGS` 存在且尚未被完整核心命令覆盖时，再解析其补充语义
    if dict_variables.get("VCS_FLAGS") and not bool_parsed_expanded_vcs_command:

        # 展开 `VCS_FLAGS`，恢复当前工程额外声明的编译参数
        list_flag_tokens = _expanded_tokens(  # 当前 `VCS_FLAGS` 变量展开后的参数 token 列表
            dict_variables["VCS_FLAGS"],  # 待展开的 `VCS_FLAGS` 原始文本
            variables=dict_variables,  # 展开 `VCS_FLAGS` 时可用的变量映射
            make_dir=path_make_dir,  # 让 `VCS_FLAGS` 里的相对 filelist/include 路径继续沿用当前 Makefile 目录语义
            project_root=path_project_root,  # `VCS_FLAGS` 路径相对化时统一参考的工程根目录
        )

        # 解析 `VCS_FLAGS` 承载的补充编译元数据
        tuple_flag_metadata = _parse_vcs_tokens(  # 当前 `VCS_FLAGS` 导入出的补充编译元数据
            list_flag_tokens,  # 待解析的 `VCS_FLAGS` 参数 token 列表
            path_context=dict_ctx_flag_tokens,  # `VCS_FLAGS` 与主命令共用的路径解释上下文
            buffers=buffers,  # 累积 `VCS_FLAGS` 解析副作用的共享缓冲区，避免丢失补充参数
        )

        # 逐字段吸收 `VCS_FLAGS` 的非空覆盖值，避免重复写四组近似注释分支。
        for str_field_name, int_metadata_index in (
            ("str_timescale", 0),
            ("str_debug", 1),
            ("str_top", 2),
            ("str_output", 3),
        ):

            # 只在 `VCS_FLAGS` 真正提供了非空覆盖值时才改写 recipe 既有字段。
            if tuple_flag_metadata[int_metadata_index]:

                # 把 `VCS_FLAGS` 补充出的字段值写回当前 recipe 编译元数据映射。
                dict_recipe_metadata[str_field_name] = tuple_flag_metadata[int_metadata_index]  # `VCS_FLAGS` 提供的字段覆盖值

    # 仍存在 `UVM_FLAGS` 时，只把它记为可选依赖，不纳入核心 manifest
    if dict_variables.get("UVM_FLAGS"):

        # 补充 UVM 可选依赖诊断
        _mark_uvm_dependency(list_diagnostics, list_optional_deps)

    # 返回合并 `VCS_FLAGS` 与 `UVM_FLAGS` 后的 recipe 编译元数据
    return dict_recipe_metadata

# 解析命令配方、`SRCS` 与 `VCS_FLAGS`，恢复核心编译入口的参数语义
def _resolve_recipe_vcs_compile_metadata(
    *,
    dict_primary_context: PrimaryCompileContext,
    dict_tools: dict[str, str],
    list_diagnostics: list[str],
    list_optional_deps: list[str],
    buffers: VcsParseBuffers,
) -> dict[str, object]:
    """
    从命令配方、`SRCS` 与 `VCS_FLAGS` 中提取核心编译元数据。

    :param dict_primary_context: 主编译入口解析上下文，dtype=PrimaryCompileContext，unit=parse context
    :param dict_tools: 当前工程导入出的工具路径映射，dtype=dict[str, str]，unit=tool path map
    :param list_diagnostics: 当前工程导入过程中的诊断消息，dtype=list[str]，unit=diagnostic
    :param list_optional_deps: 当前工程导入出的可选外部依赖，dtype=list[str]，unit=dependency name
    :param buffers: VCS token 解析共享输出缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :return: 包含源码与核心编译元数据的结构化结果，dtype=dict[str, object]，unit=compile metadata
    """

    # 读取主入口上下文里的 Makefile 路径，供命令配方扫描与诊断定位复用。
    path_makefile = dict_primary_context["path_makefile"]  # 当前导入入口 Makefile 的真实路径

    # 读取主入口上下文里的变量映射，供命令提取与 token 展开共享使用。
    dict_variables = dict_primary_context["dict_variables"]  # recipe 分支后续所有变量展开共用的变量映射表

    # 读取主入口对应的目录基准，供源码 token 和 filelist 相对化使用。
    path_make_dir = dict_primary_context["path_make_dir"]  # 当前 Makefile 所在的父目录

    # 读取当前导入统一对齐的工程根目录，避免不同分支各自猜测根路径。
    path_project_root = dict_primary_context["path_project_root"]  # 当前导入流程统一参考的工程根目录

    # 读取命令 token 与 `VCS_FLAGS` 共用的路径上下文，保证后续解析口径一致。
    dict_ctx_flag_tokens = dict_primary_context["dict_ctx_flag_tokens"]  # 核心命令与 `VCS_FLAGS` 共用的路径上下文

    # `VCS` 变量缺失时，源码列表需要从命令配方或 `SRCS` 变量恢复
    list_sources: list[str] = []  # 当前核心编译入口导入出的源码路径

    # 默认 timescale 为空，表示尚未从命令配方或 `VCS_FLAGS` 中解析出来
    str_timescale = ""  # 当前核心编译入口导入出的 timescale

    # legacy smoke 流程缺省把 debug 访问级别视为 `all`，这里继续沿用兼容值。
    str_debug = "all"  # 尚未解析到显式覆盖前沿用的兼容 debug 级别

    # 未显式解析出顶层模块名时，继续沿用 legacy smoke 使用的 `top` 入口名。
    str_top = "top"  # 当前核心编译入口导入出的顶层模块名

    # 默认输出路径为空，表示尚未从命令配方或 `VCS_FLAGS` 中解析出来
    str_output = ""  # 当前核心编译入口导入出的 simv 输出路径

    # 标记是否已经通过扩展命令完整解析过一条核心 vcs 命令
    bool_parsed_expanded_vcs_command = False  # 当前工程是否已从命令配方中提取过核心 vcs 命令

    # 从命令配方里提取一条完整的核心 vcs 命令。
    list_command_tokens = _extract_vcs_command_tokens(  # 当前从 Make 配方提取到的核心 vcs 命令 token 列表
        makefile=path_makefile,  # 参与命令配方提取的入口 Makefile 路径
        variables=dict_variables,  # 命令提取阶段用于变量展开的 Make 变量映射
        make_dir=path_make_dir,  # 命令提取阶段默认继承的 Makefile 目录
        project_root=path_project_root,  # 命令提取阶段相对路径统一对齐到的工程根目录
        diagnostics=list_diagnostics,  # 导入过程中累计的诊断输出列表
        optional_deps=list_optional_deps,  # 提取可选依赖时同步回填的依赖登记列表
    )

    # 成功提取命令配方时，优先按该命令完成一次完整导入
    if list_command_tokens:

        # 记录当前工程已经走过扩展命令导入分支
        bool_parsed_expanded_vcs_command = True  # 当前工程已通过配方提取核心 vcs 命令

        # 配方中的工具名若不是 `vcs`，说明工具路径被显式改写过
        if list_command_tokens[0] != "vcs":

            # 把命令首 token 已展开出的真实 VCS 可执行路径写回工具映射。
            dict_tools["vcs"] = list_command_tokens[0]  # 命令配方显式指定的 VCS 可执行路径

        # 若首 token 只是 `vcs` 命令名，则从第二个 token 开始才是实际参数区间。
        list_command_body = (  # 去掉工具名后的核心 vcs 参数 token 列表
            list_command_tokens[1:]  # 首 token 为 `vcs` 命令名时跳过工具名本体
            if list_command_tokens and list_command_tokens[0] == "vcs"  # 仅当首 token 确实是 `vcs` 时才缩短参数区间
            else list_command_tokens  # 首 token 已是显式工具路径时保留完整参数流
        )

        # 先从参数流中筛出显式列出的源码 token。
        list_raw_sources = _source_tokens(list_command_body)  # 当前核心 vcs 命令中显式给出的源码 token 列表

        # 把命令里显式列出的源码 token 规整成稳定的工程相对路径。
        list_sources = [  # 当前核心 vcs 命令导入出的源码路径
            _resolve_source_token(path_make_dir, str_item, path_project_root)  # 按入口目录与工程根目录换算的单个源码路径
            for str_item in list_raw_sources  # 当前核心 vcs 命令里待规范化的单个源码 token
        ]

        # 解析核心 vcs 命令承载的编译元数据
        tuple_command_metadata = _parse_vcs_tokens(  # 当前核心 vcs 命令导入出的核心编译元数据
            list_command_body,  # 当前命令剥掉工具名后真正送入解析器的 vcs 参数 token 列表
            path_context=dict_ctx_flag_tokens,  # 核心 vcs token 解析阶段复用的路径上下文
            buffers=buffers,  # 承接核心 vcs 解析结果的共享缓冲区
        )

        # 再按固定字段顺序把命令解析结果拆回四个核心编译标量。
        (
            str_timescale,  # 当前核心 vcs 命令导入出的 timescale
            str_debug,  # 当前核心 vcs 命令导入出的 debug 级别
            str_top,  # 当前核心 vcs 命令导入出的顶层模块名
            str_parsed_output,  # 当前核心 vcs 命令导入出的 simv 输出路径
        ) = tuple_command_metadata

        # 只有核心配方真的提供了输出文件名时，才覆盖前面保留的默认输出路径。
        str_output = str_parsed_output or str_output  # 若命令配方给出输出名则覆盖，否则保留既有默认值

    # 没有显式核心命令时，尝试从 `SRCS` 变量恢复源码列表
    elif dict_variables.get("SRCS"):

        # 先把 `SRCS` 变量展开成稳定 token 流，再从其中筛出源码路径。
        list_expanded_src_tokens = _expanded_tokens(  # `SRCS` 变量展开后的原始 token 列表
            dict_variables["SRCS"],  # `SRCS` 原始变量文本
            variables=dict_variables,  # 展开 `SRCS` 时复用的 Make 变量快照
            make_dir=path_make_dir,  # `SRCS` 相对路径默认继承的入口目录
            project_root=path_project_root,  # `SRCS` 相对路径统一参考的工程根目录
        )

        # 再从展开后的 token 流里筛出真正的源码路径。
        list_raw_sources = _source_tokens(list_expanded_src_tokens)  # 当前 `SRCS` 变量导入出的源码 token 列表

        # 把 `SRCS` 展开后的源码 token 同样规整成工程相对路径。
        list_sources = [  # 当前 `SRCS` 变量导入出的源码路径
            _resolve_source_token(path_make_dir, str_item, path_project_root)  # 当前源码 token 规整后的工程相对路径
            for str_item in list_raw_sources  # `SRCS` 展开结果中的单个源码 token
        ]

    # 把 `VCS_FLAGS` 与 `UVM_FLAGS` 的补充语义并回当前 recipe 导入结果
    return _apply_recipe_vcs_flag_overrides(
        dict_primary_context=dict_primary_context,
        bool_parsed_expanded_vcs_command=bool_parsed_expanded_vcs_command,
        dict_recipe_metadata={
            "list_sources": list_sources,
            "str_timescale": str_timescale,
            "str_debug": str_debug,
            "str_top": str_top,
            "str_output": str_output,
        },
        list_diagnostics=list_diagnostics,
        list_optional_deps=list_optional_deps,
        buffers=buffers,
    )

# 从 `+vcs+dumpvars+...` 参数里恢复最终要写入 manifest 的波形文件名
def _refresh_dump_name_from_vcs_args(
    list_vcs_args: list[str],
    *,
    str_dump_name: str,
) -> str:
    """
    扫描 VCS 参数，提取最后一次显式声明的波形文件名。

    :param list_vcs_args: 已收集的 VCS 参数列表，dtype=list[str]，unit=VCS argument
    :param str_dump_name: 调用方当前持有的默认波形文件名，dtype=str，unit=filesystem path
    :return: 综合显式 dumpvars 参数后的波形文件名，dtype=str，unit=filesystem path
    """

    # 先把调用方已经确认的波形文件名当作回退值保留下来。
    str_resolved_dump_name = str_dump_name  # 尚未遇到更新声明前沿用的波形文件名

    # 顺序扫描全部 VCS 参数，确保最后一次 dumpvars 声明拥有最高优先级。
    for str_vcs_arg in list_vcs_args:

        # 非 dumpvars 参数不会携带波形文件名，因此直接跳过。
        if not str_vcs_arg.startswith("+vcs+dumpvars+"):

            # 继续检查后续参数中是否还存在显式波形文件名声明。
            continue

        # 按历史约定取最后一个 `+` 之后的片段作为 FSDB 文件名。
        str_resolved_dump_name = str_vcs_arg.rsplit("+", 1)[-1]  # 由 dumpvars 参数解析出的最新波形文件名

    # 返回已经吸收 dumpvars 声明后的最终波形文件名。
    return str_resolved_dump_name

# 检查 `$readmemh` 依赖文件是否缺失，并把风险追加到诊断列表
def _report_missing_pre_sim_artifacts(
    list_pre_sim_artifacts: list[str],
    *,
    path_make_dir: Path,
    list_diagnostics: list[str],
) -> None:
    """
    为缺失的预仿真数据文件追加兼容旧行为的诊断消息。

    :param list_pre_sim_artifacts: `$readmemh` 引用到的数据文件列表，dtype=list[str]，unit=filesystem path
    :param path_make_dir: 入口 Makefile 所在目录，dtype=Path，unit=filesystem path
    :param list_diagnostics: 导入流程累积的诊断消息列表，dtype=list[str]，unit=diagnostic
    :return: 本函数只原位追加诊断，不返回业务值，dtype=None，unit=none
    """

    # 逐个检查 `$readmemh` 数据文件是否已出现在入口目录可见范围内。
    for str_artifact in list_pre_sim_artifacts:

        # 现有兼容逻辑只验证 Makefile 同目录下能否直接看到该文件。
        if (path_make_dir / str_artifact).exists():

            # 文件已经存在时不追加缺失诊断，继续检查下一项即可。
            continue

        # 缺失文件要尽快暴露给上层调用方，避免仿真前才静默失败。
        list_diagnostics.append(
            f"pre-sim artifact missing before simulation: {str_artifact}"
        )  # 当前缺失的预仿真数据文件诊断

    # 该辅助函数只承担原位补诊断职责，不生成额外返回值。
    return None

# 组装 import_project 最终返回的统一 manifest 结构
def _build_import_manifest(
    *,
    dict_manifest_context: dict[str, object],
) -> dict[str, object]:
    """
    把导入流程已收集的各类元数据收敛成最终 manifest。

    :param dict_manifest_context: 汇总好的 manifest 组装上下文，dtype=dict[str, object]，unit=manifest context
    :return: 供 non-GUI smoke 流程消费的统一 manifest，dtype=dict[str, object]，unit=manifest
    """

    # 先取出 coverage 元数据，便于下面分别整理指标列表和附加字段。
    dict_coverage = dict_manifest_context["dict_coverage"]  # 最终 coverage 元数据映射

    # 直接返回最终 manifest，保持字段名和旧消费方约定完全兼容。
    return {
        # 先汇总源码、目录与宏定义这些静态编译输入字段。
        "sources": _dedupe([*dict_manifest_context["list_sources"], *dict_manifest_context["list_filelist_entries"]]),
        "source_lists": _dedupe(dict_manifest_context["list_source_lists"]),
        "include_dirs": _dedupe(dict_manifest_context["list_include_dirs"]),
        "defines": dict(sorted(dict_manifest_context["dict_defines"].items())),

        # 再写入编译入口需要消费的核心控制字段。
        "libraries": ["work"],
        "top": dict_manifest_context["str_top"],
        "timescale": dict_manifest_context["str_timescale"],
        "debug": dict_manifest_context["str_debug"],
        "kdb": True,
        "vlogan_args": _dedupe(dict_manifest_context["list_vlogan_args"]),
        "vcs_args": _dedupe(dict_manifest_context["list_vcs_args"]),

        # coverage 字段既要保留指标列表，也要兼容额外目录元数据。
        "coverage": {
            "metrics": _dedupe(dict_coverage.get("metrics", [])),
            **{
                str_key: obj_value
                for str_key, obj_value in dict_coverage.items()
                if str_key != "metrics"
            },
        },

        # 最后写入仿真回放、工件检查与诊断相关字段。
        "output": dict_manifest_context["str_output"],
        "simv_args": dict_manifest_context["list_simv_args"],
        "post_compile_checks": _dedupe(dict_manifest_context["list_post_compile_checks"]),
        "post_sim_checks": _dedupe(dict_manifest_context["list_post_sim_checks"]),
        "original_vcs_args": dict_manifest_context["list_original_vcs_args"],
        "tools": dict_manifest_context["dict_tools"],
        "dump_name": dict_manifest_context["str_dump_name"],
        "workdir": dict_manifest_context["str_workdir"],
        "expected_artifacts": {"dump": {"path": dict_manifest_context["str_dump_name"], "min_bytes": 1}},
        "verdi_check": "fsdbreport",
        "report_signal": "/top/clk",
        "filelist_entries": dict_manifest_context["list_filelist_entries"],
        "pre_sim_artifacts": dict_manifest_context["list_pre_sim_artifacts"],
        "optional_external_dependencies": _dedupe(dict_manifest_context["list_optional_deps"]),
        "diagnostics": _dedupe(dict_manifest_context["list_diagnostics"]),
    }

# 根据显式 `VCS` 变量或 recipe 分支恢复主编译入口的核心元数据
def _build_primary_compile_context(
    dict_import_workflow_state: ImportProjectState,
) -> PrimaryCompileContext:
    """
    从导入主流程共享状态中提取主编译入口需要的上下文。

    :param dict_import_workflow_state: `import_project` 的共享状态映射，dtype=ImportProjectState，unit=workflow state
    :return: 主编译入口解析上下文，dtype=PrimaryCompileContext，unit=parse context
    """

    # 统一导出主编译入口恢复逻辑需要复用的路径、变量与 token 上下文。
    return {
        "path_makefile": dict_import_workflow_state["path_makefile"],
        "dict_variables": dict_import_workflow_state["dict_variables"],
        "path_make_dir": dict_import_workflow_state["path_make_dir"],
        "path_project_root": dict_import_workflow_state["path_project_root"],
        "dict_ctx_make_tokens": dict_import_workflow_state["dict_ctx_make_tokens"],
        "dict_ctx_flag_tokens": dict_import_workflow_state["dict_ctx_flag_tokens"],
    }

# 初始化 `import_project` 主流程会跨阶段复用的路径、变量与共享缓冲区
def _initialize_import_project_state(
    *,
    makefile: Path,
    project_root: Path | None,
    make_vars: dict[str, str] | None,
) -> ImportProjectState:
    """
    构建 `import_project` 主流程使用的共享状态映射。

    :param makefile: 工程入口 Makefile 路径，dtype=Path，unit=filesystem path
    :param project_root: 调用方可选传入的工程根目录，dtype=Path | None，unit=filesystem path
    :param make_vars: 调用方注入或覆盖的 Make 变量表，dtype=dict[str, str] | None，unit=Make variable override map
    :return: 供各阶段复用的导入共享状态，dtype=ImportProjectState，unit=workflow state
    """

    # 先把入口 Makefile 固定成绝对路径，避免后面混入调用目录语义。
    path_makefile = makefile.resolve()  # 后续所有相对解析都锚定到这个入口 Makefile

    # 若调用方未单独指定工程根，就沿用入口 Makefile 所在目录作为根锚点。
    path_project_root = (project_root or path_makefile.parent).resolve()  # 源码、工件和 filelist 统一参考的工程根

    # 单独保留入口目录，供 Make 变量和相对命令展开继承原始语义。
    path_make_dir = path_makefile.parent  # 继承入口 Makefile 相对语义的目录锚点

    # 先读取入口 Makefile 顶层变量，后面解析 `SRCS`、`VCS_FLAGS` 都会复用这份快照。
    dict_variables = _read_make_vars(path_makefile)  # 当前导入会使用的 Make 变量快照

    # 调用方若传入覆盖变量，则直接合并到变量映射表中。
    if make_vars:

        # 将外部变量值统一字符串化后并入当前变量映射。
        dict_variables.update(
            {str(str_key): str(str_value) for str_key, str_value in make_vars.items()}
        )  # 应用于本次导入流程的覆盖变量映射

    # 显式 `VCS` 变量若存在，会优先定义核心编译入口。
    list_vcs_tokens = _read_make_var(path_makefile, "VCS")  # 当前 Makefile 中的 VCS 变量 token 列表

    # 显式 `VERDI` 变量只补 filelist、工具路径和波形名，不参与核心编译入口判定。
    list_verdi_tokens = _read_make_var(path_makefile, "VERDI")  # 供 Verdi 入口补充解析的 token 列表

    # 这一组列表承接 source list、include 目录、vlogan 参数和最终的 vcs 编译参数。
    list_source_lists, list_include_dirs, list_vlogan_args, list_vcs_args = [[], [], [], []]  # 编译阶段路径与参数缓冲区

    # 这一组列表负责回放原始参数、仿真参数以及编译前后检查命令。
    list_original_vcs_args, list_simv_args, list_post_compile_checks, list_post_sim_checks = [[], [], [], []]  # 回放与运行命令缓冲区

    # 这一组容器收集源码、诊断、可选依赖、工具路径、宏定义与 coverage 状态。
    list_sources, list_diagnostics, list_optional_deps = [[], [], []]  # 源码、诊断和可选依赖集合

    # 工具路径映射会随着 `VCS`、`VERDI` 和运行命令扫描逐步补齐。
    dict_tools = {}  # 导入流程识别到的显式工具路径映射

    # 宏定义映射既可能来自 filelist，也可能来自 `VCS_FLAGS` 与运行命令。
    dict_defines = {}  # 导入流程收集到的宏定义表

    # coverage 元数据默认先保留一个空 metrics 列表，后续再逐步追加目录与指标。
    dict_coverage = {"metrics": []}  # 导入流程累计的 coverage 元数据

    # timescale 没有显式来源时保持空串，交给后续主编译入口覆盖。
    str_timescale = ""  # 尚未解析出显式值时使用的 timescale 占位

    # simv 输出路径也允许为空，只有命中 `-o` 或等价来源时才写入。
    str_output = ""  # 尚未解析出显式值时使用的 simv 输出路径占位

    # debug 访问级别默认保留 `all`，与既有非 GUI smoke 行为保持一致。
    str_debug = "all"  # 尚未命中新 debug 配置时沿用的访问级别

    # 顶层模块在没有更可靠来源时先占位为 `top`。
    str_top = "top"  # 尚未命中新 top 配置时沿用的顶层模块名

    # 波形文件名默认沿用历史使用的 `waves.fsdb`。
    str_dump_name = "waves.fsdb"  # 尚未命中新波形配置时沿用的文件名

    # 工作目录会影响 `VCS_FLAGS` 与运行命令中的相对路径解释方式。
    str_workdir = _vcs_workdir_from_commands(path_makefile, project_root=path_project_root, variables=dict_variables)  # 当前工程默认采用的编译工作目录

    # `VCS_FLAGS` 若约定了独立 workdir，就要改用该目录解释相对路径参数。
    path_flag_base = path_project_root / str_workdir if str_workdir != "run" else path_make_dir  # `VCS_FLAGS` 与扩展命令使用的相对路径基目录

    # 显式 `VCS` 变量沿用 Makefile 同目录语义，因此上下文只需要入口目录和工程根。
    dict_ctx_make_tokens = {"path_make_dir": path_make_dir, "path_project_root": path_project_root}  # 显式 `VCS` 变量的路径解释上下文

    # 命令配方与 `VCS_FLAGS` 则要按工作目录语义解释相对路径。
    dict_ctx_flag_tokens = {"path_make_dir": path_flag_base, "path_project_root": path_project_root}  # 配方命令与 `VCS_FLAGS` 的路径解释上下文

    # 把主编译、filelist 和运行命令都会写到的容器集中成一份共享缓冲区视图。
    dict_view_vcs_buffers = dict(  # 导入流程共享的 VCS 与 filelist 解析缓冲区
        # 编译输入与前端参数缓冲区统一放在第一组。
        list_source_lists=list_source_lists,  # 主入口与 Verdi 发现到的 filelist 路径集合
        list_include_dirs=list_include_dirs,  # 编译阶段累计的 include 目录集合
        list_vlogan_args=list_vlogan_args,  # 需要交给 vlogan 的前端语法开关
        list_vcs_args=list_vcs_args,  # 需要保留给 VCS 的编译参数集合

        # 运行回放与派生元数据则集中放在第二组。
        list_original_vcs_args=list_original_vcs_args,  # 原始 VCS 命令透传参数集合
        list_diagnostics=list_diagnostics,  # 导入过程逐步追加的诊断消息集合
        dict_defines=dict_defines,  # 各入口归并得到的宏定义映射
        dict_coverage=dict_coverage,  # 编译与运行期共同维护的 coverage 元数据
    )

    # 返回 `import_project` 后续阶段会反复读写的共享状态。
    return {
        "path_makefile": path_makefile,
        "path_project_root": path_project_root,
        "path_make_dir": path_make_dir,
        "dict_variables": dict_variables,
        "list_vcs_tokens": list_vcs_tokens,
        "list_verdi_tokens": list_verdi_tokens,
        "list_source_lists": list_source_lists,
        "list_include_dirs": list_include_dirs,
        "list_vlogan_args": list_vlogan_args,
        "list_vcs_args": list_vcs_args,
        "list_original_vcs_args": list_original_vcs_args,
        "list_simv_args": list_simv_args,
        "list_post_compile_checks": list_post_compile_checks,
        "list_post_sim_checks": list_post_sim_checks,
        "list_sources": list_sources,
        "list_diagnostics": list_diagnostics,
        "list_optional_deps": list_optional_deps,
        "dict_tools": dict_tools,
        "dict_defines": dict_defines,
        "dict_coverage": dict_coverage,
        "str_timescale": str_timescale,
        "str_output": str_output,
        "str_debug": str_debug,
        "str_top": str_top,
        "str_dump_name": str_dump_name,
        "str_workdir": str_workdir,
        "dict_ctx_make_tokens": dict_ctx_make_tokens,
        "dict_ctx_flag_tokens": dict_ctx_flag_tokens,
        "dict_view_vcs_buffers": dict_view_vcs_buffers,
    }

# 把主编译入口输出并回共享状态，避免 `import_project` 里堆积重复字段归并语句
def _apply_primary_compile_metadata(
    dict_import_workflow_state: ImportProjectState,
    *,
    dict_primary_metadata: dict[str, object],
) -> None:
    """
    把主编译入口恢复出的标量与源码集合写回导入主流程共享状态。

    :param dict_import_workflow_state: `import_project` 的共享状态映射，dtype=ImportProjectState，unit=workflow state
    :param dict_primary_metadata: 主编译入口恢复出的结构化元数据，dtype=dict[str, object]，unit=compile metadata
    :return: 无业务返回值，直接原位更新共享状态
    """

    # 主编译入口先确定源码集合，这部分不会再由二级入口覆盖。
    dict_import_workflow_state["list_sources"] = dict_primary_metadata["list_sources"]  # 主编译入口确认后的源码路径列表

    # 先把四个核心编译标量整理成具名字典，避免元组拆包弱化字段语义。
    dict_effective_compile_scalars = {  # 主编译入口最终回写的核心编译标量映射
        "str_timescale": dict_primary_metadata["str_timescale"] or dict_import_workflow_state["str_timescale"],  # 优先采用主编译入口显式恢复出的 timescale
        "str_debug": dict_primary_metadata["str_debug"] or dict_import_workflow_state["str_debug"],  # 若编译命令声明调试模式则覆盖初始化阶段默认值
        "str_top": dict_primary_metadata["str_top"] or dict_import_workflow_state["str_top"],  # 主编译入口直接指定的顶层模块名保持最高优先级
        "str_output": dict_primary_metadata["str_output"] or dict_import_workflow_state["str_output"],  # 主编译入口确认的输出目标名覆盖共享状态占位值
    }

    # 再把归并后的核心编译标量整体回写到共享状态。
    dict_import_workflow_state.update(dict_effective_compile_scalars)

# 把二级前端入口补充出的 top/workdir 写回共享状态
def _apply_secondary_frontend_metadata(
    dict_import_workflow_state: ImportProjectState,
    *,
    modelsim_tcl: Path | None,
) -> None:
    """
    解析二级前端入口，并把补充出的 top/workdir 写回共享状态。

    :param dict_import_workflow_state: `import_project` 的共享状态映射，dtype=ImportProjectState，unit=workflow state
    :param modelsim_tcl: 调用方显式传入的 ModelSim Tcl 候选路径，dtype=Path | None，unit=filesystem path
    :return: 无业务返回值，直接原位更新共享状态
    """

    # 先收束二级入口解析所需的共享上下文，避免调用点堆满内联映射。
    dict_secondary_frontend_context = {  # 二级入口解析 top/workdir 时复用的工程视图
        # 先描述二级入口需要复用的路径锚点。
        "path_base": dict_import_workflow_state["path_make_dir"] / "sim",  # ModelSim 辅助脚本默认搜索的 sim 子目录
        "path_project_root": dict_import_workflow_state["path_project_root"],  # ModelSim 子入口解释相对路径时统一锚定的工程根目录

        # 再传入主编译入口已经确认的共享编译状态。
        "list_sources": dict_import_workflow_state["list_sources"],  # 主编译入口已经确认的源码路径列表
        "list_include_dirs": dict_import_workflow_state["list_include_dirs"],  # 主编译入口恢复出的头文件目录列表
        "dict_defines": dict_import_workflow_state["dict_defines"],  # 主编译入口归并后的宏定义映射
        "list_vlogan_args": dict_import_workflow_state["list_vlogan_args"],  # 主编译入口恢复出的 vlogan 参数列表
        "list_diagnostics": dict_import_workflow_state["list_diagnostics"],  # 二级入口允许继续追加的诊断消息列表
    }

    # 二级入口只负责补充 top 与 workdir，不改变主编译入口已确认的核心参数。
    tuple_secondary_metadata = _resolve_secondary_frontend_metadata(  # 二级入口补充出的顶层模块名与工作目录
        modelsim_tcl=modelsim_tcl,  # 调用方显式传入的 ModelSim Tcl 候选路径
        dict_variables=dict_import_workflow_state["dict_variables"],  # 二级入口复用的 Make 变量快照
        path_make_dir=dict_import_workflow_state["path_make_dir"],  # 二级入口继承的入口目录
        path_project_root=dict_import_workflow_state["path_project_root"],  # 二级入口统一参考的工程根目录
        context=dict_secondary_frontend_context,  # 供二级入口回填 top/workdir 时复用的工程视图
    )

    # 让二级入口补充的顶层模块名覆盖默认值。
    dict_import_workflow_state["str_top"] = tuple_secondary_metadata[0] or dict_import_workflow_state["str_top"]  # 二级入口确认后的最终顶层模块名

    # 让二级入口补充的工作目录覆盖默认值。
    dict_import_workflow_state["str_workdir"] = tuple_secondary_metadata[1] or dict_import_workflow_state["str_workdir"]  # 二级入口确认后的最终工作目录

# 把主流程共享状态收束成最终 manifest，避免 `import_project` 末尾堆积大段字典拼装
def _build_import_manifest_from_state(
    dict_import_workflow_state: ImportProjectState,
    *,
    list_filelist_entries: list[str],
) -> dict[str, object]:
    """
    根据导入主流程共享状态构造最终 manifest。

    :param dict_import_workflow_state: `import_project` 的共享状态映射，dtype=ImportProjectState，unit=workflow state
    :param list_filelist_entries: filelist 导入出的源码条目列表，dtype=list[str]，unit=filesystem path
    :return: 汇总后的 non-GUI smoke manifest，dtype=dict[str, object]，unit=manifest
    """

    # 先把最终 manifest 所需字段收束成独立上下文，便于分组维护。
    dict_manifest_context = {  # non-GUI smoke manifest 组装时使用的统一上下文
        # 先写入源码、filelist 与头文件目录等编译输入视图。
        "list_sources": dict_import_workflow_state["list_sources"],  # 作为最终 manifest 主源码视图的入口源码路径列表
        "list_filelist_entries": list_filelist_entries,  # filelist 额外补入的源码条目列表
        "list_source_lists": dict_import_workflow_state["list_source_lists"],  # 导入流程累计发现的 filelist 路径集合
        "list_include_dirs": dict_import_workflow_state["list_include_dirs"],  # 编译阶段恢复出的头文件目录列表
        "dict_defines": dict_import_workflow_state["dict_defines"],  # 主编译与 filelist 共同归并后的宏定义映射

        # 再写入编译入口最终确认的核心控制标量与参数列表。
        "str_top": dict_import_workflow_state["str_top"],  # 导入流程最终采用的顶层模块名
        "str_timescale": dict_import_workflow_state["str_timescale"],  # 导入流程最终采用的 timescale 配置
        "str_debug": dict_import_workflow_state["str_debug"],  # 导入流程最终采用的调试开关
        "list_vlogan_args": dict_import_workflow_state["list_vlogan_args"],  # 编译前端需要透传的 vlogan 参数列表
        "list_vcs_args": dict_import_workflow_state["list_vcs_args"],  # 编译阶段最终采用的 VCS 参数列表
        "dict_coverage": dict_import_workflow_state["dict_coverage"],  # 编译期与运行期联合归并后的 coverage 元数据
        "str_output": dict_import_workflow_state["str_output"],  # 编译流程最终生成的输出目标名
    }

    # 再单独收束运行期、依赖与诊断收尾信息，避免最终 manifest 组装块过度密集。
    dict_runtime_manifest_fields = {  # manifest 收尾阶段追加的运行期与依赖字段
        "list_simv_args": dict_import_workflow_state["list_simv_args"],  # 仿真阶段最终透传的 simv 参数列表
        "list_post_compile_checks": dict_import_workflow_state["list_post_compile_checks"],  # 编译完成后需要执行的检查命令列表
        "list_post_sim_checks": dict_import_workflow_state["list_post_sim_checks"],  # 仿真完成后需要执行的检查命令列表

        # 再补入原始入口参数与工具路径视图。
        "list_original_vcs_args": dict_import_workflow_state["list_original_vcs_args"],  # 入口原始 VCS 命令拆出的参数列表
        "dict_tools": dict_import_workflow_state["dict_tools"],  # 导入阶段解析出的工具路径映射

        # 再补入波形、运行目录与依赖收尾字段。
        "str_dump_name": dict_import_workflow_state["str_dump_name"],  # 最终推断出的波形文件名
        "str_workdir": dict_import_workflow_state["str_workdir"],  # 仿真阶段相对路径解释所依据的工作目录
        "list_pre_sim_artifacts": dict_import_workflow_state["list_pre_sim_artifacts"],  # 预仿真阶段需要提前准备的数据文件
        "list_optional_deps": dict_import_workflow_state["list_optional_deps"],  # 导入阶段登记的可选外部依赖列表
        "list_diagnostics": dict_import_workflow_state["list_diagnostics"],  # 导入阶段累计记录的诊断消息列表
    }

    # 最后把运行期收尾字段并入最终 manifest 上下文。
    dict_manifest_context.update(dict_runtime_manifest_fields)

    # 汇总并返回 non-GUI smoke 流程需要的统一 manifest。
    return _build_import_manifest(dict_manifest_context=dict_manifest_context)

# 下面开始处理显式 `VCS` 变量与 recipe 分支之间的主编译入口选择
def _resolve_primary_compile_metadata(
    *,
    list_vcs_tokens: list[str],
    dict_primary_context: PrimaryCompileContext,
    dict_tools: dict[str, str],
    list_diagnostics: list[str], list_optional_deps: list[str],
    buffers: VcsParseBuffers,
) -> dict[str, object]:
    """
    统一分派显式 `VCS` 与 recipe 两条主编译入口恢复路径。

    :param list_vcs_tokens: Makefile 中 `VCS` 变量拆分后的 token 列表，dtype=list[str]，unit=shell token
    :param dict_primary_context: 主编译入口共享的路径、变量与 token 上下文，dtype=PrimaryCompileContext，unit=parse context
    :param dict_tools: 当前工程导入出的工具路径映射，dtype=dict[str, str]，unit=tool path map
    :param list_diagnostics: 当前工程导入过程中的诊断消息，dtype=list[str]，unit=diagnostic
    :param list_optional_deps: 当前工程导入出的可选外部依赖，dtype=list[str]，unit=dependency name
    :param buffers: VCS token 解析共享输出缓冲区，dtype=VcsParseBuffers，unit=parse buffer
    :return: 主编译入口恢复出的结构化元数据，dtype=dict[str, object]，unit=compile metadata
    """

    # 显式 `VCS` 变量存在时，优先把它当作核心编译入口。
    if list_vcs_tokens:

        # 直接按显式 `VCS` 变量恢复核心编译元数据。
        return _resolve_explicit_vcs_compile_metadata(
            list_vcs_tokens,
            dict_tools=dict_tools,
            dict_ctx_make_tokens=dict_primary_context["dict_ctx_make_tokens"],
            buffers=buffers,
        )

    # 退回命令配方、`SRCS` 与 `VCS_FLAGS` 分支恢复主编译元数据。
    return _resolve_recipe_vcs_compile_metadata(
        dict_primary_context=dict_primary_context,
        dict_tools=dict_tools,
        list_diagnostics=list_diagnostics,
        list_optional_deps=list_optional_deps,
        buffers=buffers,
    )

# 统一协调主编译、二级入口与 filelist 收尾，导出 smoke manifest
def _import_project_from_state(
    *,
    dict_manifest_state: ImportProjectState,
    filelist: Path | None = None,
    modelsim_tcl: Path | None = None,
) -> dict[str, object]:
    """
    在共享状态已经初始化后，继续完成主编译、二级入口和 filelist 收尾。

    :param dict_manifest_state: `import_project` 初始化后的共享状态映射，dtype=ImportProjectState，unit=workflow state
    :param filelist: 可选的显式 filelist 路径，dtype=Path | None，unit=filesystem path
    :param modelsim_tcl: 可选的显式 ModelSim Tcl 路径，dtype=Path | None，unit=filesystem path
    :return: 统一的 non-GUI smoke manifest，dtype=dict[str, object]，unit=manifest
    """

    # 主编译入口可能来自显式 `VCS`，也可能来自 recipe / `VCS_FLAGS` 分支。
    dict_primary_metadata = _resolve_primary_compile_metadata(  # 主编译入口恢复出的结构化元数据
        list_vcs_tokens=dict_manifest_state["list_vcs_tokens"],  # 显式 `VCS` 变量拆出的 token 列表
        dict_primary_context=_build_primary_compile_context(dict_manifest_state),  # 主编译入口复用的解析上下文
        dict_tools=dict_manifest_state["dict_tools"],  # 当前工程显式声明过的工具程序位置映射
        list_diagnostics=dict_manifest_state["list_diagnostics"],  # 主编译入口可直接追加的诊断消息列表
        list_optional_deps=dict_manifest_state["list_optional_deps"],  # 主编译入口可登记的可选外部依赖列表
        buffers=dict_manifest_state["dict_view_vcs_buffers"],  # 主编译入口共用的参数与 coverage 缓冲区
    )

    # 把主编译入口输出并回共享状态，统一维护四个核心编译标量。
    _apply_primary_compile_metadata(
        dict_manifest_state,
        dict_primary_metadata=dict_primary_metadata,
    )

    # 二级前端入口只允许补充 top/workdir，不得反向覆盖主编译入口的核心编译参数。
    _apply_secondary_frontend_metadata(
        dict_manifest_state,
        modelsim_tcl=modelsim_tcl,
    )

    # 运行命令扫描负责提取仿真参数、检查脚本和运行期工具元数据。
    tuple_runtime_metadata = _scan_make_runtime_commands(  # 运行命令扫描补充出的运行期元数据
        # 先传入命令展开与路径解释需要的基础上下文。
        commands=_make_commands(dict_manifest_state["path_makefile"]),  # 当前入口 Makefile 展开的原始命令列表
        dict_variables=dict_manifest_state["dict_variables"],  # 运行命令展开时复用的 Make 变量快照
        path_make_dir=dict_manifest_state["path_make_dir"],  # 运行命令默认继承的入口目录
        path_project_root=dict_manifest_state["path_project_root"],  # 运行命令统一参考的工程根目录

        # 再传入运行命令可能继续补充的共享状态容器。
        str_workdir=dict_manifest_state["str_workdir"],  # 运行命令使用的有效工作目录
        list_source_lists=dict_manifest_state["list_source_lists"],  # 运行命令可能继续补充的 filelist 路径集合
        dict_tools=dict_manifest_state["dict_tools"],  # 运行命令可能补充工具路径的共享映射
    )

    # 把运行命令扫描结果拆包成固定的五类运行期补充信息。
    (
        dict_manifest_state["list_simv_args"],  # 最终仿真运行参数列表
        dict_manifest_state["list_post_compile_checks"],  # 最终编译后检查命令列表
        dict_manifest_state["list_post_sim_checks"],  # 最终仿真后检查命令列表
        dict_runtime_coverage,  # 运行命令补充出的 coverage 元数据
        dict_manifest_state["str_dump_name"],  # 运行命令补充出的波形文件名
    ) = tuple_runtime_metadata

    # 运行命令导出的 coverage 路径字段需要并回总 coverage 元数据。
    dict_manifest_state["dict_coverage"].update(dict_runtime_coverage)

    # 显式 `VERDI` 变量也可能补充波形文件名和 filelist 入口。
    dict_manifest_state["str_dump_name"] = _parse_verdi_tokens(  # 显式 `VERDI` 变量补充后的波形文件名
        # 先传入 Verdi 入口自身的命令与路径上下文。
        dict_manifest_state["list_verdi_tokens"],  # `VERDI` 变量拆出的 token 列表
        path_make_dir=dict_manifest_state["path_make_dir"],  # 显式 `VERDI` 变量继承的入口目录
        path_project_root=dict_manifest_state["path_project_root"],  # 显式 `VERDI` 变量统一参考的工程根目录

        # 再传入 Verdi 分支允许补写的共享状态。
        list_source_lists=dict_manifest_state["list_source_lists"],  # `VERDI` 变量可能补充的 filelist 路径集合
        dict_tools=dict_manifest_state["dict_tools"],  # `VERDI` 变量可能补充的工具路径映射
        str_dump_name=dict_manifest_state["str_dump_name"],  # 当前已知的最新波形文件名
        bool_trust_invocation=True,  # 信任入口命令本身的 Verdi 调用语义
    )

    # filelist 既支持显式传入，也支持从 Verdi 或运行命令里自动发现。
    path_filelist = _effective_filelist_path(  # 显式输入与自动发现候选折中后的最终 filelist 路径
        filelist,  # 为空时才回退自动发现分支的显式 filelist 候选路径
        path_project_root=dict_manifest_state["path_project_root"],  # filelist 解析统一参考的工程根目录
        list_source_lists=dict_manifest_state["list_source_lists"],  # 从 Verdi 与运行命令发现的 filelist 候选列表
    )

    # filelist 结构化明细会继续补充源码、include、宏定义和覆盖率指标。
    list_filelist_entries = _merge_filelist_details(  # filelist 导入出的源码条目列表
        path_filelist=path_filelist,  # 本轮导入最终选中的 filelist 绝对路径
        path_project_root=dict_manifest_state["path_project_root"],  # filelist 条目统一参考的工程根目录
        str_workdir=dict_manifest_state["str_workdir"],  # filelist 相对路径需要继承的工作目录语义
        buffers=dict_manifest_state["dict_view_vcs_buffers"],  # filelist 解析继续写回的共享缓冲区
    )

    # 若 VCS 参数里显式声明了 dumpvars 文件名，就以最后一次声明为准。
    dict_manifest_state["str_dump_name"] = _refresh_dump_name_from_vcs_args(  # dumpvars 语义刷新后的最终波形文件名
        dict_manifest_state["list_vcs_args"],  # 当前累计的 VCS 编译参数列表
        str_dump_name=dict_manifest_state["str_dump_name"],  # Verdi 与运行命令阶段得到的最新波形文件名
    )

    # 工具链变量里声明的额外工具只记为可选依赖，不并入核心工具配置。
    list_external_tool_deps = _external_tool_dependencies(dict_manifest_state["dict_variables"])  # 工具链变量派生出的可选外部依赖列表

    # 把工具链变量派生出的可选依赖并入最终 manifest 状态。
    dict_manifest_state["list_optional_deps"].extend(list_external_tool_deps)

    # 从主源码与 filelist 合集中扫描 `$readmemh` 依赖的数据文件。
    dict_manifest_state["list_pre_sim_artifacts"] = _readmemh_artifacts(  # 预仿真阶段需要事先准备好的数据文件列表
        _dedupe([*dict_manifest_state["list_sources"], *list_filelist_entries]),  # readmemh 扫描前合并的主源码与 filelist 条目集合
        project_root=dict_manifest_state["path_project_root"],  # readmemh 依赖路径统一换算时参考的工程根目录
    )

    # 沿用旧行为的入口目录判定，为缺失数据文件补齐显式诊断。
    _report_missing_pre_sim_artifacts(
        dict_manifest_state["list_pre_sim_artifacts"],
        path_make_dir=dict_manifest_state["path_make_dir"],
        list_diagnostics=dict_manifest_state["list_diagnostics"],
    )

    # 把各阶段累计的源码、工具、检查项视图收束成最终 smoke manifest。
    return _build_import_manifest_from_state(
        dict_manifest_state,
        list_filelist_entries=list_filelist_entries,
    )

# 把简单 VCS/Verdi、ModelSim 或 Icarus 风格工程导入成统一 manifest
def import_project(
    *,
    makefile: Path,
    filelist: Path | None = None,
    project_root: Path | None = None,
    modelsim_tcl: Path | None = None,
    make_vars: dict[str, str] | None = None,
) -> dict[str, object]:
    """
    把简单 VCS/Verdi、ModelSim 或 Icarus 风格工程导入成统一 manifest。

    :param makefile: 工程入口 Makefile 路径，dtype=Path，unit=filesystem path
    :param filelist: 可选的显式 filelist 路径，dtype=Path | None，unit=filesystem path
    :param project_root: 可选的工程根目录路径，dtype=Path | None，unit=filesystem path
    :param modelsim_tcl: 可选的显式 ModelSim Tcl 路径，dtype=Path | None，unit=filesystem path
    :param make_vars: 调用方注入或覆盖的 Make 变量表，dtype=dict[str, str] | None，unit=Make variable override map
    :return: 统一的 non-GUI smoke manifest，dtype=dict[str, object]，unit=manifest
    """

    # 先初始化导入主流程需要跨阶段复用的共享状态，再交给后续阶段继续完成导入。
    import_project_state_dict_manifest_state = _initialize_import_project_state(  # 导入主流程跨阶段复用的共享状态
        makefile=makefile,  # 调用方显式传入的入口 Makefile 路径
        project_root=project_root,  # 调用方可选覆盖的工程根目录
        make_vars=make_vars,  # 调用方注入或覆盖的 Make 变量表
    )

    # 再基于共享状态补齐 filelist、二级入口与运行期收尾信息。
    return _import_project_from_state(
        dict_manifest_state=import_project_state_dict_manifest_state,
        filelist=filelist,
        modelsim_tcl=modelsim_tcl,
    )

# 构造当前 CLI 所使用的参数解析器
def _build_argument_parser() -> argparse.ArgumentParser:
    """
    构建导入器命令行参数解析器。

    :return: 已完成参数注册的解析器对象，dtype=argparse.ArgumentParser，unit=CLI parser
    :param: 当前辅助函数不接收业务参数；解析器配置全部在函数体内完成
    """

    # 先整理 CLI 帮助文本，避免参数解析器初始化时内联长字符串。
    str_parser_description = (  # 导入器命令行帮助里展示的用途说明
        "Import a simple VCS/Verdi Makefile, filelist, ModelSim Tcl, "
        "or Edalize JSON into a smoke manifest."
    )

    # 再初始化命令行参数解析器，并绑定统一的帮助文本。
    parser = argparse.ArgumentParser(  # 当前 CLI 使用的参数解析器
        description=str_parser_description,  # 参数解析器展示的命令用途说明
    )

    # 注册 Makefile 输入参数
    parser.add_argument("--makefile", type=Path)

    # 注册显式 filelist 输入参数
    parser.add_argument("--filelist", type=Path)

    # 允许调用方显式指定 ModelSim Tcl 入口，覆盖自动搜索结果。
    parser.add_argument("--modelsim-tcl", type=Path)

    # 允许直接导入 Edalize/CAPI2 JSON，绕过 Makefile 与 Tcl 解析分支。
    parser.add_argument("--edalize-json", type=Path)

    # 注册工程根目录输入参数
    parser.add_argument("--project-root", type=Path)

    # 注册 Make 变量覆盖参数
    parser.add_argument(
        "--make-var",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=(
            "Override or inject a Make variable before import, "
            "for example --make-var TOP=tb_top."
        ),
    )

    # 保留兼容性的 `--json` 开关
    parser.add_argument("--json", action="store_true")

    # 返回供主流程使用的参数解析器对象
    return parser

# 解析 `--make-var NAME=VALUE` 形式的覆盖变量列表
def _parse_make_var_overrides(
    make_var_items: list[str],
    *,
    parser: argparse.ArgumentParser,
) -> dict[str, str]:
    """
    把 `--make-var` 列表转换成覆盖变量映射。

    :param make_var_items: 原始 `--make-var` 参数列表，dtype=list[str]，unit=NAME=VALUE expression
    :param parser: 当前 CLI 使用的参数解析器，dtype=argparse.ArgumentParser，unit=CLI parser
    :return: 变量名到变量值的覆盖映射，dtype=dict[str, str]，unit=Make variable override map
    """

    # 初始化覆盖变量映射表
    dict_make_vars: dict[str, str] = {}  # 当前命令行导入出的 Make 变量覆盖映射

    # 顺序解析每一条 `NAME=VALUE` 覆盖项
    for str_item in make_var_items:

        # 不含等号时无法构成有效变量覆盖
        if "=" not in str_item:

            # 通过 argparse 的标准错误路径报告参数格式问题
            parser.error(f"--make-var must be NAME=VALUE, got {str_item!r}")

        # 按第一个等号拆出变量名与变量值
        str_name, str_value = str_item.split("=", 1)  # 当前命令行覆盖项的名称和值

        # 写入当前覆盖变量映射表
        dict_make_vars[str_name] = str_value  # 当前命令行覆盖项对应的变量值

    # 返回供导入主流程使用的覆盖变量映射
    return dict_make_vars

# CLI 主入口：根据输入形态导入工程并输出单个 JSON 文档
def main() -> int:
    """
    执行命令行入口，并向 stdout 输出单个 JSON manifest。

    :param: 当前主入口不接收业务参数；所有输入均由 argparse 从命令行读取
    :return: 成功时返回 0，dtype=int，unit=process exit code
    """

    # 构造并配置当前 CLI 的参数解析器
    parser = _build_argument_parser()  # 当前命令行入口使用的参数解析器

    # 解析命令行参数
    args = parser.parse_args()  # 当前命令行入口解析得到的参数对象

    # 传入 `--edalize-json` 时，走 Edalize/CAPI2 导入分支
    if args.edalize_json:

        # Edalize 分支要把 JSON 配置先解码成 Python 载荷再导入。
        dict_manifest = import_edalize_project(  # 当前命令行请求导入得到的 manifest
            json.loads(args.edalize_json.read_text(encoding="utf-8")),  # Edalize JSON 文件解析后的配置载荷
            project_root=args.project_root,  # 调用方指定的工程根目录
        )

    # 否则走 Makefile / filelist / Tcl 导入分支
    else:

        # 非 Edalize 模式下必须显式提供 Makefile
        if args.makefile is None:

            # 通过 argparse 的标准错误路径报告缺失参数
            parser.error("--makefile is required unless --edalize-json is used")

        # 解析 `--make-var` 列表，构造覆盖变量映射
        dict_make_vars = _parse_make_var_overrides(  # 当前命令行请求提供的 Make 变量覆盖映射
            args.make_var,  # 命令行传入的 Make 变量覆盖项列表
            parser=parser,  # 遇到非法覆盖项时复用当前 argparse 实例统一报错
        )  # 当前命令行请求携带的 Make 变量覆盖映射

        # 常规导入分支要把 Makefile、filelist 和覆盖变量一起传给导入器。
        dict_manifest = import_project(  # 当前非 Edalize 请求导入得到的 manifest
            makefile=args.makefile,  # 调用方显式指定的 Makefile 路径
            filelist=args.filelist,  # 调用方可选指定的 filelist 路径
            project_root=args.project_root,  # 导入阶段统一相对化路径时参考的工程根目录
            modelsim_tcl=args.modelsim_tcl,  # 调用方可选补充的 ModelSim Tcl 导入入口
            make_vars=dict_make_vars,  # 把命令行 `--make-var` 覆盖值一并传给常规导入分支
        )

    # 调用方显式要求机器可读输出时，按约定协议输出单个 JSON 文档
    if args.json:

        # 按模块文档约定把单个 JSON 对象写到标准输出，避免混入额外终端文本。
        json.dump(dict_manifest, sys.stdout, indent=2, sort_keys=True)

        # 为 JSON 协议输出补一个换行，避免 shell 提示符直接接在 JSON 末尾。
        sys.stdout.write("\n")

    # 默认模式只输出简短的人类可读摘要，避免终端直接泄漏结构化载荷
    else:

        # 统计源码条目数量，方便快速确认导入规模
        int_source_count = len(dict_manifest.get("sources", []))  # 当前 manifest 的源码条目数量

        # 统计 filelist 条目数量，方便快速确认是否命中 source list
        int_filelist_count = len(  # 当前 manifest 中登记的 filelist 条目数量
            dict_manifest.get("filelist_entries", [])  # manifest 中登记的全部 filelist 条目集合
        )  # 当前 manifest 的 filelist 条目数量

        # 输出简短导入摘要，供人工终端阅读
        print(
            "> INFO: [Python] prepared manifest with "
            f"{int_source_count} sources and {int_filelist_count} filelist entries"
        )

    # 返回成功退出码
    return 0

# 以脚本方式运行时，进入标准 CLI 主入口
if __name__ == "__main__":

    # 把主流程返回值转换成进程退出码
    raise SystemExit(main())
