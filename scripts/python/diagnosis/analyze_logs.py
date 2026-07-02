#!/usr/bin/env python3
"""分析 VCS / Verdi 日志中的常见失败模式。

本模块同时提供可导入的分析函数与命令行入口。

命令行标准输出协议：
- 默认输出人类可读的带前缀状态与逐条诊断信息。
- 当传入 ``--json`` 时，标准输出会写出单个 JSON 对象，供上游自动化直接消费。
"""

# 启用延后求值注解，避免类型提示在运行期引入额外解析顺序要求。
from __future__ import annotations
# 提供命令行参数解析、JSON 序列化、正则匹配、标准输出与路径处理能力。
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable

# 声明按优先级匹配的日志诊断规则，前面的规则会优先决定当前日志行的分类结果。
RULE_DEFINITIONS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "license",  # 许可证故障类别名
        "error",  # 许可证故障的严重级别
        re.compile(r"license|checkout failed|snpslmd|lm_license", re.IGNORECASE),  # 识别授权 checkout 失败的正则模式
    ),  # 许可证与授权失败模式
    (
        "platform",  # 平台兼容性问题类别名
        "warning",  # 平台兼容性问题的严重级别
        re.compile(r"unsupported linux|unsupported .*kernel|LNX_OS_VERUN|LINX_KRNL", re.IGNORECASE),  # 识别内核或平台不兼容的正则模式
    ),  # 平台与内核兼容性警告模式
    (
        "pli",  # PLI 依赖缺失类别名
        "warning",  # PLI 依赖缺失的严重级别
        re.compile(r"novas\.tab|pli\.a|pli.*missing|no such file or directory.*pli", re.IGNORECASE),  # 识别 PLI 配置或库文件缺失的正则模式
    ),  # PLI 依赖缺失告警模式
    (
        "fsdb",  # FSDB 工件失败类别名
        "error",  # FSDB 工件失败的严重级别
        re.compile(r"fsdb.*(missing|zero|failed|error)|failed to open FSDB", re.IGNORECASE),  # 识别 FSDB 缺失、空文件与打开失败的正则模式
    ),  # FSDB 产物与读回失败模式
    (
        "compile_error",  # 编译失败类别名
        "error",  # 编译失败的严重级别
        re.compile(r"(^|\s)(Error-\[|\*E,|error:|syntax error)", re.IGNORECASE),  # 识别编译器报错与语法错误的正则模式
    ),  # 编译与语法报错模式
    (
        "warning",  # 通用 warning 类别名
        "warning",  # 通用 warning 的严重级别
        re.compile(r"(^|\s)(Warning-\[|\*W,|warning:)", re.IGNORECASE),  # 识别未被更高优先级规则捕获的 warning 模式
    ),  # 通用 warning 级诊断模式
)

# 按单行文本匹配第一条命中的日志问题规则。
def _line_issue(line: str, *, line_no: int, source: str) -> dict[str, object] | None:
    """
    从单行日志文本中提取第一条命中的问题记录。

    参数：
    - line: 当前待分析的原始日志行文本。
    - line_no: 当前日志行在源文本中的 1 基行号。
    - source: 当前日志文本对应的来源标识。

    返回：
    - 返回结构化问题字典；若当前行没有命中任何规则，则返回 ``None``。

    异常：
    - 无显式异常；正则匹配异常沿用 Python 运行时默认行为。
    """

    # 逐条尝试预定义规则，确保每一行只记录最先命中的最高优先级诊断类别。
    for str_category, str_severity, pattern_rule in RULE_DEFINITIONS:

        # 只有当前规则真正命中日志行时，才构造结构化问题记录返回给上层。
        if pattern_rule.search(line):

            # 返回命中的结构化问题记录，供汇总函数统一统计严重级别与类别分布。
            return {
                "source": source,
                "line": line_no,
                "category": str_category,
                "severity": str_severity,
                "text": line.strip(),
            }

    # 显式返回空结果，表示当前日志行没有匹配到任何已知失败模式。
    return None

