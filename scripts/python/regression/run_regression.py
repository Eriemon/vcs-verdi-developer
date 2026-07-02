#!/usr/bin/env python3
"""构建并执行 manifest 驱动的 VCS/Verdi 非图形回归计划。

本模块同时提供可导入的计划构建、单用例执行、结果汇总与命令行入口。

命令行标准输出协议：
- 默认只打印带前缀的人类可读状态摘要，不把完整结构化结果直接输出到终端。
- 当传入 ``--json`` 时，标准输出会写出单个 JSON 对象，供自动化直接消费。
"""

# 启用延后求值注解，避免类型提示在运行期引入额外导入顺序要求。
from __future__ import annotations

# 提供命令行参数解析、并发执行、JSON 处理、子进程调用、标准输出和 XML 序列化能力。
import argparse
import concurrent.futures
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as xml_element_tree
from pathlib import Path

# 根据 manifest 列表构建回归执行计划，供 CLI 与单元测试共用同一份计划结构。
def build_regression_plan(
    manifests: list[Path | str],
    *,
    workdir: Path | str,
    jobs: int = 1,
    timeout: int = 300,
) -> dict[str, object]:
    """
    构建 manifest 驱动的回归执行计划。

    参数：
    - manifests: 需要纳入回归的 manifest 路径列表。
    - workdir: 本次回归根工作目录。
    - jobs: 允许并行执行的最大用例数。
    - timeout: 每条用例命令的超时时间，单位为秒。

    返回：
    - 返回包含 ``workdir``、``jobs``、``timeout`` 与 ``cases`` 的计划字典。

    异常：
    - 无显式异常；路径拼接和字符串转换沿用 Python 默认行为。
    """

    # 定位单条用例真正调用的验证脚本，确保每个 manifest 都走同一条非图形验证入口。
    path_validation_script = Path(__file__).resolve().parents[1] / "validation" / "vcs_verdi_check.py"  # 回归用例调用的验证脚本路径

    # 统一规整回归根目录路径，避免 Path 与字符串在后续拼接中反复转换。
    path_root_workdir = Path(workdir)  # 本次回归根工作目录

    # 按输入 manifest 顺序收集全部回归用例，供执行阶段并发分发。
    list_cases: list[dict[str, object]] = []  # 计划中全部回归用例的结构化描述列表

    # 逐条 manifest 生成独立用例，保证每个工作目录与命令行参数都可复现。
    for obj_manifest in manifests:

        # 把 manifest 输入统一规整成 Path，便于提取 stem 和构建工作目录。
        path_manifest = Path(obj_manifest)  # 当前回归用例对应的 manifest 路径

        # 使用 manifest 文件名 stem 作为稳定的用例名，便于结果排序和 JUnit 命名。
        str_case_name = path_manifest.stem  # 当前回归用例的稳定名称

        # 为当前用例创建独立工作目录，避免不同 manifest 的中间产物相互覆盖。
        path_case_workdir = path_root_workdir / str_case_name  # 当前回归用例的独立工作目录

        # 组装实际执行命令，确保每条用例都显式开启执行、清理和 JSON 输出模式。
        list_case_command = [  # 当前回归用例实际执行的命令参数列表
            sys.executable,  # 复用当前 Python 解释器，避免环境漂移
            str(path_validation_script),  # 进入统一的 VCS/Verdi 验证脚本
            "--manifest",  # 显式传递 manifest 参数名
            str(path_manifest),  # 当前用例要消费的 manifest 路径
            "--workdir",  # 显式传递独立工作目录参数名
            str(path_case_workdir),  # 当前用例隔离后的工作目录路径
            "--execute",  # 要求验证脚本真正执行非图形流程
            "--clean",  # 执行前清理旧产物，避免脏结果污染
            "--json",  # 让下游脚本吐出结构化结果供本模块汇总
        ]

        # 把当前 manifest 映射成一条独立回归用例记录，供执行阶段直接消费。
        list_cases.append(
            {
                "name": str_case_name,
                "manifest": str(path_manifest),
                "workdir": str(path_case_workdir),
                "cmd": list_case_command,
            }
        )

    # 返回完整回归计划，供 CLI、执行器与单元测试共用同一份结构契约。
    return {
        "workdir": str(path_root_workdir),
        "jobs": jobs,
        "timeout": timeout,
        "cases": list_cases,
    }

