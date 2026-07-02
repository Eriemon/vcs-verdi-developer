#!/usr/bin/env python3
"""规划非 GUI 的 cocotb VCS/VPI 仿真流程。

本模块解析 cocotb Makefile 中与 VCS/VPI 相关的关键变量，输出不启动 GUI 的结构化计划。

命令行标准输出协议：
- 默认只输出带前缀的人类可读摘要。
- 当传入 ``--json`` 时，标准输出写出单个 JSON 对象，供上游自动化直接消费。
"""

# 启用延后求值注解，避免类型提示在运行期引入额外解析顺序要求。
from __future__ import annotations

# 提供参数解析、JSON 序列化、正则匹配与 Makefile 风格分词能力。
import argparse
import json
import re
import shlex
import sys

# 补充路径工具与通用类型标注，供计划构造 helper 共享。
from pathlib import Path
from typing import Any

# 统一描述结构化计划、步骤与诊断返回体的 JSON 风格对象。
JsonDict = dict[str, Any]  # 计划、步骤与摘要共用的通用映射结构

# 统一描述 Makefile 变量表，避免在多个 helper 里重复书写复杂字典类型。
MakeVariableDict = dict[str, str]  # Makefile 变量名到变量值的映射结构

# 统一描述条件分支栈中的单帧状态，便于表达父层激活与是否已命中分支。
ConditionFrameDict = dict[str, bool]  # ifeq/ifneq/else 栈中的单层状态对象

# 匹配 Makefile 中最常见的赋值语句格式，供变量提取阶段统一复用。
ASSIGN_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*([:+?]?=)\s*(.*)$")  # Makefile 赋值语句的统一匹配规则

# 去掉 Makefile 单行末尾注释，保留被反斜杠转义的 `#` 字符。
def _strip_comment(str_line: str) -> str:
    """
    从 Makefile 单行文本中移除注释。

    参数：
    - str_line: 原始 Makefile 单行文本。

    返回：
    - 返回去掉非转义注释后的业务文本。

    异常：
    - 无显式异常；字符串遍历沿用 Python 默认行为。
    """

    # 原始行里没有注释起始符时，只需去掉行尾空白即可。
    if "#" not in str_line:

        # 直接返回去掉右侧空白后的业务文本，避免进入逐字符扫描分支。
        return str_line.rstrip()

    # 记录最近一个字符是否让下一个 `#` 失去注释含义。
    bool_escaped = False  # 当前遍历位置之前是否存在仍然生效的转义反斜杠

    # 按字符累积真正属于 Makefile 语义的片段，便于在命中注释前立即截断。
    list_output_characters: list[str] = []  # 去掉注释后需要保留下来的字符序列

    # 逐字符扫描当前行，直到遇到未转义的 `#` 为止。
    for str_character in str_line:

        # 碰到未转义注释起点时，后续内容都不再属于 Makefile 语义。
        if str_character == "#" and not bool_escaped:

            # 立刻结束字符扫描，避免把注释正文错误拼回业务文本。
            break

        # 只有真实属于表达式的字符才会被追加到输出缓冲区。
        list_output_characters.append(str_character)

        # 当前字符是未转义反斜杠时，需要让下一个 `#` 被当成普通字符。
        bool_escaped = str_character == "\\" and not bool_escaped  # 下一个字符是否要按转义语义解释

        # 当前字符不是反斜杠时，转义状态必须立刻清空，避免跨字符污染后续判断。
        if str_character != "\\":

            # 显式清掉转义状态，保证后续字符只由最近一个反斜杠决定。
            bool_escaped = False  # 当前遍历位置之后已经不存在待消费的转义状态

    # 返回去掉注释并修剪右侧空白后的业务文本。
    return "".join(list_output_characters).rstrip()

# 把末尾反斜杠续行的 Makefile 文本拼成逻辑行，方便后续按变量语义解析。
def _logical_lines(str_text: str) -> list[str]:
    """
    将 Makefile 文本整理为逻辑行列表。

    参数：
    - str_text: Makefile 原始完整文本。

    返回：
    - 返回已经按反斜杠续行规则拼接好的逻辑行列表。

    异常：
    - 无显式异常；字符串分行与拼接沿用 Python 默认行为。
    """

    # 逐条保存已经拼接完成的逻辑行，供主解析循环顺序消费。
    list_logical_lines: list[str] = []  # Makefile 文本按续行规则规整后的逻辑行列表

    # 暂存上一条尚未闭合的续行内容，直到遇到真正收束的行尾。
    str_pending_line = ""  # 当前仍在等待后续续接内容的半成品逻辑行

    # 按物理行顺序读取原始 Makefile 文本，保持条件分支与赋值先后关系稳定。
    for str_raw_line in str_text.splitlines():

        # 先统一去掉物理行行尾空白，避免反斜杠续行判断被尾部空格干扰。
        str_line = str_raw_line.rstrip()  # 当前物理行去掉右侧空白后的文本

        # 末尾反斜杠表示当前逻辑行还没有结束，因此先缓存前半段内容。
        if str_line.endswith("\\"):

            # 把续行前的正文拼到挂起缓冲区里，并额外补一个空格维持单词边界。
            str_pending_line += str_line[:-1] + " "  # 当前尚未闭合逻辑行的累计文本

        # 没有续行标记时，当前物理行可以和挂起缓冲区一起收束成完整逻辑行。
        else:

            # 把挂起内容与当前物理行合并后写入结果列表，供后续解析变量与条件分支。
            list_logical_lines.append(str_pending_line + str_line)

            # 当前逻辑行已经闭合，因此这里清空挂起缓冲区以迎接下一条逻辑行。
            str_pending_line = ""  # 下一轮物理行开始前不再保留旧的续行内容

    # 文件最后仍有挂起内容时，需要把它补写成一条合法逻辑行。
    if str_pending_line:

        # 保留文件尾部未闭合但已经累计出的逻辑行，避免最后一段续行内容丢失。
        list_logical_lines.append(str_pending_line)

    # 返回按 Makefile 续行规则规整后的逻辑行序列。
    return list_logical_lines

# 从 `ifeq(...)` 或 `ifneq(...)` 表达式中拆出左右两个参数，供条件求值阶段复用。
def _split_condition_args(str_expression: str) -> tuple[str, str] | None:
    """
    拆分 Makefile 条件表达式的左右参数。

    参数：
    - str_expression: 形如 ``ifeq(...)`` 或 ``ifneq(...)`` 的原始条件文本。

    返回：
    - 成功时返回去掉首尾空白的左右参数二元组；无法拆分时返回 ``None``。

    异常：
    - 无显式异常；字符串索引和遍历沿用 Python 默认行为。
    """

    # 先定位最外层括号边界，确保后续只在条件负载部分查找逗号分隔符。
    int_start_index = str_expression.find("(")  # 条件负载起始左括号的位置

    # 再定位最外层右括号，便于一次性截取真正需要分析的有效载荷文本。
    int_end_index = str_expression.rfind(")")  # 条件负载结束右括号的位置

    # 缺少合法括号边界时，没有可靠方法拆出左右参数，因此直接返回空。
    if int_start_index < 0 or int_end_index <= int_start_index:

        # 显式返回空值，让调用方把这条条件当成不可解析输入处理。
        return None

    # 截取括号内部的真实负载文本，供后续在顶层逗号处分成左右两半。
    str_payload = str_expression[int_start_index + 1 : int_end_index]  # 条件表达式括号内部的有效文本

    # 记录当前扫描位置位于多少层内嵌括号中，避免误把嵌套逗号当成主分隔符。
    int_depth = 0  # 当前扫描位置相对最外层负载的括号深度

    # 顺序扫描有效载荷，找到深度为零时出现的首个逗号分隔符。
    for int_index, str_character in enumerate(str_payload):

        # 进入新的内嵌括号层级时，需要提升深度计数。
        if str_character == "(":

            # 记录新的括号嵌套层级，避免后续误拆嵌套表达式里的参数。
            int_depth += 1  # 当前扫描位置之后的括号深度加一

        # 离开一个内嵌括号层级时，需要回退深度计数。
        elif str_character == ")":

            # 把括号深度减一，恢复到更外层的分隔符判断语义。
            int_depth -= 1  # 当前层级已经闭合，需要回到更外层的分隔语义

        # 只有位于最外层的逗号才是 Makefile 条件的主分隔符。
        elif str_character == "," and int_depth == 0:

            # 返回去掉首尾空白的左右参数，让上层条件求值逻辑直接消费。
            return str_payload[:int_index].strip(), str_payload[int_index + 1 :].strip()

    # 没有找到合法主分隔符时，说明这条条件语法不足以被当前解析器可靠处理。
    return None