# 汇总多条问题记录的统计信息，供文本分析和多文件分析复用。
def summarize(issues: Iterable[dict[str, object]], *, files: int = 1) -> dict[str, object]:
    """
    统计问题记录集合中的文件数、问题数和严重级别分布。

    参数：
    - issues: 待汇总的问题记录可迭代对象。
    - files: 当前汇总对应的日志文件数量，默认按单文件处理。

    返回：
    - 返回包含文件数、问题数、错误数、告警数和类别列表的汇总字典。

    异常：
    - 无显式异常；若传入记录结构不符合约定，键访问异常沿用默认行为。
    """

    # 先把可迭代对象落成列表，避免后续统计次数和类别集合时重复消耗输入迭代器。
    list_issue_records = list(issues)  # 当前待汇总的问题记录列表

    # 初始化 error 级计数器，用于记录阻断性问题在当前输入中的出现次数。
    int_error_count = 0  # 当前输入命中的阻断性问题数量

    # 逐条扫描问题记录，把 error 级问题单独累计到阻断计数器中。
    for dict_issue_record in list_issue_records:

        # 只有 error 级问题才会增加阻断计数，用于驱动最终 failed 状态。
        if dict_issue_record["severity"] == "error":

            # 为每条 error 级记录累加一次计数，保留和测试断言一致的统计语义。
            int_error_count += 1  # 已累计的 error 级问题数量

    # 初始化 warning 级计数器，用于统计非阻断风险在当前输入中的出现次数。
    int_warning_count = 0  # 当前输入命中的非阻断风险数量

    # 再扫描一次问题记录，把 warning 级问题累计到风险计数器中。
    for dict_issue_record in list_issue_records:

        # 只有 warning 级问题才会增加风险计数，用于保留既有汇总字段语义。
        if dict_issue_record["severity"] == "warning":

            # 为每条 warning 级记录累加一次计数，便于上游评估非阻断风险规模。
            int_warning_count += 1  # 当前风险计数器记录的 warning 总数

    # 汇总并排序所有命中的类别名称，保证测试与自动化输出具有稳定顺序。
    list_category_names = sorted({str(dict_issue_record["category"]) for dict_issue_record in list_issue_records})  # 已排序的类别名称列表

    # 返回结构化汇总结果，供文本级与多文件级分析统一复用。
    return {
        "files": files,
        "issues": len(list_issue_records),
        "errors": int_error_count,
        "warnings": int_warning_count,
        "categories": list_category_names,
    }

# 分析单段日志文本，返回可直接供测试和 CLI 输出消费的结构化报告。
def analyze_text(text: str, *, source: str = "<text>") -> dict[str, object]:
    """
    分析单段日志文本中的常见 VCS / Verdi 失败模式。

    参数：
    - text: 待分析的完整日志文本内容。
    - source: 当前日志文本对应的来源标识，默认使用 ``<text>``。

    返回：
    - 返回包含 ``status``、``summary`` 与 ``issues`` 三个字段的结构化报告字典。

    异常：
    - 无显式异常；字符串遍历与内部字典访问异常沿用默认行为。
    """

    # 累积当前文本中命中的问题记录，供后续统一汇总状态和类别。
    list_issue_records: list[dict[str, object]] = []  # 当前文本命中的问题记录列表

    # 逐行扫描日志文本，保持输出行号与原始日志的 1 基编号一致。
    for int_line_number, str_line in enumerate(text.splitlines(), start=1):

        # 先尝试从当前日志行提取问题记录，避免在循环体中重复展开规则匹配逻辑。
        dict_issue_record = _line_issue(str_line, line_no=int_line_number, source=source)  # 当前日志行命中的问题记录

        # 只有命中规则的日志行才需要写入问题列表，未命中行直接跳过即可。
        if dict_issue_record is not None:

            # 把当前命中的问题记录追加到结果列表，供后续汇总与命令行输出复用。
            list_issue_records.append(dict_issue_record)

    # 生成当前文本的问题汇总，统一计算错误数、告警数与类别分布。
    dict_summary = summarize(list_issue_records)  # 当前文本的汇总统计结果

    # 根据是否存在 error 级问题决定最终状态，保持与既有测试约定一致。
    str_status = "failed" if dict_summary["errors"] else "passed"  # 当前文本的整体状态

    # 返回单段文本的结构化分析结果，供测试、库调用与 CLI 输出共同消费。
    return {
        "status": str_status,
        "summary": dict_summary,
        "issues": list_issue_records,
    }