# 把真实执行命令转换成 dry-run 版本，避免试跑阶段误触发真正的 EDA 流程。
def _dry_run_cmd(list_command: list[str]) -> list[str]:
    """
    生成 dry-run 模式下应执行的命令列表。

    参数：
    - list_command: 原始执行命令参数列表。

    返回：
    - 返回移除了 ``--execute``、``--clean`` 并补上 ``--dry-run`` 的命令列表。

    异常：
    - 无显式异常；列表推导和插入沿用 Python 默认行为。
    """

    # 先剔除真正执行与清理标记，避免 dry-run 场景仍触发重操作。
    list_dry_run_command = [str_part for str_part in list_command if str_part not in {"--execute", "--clean"}]  # dry-run 模式下保留的基础命令列表

    # 只有原命令里还没有 dry-run 标记时，才在脚本路径之后补上该开关。
    if "--dry-run" not in list_dry_run_command:

        # 把 dry-run 插到解释器和脚本路径之后，保持与验证脚本参数顺序兼容。
        list_dry_run_command.insert(2, "--dry-run")

    # 返回 dry-run 命令，让试跑模式仍走与正式执行一致的脚本入口。
    return list_dry_run_command

# 尝试从子进程 stdout 中提取单个 JSON 对象，兼容前后混入少量非 JSON 文本的场景。
def _json_from_stdout(str_stdout_text: str) -> dict[str, object]:
    """
    从标准输出文本中解析结构化 JSON 结果。

    参数：
    - str_stdout_text: 子进程标准输出全文。

    返回：
    - 若成功解析到 JSON 对象则返回该字典；否则返回空字典。

    异常：
    - 无显式异常；JSON 解析失败会被函数内部收敛为空字典。
    """

    # 先尝试把 stdout 全文当成纯 JSON 解析，覆盖最常见的协议化输出场景。
    try:

        # 直接解析整段 stdout，优先覆盖完全遵守 JSON 协议的理想输出路径。
        dict_stdout_payload = json.loads(str_stdout_text)  # 把整段 stdout 直接解析后的 JSON 对象

    # 整段文本不是纯 JSON 时，再回退到大括号片段提取路径。
    except json.JSONDecodeError:

        # 解析失败说明 stdout 混入了额外日志，继续走片段恢复路径。
        dict_stdout_payload = {}  # 纯 JSON 解析失败后的空占位结果

    # 纯 JSON 解析成功时直接返回，避免继续做无意义的片段搜索。
    if dict_stdout_payload:

        # 返回整段 stdout 直接解析得到的 JSON 结果。
        return dict_stdout_payload

    # 记录首个左花括号位置，作为可能的 JSON 片段起点。
    int_json_start = str_stdout_text.find("{")  # stdout 中首个左花括号索引

    # 记录最后一个右花括号位置，作为可能的 JSON 片段终点。
    int_json_end = str_stdout_text.rfind("}")  # stdout 中最后一个右花括号索引

    # 只有确实找到闭合的大括号片段时，才尝试按 JSON 对象重新解析。
    if int_json_start >= 0 and int_json_end > int_json_start:

        # 抽取可能包裹在日志中的 JSON 片段，尽量保住下游汇总所需的结构化信息。
        str_json_slice = str_stdout_text[int_json_start : int_json_end + 1]  # stdout 中可能的 JSON 对象片段

        # 再次尝试解析提取出的 JSON 片段，兼容前后带日志前缀的 stdout。
        try:

            # 只解析大括号包裹的片段，尽量从日志文本中恢复内层结构化结果。
            dict_slice_payload = json.loads(str_json_slice)  # 从 stdout 片段恢复出来的 JSON 对象

        # 片段仍无法解析时，说明 stdout 中没有可信的 JSON 对象可用。
        except json.JSONDecodeError:

            # 片段恢复同样失败时，后续会统一回退到空字典。
            dict_slice_payload = {}  # JSON 片段恢复失败后的空占位结果

        # 片段解析成功时直接返回，保住内层验证脚本吐出的结构化结果。
        if dict_slice_payload:

            # 返回从日志包裹文本中恢复出来的 JSON 对象。
            return dict_slice_payload

    # 没有可恢复的 JSON 结果时返回空字典，让汇总逻辑走缺省分支。
    return {}