# 解析 `$(shell ...)` 片段，尽量把仓库夹具里常见的 shell 结果折叠成稳定字符串。
def _replace_shell_expr(
    obj_match_shell: re.Match[str],
    dict_make_variables: MakeVariableDict,
    path_make_dir: Path,
) -> str:
    """
    展开 Makefile 中的 `$(shell ...)` 表达式。

    参数：
    - obj_match_shell: 正则命中的 shell 表达式对象。
    - dict_make_variables: 当前已经解析出的 Makefile 变量表。
    - path_make_dir: Makefile 所在目录，供 `pwd` 风格表达式回填。

    返回：
    - 返回当前 shell 表达式展开后的稳定字符串。

    异常：
    - 无显式异常；未知 shell 片段会按原文本回写。
    """

    # 提取 shell 表达式正文，便于针对仓库夹具里常见模式做显式分支判断。
    str_shell_body = obj_match_shell.group(1).strip()  # 当前 `$(shell ...)` 片段内部的命令文本

    # `pwd` 只需要映射到 Makefile 目录本身，就能满足 cocotb 夹具里的路径展开需求。
    if str_shell_body == "pwd":

        # 直接返回 Makefile 目录绝对路径，保持 `$(PWD)` 与 `$(shell pwd)` 的语义一致。
        return str(path_make_dir)

    # cocotb Makeflow 常见的 `SIM` 小写化片段可以直接用本地变量表折叠。
    if str_shell_body == "echo $(SIM) | tr A-Z a-z":

        # 返回 `SIM` 变量的小写形态，保持历史 Makefile 对工具名大小写折叠的约定。
        return dict_make_variables.get("SIM", "").lower()

    # 其余 shell 片段在本地静态规划阶段无法可靠执行，因此按原文本保留。
    return f"$(shell {str_shell_body})"

# 解析 `$(filter a,b c d)` 片段，保持测试夹具中条件筛选语义可被静态恢复。
def _replace_filter_expr(
    obj_match_filter: re.Match[str],
    dict_make_variables: MakeVariableDict,
    path_make_dir: Path,
) -> str:
    """
    展开 Makefile 中的 `$(filter ...)` 表达式。

    参数：
    - obj_match_filter: 正则命中的 filter 表达式对象。
    - dict_make_variables: 当前已经解析出的 Makefile 变量表。
    - path_make_dir: Makefile 所在目录，供递归展开子表达式时复用。

    返回：
    - 返回 filter 命中后的稳定字符串；未命中时返回空字符串。

    异常：
    - 无显式异常；子表达式展开沿用 `_expand_vars` 的容错语义。
    """

    # 先递归展开 filter 左值，避免它仍然保留未解析的变量或 shell 片段。
    str_first_value = _expand_vars(obj_match_filter.group(1).strip(), dict_make_variables, path_make_dir)  # filter 左值展开后的文本

    # 再递归展开 filter 右值集合，保证成员判断基于最终字符串而不是原始表达式。
    str_second_value = _expand_vars(obj_match_filter.group(2).strip(), dict_make_variables, path_make_dir)  # filter 右值集合展开后的文本

    # 左值属于右值集合时，Makefile 语义要求返回左值本身。
    if str_first_value in str_second_value.split():

        # 把命中的左值原样回写，供上层继续拼接后续变量表达式。
        return str_first_value

    # 左值不在候选集合里时，Makefile 语义要求返回空字符串。
    return ""

# 解析普通 `$(NAME)` 与 `${NAME}` 变量片段，恢复当前变量表中的最终值。
def _replace_var_expr(obj_match_var: re.Match[str], dict_make_variables: MakeVariableDict) -> str:
    """
    展开 Makefile 中的普通变量引用。

    参数：
    - obj_match_var: 正则命中的变量引用对象。
    - dict_make_variables: 当前已经解析出的 Makefile 变量表。

    返回：
    - 返回当前变量名在变量表中的最终值；缺失时回退为空字符串。

    异常：
    - 无显式异常；缺失变量按空字符串处理。
    """

    # 读取当前变量引用里的变量名，供后续按 `PWD` 特例或普通表项分支处理。
    str_variable_name = obj_match_var.group(1)  # 当前需要从变量表中读取的变量名

    # `PWD` 在 cocotb Makefile 中常被当作绝对路径锚点，因此优先按显式表项读取。
    if str_variable_name == "PWD":

        # 沿用已经写入变量表的 `PWD` 值，保证 shell 与普通变量两条路径回填一致。
        return dict_make_variables.get("PWD", "")

    # 普通变量直接从已解析变量表中读取，缺失时回退为空字符串。
    return dict_make_variables.get(str_variable_name, "")

# 递归展开 Makefile 值里的 shell、filter 与普通变量表达式，得到稳定字符串结果。
def _expand_vars(
    str_value: str,
    dict_make_variables: MakeVariableDict,
    path_make_dir: Path,
) -> str:
    """
    展开 Makefile 值中的常见变量表达式。

    参数：
    - str_value: 原始 Makefile 变量值文本。
    - dict_make_variables: 当前已经解析出的 Makefile 变量表。
    - path_make_dir: Makefile 所在目录，供 shell 与路径类表达式回填。

    返回：
    - 返回尽量折叠后的稳定字符串结果。

    异常：
    - 无显式异常；无法静态展开的片段会尽量按原语义保留。
    """

    # 先折叠 shell 表达式，避免后续普通变量替换误把命令正文再次当成变量语法。
    str_expanded_value = re.sub(  # shell 片段折叠后的暂存文本
        r"\$\(shell\s+([^)]*(?:\)[^)]*)?)\)",  # 匹配 `$(shell ...)` 片段的正则
        lambda obj_match_shell: _replace_shell_expr(obj_match_shell, dict_make_variables, path_make_dir),  # 把 shell 片段改写成稳定字符串
        str_value,  # 当前等待执行 shell 折叠的原始值文本
    )

    # 再跑一遍 filter 语义，让后续条件判断看到的就是筛选后的最终候选集。
    str_expanded_value = re.sub(  # filter 裁剪后的暂存文本
        r"\$\(filter\s+([^,]+),([^)]+)\)",  # 分别捕获 filter 的 pattern 与候选文本
        lambda obj_match_filter: _replace_filter_expr(obj_match_filter, dict_make_variables, path_make_dir),  # 把 filter 片段折叠成命中结果
        str_expanded_value,  # 当前已经完成 shell 折叠的中间文本
    )

    # 记录上一轮展开结果，供普通变量替换阶段检测是否还需要继续递归。
    str_previous_value = None  # 上一轮普通变量展开前的文本快照

    # 只要变量展开仍然会改变文本，就继续迭代直到结果收敛。
    while str_previous_value != str_expanded_value:

        # 先保存当前文本快照，供本轮展开后判断是否已经稳定。
        str_previous_value = str_expanded_value  # 本轮普通变量替换前的文本状态

        # 先解析圆括号形式的变量引用，这是 Makefile 里更常见的一种写法。
        str_expanded_value = re.sub(  # 圆括号变量回填后的暂存文本
            r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)",  # 匹配 `$(NAME)` 形式变量引用的正则
            lambda obj_match_var: _replace_var_expr(obj_match_var, dict_make_variables),  # 用变量表回填圆括号引用
            str_expanded_value,  # 当前准备处理圆括号变量的中间文本
        )

        # 最后一并扫掉花括号宏，避免工具宏或环境别名停留在半展开状态。
        str_expanded_value = re.sub(  # 花括号宏回填后的暂存文本
            r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",  # 专门识别 `${NAME}` 风格的备用宏写法
            lambda obj_match_var: _replace_var_expr(obj_match_var, dict_make_variables),  # 用变量表回填花括号引用
            str_expanded_value,  # 当前准备处理花括号宏的中间文本
        )

    # 返回去掉首尾空白后的最终展开结果，供变量表或条件求值直接使用。
    return str_expanded_value.strip()