# 分析多个日志文件路径，并返回跨文件聚合后的统一诊断报告。
def analyze_paths(paths: Iterable[Path | str]) -> dict[str, object]:
    """
    分析多个日志文件并汇总最坏状态与全部问题记录。

    参数：
    - paths: 待分析的日志文件路径集合，元素可以是 ``Path`` 或字符串路径。

    返回：
    - 返回跨文件聚合后的结构化诊断报告字典。

    异常：
    - 读取文件失败时会抛出底层 ``OSError`` 及其子类异常。
    """

    # 先把输入路径统一规范成 Path 列表，避免后续多次迭代时丢失原始输入顺序。
    list_path_objects = [Path(path) for path in paths]  # 当前待分析的日志路径列表

    # 累积所有文件命中的问题记录，供跨文件汇总与最终状态判定复用。
    list_all_issue_records: list[dict[str, object]] = []  # 全部日志文件的问题记录列表

    # 逐个读取日志文件并复用单文本分析逻辑，避免路径模式与文本模式出现分叉行为。
    for path_log in list_path_objects:

        # 读取当前日志文件全文，遇到非法字节时用替换模式保证分析过程继续进行。
        str_text = path_log.read_text(encoding="utf-8", errors="replace")  # 当前日志文件的原始文本

        # 复用单文本分析逻辑生成当前文件报告，保持分类与汇总语义一致。
        dict_report = analyze_text(str_text, source=str(path_log))  # 当前日志文件的结构化报告

        # 把当前文件的问题记录并入总列表，供跨文件汇总时统一统计。
        list_all_issue_records.extend(dict_report["issues"])

    # 对全部文件的问题记录做一次总汇总，得到文件数、错误数和类别分布。
    dict_summary = summarize(list_all_issue_records, files=len(list_path_objects))  # 多文件场景的汇总统计结果

    # 只要任意文件命中 error 级问题，就把整体状态标记为 failed。
    str_status = "failed" if dict_summary["errors"] else "passed"  # 多文件场景的整体状态

    # 返回跨文件聚合后的结构化分析结果，供测试与 CLI 入口统一消费。
    return {
        "status": str_status,
        "summary": dict_summary,
        "issues": list_all_issue_records,
    }

# 解析命令行参数并执行日志分析 CLI。
def main() -> int:
    """
    运行日志分析命令行入口并返回进程退出码。

    参数：
    - 无额外业务参数；命令行输入通过 ``argparse`` 从标准入口解析。

    返回：
    - 返回 ``0`` 表示未发现 error 级问题，返回 ``1`` 表示至少存在一条 error 级问题。

    异常：
    - 参数解析失败时由 ``argparse`` 抛出并终止进程；文件读取失败时沿用底层异常。
    """

    # 创建命令行参数解析器，统一声明脚本用途与可选输出协议。
    parser = argparse.ArgumentParser(description="Analyze VCS/Verdi logs for common failures.")  # 当前脚本的命令行参数解析器

    # 注册待分析日志路径参数，要求调用方至少提供一个日志文件。
    parser.add_argument("logs", nargs="+", type=Path)

    # 注册 JSON 输出开关，启用后按模块文档声明输出单个结构化 JSON 对象。
    parser.add_argument("--json", action="store_true")

    # 解析调用方传入的命令行参数，得到本次执行所需的路径与输出模式。
    args = parser.parse_args()  # 当前 CLI 解析得到的参数对象

    # 运行多文件分析逻辑，生成供 JSON 输出或终端摘要复用的统一报告。
    dict_report = analyze_paths(args.logs)  # 当前命令行执行得到的结构化报告

    # 当调用方显式请求 JSON 协议时，输出单个结构化 JSON 对象供上游自动化消费。
    if args.json:

        # 按模块文档约定把单个 JSON 对象写到标准输出，避免与机器消费协议混入其他文本。
        json.dump(dict_report, sys.stdout, indent=2, sort_keys=True)

        # 为 JSON 协议输出补一个换行，避免 shell 提示符直接接在 JSON 末尾。
        sys.stdout.write("\n")

    # 未请求 JSON 协议时，只输出带固定前缀的摘要，避免把结构化内容直接打印到终端。
    else:

        # 根据整体状态选择终端摘要前缀，让人工排查时能一眼看出是否失败。
        if dict_report["status"] == "passed":

            # 输出成功摘要，表示当前输入日志中没有命中 error 级问题。
            print("> INFO: [Python] passed with no error-level findings")

        # 失败状态需要使用错误前缀，提醒调用方至少存在一条 error 级问题。
        else:

            # 输出失败摘要，提示调用方改用 --json 查看完整结构化问题明细。
            print("> ERR: [Python] failed with error-level findings; rerun with --json for details")

    # 根据最终状态返回命令行退出码，保持脚本语义与既有测试期望一致。
    return 0 if dict_report["status"] == "passed" else 1

# 只有以脚本方式直接执行时才启动 CLI，避免导入测试模块时立刻退出当前 Python 进程。
if __name__ == "__main__":

    # 把 main 返回值转换为进程退出码，供 shell、CI 与远端 smoke 流程直接判定成败。
    raise SystemExit(main())