# 从单条回归结果中抽取缺工具、产物状态和内部验证状态，形成更短的摘要结构。
def summarize_case_result(dict_case_result: dict[str, object]) -> dict[str, object]:
    """
    汇总单条回归用例的关键诊断摘要。

    参数：
    - dict_case_result: 单条回归用例执行后的结构化结果字典。

    返回：
    - 返回包含 ``missing_tools``、``artifacts``、``tool_confidence`` 等字段的摘要字典。

    异常：
    - 无显式异常；字段缺失时会回退到稳定的缺省值。
    """

    # 尝试从 stdout 中恢复内层验证脚本输出的 JSON 结果，供摘要阶段二次消费。
    dict_parsed_stdout = _json_from_stdout(str(dict_case_result.get("stdout", "")))  # 从 stdout 解析出的内层结构化结果

    # diagnostics 可能承载更细粒度的 artifact 信息，需要先安全收敛成字典。
    obj_diagnostics_raw: object = dict_parsed_stdout.get("diagnostics", {})  # 解析结果中 diagnostics 原始字段

    # 只有 diagnostics 真的是字典时，才允许后续读取 artifacts 子字段。
    dict_diagnostics = obj_diagnostics_raw if isinstance(obj_diagnostics_raw, dict) else {}  # 可安全读取的 diagnostics 字典

    # 优先读取 diagnostics.artifacts，没有时再退回旧格式的 artifact_status 兼容字段。
    obj_artifacts_raw: object = dict_diagnostics.get("artifacts") or dict_parsed_stdout.get("artifact_status", {})  # 解析结果中记录产物状态的原始字段

    # 只保留可安全向上游暴露的 artifacts 字典，避免坏结构污染摘要结果。
    dict_artifacts = obj_artifacts_raw if isinstance(obj_artifacts_raw, dict) else {}  # 当前用例可消费的产物状态字典

    # 这里抓的是内层脚本原样回传的缺工具字段，后面会据此区分真列表和异常包装结构。
    obj_missing_tools = dict_parsed_stdout.get("missing_tools", [])  # 供后续类型分流使用的缺工具原始字段

    # 只有列表形态才作为缺工具集合使用，其他结构统一降级为空列表。
    list_missing_tools = (  # 当前用例缺失的工具名列表
        [str(obj_tool_name) for obj_tool_name in obj_missing_tools]  # 把缺失工具名统一规整成字符串
        if isinstance(obj_missing_tools, list)  # 只有列表形态才允许逐项转成字符串
        else []  # 其他形态统一降级为空列表
    )

    # 先把返回码规整成整数或 None，避免 tool_confidence 判断受混杂类型影响。
    obj_returncode_raw: object = dict_case_result.get("returncode")  # 当前用例返回码的原始字段

    # 只有整数返回码才参与 passed/failed 判定，其余形态统一按 None 处理。
    int_returncode = obj_returncode_raw if isinstance(obj_returncode_raw, int) else None  # 当前用例经类型确认后的返回码

    # 缺工具时优先标记 blocked；否则由返回码决定是真通过还是执行失败。
    str_tool_confidence = "blocked" if list_missing_tools else "passed" if int_returncode == 0 else "failed"  # 当前用例工具与执行证据合成后的可信度

    # 返回单条回归结果的摘要字典，供最终结果和 JUnit 之外的轻量展示复用。
    return {
        "name": str(dict_case_result.get("name", "")),
        "status": str(dict_case_result.get("status", "")),
        "returncode": int_returncode,
        "missing_tools": list_missing_tools,
        "artifacts": dict_artifacts,
        "tool_confidence": str_tool_confidence,
        "inner_status": str(dict_parsed_stdout.get("status", "")),
    }