# 计算一条 `ifeq` 或 `ifneq` 语句在当前变量表下是否命中。
def _eval_condition(
    str_line: str,
    dict_make_variables: MakeVariableDict,
    path_make_dir: Path,
    *,
    bool_negated: bool,
) -> bool:
    """
    计算一条 Makefile 条件语句的真假。

    参数：
    - str_line: 原始 `ifeq(...)` 或 `ifneq(...)` 文本。
    - dict_make_variables: 当前已经解析出的 Makefile 变量表。
    - path_make_dir: Makefile 所在目录，供变量展开阶段复用。
    - bool_negated: 为真时表示按 `ifneq` 语义取反；为假时表示按 `ifeq` 语义比较。

    返回：
    - 返回当前条件在给定变量表下的真假值。

    异常：
    - 无显式异常；不可解析条件会按未命中处理。
    """

    # 先把条件表达式拆成左右两部分，便于后续分别展开变量再比较。
    tuple_condition_parts = _split_condition_args(str_line)  # 当前条件语句拆分出的左右参数

    # 无法拆出合法左右参数时，当前条件没有可靠求值基础，因此按未命中处理。
    if not tuple_condition_parts:

        # 显式返回假值，让上层条件栈保持保守行为。
        return False

    # 取出展开前的左值文本，供变量与 shell 表达式折叠阶段使用。
    str_left_expr, str_right_expr = tuple_condition_parts  # 当前条件语句拆分出的左右原始表达式

    # 把左值整理成最终文本，确保比较动作基于已经收敛的 Makefile 语义。
    str_left_value = _expand_vars(str_left_expr, dict_make_variables, path_make_dir)  # 当前条件左值展开后的最终文本

    # 右侧文本常常携带默认值或环境选择，因此也要走完全相同的收束流程。
    str_right_value = _expand_vars(str_right_expr, dict_make_variables, path_make_dir)  # 当前条件右侧收敛后的比较基准文本

    # 先按 `ifeq` 语义计算原始比较结果，再视需要对 `ifneq` 做逻辑取反。
    bool_equal = str_left_value == str_right_value  # 当前条件左右值是否完全相等

    # `ifneq` 需要把相等结果取反，而 `ifeq` 直接沿用原始比较值即可。
    return not bool_equal if bool_negated else bool_equal

# 汇总条件栈里所有父层的激活状态，供新分支决定自己是否有机会生效。
def _current_active(list_condition_frames: list[ConditionFrameDict]) -> bool:
    """
    判断当前条件栈整体是否处于激活状态。

    参数：
    - list_condition_frames: 当前 Makefile 条件分支栈。

    返回：
    - 只有所有父层分支都处于激活状态时才返回真。

    异常：
    - 无显式异常；空栈会按 `all([])` 语义返回真。
    """

    # 所有父层都保持激活时，当前解析位置才允许继续消费赋值语句。
    return all(dict_condition_frame["active"] for dict_condition_frame in list_condition_frames)

# 更新 `else ifeq` 或 `else ifneq` 对应的栈顶帧状态，保持 Makefile 分支接管语义一致。
def _update_else_if_frame(
    list_condition_frames: list[ConditionFrameDict],
    str_condition_line: str,
    dict_make_variables: MakeVariableDict,
    path_make_dir: Path,
    *,
    bool_negated: bool,
) -> None:
    """
    处理 `else ifeq` 或 `else ifneq` 的条件栈切换。

    参数：
    - list_condition_frames: 当前 Makefile 条件分支栈。
    - str_condition_line: 去掉 `else ` 前缀后的条件文本。
    - dict_make_variables: 当前已经解析出的 Makefile 变量表。
    - path_make_dir: Makefile 所在目录，供条件求值阶段复用。
    - bool_negated: 为真时按 `else ifneq` 语义取反；为假时按 `else ifeq` 语义比较。

    返回：
    - 当前函数只会原地更新条件栈，不返回业务结果。

    异常：
    - 无显式异常；栈顶读取与字典写入沿用 Python 默认行为。
    """

    # 取出当前条件栈顶帧，便于复用父层激活状态和前序分支命中记录。
    dict_current_frame = list_condition_frames[-1]  # 当前正在切换的条件分支栈顶帧

    # 重新计算这条 `else if` 是否命中，决定本帧是否应该接管后续语句。
    bool_condition_hit = _eval_condition(  # 当前 `else if` 条件在变量表下是否命中
        str_line=str_condition_line,  # 当前正在接管的条件文本
        dict_make_variables=dict_make_variables,  # 从已恢复变量表读取参与比较的值
        path_make_dir=path_make_dir,  # 为 `$(shell pwd)` 一类表达式提供目录锚点
        bool_negated=bool_negated,  # 标记本帧采用 `ifeq` 还是 `ifneq` 语义
    )

    # 只有父层激活且先前分支尚未命中时，本条 `else if` 才可能真正生效。
    bool_active = dict_current_frame["parent"] and (not dict_current_frame["matched"]) and bool_condition_hit  # 当前 `else if` 是否成为激活分支

    # 把本帧激活状态切换成当前 `else if` 的计算结果，供后续行判断是否可见。
    dict_current_frame["active"] = bool_active  # 当前栈顶帧是否允许继续消费后续 Makefile 语句

    # 只要当前 `else if` 生效，就要把本帧标记为已经命中过某一支。
    dict_current_frame["matched"] = dict_current_frame["matched"] or bool_active  # 当前栈顶帧是否已经命中过任何一个分支

# 更新 `else` 对应的栈顶帧状态，保持 Makefile 的尾部分支接管规则稳定。
def _activate_else_frame(list_condition_frames: list[ConditionFrameDict]) -> None:
    """
    处理 `else` 对应的条件栈切换。

    参数：
    - list_condition_frames: 当前 Makefile 条件分支栈。

    返回：
    - 当前函数只会原地更新条件栈，不返回业务结果。

    异常：
    - 无显式异常；栈顶读取与字典写入沿用 Python 默认行为。
    """

    # 取出当前条件栈顶帧，便于根据父层状态和命中历史决定 `else` 是否可见。
    dict_current_frame = list_condition_frames[-1]  # 当前正在接管解析权的条件栈顶帧

    # 只有父层仍然激活且前序分支从未命中时，`else` 才会成为真正可见分支。
    bool_active = dict_current_frame["parent"] and not dict_current_frame["matched"]  # 当前 `else` 分支是否应该对后续语句生效

    # 把栈顶帧的激活状态切换成 `else` 的可见性结果。
    dict_current_frame["active"] = bool_active  # 当前 `else` 分支是否允许消费后续语句

    # `else` 一旦被处理，就视为当前条件块已经完成命中收束。
    dict_current_frame["matched"] = True  # 当前条件块是否已经确认最终命中的分支

# 把一条新的 `ifeq` 或 `ifneq` 分支压入条件栈，保持嵌套条件语义稳定。
def _push_condition_frame(
    list_condition_frames: list[ConditionFrameDict],
    str_condition_line: str,
    dict_make_variables: MakeVariableDict,
    path_make_dir: Path,
    *,
    bool_negated: bool,
) -> None:
    """
    把新的条件分支帧压入 Makefile 条件栈。

    参数：
    - list_condition_frames: 当前 Makefile 条件分支栈。
    - str_condition_line: 原始 `ifeq(...)` 或 `ifneq(...)` 条件文本。
    - dict_make_variables: 当前已经解析出的 Makefile 变量表。
    - path_make_dir: Makefile 所在目录，供条件求值阶段复用。
    - bool_negated: 为真时按 `ifneq` 语义取反；为假时按 `ifeq` 语义比较。

    返回：
    - 当前函数只会向条件栈追加一帧，不返回业务结果。

    异常：
    - 无显式异常；列表追加沿用 Python 默认行为。
    """

    # 新分支能否生效首先取决于所有父层是否仍然允许当前解析位置可见。
    bool_parent_active = _current_active(list_condition_frames)  # 当前即将入栈分支所在父层是否整体激活

    # 计算当前 `ifeq` 或 `ifneq` 本身是否命中，供新帧初始化 active 与 matched 字段。
    bool_condition_hit = _eval_condition(  # 当前即将入栈分支在变量表下是否命中
        str_line=str_condition_line,  # 当前准备入栈的条件文本
        dict_make_variables=dict_make_variables,  # 当前条件求值共享的变量表
        path_make_dir=path_make_dir,  # 当前 Makefile 所在目录路径
        bool_negated=bool_negated,  # 当前分支是否按不等比较求值
    )

    # 只有父层激活且本条条件命中时，新分支帧才会对后续赋值语句可见。
    bool_active = bool_parent_active and bool_condition_hit  # 当前新建分支帧是否处于激活状态

    # 构造新条件帧的三项核心状态，供嵌套解析阶段持续复用。
    dict_condition_frame = {  # 当前新建的条件分支栈帧
        "parent": bool_parent_active,  # 父层分支是否允许当前层可见
        "active": bool_active,  # 当前层本轮是否真正生效
        "matched": bool_active,  # 当前层是否已经命中过任一分支
    }

    # 把新帧压入条件栈，让后续行按照这层分支可见性继续解析。
    list_condition_frames.append(dict_condition_frame)