# 以逐步赋值方式构建单条回归结果，避免在执行函数里塞入过大的字典字面量表。
def _build_case_result_payload(
    dict_case: dict[str, object], *,
    list_effective_command: list[str],
    str_case_status: str, obj_returncode: int | None,
    float_elapsed_seconds: float,
    tuple_output_texts: tuple[str, str],
) -> dict[str, object]:
    """
    构建单条回归用例的结构化结果字典。

    参数：
    - dict_case: 原始回归用例计划字典。
    - list_effective_command: 本次实际执行的命令参数列表。
    - str_case_status: 归一化后的用例状态文本。
    - obj_returncode: 当前用例的退出码；超时时传入 ``None``。
    - float_elapsed_seconds: 当前用例执行耗时，单位为秒。
    - tuple_output_texts: 当前用例捕获到的 ``(stdout, stderr)`` 文本元组。

    返回：
    - 返回包含原始计划字段、执行结果字段与摘要字段的结果字典。

    异常：
    - 无显式异常；字典复制与字段写入沿用 Python 默认行为。
    """

    # 先拆开 stdout/stderr 文本元组，后续字段回填时无需重复写下标访问。
    str_stdout_text, str_stderr_text = tuple_output_texts  # 当前用例捕获到的标准输出与标准错误文本

    # 先复制原始用例计划字段，保证上游仍可从结果里回溯 manifest 和工作目录。
    dict_case_result = dict(dict_case)  # 当前用例结果字典的基础字段副本

    # 回填本次真正执行的命令，便于区分 dry-run 和真实执行路径。
    dict_case_result["cmd"] = list_effective_command  # 当前用例实际执行过的命令列表

    # 记录用例状态，供总回归结果聚合和 JUnit 失败判断使用。
    dict_case_result["status"] = str_case_status  # 当前用例的归一化状态文本

    # 保留退出码，供上游区分执行失败和超时等场景。
    dict_case_result["returncode"] = obj_returncode  # 当前用例的稳定退出码或 None

    # 记录耗时秒数，便于后续比较不同 manifest 的执行时长。
    dict_case_result["elapsed_sec"] = float_elapsed_seconds  # 当前用例实际耗时秒数

    # 保留标准输出，供摘要解析内层 JSON 或后续人工复盘。
    dict_case_result["stdout"] = str_stdout_text  # 当前用例捕获到的标准输出全文

    # 保留标准错误，供失败诊断和 JUnit failure 正文生成。
    dict_case_result["stderr"] = str_stderr_text  # 当前用例捕获到的标准错误全文

    # 立即补齐摘要，避免调用方重复解析 stdout 中的结构化 JSON。
    dict_case_result["summary"] = summarize_case_result(dict_case_result)  # 当前用例派生出的轻量摘要字典

    # 返回构建完成的结构化结果，供 run_case 正常或超时分支复用。
    return dict_case_result

# 执行单条回归用例，并把 stdout/stderr、超时信息和轻量摘要一起收集回来。
def run_case(
    dict_case: dict[str, object],
    *,
    timeout: int,
    dry_run: bool = False,
) -> dict[str, object]:
    """
    执行单条回归用例并返回结构化结果。

    参数：
    - dict_case: 单条回归用例的计划字典。
    - timeout: 当前用例允许的最大执行时长，单位为秒。
    - dry_run: 是否改用 dry-run 命令而不真正执行 EDA 流程。

    返回：
    - 返回包含命令、返回码、耗时、输出文本与摘要的结果字典。

    异常：
    - ``subprocess.TimeoutExpired`` 会在函数内部收敛为 ``timeout`` 结果，不向外继续抛出。
    """

    # 把 case 中的命令字段规整成字符串列表，供 subprocess.run 直接消费。
    list_case_command = list(dict_case["cmd"])  # 当前用例原始命令参数列表

    # dry-run 场景改写命令，其余场景保持计划里声明的真实执行命令不变。
    list_effective_command = _dry_run_cmd(list_case_command) if dry_run else list_case_command  # 当前用例真正要执行的命令列表

    # 用单调时钟记录开始时间，后续才能稳定计算本条用例的真实耗时。
    float_started_at = time.monotonic()  # 当前用例开始执行时的单调时钟值

    # 尝试在超时限制内执行当前用例，并捕获 stdout/stderr 供摘要和复盘复用。
    try:

        # 执行当前回归命令，统一使用文本模式和显式 stdout/stderr 捕获。
        completed_process_case: subprocess.CompletedProcess[str] = subprocess.run(  # 当前用例执行完成后的子进程结果对象
            list_effective_command,  # 当前用例真正执行的命令参数列表
            text=True,  # 按文本模式捕获 stdout/stderr
            stdout=subprocess.PIPE,  # 捕获标准输出供摘要与调试复盘
            stderr=subprocess.PIPE,  # 捕获标准错误供失败定位
            timeout=timeout,  # 为当前用例施加超时上限
        )

        # 根据退出码归一化执行状态，便于最终回归结果只关心 passed/failed 两类。
        str_case_status = "passed" if completed_process_case.returncode == 0 else "failed"  # 当前用例归一化后的执行状态

        # 先把当前用例耗时规整成稳定小数位，避免结构化结果里出现抖动过大的浮点文本。
        float_elapsed_seconds = round(time.monotonic() - float_started_at, 3)  # 当前正常执行用例的耗时秒数

        # 先收拢正常执行分支的输出文本，避免构建结果时重复展开 stdout/stderr 字段。
        tuple_output_texts = (completed_process_case.stdout, completed_process_case.stderr)  # 当前正常执行分支捕获到的输出文本元组

        # 基于正常执行结果构建完整结构化字典，供上游继续汇总和输出。
        dict_case_result = _build_case_result_payload(  # 正常执行分支最终生成的结构化结果字典
            dict_case,  # 继续带上原始 manifest/workdir 上下文，便于定位哪条回归卡在超时点
            list_effective_command=list_effective_command,  # 当前用例实际执行的命令列表
            str_case_status=str_case_status, obj_returncode=completed_process_case.returncode,  # 正常执行分支确认下来的状态与退出码
            float_elapsed_seconds=float_elapsed_seconds,  # 当前正常执行分支的耗时秒数
            tuple_output_texts=tuple_output_texts,  # 把 stdout/stderr 成对交给 helper 统一回填
        )

        # 返回正常执行完成后的完整结果字典。
        return dict_case_result

    # 超时场景转换成结构化 timeout 结果，避免线程池外层因为异常中断整个回归。
    except subprocess.TimeoutExpired as exception_timeout:

        # 先记录截至超时抛出时的耗时，便于上游比较卡在哪个时间点。
        float_elapsed_seconds = round(time.monotonic() - float_started_at, 3)  # 当前超时用例的耗时秒数

        # 先收拢超时分支已经产生的输出文本，保持与正常分支一致的构建输入形态。
        tuple_output_texts = (exception_timeout.stdout or "", exception_timeout.stderr or f"timeout after {timeout}s")  # 当前超时分支可回收的输出文本元组

        # 基于超时异常携带的信息构建结构化结果，保持与正常分支的字段契约一致。
        dict_case_result = _build_case_result_payload(  # 超时分支最终生成的结构化结果字典
            dict_case,  # 当前回归用例原始计划字典
            list_effective_command=list_effective_command,  # 触发超时的有效执行命令列表
            str_case_status="timeout", obj_returncode=None,  # 超时分支固定落成 timeout 与空退出码
            float_elapsed_seconds=float_elapsed_seconds,  # 当前超时分支的耗时秒数
            tuple_output_texts=tuple_output_texts,  # 把超时前保留下来的输出对交给 helper 生成 timeout 结果
        )

        # 返回超时用例的结构化结果，交给回归总结果统一汇总。
        return dict_case_result

# 把多条回归结果转换成最小 JUnit XML，供 CI 或外部汇总系统读取。
def junit_xml(list_results: list[dict[str, object]]) -> str:
    """
    把回归结果列表渲染成 JUnit XML 文本。

    参数：
    - list_results: 全部回归用例的结构化结果列表。

    返回：
    - 返回单个 ``testsuite`` XML 字符串。

    异常：
    - 无显式异常；XML 节点构造和序列化沿用标准库默认行为。
    """

    # 先收集失败与超时用例，供 testsuite 失败计数与 failure 节点生成复用。
    list_failed_results = [dict_result for dict_result in list_results if dict_result["status"] != "passed"]  # 当前回归中全部非 passed 用例列表

    # 构建 testsuite 根节点，把总测试数与失败数直接写进 XML 属性。
    element_xml_suite: xml_element_tree.Element = xml_element_tree.Element(  # 整份 JUnit 文档的 testsuite 根节点
        "testsuite",  # 固定 JUnit testsuite 根节点名称
        tests=str(len(list_results)),  # 本次回归纳入 JUnit 的总用例数
        failures=str(len(list_failed_results)),  # 本次回归在 JUnit 中的失败用例数
    )

    # 逐条回归结果生成 testcase 节点，保持 XML 与执行结果一一对应。
    for dict_result in list_results:

        # 每条用例至少都会生成一个 testcase，供 CI 侧显示单用例名称。
        xml_case = xml_element_tree.SubElement(element_xml_suite, "testcase", name=str(dict_result["name"]))  # 当前结果对应的 testcase 节点

        # 只有执行失败或超时的用例才追加 failure 节点，避免通过结果产生噪声。
        if dict_result["status"] != "passed":

            # failure 的 message 优先展示显式 reason，没有时退回归一化 status。
            element_xml_failure: xml_element_tree.Element = xml_element_tree.SubElement(  # 当前失败用例的 failure 子节点
                xml_case,  # 当前 testcase 节点
                "failure",  # 固定 JUnit 失败节点名称
                message=str(dict_result.get("reason") or dict_result["status"]),  # 优先写显式失败原因，其次退回状态名
            )

            # 先合成 failure 正文文本，再单独写入 XML 节点，避免多行属性赋值触发额外门禁。
            str_failure_text = str(  # 当前 failure 节点展示给 CI 的正文说明文本
                dict_result.get("stderr")  # 优先展示标准错误中的失败上下文
                or dict_result.get("stdout")  # 没有 stderr 时退回标准输出内容
                or dict_result.get("reason")  # 再退回显式 reason 字段
                or dict_result["status"]  # 最后退回归一化状态名
            )

            # 把合成后的失败说明正文写入当前 failure 节点。
            element_xml_failure.text = str_failure_text  # 当前 failure 节点的最终正文文本

    # 返回完整 JUnit XML 文本，供 CLI 写入文件或上游测试直接断言。
    return xml_element_tree.tostring(element_xml_suite, encoding="unicode")