# 识别并处理 Makefile 条件控制语句，避免主解析循环堆积过多嵌套分支。
def _consume_control_line(
    str_stripped_line: str,
    str_lowered_line: str,
    list_condition_frames: list[ConditionFrameDict],
    dict_make_variables: MakeVariableDict,
    path_make_dir: Path,
) -> bool:
    """
    处理单条 Makefile 条件控制语句。

    参数：
    - str_stripped_line: 去掉注释并修剪首尾空白后的逻辑行文本。
    - str_lowered_line: 当前逻辑行的小写视图。
    - list_condition_frames: 当前 Makefile 条件分支栈。
    - dict_make_variables: 当前已经解析出的 Makefile 变量表。
    - path_make_dir: Makefile 所在目录，供条件求值阶段复用。

    返回：
    - 命中任一条件控制语句时返回真；否则返回假。

    异常：
    - 无显式异常；控制语句处理沿用各 helper 的默认行为。
    """

    # `else ifeq` 会让当前栈顶帧在未命中前序分支时重新评估下一条候选条件。
    if str_lowered_line.startswith("else ifeq"):

        # 交给专用 helper 重算栈顶帧状态，保持 `else ifeq` 的接管语义正确。
        _update_else_if_frame(
            list_condition_frames=list_condition_frames,
            str_condition_line=str_stripped_line[len("else ") :],
            dict_make_variables=dict_make_variables,
            path_make_dir=path_make_dir,
            bool_negated=False,
        )

        # 当前逻辑行已经被控制语句消费，主循环不应再把它当成赋值处理。
        return True

    # `else ifneq` 与 `else ifeq` 路径相同，只是条件结果需要取反。
    if str_lowered_line.startswith("else ifneq"):

        # 用取反语义更新栈顶帧，保证 `else ifneq` 的可见性切换符合 Makefile 规则。
        _update_else_if_frame(
            list_condition_frames=list_condition_frames,
            str_condition_line=str_stripped_line[len("else ") :],
            dict_make_variables=dict_make_variables,
            path_make_dir=path_make_dir,
            bool_negated=True,
        )

        # 当前逻辑行已经完成条件接力处理，不再进入普通正文解析。
        return True

    # 单独的 `else` 只负责接管前序分支都未命中的收尾路径。
    if str_lowered_line == "else":

        # 直接切换当前栈顶帧的激活状态，让后续正文继承 `else` 可见性。
        _activate_else_frame(list_condition_frames)

        # `else` 属于控制语句，消费后主循环应继续读取下一条逻辑行。
        return True

    # `ifeq` 代表进入新的一层条件块，需要把新帧压入条件栈。
    if str_lowered_line.startswith("ifeq"):

        # 用等值比较语义创建新的条件帧，供后续正文判断当前分支是否可见。
        _push_condition_frame(
            list_condition_frames=list_condition_frames,
            str_condition_line=str_stripped_line,
            dict_make_variables=dict_make_variables,
            path_make_dir=path_make_dir,
            bool_negated=False,
        )

        # 当前逻辑行仅承担条件入栈职责，不应再进入正文解析阶段。
        return True

    # `ifneq` 同样开启新条件块，只是初始化命中状态时按不等比较求值。
    if str_lowered_line.startswith("ifneq"):

        # 用取反比较语义压入条件帧，保持 `ifneq` 分支的激活判定一致。
        _push_condition_frame(
            list_condition_frames=list_condition_frames,
            str_condition_line=str_stripped_line,
            dict_make_variables=dict_make_variables,
            path_make_dir=path_make_dir,
            bool_negated=True,
        )

        # 当前逻辑行已经完成条件入栈，不再继续进入正文赋值分支。
        return True

    # `endif` 表示最内层条件块结束，需要恢复到外层的分支上下文。
    if str_lowered_line == "endif":

        # 条件栈非空时才允许弹出栈顶，避免空栈输入导致额外异常。
        if list_condition_frames:

            # 丢弃已经闭合的最内层条件帧，恢复外层分支可见性。
            list_condition_frames.pop()

        # `endif` 只参与控制流维护，不参与普通变量恢复。
        return True

    # 其余文本不属于条件控制语句，应交回主解析循环继续处理。
    return False

# 判定当前可见正文是否属于应被跳过的 Makefile 语句类型。
def _should_skip_makefile_body_line(str_visible_line: str, str_stripped_line: str) -> bool:
    """
    判断当前正文行是否应被静态规划器忽略。

    参数：
    - str_visible_line: 去掉注释但保留原始前导空白后的逻辑行文本。
    - str_stripped_line: 去掉注释并修剪首尾空白后的逻辑行文本。

    返回：
    - 配方命令、include 语句或纯函数调用行返回真；其余情况返回假。

    异常：
    - 无显式异常；字符串前缀判断沿用 Python 默认行为。
    """

    # Makefile 配方命令由前导制表符区分，它们属于执行期动作而不是静态变量事实。
    if str_visible_line.startswith("\t"):

        # 当前逻辑行是 recipe 命令，静态规划阶段必须直接跳过。
        return True

    # 外部 include 会引入额外文件依赖，本地最小规划器不在这里递归展开。
    if str_stripped_line.startswith("include "):

        # 当前逻辑行依赖外部 Makefile 片段，静态规划器保持保守忽略。
        return True

    # 纯函数调用行通常只是单独求值，不会形成当前规划器需要恢复的命名变量。
    if str_stripped_line.startswith("$("):

        # 当前逻辑行没有产生具名变量绑定，因此无需进入赋值分支。
        return True

    # 其余正文行仍然有机会成为赋值语句，交回调用方继续解析。
    return False

# 将一条普通 Makefile 赋值语句写入变量表，集中处理 `= / += / ?=` 三类语义。
def _apply_assignment_line(
    str_stripped_line: str,
    dict_make_variables: MakeVariableDict,
    path_make_dir: Path,
) -> None:
    """
    解析并应用一条普通 Makefile 赋值语句。

    参数：
    - str_stripped_line: 去掉注释并修剪首尾空白后的逻辑行文本。
    - dict_make_variables: 当前已经解析出的 Makefile 变量表。
    - path_make_dir: Makefile 所在目录，供右值表达式展开阶段复用。

    返回：
    - 当前函数只会原地更新变量表，不返回业务结果。

    异常：
    - 无显式异常；正则匹配与字典写入沿用 Python 默认行为。
    """

    # 先判断当前正文是否确实属于赋值语句，避免无关文本污染变量恢复结果。
    obj_assign_match = ASSIGN_RE.match(str_stripped_line)  # 当前正文对应的赋值语句匹配结果

    # 没有命中赋值语句时，当前逻辑行对变量表没有直接影响。
    if not obj_assign_match:

        # 直接结束当前 helper，让主循环继续读取后续逻辑行。
        return

    # 拆出变量名、操作符与原始右值文本，供后续按 Makefile 语义写回变量表。
    str_name, str_operator, str_raw_value = obj_assign_match.groups()  # 当前赋值语句拆出的三元核心信息

    # 先收敛右值表达式，避免回写阶段仍然保留未折叠的变量引用或 shell 片段。
    str_value = _expand_vars(str_raw_value, dict_make_variables, path_make_dir)  # 当前赋值语句右值展开后的最终文本

    # `?=` 只在目标变量尚未拥有有效值时生效，因此这里先显式守住默认值语义。
    if str_operator == "?=" and str_name in dict_make_variables and dict_make_variables[str_name]:

        # 变量已经有值时保留旧结果，避免默认赋值覆盖更高优先级来源。
        return

    # `+=` 需要把旧值与新值拼接成空格分隔文本，同时滤掉空串避免重复空格。
    if str_operator == "+=":

        # 用非空片段重建 `+=` 后的最终文本，保证后续分词阶段得到稳定结果。
        dict_make_variables[str_name] = " ".join(  # 当前变量在 `+=` 语义下合并后的最终文本
            str_item  # 当前准备并入 `+=` 结果的非空片段
            for str_item in (dict_make_variables.get(str_name, ""), str_value)  # 依次查看旧值和新增值两个候选片段
            if str_item  # 只保留真正非空的文本片段
        ).strip()

        # 当前 `+=` 语义已经处理完成，无需再落入普通覆盖写回路径。
        return

    # 其余赋值语义都直接覆盖旧值，让变量表反映当前位置真正可见的结果。
    dict_make_variables[str_name] = str_value  # 当前变量覆盖写入后的最终文本