# 按计划并发执行全部回归用例，并给出总状态与按名称排序后的结果列表。
def run_regression(dict_plan: dict[str, object], *, dry_run: bool = False) -> dict[str, object]:
    """
    执行整份回归计划并返回总结果。

    参数：
    - dict_plan: 由 ``build_regression_plan`` 生成的回归计划字典。
    - dry_run: 是否把全部用例改成 dry-run 执行模式。

    返回：
    - 返回在计划字典基础上补充 ``results`` 与 ``status`` 的总结果字典。

    异常：
    - 无显式异常；线程池内部用例异常由 ``run_case`` 自行收敛。
    """

    # 用列表累积全部已完成用例结果，供最终排序、状态聚合和 JUnit 渲染复用。
    list_results: list[dict[str, object]] = []  # 当前回归计划全部用例的执行结果列表

    # 读取计划声明的并发度，并保证线程池至少有一个 worker 可运行。
    int_max_workers = max(1, int(dict_plan["jobs"]))  # 本次回归线程池实际并发度

    # 读取计划声明的单用例超时时间，供线程池里每条任务统一执行。
    int_case_timeout = int(dict_plan["timeout"])  # 当前回归计划的单用例超时秒数

    # 使用线程池并发调度全部用例，避免多个独立 manifest 串行拖慢总回归时间。
    with concurrent.futures.ThreadPoolExecutor(max_workers=int_max_workers) as executor:

        # 先准备 future 列表容器，后续逐条提交用例时统一回填。
        list_futures: list[concurrent.futures.Future[dict[str, object]]] = []  # 当前回归计划全部已提交的 future 列表

        # 把计划中的每条用例提交到线程池，保持任务分发和结果收集解耦。
        for dict_case in list(dict_plan["cases"]):

            # 为当前用例创建一个异步 future，后续按完成顺序统一取回结果。
            list_futures.append(  # 当前用例对应的异步执行 future
                executor.submit(run_case, dict_case, timeout=int_case_timeout, dry_run=dry_run)
            )

        # 按 future 完成顺序收集结果，保证慢用例不会阻塞快用例先回填。
        for future_case_result in concurrent.futures.as_completed(list_futures):

            # 取回已完成 future 的结构化结果，并放入总结果列表等待后续排序。
            list_results.append(future_case_result.result())

    # 按用例名重新排序结果，确保最终输出对测试与人工阅读都保持稳定顺序。
    list_results.sort(key=lambda dict_result: str(dict_result["name"]))

    # 先判断是否所有用例都通过，避免状态归一化语句过长且不利于后续维护。
    bool_all_cases_passed = all(dict_result["status"] == "passed" for dict_result in list_results)  # 当前回归是否所有用例都已通过

    # 根据全量结果是否全部通过，归一化出最终回归状态。
    str_regression_status = "passed" if bool_all_cases_passed else "failed"  # 当前整份回归计划的聚合状态

    # 返回附带结果列表和总状态的回归总结果字典，供 CLI 和测试继续消费。
    return {
        **dict_plan,
        "results": list_results,
        "status": str_regression_status,
    }

# 解析命令行参数、执行回归并按协议输出状态摘要或 JSON 结果。
def main(argv: list[str] | None = None) -> int:
    """
    运行回归命令行入口并返回进程退出码。

    参数：
    - argv: 可选的命令行参数列表；传入 ``None`` 时使用进程默认参数。

    返回：
    - 当总回归状态为 ``passed`` 时返回 ``0``，否则返回 ``1``。

    异常：
    - 参数解析失败时由 ``argparse`` 抛出并终止进程；文件写入异常沿用底层行为。
    """

    # 创建参数解析器，集中声明 manifest 输入、并发度和输出协议开关。
    parser = argparse.ArgumentParser(description="Run a manifest-driven VCS/Verdi non-GUI regression.")  # 当前 CLI 的参数解析器

    # 要求调用方至少传入一个 manifest，回归计划才能有实际用例可执行。
    parser.add_argument("manifests", nargs="+", type=Path)

    # 允许调用方覆盖默认回归工作目录，便于把不同批次结果写到不同位置。
    parser.add_argument("--workdir", type=Path, default=Path("build/vcs-verdi-regression"))

    # 允许调用方声明回归并发度，以适配本地或远端机器资源上限。
    parser.add_argument("--jobs", type=int, default=1)

    # 允许调用方调整单用例超时时间，兼容慢用例或 smoke 用例的不同节奏。
    parser.add_argument("--timeout", type=int, default=300)

    # dry-run 模式只验证命令拼装和脚本协作，不真正执行 EDA 流程。
    parser.add_argument("--dry-run", action="store_true")

    # JUnit 输出路径可选，供 CI 或外部报告系统读取。
    parser.add_argument("--junit", type=Path)

    # JSON 输出开关启用后，标准输出将切换到单对象机器可读协议。
    parser.add_argument("--json", action="store_true")

    # 解析当前 CLI 调用的参数，得到本次回归需要的输入与输出配置。
    args = parser.parse_args(argv)  # 当前 CLI 解析得到的参数对象

    # 根据 manifest、工作目录和超时配置构建本次回归计划。
    dict_plan = build_regression_plan(  # 本次 CLI 构建出的回归计划字典
        args.manifests,  # 当前 CLI 收到的 manifest 路径列表
        workdir=args.workdir,  # 当前 CLI 指定的回归根目录
        jobs=args.jobs,  # 当前 CLI 指定的并发度
        timeout=args.timeout,  # 当前 CLI 指定的单用例超时秒数
    )

    # 执行回归计划，得到总状态与全部用例结果。
    dict_regression_result = run_regression(dict_plan, dry_run=args.dry_run)  # 本次 CLI 运行得到的回归总结果

    # 调用方显式要求 JUnit 文件时，先确保父目录存在再写出 XML 报告。
    if args.junit:

        # JUnit 输出目录可能尚未创建，先补齐目录树再写入文件。
        args.junit.parent.mkdir(parents=True, exist_ok=True)

        # 把全部结果渲染成 JUnit XML 并写入指定文件路径。
        args.junit.write_text(junit_xml(dict_regression_result["results"]), encoding="utf-8")

    # JSON 模式下按模块文档约定输出单个结构化对象，供自动化直接消费。
    if args.json:

        # 把完整回归结果写到标准输出，避免混入其他人类可读状态文本。
        json.dump(dict_regression_result, sys.stdout, indent=2, sort_keys=True)

        # 为 JSON 对象补一个换行，避免 shell 提示符直接接在末尾。
        sys.stdout.write("\n")

    # 默认模式只输出短摘要，不把完整结果字典直接刷到终端。
    elif dict_regression_result["status"] == "passed":

        # 通过时输出带前缀的简短状态摘要，便于终端和 CI 日志快速判断结果。
        print("> INFO: [Python] regression passed")

    # 非 JSON 且总状态失败时，输出带前缀的错误摘要。
    else:

        # 失败时仅报告回归总状态，详细结构化结果应改用 --json 或 junit 文件查看。
        print("> ERR: [Python] regression failed")

    # 只有整份回归计划通过时才返回零退出码，供 shell 与 CI 直接判定成败。
    return 0 if dict_regression_result["status"] == "passed" else 1

# 只有脚本被直接执行时才启动 CLI，避免导入测试模块时意外触发回归。
if __name__ == "__main__":

    # 把 main 返回值转换成进程退出码，交给 shell 或 CI 统一消费。
    raise SystemExit(main())