# 解析 cocotb Makefile 中与 VCS/VPI 规划相关的变量，恢复测试夹具需要的静态事实。
def parse_cocotb_makefile(
    path_makefile: Path,
    *,
    str_toplevel_lang: str = "verilog",
    dict_make_var_overrides: MakeVariableDict | None = None,
) -> MakeVariableDict:
    """
    解析 cocotb Makefile 中的关键变量。

    参数：
    - path_makefile: 需要解析的 cocotb Makefile 路径。
    - str_toplevel_lang: 调用方显式指定的默认顶层语言。
    - dict_make_var_overrides: 调用方显式覆盖的 Makefile 变量映射；为空时不额外覆盖。

    返回：
    - 返回恢复后的 Makefile 变量表，供后续计划构造阶段消费。

    异常：
    - 无显式异常；文件读取、正则匹配与字典更新沿用 Python 默认行为。
    """

    # 先把 Makefile 路径规整成绝对路径，避免变量展开依赖调用时 cwd。
    path_makefile = path_makefile.resolve()  # 当前 cocotb Makefile 的绝对路径

    # Makefile 目录既用于回填 `PWD`，也用于 shell 风格路径表达式展开。
    path_make_dir = path_makefile.parent  # 当前 Makefile 所在的目录路径

    # 准备 VCS cocotb 规划阶段默认总会存在的一组基础变量。
    make_variable_dict_dict_make_variables: MakeVariableDict = {  # 当前解析循环共享的 Makefile 变量表
        "SIM": "vcs",  # 当前 cocotb 解析器固定采用的仿真器名称
        "TOPLEVEL_LANG": str_toplevel_lang,  # 当前调用方传入的默认顶层语言
        "PWD": str(path_make_dir),  # 当前 Makefile 所在目录的绝对路径
        "COCOTB_HDL_TIMEUNIT": "1ns",  # 当前 cocotb 默认时间单位
        "COCOTB_HDL_TIMEPRECISION": "1ps",  # 当前 cocotb 默认时间精度
    }

    # 调用方显式传入的变量覆盖应当优先于文件正文默认值，因此这里先统一写入变量表。
    make_variable_dict_dict_make_variables.update(dict_make_var_overrides or {})

    # 用条件栈跟踪嵌套 ifeq/ifneq/else 块的可见性，保证只解析当前真正生效的赋值。
    list_condition_frames: list[ConditionFrameDict] = []  # Makefile 条件分支的状态栈

    # 顺序消费规整后的逻辑行，让变量赋值与条件切换遵循 Makefile 原始先后关系。
    for str_raw_line in _logical_lines(path_makefile.read_text(encoding="utf-8")):

        # 先去掉注释但保留前导空白，后续需要依靠它判断 recipe 命令是否应被跳过。
        str_visible_line = _strip_comment(str_raw_line)  # 当前逻辑行去掉注释但保留缩进后的文本

        # 再单独构造修剪首尾空白的视图，供关键字匹配与赋值解析复用。
        str_stripped_line = str_visible_line.strip()  # 当前逻辑行去掉首尾空白后的有效文本

        # 同步准备小写视图，便于识别 ifeq/else/endif 这些控制关键字。
        str_lowered_line = str_stripped_line.lower()  # 当前逻辑行用于分支识别的小写文本

        # 空白逻辑行不携带任何语义，因此主循环可以立刻转去读取下一条。
        if not str_stripped_line:

            # 当前逻辑行已经为空，没有任何变量或条件状态需要更新。
            continue

        # 先把条件控制语句集中交给专用 helper，主循环只保留正文恢复主线。
        if _consume_control_line(
            str_stripped_line=str_stripped_line,
            str_lowered_line=str_lowered_line,
            list_condition_frames=list_condition_frames,
            dict_make_variables=make_variable_dict_dict_make_variables,
            path_make_dir=path_make_dir,
        ):

            # 当前逻辑行已经被控制流 helper 消费，直接读取下一条逻辑行。
            continue

        # 父层条件未激活时，当前正文对最终变量事实没有影响，应当直接忽略。
        if not _current_active(list_condition_frames):

            # 当前逻辑行位于失活分支内部，不允许写入最终变量表。
            continue

        # recipe、include 与纯函数调用行不属于本规划器要恢复的具名变量绑定。
        if _should_skip_makefile_body_line(str_visible_line, str_stripped_line):

            # 当前逻辑行不提供需要持久化的变量事实，继续读取下一条输入。
            continue

        # 其余正文都交给赋值 helper 处理，集中收口 `= / += / ?=` 的写回语义。
        _apply_assignment_line(
            str_stripped_line=str_stripped_line,
            dict_make_variables=make_variable_dict_dict_make_variables,
            path_make_dir=path_make_dir,
        )

    # 返回已经恢复出的 Makefile 变量表，供计划构造阶段直接读取。
    return make_variable_dict_dict_make_variables

# 按 shell 语义拆分一条空格分隔参数字符串，便于恢复命令行参数列表。
def _split_words(str_value: str) -> list[str]:
    """
    将空格分隔字符串拆成命令参数列表。

    参数：
    - str_value: 原始空格分隔参数字符串。

    返回：
    - 返回按 shell 词法拆分后的参数列表；空串时返回空列表。

    异常：
    - 无显式异常；分词沿用 `shlex.split` 的默认行为。
    """

    # 空字符串不对应任何参数，因此这里直接回退为空列表。
    if not str_value:

        # 显式返回空列表，避免上层命令拼接阶段再额外处理空串。
        return []

    # 按 Windows 友好的非 POSIX 模式拆词，保持历史脚本里路径与引号语义更接近原始输入。
    return shlex.split(str_value, posix=False)

# 把源文件路径尽量转换成相对项目根的表示，便于计划输出更稳定可读。
def _rel(str_path_text: str, path_project_root: Path) -> str:
    """
    将一个源文件路径规整成优先相对项目根的表示。

    参数：
    - str_path_text: 原始路径文本。
    - path_project_root: 计划输出使用的项目根目录。

    返回：
    - 能相对项目根表示时返回 POSIX 相对路径；否则返回绝对 POSIX 路径。

    异常：
    - 无显式异常；路径解析与相对化沿用 Python 默认行为。
    """

    # 先把原始文本转换成 `Path` 对象，便于后续统一做绝对化与相对化处理。
    path_candidate = Path(str_path_text).resolve()  # 当前源文件路径解析后的绝对路径

    # 能相对项目根表示时，优先输出短路径以增强计划可读性。
    try:

        # 返回相对项目根的 POSIX 路径，保持 JSON 计划在不同平台上的展示更稳定。
        return path_candidate.relative_to(path_project_root.resolve()).as_posix()

    # 路径不在项目根内部时，只能保留绝对路径避免错误截断。
    except ValueError:

        # 回退到绝对 POSIX 路径，确保项目外部依赖文件仍然被准确记录。
        return path_candidate.as_posix()

# 将 Makefile 变量里的源文件字符串恢复成稳定的路径列表。
def _source_list(str_value: str, path_project_root: Path) -> list[str]:
    """
    把源文件字符串展开成路径列表。

    参数：
    - str_value: Makefile 变量里的原始源文件字符串。
    - path_project_root: 计划输出使用的项目根目录。

    返回：
    - 返回已经规整成 POSIX 路径的源文件列表。

    异常：
    - 无显式异常；分词与路径规整沿用各自 helper 的默认行为。
    """

    # 把拆词后的每个源文件路径都规整成稳定的输出形式，供计划对象直接复用。
    return [_rel(str_item, path_project_root) for str_item in _split_words(str_value)]

# 检查多组参数里是否显式请求 GUI，确保 non-GUI 技能不会误放行交互式流程。
def _has_gui_request(*list_value_groups: list[str]) -> bool:
    """
    判断多组参数中是否包含 GUI 请求。

    参数：
    - list_value_groups: 多组已经拆词完成的参数列表。

    返回：
    - 只要任意参数组显式请求 GUI，就返回真。

    异常：
    - 无显式异常；列表遍历沿用 Python 默认行为。
    """

    # 只要任意参数项命中 GUI 关键字，就说明当前请求超出了 non-GUI 技能边界。
    return any(
        str_item.lower() in {"-gui", "+gui", "gui=1"}
        for list_values in list_value_groups
        for str_item in list_values
    )

# 构造所有返回路径都会共享的基础计划对象，避免后续 blocked/unsupported/dry-run 分支重复写字段。
def _base_plan(
    dict_make_variables: MakeVariableDict,
    path_project_root: Path,
    list_diagnostics: list[str],
) -> JsonDict:
    """
    构造 cocotb VCS/VPI 计划的基础公共字段。

    参数：
    - dict_make_variables: 当前已经解析出的 Makefile 变量表。
    - path_project_root: 计划输出使用的项目根目录。
    - list_diagnostics: 需要回写到计划对象中的诊断列表。

    返回：
    - 返回所有状态路径都会共享的基础计划对象。

    异常：
    - 无显式异常；字典构造沿用 Python 默认行为。
    """

    # 返回所有状态路径共用的基础字段，避免 blocked/unsupported/dry-run 分支各自重复拼装。
    return {
        "tool": "vcs",
        "frontend": "cocotb",
        "scope": "non-gui scripted subset",
        "top": dict_make_variables.get("TOPLEVEL", ""),
        "module": dict_make_variables.get("MODULE", ""),
        "testcase": dict_make_variables.get("TESTCASE", ""),
        "toplevel_lang": dict_make_variables.get("TOPLEVEL_LANG", "verilog"),

        # 先收口 HDL 源文件清单，让后续 blocked 与 unsupported 分支共享同一份输入快照。
        "sources": {
            "verilog": _source_list(dict_make_variables.get("VERILOG_SOURCES", ""), path_project_root),
            "vhdl": _source_list(dict_make_variables.get("VHDL_SOURCES", ""), path_project_root),
        },
        "diagnostics": list_diagnostics,

        # 再显式列出当前技能不承诺覆盖的官方范围，避免调用方误解 non-GUI 子集边界。
        "unsupported_official_scope": [
            "GUI/DVE/interactive Verdi launch",
            "VCS cocotb VHDL/VHPI execution",
            "complete coverage of every official Synopsys option",
        ],
    }

# 构造 cocotb VCS 编译阶段命令，确保 VPI、`pli.tab` 与 cocotb define 都被显式写入。
def _compile_command(
    json_dict_dict_plan: JsonDict,
    list_plusargs: list[str],
    list_extra_args: list[str], list_compile_args: list[str],
    str_pli_tab: str, str_timescale: str,
    str_cocotb_vpi_lib: str,
) -> list[str]:
    """
    构造 cocotb VCS 编译阶段命令。

    参数：
    - json_dict_dict_plan: 当前基础计划对象，主要读取顶层名与源文件列表。
    - list_plusargs: Makefile 中的 `PLUSARGS` 拆词结果。
    - list_extra_args: Makefile 中的 `EXTRA_ARGS` 拆词结果。
    - list_compile_args: Makefile 中的 `COMPILE_ARGS` 拆词结果。
    - str_pli_tab: 编译阶段需要引用的 `pli.tab` 路径。
    - str_timescale: 编译阶段需要写入的时间精度字符串。
    - str_cocotb_vpi_lib: 编译阶段需要 `-load` 的 cocotb VPI 库路径。

    返回：
    - 返回 VCS 编译阶段的完整命令参数列表。

    异常：
    - 无显式异常；列表构造沿用 Python 默认行为。
    """

    # 返回 compile 阶段完整命令，确保 VPI、`pli.tab`、timescale 与 cocotb define 都被显式带上。
    return [
        "vcs",
        "-top",
        json_dict_dict_plan["top"],
        *list_plusargs,

        # 先补齐 cocotb VPI 编译必须显式打开的访问与插件入口开关。
        "+acc+1",
        "+vpi",
        "-P",
        str_pli_tab,

        # 继续补上 cocotb 固定 define、timescale 与额外编译参数。
        "+define+COCOTB_SIM=1",
        "-sverilog",
        f"-timescale={str_timescale}",
        *list_extra_args,
        "-debug",
        "-load",
        str_cocotb_vpi_lib,

        # 最后把调用方追加参数与待编译的 Verilog 源文件附加到命令尾部。
        *list_compile_args,
        *json_dict_dict_plan["sources"]["verilog"],
    ]

# 构造 cocotb VCS 仿真阶段命令，保持 `simv` 与 cocotb define 的历史约定。
def _simulate_command(
    str_sim_build: str,
    list_sim_args: list[str],
    list_extra_args: list[str],
) -> list[str]:
    """
    构造 cocotb VCS 仿真阶段命令。

    参数：
    - str_sim_build: Makefile 中的 `SIM_BUILD` 目录名。
    - list_sim_args: Makefile 中的 `SIM_ARGS` 拆词结果。
    - list_extra_args: Makefile 中的 `EXTRA_ARGS` 拆词结果。

    返回：
    - 返回 simv 仿真阶段的完整命令参数列表。

    异常：
    - 无显式异常；列表构造沿用 Python 默认行为。
    """

    # `SIM_BUILD` 为空时沿用当前目录下的 `simv`，否则拼成构建目录内的可执行文件路径。
    str_simv = f"{str_sim_build}/simv" if str_sim_build else "simv"  # 当前仿真阶段需要执行的 simv 路径

    # 返回仿真阶段完整命令，保证 cocotb define 与 Makefile 追加参数同时生效。
    return [str_simv, "+define+COCOTB_SIM=1", *list_sim_args, *list_extra_args]

# 构造 cocotb 仿真阶段环境变量，确保 MODULE/TOPLEVEL/TOPLEVEL_LANG 三项都显式落盘。
def _simulate_env(
    json_dict_dict_plan: JsonDict,
    dict_make_variables: MakeVariableDict,
) -> MakeVariableDict:
    """
    构造 cocotb 仿真阶段环境变量。

    参数：
    - json_dict_dict_plan: 当前基础计划对象，主要读取模块名、顶层名与顶层语言。
    - dict_make_variables: 当前已经解析出的 Makefile 变量表。

    返回：
    - 返回仿真阶段需要注入的环境变量映射。

    异常：
    - 无显式异常；字典构造与条件写入沿用 Python 默认行为。
    """

    # 先声明 cocotb VPI 运行最关键的三项环境变量和可选 testcase。
    make_variable_dict_dict_simulate_env: MakeVariableDict = {  # 当前仿真阶段需要注入的基础环境变量映射
        "MODULE": str(json_dict_dict_plan["module"]),  # 当前 cocotb Python 测试模块名
        "TESTCASE": str(json_dict_dict_plan["testcase"]),  # 当前计划显式指定的 testcase 名称
        "TOPLEVEL": str(json_dict_dict_plan["top"]),  # 当前仿真阶段使用的 HDL 顶层名
        "TOPLEVEL_LANG": str(json_dict_dict_plan["toplevel_lang"]),  # 当前仿真阶段使用的顶层语言
    }

    # Makefile 已经声明 `PYTHONPATH` 时，需要把它一并透传到 cocotb 运行环境中。
    if dict_make_variables.get("PYTHONPATH"):

        # 沿用解析结果里的 `PYTHONPATH`，避免 cocotb Python 包搜索路径被静默丢失。
        make_variable_dict_dict_simulate_env["PYTHONPATH"] = dict_make_variables["PYTHONPATH"]  # 当前仿真阶段额外透传的 Python 搜索路径

    # 返回仿真阶段需要注入的完整环境变量映射。
    return make_variable_dict_dict_simulate_env

# 根据结构化计划状态输出简短的人类可读摘要，避免默认模式把完整 JSON 倾倒到终端。
def _emit_human_summary(json_dict_dict_plan: JsonDict) -> None:
    """
    输出 cocotb VCS/VPI 计划的人类可读摘要。

    参数：
    - json_dict_dict_plan: 当前准备输出的结构化计划对象。

    返回：
    - 当前函数只产生标准输出副作用，不返回业务结果。

    异常：
    - 无显式异常；标准输出沿用 Python 默认行为。
    """

    # 先提取计划状态，便于后续按 dry-run、blocked 与 unsupported 三类路径输出不同摘要。
    str_status = str(json_dict_dict_plan.get("status", "unknown"))  # 当前结构化计划对象记录的总体状态

    # 同步提取阻断或不支持原因，供 warning 摘要在需要时补充最关键的短句信息。
    str_reason = str(json_dict_dict_plan.get("reason", ""))  # 当前结构化计划对象记录的原因码文本

    # dry-run 模式只需告诉调用方计划已经生成，并提示如何获取结构化 JSON。
    if str_status == "dry-run":

        # 输出非 GUI cocotb 计划已经生成的简短提示，避免默认模式打印大段结构化内容。
        print("> INFO: [Python] dry-run cocotb VCS/VPI plan generated; rerun with --json for structured details")

    # planned 模式代表调用方显式关闭 dry-run，此时仍然只需要输出简短摘要。
    elif str_status == "planned":

        # 输出已经进入 planned 状态的摘要，提示调用方如需结构化结果可改用 JSON 协议。
        print("> INFO: [Python] cocotb VCS/VPI plan generated; rerun with --json for structured details")

    # unsupported 模式需要明确点出为什么当前请求超出技能承诺边界。
    elif str_status == "unsupported":

        # 用固定 WARNING 前缀说明这是受支持边界之外的请求，而不是执行时异常。
        print(f"> WARNING: [Python] cocotb plan unsupported: {str_reason or 'see diagnostics'}")

    # blocked 模式表示当前请求触碰了 non-GUI 或输入完整性门禁，需要显式报告原因码。
    elif str_status == "blocked":

        # 用固定 WARNING 前缀提示这是输入或策略门禁导致的阻断，而不是脚本崩溃。
        print(f"> WARNING: [Python] cocotb plan blocked: {str_reason or 'see diagnostics'}")

    # 其余状态都不是当前 CLI 预期输出路径，需要显式给出错误摘要便于排查。
    else:

        # 用固定 ERR 前缀报告意外状态，提醒调用方这不是经过治理的正常协议路径。
        print(f"> ERR: [Python] unexpected cocotb plan status: {str_status}")

# 基于 cocotb Makefile 恢复 non-GUI VCS/VPI 规划结果，并显式守住 GUI 与 VHDL/VHPI 边界。
def build_cocotb_vcs_plan(
    *,
    makefile: Path,
    project_root: Path,
    toplevel_lang: str = "verilog",
    make_vars: MakeVariableDict | None = None,
    cocotb_lib: str | None = None,
    dry_run: bool = True,
) -> JsonDict:
    """
    构造 cocotb VCS/VPI non-GUI 计划对象。

    参数：
    - makefile: cocotb Makefile 路径。
    - project_root: 计划输出使用的项目根目录。
    - toplevel_lang: 调用方显式指定的顶层语言。
    - make_vars: 调用方显式覆盖的 Makefile 变量映射；为空时不额外覆盖。
    - cocotb_lib: 调用方显式指定的 cocotb VPI 库路径；为空时回退到标准 `cocotb-config` 表达式。
    - dry_run: 为真时返回 `dry-run` 状态；为假时返回 `planned` 状态。

    返回：
    - 返回包含 compile/simulate 计划、VPI 守卫与诊断信息的结构化对象。

    异常：
    - 无显式异常；路径解析、Makefile 读取与字典拼装沿用 Python 默认行为。
    """

    # 先把项目根目录规整成绝对路径，避免源文件相对化输出受调用时 cwd 干扰。
    path_project_root = project_root.resolve()  # 当前 cocotb 计划采用的项目根绝对路径

    # 从 Makefile 与调用方覆盖项中恢复最终变量表，供后续所有规划分支共享。
    make_variable_dict_dict_make_variables = parse_cocotb_makefile(  # 当前 cocotb Makefile 恢复出的最终变量表
        path_makefile=makefile,  # 当前请求要解析的 cocotb Makefile 路径
        str_toplevel_lang=toplevel_lang,  # 当前请求显式指定的默认顶层语言
        dict_make_var_overrides=make_vars,  # 当前请求附带的变量覆盖映射
    )

    # 先声明这条技能边界的核心事实，保证所有返回路径都会保留 VPI-only 约束说明。
    list_diagnostics = [  # 当前 cocotb VCS/VPI 计划默认携带的边界诊断列表
        (
            "cocotb VCS support is planned through VPI-only Verilog/SystemVerilog access; "
            "VHDL/VHPI is guarded unsupported for this flow."
        ),  # 当前技能默认公开的 VPI-only 范围说明
    ]

    # 构造所有状态路径都会共享的基础计划字段，避免 blocked/unsupported/dry-run 分支重复拼接。
    json_dict_dict_plan = _base_plan(  # 当前 cocotb 请求对应的基础计划对象
        make_variable_dict_dict_make_variables,  # 当前请求恢复出的 Makefile 变量表
        path_project_root,  # 当前计划采用的项目根目录
        list_diagnostics,  # 当前计划共享的边界诊断列表
    )

    # 把 Makefile 中几类常见参数字符串先拆成命令行列表，便于 GUI 守卫和后续命令拼接复用。
    list_compile_args = _split_words(make_variable_dict_dict_make_variables.get("COMPILE_ARGS", ""))  # Makefile 里的编译阶段补充参数列表

    # 仿真阶段补充参数会直接进入 simv 命令，因此要单独拆词保留原始顺序。
    list_sim_args = _split_words(make_variable_dict_dict_make_variables.get("SIM_ARGS", ""))  # Makefile 里的仿真阶段补充参数列表

    # `EXTRA_ARGS` 既可能进入编译也可能进入仿真，因此在这里先拆成独立列表复用。
    list_extra_args = _split_words(make_variable_dict_dict_make_variables.get("EXTRA_ARGS", ""))  # Makefile 里的通用补充参数列表

    # `PLUSARGS` 主要服务于编译与仿真共同可见的加号参数，因此也需要稳定拆词。
    list_plusargs = _split_words(make_variable_dict_dict_make_variables.get("PLUSARGS", ""))  # Makefile 里的 plusargs 参数列表

    # non-GUI 技能只允许纯命令行路径，因此先统一阻断所有显式 GUI 请求。
    if _has_gui_request(
        list_compile_args,
        list_sim_args,
        list_extra_args,
        list_plusargs,
    ) or make_variable_dict_dict_make_variables.get("GUI", "").lower() in {"1", "true", "yes"}:

        # 一旦检测到 GUI 请求，就立刻返回 blocked 计划，避免误生成交互式 VCS/Verdi 命令。
        return {**json_dict_dict_plan, "status": "blocked", "reason": "gui_requested"}

    # cocotb 的这条 VCS 技能只承诺 VPI-only Verilog/SystemVerilog，因此必须显式守住 VHDL/VHPI 边界。
    if json_dict_dict_plan["sources"]["vhdl"]:

        # 对带有 VHDL 源的请求返回 unsupported，让调用方明确这是技能边界而不是脚本故障。
        return {
            **json_dict_dict_plan,
            "status": "unsupported",
            "reason": "vcs_cocotb_vhdl_unsupported",
        }

    # 没有任何 Verilog/SystemVerilog 源时，当前计划缺少最基础的编译输入，必须直接阻断。
    if not json_dict_dict_plan["sources"]["verilog"]:

        # 显式返回 blocked 状态，提醒调用方先补齐非 VHDL 的 cocotb 设计输入。
        return {**json_dict_dict_plan, "status": "blocked", "reason": "no_verilog_sources"}

    # 顶层名和 cocotb Python 模块名是 compile/simulate 两个阶段的最小锚点，缺失时无法继续规划。
    if not json_dict_dict_plan["top"] or not json_dict_dict_plan["module"]:

        # 缺少顶层或模块名时直接阻断，避免继续生成缺字段的半成品命令。
        return {**json_dict_dict_plan, "status": "blocked", "reason": "missing_toplevel_or_module"}

    # `SIM_BUILD` 决定 simv 与 `pli.tab` 的落盘目录，因此这里先规整成稳定字符串。
    str_sim_build = make_variable_dict_dict_make_variables.get("SIM_BUILD") or "sim_build"  # 当前计划最终采用的 cocotb 构建目录名

    # `pli.tab` 路径必须显式暴露在计划里，供调用方或远端执行器提前准备文件。
    str_pli_tab = f"{str_sim_build}/pli.tab"  # 当前计划 compile 阶段需要引用的 pli.tab 路径

    # cocotb 时间精度来自 Makefile 中的两个分量，因此先统一拼成 VCS 期望的 `unit/precision` 字符串。
    str_timescale = (
        f"{make_variable_dict_dict_make_variables.get('COCOTB_HDL_TIMEUNIT', '1ns')}/"
        f"{make_variable_dict_dict_make_variables.get('COCOTB_HDL_TIMEPRECISION', '1ps')}"
    )  # 当前计划 compile 阶段采用的 timescale 字符串

    # 调用方未显式给出 cocotb VPI 库时，沿用官方 `cocotb-config` 查询表达式。
    str_cocotb_vpi_lib = cocotb_lib or "$(cocotb-config --lib-name-path vpi vcs)"  # 当前计划 compile 阶段需要 `-load` 的 cocotb VPI 库路径

    # 结果文件名为空时回退到 `results.xml`，保持 cocotb 样例工程的历史默认约定。
    str_results_file = make_variable_dict_dict_make_variables.get("COCOTB_RESULTS_FILE") or "results.xml"  # 当前计划执行完成后应当产出的 cocotb 结果文件名

    # 返回 dry-run 或 planned 状态下的完整计划对象，显式写出 compile/simulate 两个阶段与 VPI 边界信息。
    return {
        **json_dict_dict_plan,
        "status": "dry-run" if dry_run else "planned",
        "required_external_dependencies": ["vcs", "cocotb VPI library"],
        "write_pli_tab": {"path": str_pli_tab, "content": "acc+=rw,wn:*"},

        # 先写出 compile 阶段命令，供上游执行器按 non-GUI 路径准备仿真构建。
        "compile": {
            "cwd": str(path_project_root),
            "cmd": _compile_command(
                json_dict_dict_plan,
                list_plusargs,
                list_extra_args,
                list_compile_args,

                # 这三项共同决定 cocotb VPI 编译绑定的时序与动态库装载入口。
                str_pli_tab,
                str_timescale,
                str_cocotb_vpi_lib,
            ),
        },

        # 再写出 simulate 阶段的环境与命令，保持 cocotb VPI 运行入口完整可复现。
        "simulate": {
            "cwd": str(path_project_root),
            "env": _simulate_env(json_dict_dict_plan, make_variable_dict_dict_make_variables),
            "cmd": _simulate_command(str_sim_build, list_sim_args, list_extra_args),
        },

        # 最后列出调用方应当观察到的主要产物，便于 smoke 阶段核对结果。
        "expected_artifacts": [str_results_file],
    }

# 解析 `--make-var NAME=VALUE` 形式的命令行覆盖项，供 CLI 入口统一复用。
def _parse_make_var_entries(list_make_var_entries: list[str], parser: argparse.ArgumentParser) -> MakeVariableDict:
    """
    解析命令行传入的 Makefile 变量覆盖项。

    参数：
    - list_make_var_entries: CLI 中重复出现的 `--make-var` 文本列表。
    - parser: 当前 CLI 使用的参数解析器对象。

    返回：
    - 返回解析好的 Makefile 变量覆盖映射。

    异常：
    - 参数格式非法时通过 `parser.error(...)` 终止当前 CLI 解析流程。
    """

    # 初始化空映射，供循环逐条写入 `NAME=VALUE` 形式的覆盖项。
    make_variable_dict_dict_make_var_overrides: MakeVariableDict = {}  # 当前 CLI 传入的 Makefile 变量覆盖映射

    # 顺序解析每一条 `--make-var` 文本，保持调用方显式给出的覆盖顺序。
    for str_item in list_make_var_entries:

        # 没有等号时无法区分变量名和值，因此必须立刻阻断为参数格式错误。
        if "=" not in str_item:

            # 借助 argparse 的统一报错出口，给调用方一个结构稳定的输入错误提示。
            parser.error(f"> ERR: [Python] --make-var must be NAME=VALUE, got {str_item!r}")

        # 按首个等号拆出变量名和值，允许值部分继续保留后续等号文本。
        str_key, str_value = str_item.split("=", 1)  # 当前覆盖项拆出的变量名和值文本

        # 把当前覆盖项写回映射，供 Makefile 解析阶段直接优先采用。
        make_variable_dict_dict_make_var_overrides[str_key] = str_value  # 当前 CLI 覆盖项写入后的最终值

    # 返回 CLI 解析出的 Makefile 变量覆盖映射。
    return make_variable_dict_dict_make_var_overrides

# 提供 cocotb non-GUI VCS/VPI 规划的命令行入口，负责参数解析与输出协议收束。
def main(argv: list[str] | None = None) -> int:
    """
    提供 cocotb VCS/VPI 规划的命令行入口。

    参数：
    - argv: 可选的命令行参数列表；为空时由 argparse 直接读取进程参数。

    返回：
    - 对于 `dry-run`、`planned`、`unsupported` 与 `blocked` 状态返回零；意外状态返回非零。

    异常：
    - 参数格式错误时由 argparse 直接终止并输出错误。
    """

    # 构造 CLI 解析器，集中声明 non-GUI cocotb 规划入口允许接收的命令行参数。
    parser = argparse.ArgumentParser(description="Plan a non-GUI cocotb VCS/VPI simulation flow.")  # 当前 cocotb CLI 使用的参数解析器

    # Makefile 路径是恢复 cocotb 变量的核心输入，因此设为必填参数。
    parser.add_argument("--makefile", type=Path, required=True)

    # 项目根目录决定源文件相对化与 compile/simulate 的工作目录，因此同样必须显式给出。
    parser.add_argument("--project-root", type=Path, required=True)

    # 顶层语言默认为 Verilog，允许调用方在需要时显式改写。
    parser.add_argument("--toplevel-lang", default="verilog")

    # 支持多次传入 `--make-var`，让调用方可以覆盖 Makefile 中的局部变量。
    parser.add_argument("--make-var", action="append", default=[])

    # 允许调用方显式给出 cocotb VPI 库路径，避免完全依赖远端环境的 `cocotb-config`。
    parser.add_argument("--cocotb-lib")

    # dry-run 模式只做规划不做执行，因此保留为显式布尔开关。
    parser.add_argument("--dry-run", action="store_true")

    # `--json` 代表调用方需要单个 JSON 对象作为机器可读协议输出。
    parser.add_argument("--json", action="store_true")

    # 解析当前 CLI 请求，得到后续计划构造阶段共享的参数对象。
    args = parser.parse_args(argv)  # 当前 CLI 请求解析得到的参数对象

    # 把重复出现的 `--make-var` 文本统一收口成变量覆盖映射，供 Makefile 解析阶段直接消费。
    make_variable_dict_dict_make_var_overrides = _parse_make_var_entries(args.make_var, parser)  # 当前 CLI 请求携带的 Makefile 变量覆盖映射

    # 构造当前请求对应的结构化 cocotb VCS/VPI 计划对象。
    json_dict_dict_plan = build_cocotb_vcs_plan(  # 当前 CLI 请求生成的结构化 cocotb 计划对象
        makefile=args.makefile,  # 当前 CLI 指定的 cocotb Makefile 路径
        project_root=args.project_root,  # 当前 CLI 指定的项目根目录
        toplevel_lang=args.toplevel_lang,  # 当前 CLI 传入的默认顶层语言
        make_vars=make_variable_dict_dict_make_var_overrides,  # 让命令行覆盖项优先压过 Makefile 默认值
        cocotb_lib=args.cocotb_lib,  # 当前 CLI 指定的 cocotb VPI 库路径
        dry_run=args.dry_run,  # 当前 CLI 是否只生成 dry-run 计划
    )

    # 显式请求 JSON 协议时，标准输出只允许写出单个 JSON 对象。
    if args.json:

        # 按模块文档约定把结构化计划对象写到标准输出，供上游自动化直接消费。
        json.dump(json_dict_dict_plan, sys.stdout, indent=2, sort_keys=True)

        # 为 JSON 输出补一个换行，避免 shell 提示符直接粘在 JSON 末尾。
        sys.stdout.write("\n")

    # 默认模式只输出带前缀的人类可读摘要，避免完整计划对象污染终端日志。
    else:

        # 输出简短摘要并提示如何获取 JSON 协议结果，保持默认终端输出整洁。
        _emit_human_summary(json_dict_dict_plan)

    # dry-run、planned、blocked 与 unsupported 都属于受控协议状态，因此统一按成功退出处理。
    return 0 if json_dict_dict_plan["status"] in {"dry-run", "planned", "unsupported", "blocked"} else 1

# 只有脚本被直接执行时才触发 CLI，避免测试导入模块时立即退出当前 Python 进程。
if __name__ == "__main__":

    # 把 main 返回值转换为进程退出码，供 shell、CI 与远端 smoke 流程直接判定成败。
    raise SystemExit(main())
