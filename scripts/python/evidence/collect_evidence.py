#!/usr/bin/env python3
"""聚合 VCS/Verdi 非 GUI 交付证据。

本模块负责读取 smoke、环境检查、coverage、URG 诊断与报告文本，并把这些输入整理成稳定的 evidence JSON 结构。
stdout_protocol: json
当 CLI 使用 `--json` 或未提供 `--output` 时，stdout 输出单一 JSON 文本，供上层脚本直接消费。
"""

# 延后注解求值，避免脚本级类型提示在导入阶段产生额外前向依赖。
from __future__ import annotations

# 标准库中的命令行、序列化、环境变量和时间能力。
import argparse
import json
import os
import sys
from datetime import UTC, datetime

# 路径对象与通用类型注解用于约束证据收集接口。
from pathlib import Path
from typing import Any

# 证据快照需要回收的关键环境变量名单。
TUPLE_ENV_KEYS = (  # 固定输出这些宿主环境键，便于跨会话比较运行上下文
    "VCS_HOME",  # 用来标识 VCS 安装根目录
    "VERDI_HOME",  # 用来判断 Verdi 是否与 VCS 共享同一安装来源
    "NOVAS_HOME",  # 用来兼容部分机器上的 Novas 目录暴露方式
    "SNPSLMD_LICENSE_FILE",  # 优先记录 Synopsys 新版许可证变量
    "LM_LICENSE_FILE",  # 回退兼容旧版许可证环境变量
    "DISPLAY",  # 保留图形显示上下文，辅助定位远端显示问题
    "SHELL",  # 记录实际 shell 类型，辅助解释 wrapper 兼容性
    "LD_LIBRARY_PATH",  # 保留动态库搜索路径，辅助定位加载失败
)

# 交付口径声明需要覆盖的矩阵键。
TUPLE_DELIVERY_MATRIX_KEYS = (  # delivery_matrix 只保留面向交付承诺的主线检查项
    "minimal_smoke",  # 最小主线 smoke 决定脚本化流程是否跑通
    "mixed_vhdl_sv",  # 混合语言 smoke 代表 VHDL/SV 组合能力
    "fsdb_conversion",  # FSDB 转换链路代表波形交付可读性
)

# 真值口径声明需要覆盖的矩阵键。
TUPLE_TRUTH_MATRIX_KEYS = (  # truth_matrix 额外纳入完整能力声明必须覆盖的检查项
    "minimal_smoke",  # 真值矩阵沿用基础 smoke 作为主线入口
    "mixed_vhdl_sv",  # 真值矩阵保留混合语言结果用于完整性判断
    "coverage_urg",  # 真值矩阵额外要求 URG coverage 能力成立
    "fsdb_conversion",  # 真值矩阵保留 FSDB 转换结果作为读回证据
)

# host fingerprint 要检查的工具名称名单。
TUPLE_TOOL_NAMES = (  # 只回收证据摘要需要展示定位路径的核心可执行文件
    "vlogan",  # Verilog 前端编译器决定 SV 编译入口
    "vhdlan",  # VHDL 前端编译器决定混合语言入口
    "vcs",  # VCS 主模拟器用于定位运行时来源
    "verdi",  # Verdi 可执行文件用于确认 GUI/CLI 安装来源
    "fsdbreport",  # FSDB 工具用于确认波形读回链路来源
    "python3",  # Python 解释器路径用于复现脚本运行环境
    "bash",  # Bash 路径用于解释 vendor wrapper 的进入方式
    "sh",  # POSIX sh 路径用于解释 wrapper 兼容性失败
)

# collect_evidence 接受的可选关键字集合。
SET_OPTIONAL_COLLECT_KEYS = {  # 统一限制 collect_evidence 可接受的外部覆盖字段
    "report_path",  # 文本报告路径来自上游 smoke 汇总器
    "mixed_smoke_path",  # mixed smoke 路径来自混合语言补充流程
    "coverage_path",  # coverage JSON 路径来自 coverage 汇总脚本
    "conversion_path",  # conversion JSON 路径来自 FSDB 转换流程
    "ucapi_scan_path",  # UCAPI scan 路径来自兼容性扫描步骤
    "ucapi_manifest_path",  # UCAPI manifest 路径来自覆盖层清单生成步骤
    "urg_probe_path",  # URG probe 路径来自 urg 运行时探测步骤
    "urg_matrix_path",  # URG matrix 路径来自 coverage 真值矩阵汇总步骤
    "urg_troubleshoot_path",  # troubleshoot 路径来自 urg 故障深挖步骤
    "job_exit_code",  # 作业退出码用于保留外层驱动脚本状态
    "env",  # 环境覆盖字典用于复现离线证据采集场景
    "timestamp_utc",  # 固定时间戳用于测试或回放稳定输出
}

# 读取单个可选 JSON 文件，统一把缺失或损坏输入折算成结构化默认值。
def _load_json(path_target: Path | None) -> dict[str, Any]:
    """
    读取一个可选 JSON 文件。

    参数：
    - path_target: 待读取的 JSON 路径；可为 `None`。

    返回：
    - 返回解析后的 JSON 字典；路径缺失或文件不存在时返回空字典；解析失败时返回失败描述字典。

    异常：
    - 无显式异常；JSON 解析错误会被折算成结构化失败结果。
    """

    # 未提供路径时直接回退空字典，表示这一类证据没有执行。
    if not path_target:

        # 缺省路径不应伪造失败，只标记为无输入。
        return {}

    # 路径指向的文件不存在时，同样把结果视为“无输入”。
    if not path_target.exists():

        # 缺失文件交给上层通过矩阵状态去解释。
        return {}

    # 文件存在时优先尝试按 UTF-8 文本解析 JSON 载荷。
    try:

        # 先读取完整 JSON 文本，再交给标准库解析器生成 Python 结构。
        str_json_text = path_target.read_text(encoding="utf-8")  # 当前 JSON 文件读取到的原始文本

        # 把原始 JSON 文本转换成结构化 Python 字典或列表。
        obj_payload = json.loads(str_json_text)  # 当前 JSON 文件解析得到的 Python 对象

        # 只有字典结构符合本模块的消费契约，其余 JSON 类型统一包成失败信息。
        if isinstance(obj_payload, dict):

            # 命中字典载荷时直接返回原始结构，保持调用方可读性。
            return obj_payload

        # 非字典 JSON 无法直接进入本模块的键值聚合逻辑，因此返回显式失败说明。
        return {
            "status": "failed",
            "parse_error": "json_root_not_object",
            "path": str(path_target),
        }

    # JSON 文本损坏时返回结构化失败信息，便于最终证据报告保留问题来源。
    except Exception as exc:

        # 解析失败时把异常文本和路径都带回 evidence，方便定位破损输入。
        return {
            "status": "failed",
            "parse_error": str(exc),
            "path": str(path_target),
        }

# 读取可选文本报告，供 evidence 中保留人工可读的原始回显。
def _read_report_text(path_report: Path | None) -> str:
    """
    读取可选报告文本。

    参数：
    - path_report: 可能存在的文本报告路径。

    返回：
    - 返回完整报告文本；未提供路径或文件缺失时返回空字符串。

    异常：
    - 无显式异常；文件缺失会回退空字符串，其他读取错误沿用底层异常。
    """

    # 没有报告路径时直接回退空文本，表示本轮没有额外的人类可读报告。
    if not path_report:

        # 缺省报告路径不会阻断 evidence 聚合主流程。
        return ""

    # 文件存在时再尝试读取文本，避免对缺失路径抛出异常。
    try:

        # 使用 replace 策略读取报告文本，确保损坏字符不会打断整体收集流程。
        return path_report.read_text(encoding="utf-8", errors="replace")

    # 报告文件缺失时直接回退空字符串，让 evidence 保持稳定数据形状。
    except FileNotFoundError:

        # 缺失报告文本只影响可读摘要，不影响其他结构化证据。
        return ""

# 收集环境变量快照，并把调用方的覆盖值并入最终 evidence。
def _collect_environment(dict_env_override: dict[str, str] | None) -> dict[str, str]:
    """
    组装环境变量快照。

    参数：
    - dict_env_override: 调用方额外覆盖的环境变量字典；可为 `None`。

    返回：
    - 返回写入 evidence 的环境变量字典。

    异常：
    - 无显式异常；缺省覆盖值会被视为空字典。
    """

    # 先按固定白名单从当前进程环境中提取基础快照。
    dict_environment = {key: os.environ.get(key, "") for key in TUPLE_ENV_KEYS}  # 当前 evidence 要保留的基础环境快照

    # 调用方提供覆盖字典时，再把它合并到基础环境快照里。
    if dict_env_override:

        # 覆盖值通常来自远端执行上下文，需要优先生效。
        dict_environment.update(dict_env_override)

    # 返回最终环境快照，供 evidence 与 host fingerprint 共同复用。
    return dict_environment

# 从 smoke JSON 中提取 waves.fsdb 工件描述。
def _artifact_from_smoke(dict_smoke: dict[str, Any], path_run_dir: Path) -> dict[str, Any]:
    """
    从 smoke 结果中提取波形工件状态。

    参数：
    - dict_smoke: smoke 结果字典。
    - path_run_dir: 本轮运行目录。

    返回：
    - 返回 `waves.fsdb` 的路径、状态与字节数描述字典。

    异常：
    - 无显式异常；缺失字段会回退到稳定默认值。
    """

    # 优先读取新格式 artifact_status，再回退兼容旧 diagnostics.artifacts 结构。
    dict_dump_status = (
        dict_smoke.get("artifact_status", {}).get("dump")  # 优先读取 smoke 新格式里的 dump 状态
        or dict_smoke.get("diagnostics", {}).get("artifacts", {}).get("dump", {})  # 再回退兼容旧 diagnostics.artifacts 结构
    )  # smoke 中记录的 dump 工件状态字典

    # 返回 waves.fsdb 的稳定描述，缺失字段统一补默认值。
    return {
        "path": dict_dump_status.get("path", str(path_run_dir / "waves.fsdb")),
        "state": dict_dump_status.get("state", "missing"),
        "bytes": dict_dump_status.get("bytes", 0),
    }

# 把单个流程结果统一折算成矩阵状态字典。
def _matrix_status(dict_result: dict[str, Any], *, str_default_status: str = "not_executed") -> dict[str, Any]:
    """
    统一构造矩阵状态条目。

    参数：
    - dict_result: 某一步流程的原始结果字典。
    - str_default_status: 输入缺失时采用的默认状态文本。

    返回：
    - 返回包含 `status`、`returncode` 与 `reason` 的矩阵状态字典。

    异常：
    - 无显式异常；缺失字段会回退到稳定默认值。
    """

    # 空输入说明该步骤未执行，因此直接写入默认状态。
    if not dict_result:

        # 未执行状态不需要额外携带返回码或原因。
        return {"status": str_default_status}

    # 先抽取原始状态文本，供后续 dry-run 兼容逻辑修正。
    str_status = dict_result.get("status", str_default_status)  # 当前流程原始状态文本

    # dry-run 对交付矩阵应视为 not_executed，而不是失败或通过。
    if str_status == "dry-run":

        # 将 dry-run 显式折算成未执行状态，避免误报通过。
        str_status = "not_executed"  # dry-run 在矩阵里统一折算成未执行

    # 返回统一矩阵结构，供 delivery_matrix 与 truth_matrix 共同复用。
    return {
        "status": str_status,
        "returncode": dict_result.get("returncode"),
        "reason": dict_result.get("reason", ""),
    }

# coverage_urg 需要优先采用 urg_matrix 的新口径状态。
def _coverage_matrix_status(dict_coverage: dict[str, Any], dict_urg_matrix: dict[str, Any]) -> dict[str, Any]:
    """
    生成 coverage_urg 的矩阵状态。

    参数：
    - dict_coverage: 旧版 coverage 汇总结果字典。
    - dict_urg_matrix: 新版 URG 覆盖率矩阵结果字典。

    返回：
    - 返回 coverage_urg 应写入矩阵的最终状态字典。

    异常：
    - 无显式异常；缺失输入会回退到默认矩阵状态。
    """

    # 新版 URG 矩阵存在时优先采用它，避免旧 coverage 原因覆盖更精确的新口径。
    if dict_urg_matrix:

        # 使用新版矩阵结果作为 coverage_urg 的主状态来源。
        return _matrix_status(dict_urg_matrix)

    # 没有新版 URG 矩阵时，再回退到旧 coverage 汇总状态。
    return _matrix_status(dict_coverage)

# 从完整矩阵里筛出指定键集合，构造 delivery 或 truth 口径的子矩阵。
def _filter_matrix(dict_matrix: dict[str, Any], tuple_keys: tuple[str, ...]) -> dict[str, Any]:
    """
    从总矩阵中筛选指定条目。

    参数：
    - dict_matrix: 完整流程矩阵字典。
    - tuple_keys: 需要保留的矩阵键名元组。

    返回：
    - 返回只包含指定键的子矩阵字典。

    异常：
    - 无显式异常；缺失键会回退成 `not_executed` 状态。
    """

    # 逐个复制指定矩阵条目，确保返回结果与原矩阵解耦。
    return {key: dict(dict_matrix.get(key, {"status": "not_executed"})) for key in tuple_keys}

# 根据环境检查、矩阵状态和 coverage 结果抽取已知阻断项列表。
def _known_blockers(
    dict_check_env: dict[str, Any],
    dict_matrix: dict[str, Any],
    dict_coverage: dict[str, Any],
    dict_urg_matrix: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    收集 evidence 中的已知阻断项。

    参数：
    - dict_check_env: 环境检查结果字典。
    - dict_matrix: 完整流程矩阵字典。
    - dict_coverage: coverage 汇总结果字典。
    - dict_urg_matrix: URG 覆盖率矩阵结果字典。

    返回：
    - 返回阻断项字典列表。

    异常：
    - 无显式异常；缺失字段会被当作空集合处理。
    """

    # 先准备空阻断项列表，后续按来源顺序追加结构化条目。
    list_blockers: list[dict[str, Any]] = []  # evidence 要输出的已知阻断项列表

    # 先吸收 check_env 总体阻断项，保留最原始的环境问题表述。
    for obj_blocker in dict_check_env.get("overall", {}).get("blockers", []):

        # 环境阻断项直接标记为 environment scope。
        list_blockers.append({"scope": "environment", "reason": str(obj_blocker)})

    # 再扫描交付矩阵中非 passed / 非 not_executed 的失败项。
    for str_name, dict_state in dict_matrix.items():

        # 只有真正失败或异常的矩阵状态才应写入 known_blockers。
        if dict_state.get("status") not in {"passed", "not_executed"}:

            # 将矩阵条目的失败状态、名称与原因写入阻断项列表。
            list_blockers.append(
                {
                    "scope": "matrix",
                    "name": str_name,
                    "status": dict_state.get("status", "missing"),
                    "reason": dict_state.get("reason", ""),
                }
            )

    # 旧 coverage 汇总只有在明确失败时才应该追加 coverage scope 阻断项。
    if dict_coverage.get("status") not in {"", "passed", "not_executed"}:

        # 旧 coverage 汇总仍然是许多流程的兼容输入，因此单独保留一条阻断项。
        list_blockers.append(
            {
                "scope": "coverage",
                "name": "coverage_summary",
                "status": dict_coverage.get("status", "missing"),
                "reason": dict_coverage.get("reason", ""),
            }
        )

    # 新版 URG 矩阵明确失败时，也单独保留一条 URG scope 阻断项。
    if dict_urg_matrix.get("status") not in {"", "passed", "not_executed"}:

        # URG 矩阵失败通常提供更精确的覆盖率失败分类，因此需要直接暴露。
        list_blockers.append(
            {
                "scope": "urg",
                "name": "urg_coverage_matrix",
                "status": dict_urg_matrix.get("status", "missing"),
                "reason": dict_urg_matrix.get("reason", ""),
            }
        )

    # 返回完整阻断项列表，供 evidence 主体直接挂载。
    return list_blockers

# 从环境快照和 check_env 结果提取宿主机指纹。
def _host_fingerprint(dict_environment: dict[str, str], dict_check_env: dict[str, Any]) -> dict[str, Any]:
    """
    构造宿主机指纹摘要。

    参数：
    - dict_environment: evidence 中保存的环境变量快照。
    - dict_check_env: 环境检查结果字典。

    返回：
    - 返回 shell、license 变量和工具路径等宿主机摘要字典。

    异常：
    - 无显式异常；缺失字段会回退空字符串或空字典。
    """

    # 工具检查结果通常已经包含解析后的路径，因此先取出整张工具表。
    dict_tools = dict_check_env.get("tools", {})  # check_env 中记录的工具结果字典

    # 再准备空路径字典，只保留本模块关心的少量关键工具。
    dict_tool_paths: dict[str, str] = {}  # host_fingerprint 里要暴露的工具路径字典

    # 按固定工具名单顺序采集路径，确保 evidence 输出顺序稳定可比。
    for str_tool_name in TUPLE_TOOL_NAMES:

        # 只有 check_env 真正记录过该工具时，才把路径写进 fingerprint。
        if str_tool_name in dict_tools:

            # 工具路径回退空字符串，避免缺失 path 字段破坏结构稳定性。
            dict_tool_paths[str_tool_name] = (
                dict_tools.get(str_tool_name, {}).get("path", "")  # 当前工具在宿主机上的解析路径
            )

    # 计算最终应暴露的许可证变量名称，优先使用新版 Synopsys 变量。
    str_license_var = "SNPSLMD_LICENSE_FILE" if dict_environment.get("SNPSLMD_LICENSE_FILE") else "LM_LICENSE_FILE"  # 当前宿主机有效许可证变量名称

    # 返回宿主机指纹摘要，供证据链快速识别工具和 shell 环境。
    return {
        "shell": dict_environment.get("SHELL", ""),
        "display": dict_environment.get("DISPLAY", ""),
        "vcs_home": dict_environment.get("VCS_HOME", ""),
        "verdi_home": dict_environment.get("VERDI_HOME", ""),
        "license_var": str_license_var,
        "tool_paths": dict_tool_paths,
        "sh_compat": dict_check_env.get("shell", {}).get("sh_compat", {}),
    }

# 综合 coverage、URG 矩阵与 troubleshoot 结果，生成稳定的失败签名摘要。
def _urg_failure_signature(
    dict_coverage: dict[str, Any],
    dict_urg_matrix: dict[str, Any],
    dict_ucapi_scan: dict[str, Any],
    dict_urg_probe: dict[str, Any],
    dict_troubleshoot: dict[str, Any],
) -> dict[str, Any]:
    """
    汇总 URG 失败签名与复现特征。

    参数：
    - dict_coverage: coverage 汇总结果字典。
    - dict_urg_matrix: URG 覆盖率矩阵结果字典。
    - dict_ucapi_scan: UCAPI 扫描结果字典。
    - dict_urg_probe: URG 运行时探针结果字典。
    - dict_troubleshoot: URG 故障排查循环结果字典。

    返回：
    - 返回 evidence 中 `urg_failure_signature` 对应的结构化摘要字典。

    异常：
    - 无显式异常；缺失输入会回退到稳定默认值。
    """

    # 只有 URG 矩阵是字典时，才尝试读取默认变体描述。
    dict_default_variant = dict_urg_matrix.get("default_variant", {}) if isinstance(dict_urg_matrix, dict) else {}  # URG 矩阵里记录的默认变体摘要

    # troubleshoot 结果不是字典时，直接把 attempts 视为空列表。
    list_attempts = dict_troubleshoot.get("attempts", []) if isinstance(dict_troubleshoot, dict) else []  # troubleshoot 中记录的尝试列表

    # shell 层失败签名集合专门描述 wrapper 或环境未进入二进制的场景。
    set_shell_failure_signatures = {  # 这些签名表示问题还停留在 shell 或 wrapper 入口层
        "vendor_wrapper_shell_incompatible",  # /bin/sh 与 vendor wrapper 不兼容
        "vendor_bash_wrapper_env_incomplete",  # bash wrapper 需要的环境变量未完整注入
    }

    # 内部崩溃签名集合描述已经进入 wrapper 或 urg1 二进制内部的场景。
    set_internal_crash_signatures = {  # 这些签名表示已经进入 wrapper 或 urg1 内部崩溃路径
        "overlay_wrapper_internal_crash",  # 覆盖层 wrapper 进入后仍发生内部崩溃
        "direct_urg1_internal_crash",  # 直接调用 urg1 二进制后发生内部崩溃
    }

    # 默认先准备空的 troubleshoot 摘要原因，只有字典输入时才尝试提取。
    str_troubleshoot_reason = ""  # troubleshoot 汇总结果里的原因文本

    # troubleshoot 字典存在时，再读取 summary.reason 作为高优先级失败来源。
    if isinstance(dict_troubleshoot, dict):

        # troubleshoot summary.reason 往往比旧 coverage reason 更具体。
        str_troubleshoot_reason = (
            dict_troubleshoot.get("summary", {}).get("reason", "")  # troubleshoot 汇总出的首要失败原因
        )

    # 先准备空的主复现尝试名，后续按优先级从 attempts 中选择。
    str_primary_attempt = ""  # 最能代表失败形态的主复现尝试名称

    # 优先选择 shell 层或内部崩溃集合里的尝试，便于对齐排查链路。
    for dict_attempt in list_attempts:

        # 命中关键失败签名时立即把该尝试记为主复现项。
        if dict_attempt.get("failure_signature") in (*set_internal_crash_signatures, *set_shell_failure_signatures):

            # 优先保留第一个关键失败尝试，保证复现入口稳定。
            str_primary_attempt = dict_attempt.get("name", "")  # 命中的关键失败尝试名称

            # 首个关键失败尝试已经足够说明问题入口，因此直接结束扫描。
            break

    # 如果没有命中关键签名但 attempts 非空，则回退到第一条尝试名称。
    if not str_primary_attempt and list_attempts:

        # 回退到首条尝试名称，至少保留一个可复现入口提示。
        str_primary_attempt = list_attempts[0].get("name", "")  # attempts 首条记录的尝试名称

    # shell 层失败标志用于说明问题是否在 wrapper 兼容性阶段就暴露。
    bool_shell_layer_failure = any(  # 是否存在停留在 shell 或 wrapper 入口层的失败尝试
        dict_attempt.get("failure_signature") in set_shell_failure_signatures  # 当前尝试命中 shell 或 wrapper 入口层失败签名
        for dict_attempt in list_attempts  # 逐条检查是否仍有进入二进制后的崩溃样本
    )

    # 内部崩溃标志用于说明修复 shell 兼容后是否仍然进入了二进制崩溃路径。
    bool_internal_crash_after_shell_fix = any(  # 是否存在已经进入 wrapper 或 urg1 内部的崩溃尝试
        dict_attempt.get("failure_signature") in set_internal_crash_signatures  # 当前尝试命中 wrapper 或 urg1 内部崩溃签名
        for dict_attempt in list_attempts  # 顺序扫描全部 troubleshoot attempts
    )

    # 默认先准备空的 gdb 根因签名，后续按 attempts 顺序抽取第一条命中值。
    str_gdb_root_cause = ""  # 从 troubleshoot attempts 里抽取到的 gdb 根因签名

    # gdb 根因通常比 troubleshoot 汇总原因更细，因此要优先尝试提取。
    for dict_attempt in list_attempts:

        # 命中 root_cause_signature 时直接采用它作为最高优先级分类线索。
        if dict_attempt.get("root_cause_signature"):

            # 记录第一条 gdb 根因签名，保持排序与原始排查顺序一致。
            str_gdb_root_cause = dict_attempt.get("root_cause_signature", "")  # 第一条命中的 gdb 根因签名

            # 第一条 gdb 根因已经足够说明问题，因此结束扫描。
            break

    # 最终分类优先级依次是 gdb 根因、troubleshoot 汇总、新矩阵原因和旧 coverage 原因。
    str_classification = (
        str_gdb_root_cause  # 首选 gdb 根因签名
        or str_troubleshoot_reason  # 次选 troubleshoot 汇总原因
        or dict_urg_matrix.get("reason")  # 再回退到 urg_matrix 的原因字段
        or dict_coverage.get("reason")  # 最后回退到旧 coverage 的原因字段
        or ""  # 所有来源都为空时回退空字符串
    )  # evidence 要记录的最终失败分类

    # vendor_or_host_blocked 只在既定三类宿主阻断口径下为真。
    bool_vendor_or_host_blocked = bool(  # 是否属于 vendor 或宿主环境阻断场景
        dict_urg_matrix.get("reason") == "urg_internal_stack_trace_ucapi_patch_not_applicable"  # vendor 栈追踪显示 UCAPI 补丁不适用
        or str_troubleshoot_reason == "vendor_or_host_blocked"  # troubleshoot 已归类为 vendor 或宿主阻断
        or str_gdb_root_cause == "ucapi_license_checkout_segv"  # gdb 已归类为 UCAPI 许可证签出段错误
    )

    # 返回 URG 失败签名摘要，供 evidence 主体和后续门禁直接消费。
    return {
        "classification": str_classification,
        "coverage_reason": dict_coverage.get("reason", ""),
        "default_variant": dict_default_variant.get("name", ""),
        "default_variant_status": dict_default_variant.get("status", ""),
        "default_variant_report_file_count": dict_default_variant.get("report_file_count", 0),
        "ucapi_scan_status": dict_ucapi_scan.get("status", ""),
        "urg_runtime_probe_status": dict_urg_probe.get("status", ""),
        "troubleshoot_summary": str_troubleshoot_reason,
        "system_gdb_root_cause": str_gdb_root_cause,
        "primary_repro_attempt": str_primary_attempt,
        "shell_layer_failure": bool_shell_layer_failure,
        "internal_crash_after_shell_fix": bool_internal_crash_after_shell_fix,
        "vendor_or_host_blocked": bool_vendor_or_host_blocked,
    }

# 根据 delivery_matrix 与 truth_matrix 生成可支持声明列表。
def _supported_claims(dict_delivery_matrix: dict[str, Any], dict_truth_matrix: dict[str, Any]) -> dict[str, Any]:
    """
    生成 supported_claims 结构。

    参数：
    - dict_delivery_matrix: 交付口径矩阵字典。
    - dict_truth_matrix: 真值口径矩阵字典。

    返回：
    - 返回包含 claim 列表的结构化字典。

    异常：
    - 无显式异常；缺失矩阵条目会被视为 blocked。
    """

    # delivery 口径要求 minimal、mixed 和 fsdb conversion 三条主线同时通过。
    bool_delivery_passed = all(  # 交付主线声明是否全部满足
        dict_delivery_matrix.get(str_name, {}).get("status") == "passed"  # 每个交付主线条目都必须通过
        for str_name in TUPLE_DELIVERY_MATRIX_KEYS  # 逐项检查交付主线需要的矩阵键
    )

    # truth 口径除了交付主线，还要求完整能力声明涉及的全部矩阵项通过。
    bool_truth_passed = all(  # 完整能力声明所需的真值矩阵是否全部满足
        dict_truth_matrix.get(str_name, {}).get("status") == "passed"  # 每个真值矩阵条目都必须通过
        for str_name in TUPLE_TRUTH_MATRIX_KEYS  # 逐项检查真值声明需要的矩阵键
    )

    # 返回支持声明列表，状态值只使用 supported 或 blocked 两种口径。
    return {
        "claims": [
            {
                "claim_id": "non_gui_scripted_mainline",
                "scope": "delivery_mainline",
                "status": "supported" if bool_delivery_passed else "blocked",
            },
            {
                "claim_id": "fsdb_generation",
                "scope": "delivery_mainline",
                "status": (
                    "supported"
                    if dict_delivery_matrix.get("minimal_smoke", {}).get("status") == "passed"
                    else "blocked"
                ),
            },
            {
                "claim_id": "fsdb_readback",
                "scope": "delivery_mainline",
                "status": (
                    "supported"
                    if dict_delivery_matrix.get("minimal_smoke", {}).get("status") == "passed"
                    else "blocked"
                ),
            },
            {
                "claim_id": "mixed_language_smoke",
                "scope": "delivery_mainline",
                "status": (
                    "supported"
                    if dict_delivery_matrix.get("mixed_vhdl_sv", {}).get("status") == "passed"
                    else "blocked"
                ),
            },
            {
                "claim_id": "fsdb_conversion",
                "scope": "delivery_mainline",
                "status": (
                    "supported"
                    if dict_delivery_matrix.get("fsdb_conversion", {}).get("status") == "passed"
                    else "blocked"
                ),
            },
            {
                "claim_id": "urg_coverage_reporting",
                "scope": "truth_full",
                "status": (
                    "supported"
                    if dict_truth_matrix.get("coverage_urg", {}).get("status") == "passed"
                    else "blocked"
                ),
            },
            {
                "claim_id": "complete_official_option_support",
                "scope": "truth_full",
                "status": "supported" if bool_truth_passed else "blocked",
            },
        ]
    }

# 生成 coverage_summary 子结构，统一旧 coverage 输入的字段缺省逻辑。
def _build_coverage_summary(dict_coverage: dict[str, Any]) -> dict[str, Any]:
    """
    构造 coverage_summary 子结构。

    参数：
    - dict_coverage: coverage 汇总结果字典。

    返回：
    - 返回写入 evidence 的 coverage_summary 字典。

    异常：
    - 无显式异常；缺失 coverage 输入会回退到稳定默认值。
    """

    # 空 coverage 输入应被视为 not_executed，而不是失败。
    if not dict_coverage:

        # 没有 coverage 输入时返回全默认值，保持 evidence 数据形状稳定。
        return {
            "status": "not_executed",
            "returncode": None,
            "reason": "",
            "diagnostics": {},
            "stdout_tail": "",
            "stderr_tail": "",
            "urg1_command_line": "",
            "coverage": {},
        }

    # 存在 coverage 输入时直接复制关心字段，保留兼容旧 JSON 的缺省回退。
    return {
        "status": dict_coverage.get("status", "not_executed"),
        "returncode": dict_coverage.get("returncode"),
        "reason": dict_coverage.get("reason", ""),
        "diagnostics": dict_coverage.get("diagnostics", {}),
        "stdout_tail": dict_coverage.get("stdout_tail", ""),
        "stderr_tail": dict_coverage.get("stderr_tail", ""),
        "urg1_command_line": dict_coverage.get("urg1_command_line", ""),
        "coverage": dict_coverage.get("coverage", {}),
    }

# 生成 artifacts 子结构，统一 smoke、smoke.json 与 report.txt 的工件记录。
def _build_artifact_records(
    dict_smoke: dict[str, Any],
    path_run_dir: Path,
    path_smoke: Path,
    path_report: Path | None,
) -> dict[str, Any]:
    """
    构造 artifacts 子结构。

    参数：
    - dict_smoke: smoke 结果字典。
    - path_run_dir: 本轮运行目录。
    - path_smoke: smoke JSON 文件路径。
    - path_report: 可选文本报告路径。

    返回：
    - 返回写入 evidence 的 artifacts 字典。

    异常：
    - 无显式异常；缺失文件会回退为 0 字节或空路径。
    """

    # 先计算 smoke.json 是否存在，供字节数统计复用。
    bool_smoke_exists = path_smoke.exists()  # smoke.json 文件是否存在

    # 再计算 report.txt 是否存在，专门用于报告路径的字节数与显示策略。
    bool_report_exists = bool(path_report and path_report.exists())  # report.txt 工件是否真实落盘

    # 返回统一的工件记录字典，供 evidence 主体直接挂载。
    return {
        "waves.fsdb": _artifact_from_smoke(dict_smoke, path_run_dir),
        "smoke.json": {
            "path": str(path_smoke),
            "bytes": path_smoke.stat().st_size if bool_smoke_exists else 0,
        },
        "report.txt": {
            "path": str(path_report or ""),
            "bytes": path_report.stat().st_size if bool_report_exists and path_report is not None else 0,
        },
    }

# 规范化 collect_evidence 的可选关键字，避免主函数维护过长形参列表。
def _normalize_collect_options(dict_optional_inputs: dict[str, Any]) -> dict[str, Any]:
    """
    归一化 collect_evidence 的可选关键字输入。

    参数：
    - dict_optional_inputs: collect_evidence 接收到的可选关键字字典。

    返回：
    - 返回补齐默认值后的可选参数字典。

    异常：
    - 当存在未支持的关键字时抛出 `TypeError`。
    """

    # 先找出所有未被允许的关键字，避免静默吞掉调用方拼写错误。
    set_unknown_keys = set(dict_optional_inputs) - SET_OPTIONAL_COLLECT_KEYS  # collect_evidence 收到的未知关键字集合

    # 一旦出现未知关键字，就立即抛出显式错误阻止错误参数继续传播。
    if set_unknown_keys:

        # 先把未知关键字按字母顺序拼成稳定字符串，便于测试和人工排查。
        str_unknown_keys = ", ".join(sorted(set_unknown_keys))  # 未知关键字集合格式化后的文本

        # 用固定错误前缀抛出类型错误，符合 current-project CLI 错误文本约束。
        raise TypeError(f"> ERR: [Python] collect_evidence 收到未支持的关键字参数: {str_unknown_keys}")

    # 返回补齐默认值后的可选参数字典，供主流程继续读取。
    return {
        "report_path": dict_optional_inputs.get("report_path"),
        "mixed_smoke_path": dict_optional_inputs.get("mixed_smoke_path"),
        "coverage_path": dict_optional_inputs.get("coverage_path"),
        "conversion_path": dict_optional_inputs.get("conversion_path"),
        "ucapi_scan_path": dict_optional_inputs.get("ucapi_scan_path"),
        "ucapi_manifest_path": dict_optional_inputs.get("ucapi_manifest_path"),
        "urg_probe_path": dict_optional_inputs.get("urg_probe_path"),
        "urg_matrix_path": dict_optional_inputs.get("urg_matrix_path"),
        "urg_troubleshoot_path": dict_optional_inputs.get("urg_troubleshoot_path"),
        "job_exit_code": dict_optional_inputs.get("job_exit_code", 0),
        "env": dict_optional_inputs.get("env"),
        "timestamp_utc": dict_optional_inputs.get("timestamp_utc"),
    }

# 按固定路径集合读取 evidence 所需的全部输入载荷。
def _load_collect_sources(
    smoke_path: Path,
    check_env_path: Path,
    dict_options: dict[str, Any],
) -> dict[str, Any]:
    """
    读取 collect_evidence 需要消费的全部输入载荷。

    参数：
    - smoke_path: minimal smoke 结果 JSON 路径。
    - check_env_path: 环境检查结果 JSON 路径。
    - dict_options: 归一化后的 collect_evidence 可选参数字典。

    返回：
    - 返回包含 smoke、coverage、URG 诊断和报告文本的输入字典。

    异常：
    - 无显式异常；各子输入缺失时会回退为空字典或空字符串。
    """

    # 返回完整输入载荷字典，减少主流程里重复的逐项读取赋值。
    return {
        "smoke": _load_json(smoke_path),
        "mixed": _load_json(dict_options["mixed_smoke_path"]),
        "coverage": _load_json(dict_options["coverage_path"]),
        "conversion": _load_json(dict_options["conversion_path"]),
        "ucapi_scan": _load_json(dict_options["ucapi_scan_path"]),
        "ucapi_manifest": _load_json(dict_options["ucapi_manifest_path"]),
        "urg_probe": _load_json(dict_options["urg_probe_path"]),
        "urg_matrix": _load_json(dict_options["urg_matrix_path"]),
        "urg_troubleshoot": _load_json(dict_options["urg_troubleshoot_path"]),
        "check_env": _load_json(check_env_path),
        "report_text": _read_report_text(dict_options["report_path"]),
    }

# 构造 collect_evidence 需要的矩阵集合，并保留 mixed 兼容回退逻辑。
def _build_matrix_bundle(dict_sources: dict[str, Any]) -> dict[str, Any]:
    """
    构造 collect_evidence 的矩阵集合。

    参数：
    - dict_sources: `_load_collect_sources` 返回的输入载荷字典。

    返回：
    - 返回包含完整矩阵、delivery_matrix 与 truth_matrix 的字典。

    异常：
    - 无显式异常；缺失输入会回退到稳定默认状态。
    """

    # mixed smoke 的基础状态先按普通矩阵条目构造，后续再做 VHDL 来源兼容修正。
    dict_mixed_status = _matrix_status(dict_sources["mixed"])  # mixed_vhdl_sv 的初始矩阵状态

    # mixed 未执行但 smoke 计划里存在 vhdl_sources 时，回退为沿用 smoke 状态的兼容逻辑。
    if dict_mixed_status["status"] == "not_executed" and dict_sources["smoke"].get("plan", {}).get("vhdl_sources"):

        # 旧流程常把 mixed 结果折叠进 smoke，因此这里保留兼容回退。
        dict_mixed_status = {
            "status": dict_sources["smoke"].get("status", "unknown"),  # 兼容旧流程时沿用 smoke 状态
            "returncode": None,  # 兼容回退路径没有独立 mixed 返回码
            "reason": "",  # 兼容回退路径没有独立 mixed 失败原因
        }

    # 先构造完整矩阵，统一承接 minimal、mixed、coverage 和 conversion 四条主线。
    dict_matrix = {
        "minimal_smoke": _matrix_status(dict_sources["smoke"]),  # minimal smoke 对应基础交付主线
        "mixed_vhdl_sv": dict_mixed_status,  # mixed 结果要兼容独立流程和旧版折叠流程
        "coverage_urg": _coverage_matrix_status(dict_sources["coverage"], dict_sources["urg_matrix"]),  # coverage 结果要优先服从 urg_matrix 新口径
        "fsdb_conversion": _matrix_status(dict_sources["conversion"]),  # FSDB 转换结果决定波形转换链路是否成立
    }

    # 返回完整矩阵以及两个口径子矩阵，减少主流程里的重复筛选逻辑。
    return {
        "matrix": dict_matrix,
        "delivery_matrix": _filter_matrix(dict_matrix, TUPLE_DELIVERY_MATRIX_KEYS),
        "truth_matrix": _filter_matrix(dict_matrix, TUPLE_TRUTH_MATRIX_KEYS),
    }

# 根据已加载的输入和矩阵结果构造最终 evidence 载荷。
def _build_evidence_payload(
    path_run_dir: Path,
    smoke_path: Path,
    dict_options: dict[str, Any],
    dict_environment: dict[str, str],
    dict_sources: dict[str, Any],
    dict_matrix_bundle: dict[str, Any],
) -> dict[str, Any]:
    """
    构造最终 evidence 载荷。

    参数：
    - path_run_dir: 绝对运行目录。
    - smoke_path: minimal smoke 结果 JSON 路径。
    - dict_options: 归一化后的 collect_evidence 可选参数字典。
    - dict_environment: evidence 要写入的环境快照字典。
    - dict_sources: `_load_collect_sources` 返回的输入载荷字典。
    - dict_matrix_bundle: `_build_matrix_bundle` 返回的矩阵集合字典。

    返回：
    - 返回最终可序列化的 evidence 字典。

    异常：
    - 无显式异常；缺失输入会通过下层 helper 回退到稳定默认值。
    """

    # 先从矩阵集合里提取完整矩阵，供后续多个 helper 复用。
    dict_matrix = dict_matrix_bundle["matrix"]  # evidence 要写入的完整矩阵字典

    # 再提取 delivery 口径矩阵，供 supported_claims 计算使用。
    dict_delivery_matrix = dict_matrix_bundle["delivery_matrix"]  # evidence 要写入的 delivery 口径矩阵

    # 再提取 truth 口径矩阵，这份子集专门支撑完整能力声明和 coverage 真值判断。
    dict_truth_matrix = dict_matrix_bundle["truth_matrix"]  # 这份子矩阵专门服务完整能力声明与 coverage 真值判断

    # 返回完整 evidence 载荷，集中承载时间戳、环境、矩阵与诊断摘要。
    return {
        "timestamp_utc": dict_options["timestamp_utc"] or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "fresh": True,
        "job_exit_code": dict_options["job_exit_code"],
        "environment": dict_environment,
        "check_env": dict_sources["check_env"],
        "steps": dict_sources["smoke"].get("results", []),
        "artifacts": _build_artifact_records(
            dict_sources["smoke"],
            path_run_dir,
            smoke_path,
            dict_options["report_path"],
        ),
        "matrix": dict_matrix,
        "delivery_matrix": dict_delivery_matrix,
        "truth_matrix": dict_truth_matrix,
        "ucapi_patch": {
            "scan": dict_sources["ucapi_scan"],
            "manifest": dict_sources["ucapi_manifest"],
        },
        "urg_runtime_probe": dict_sources["urg_probe"],
        "urg_coverage_matrix": dict_sources["urg_matrix"],
        "urg_troubleshoot": dict_sources["urg_troubleshoot"],
        "coverage_summary": _build_coverage_summary(dict_sources["coverage"]),
        "report_text": dict_sources["report_text"],
        "smoke_status": dict_sources["smoke"].get("status", "unknown"),
        "smoke_reason": dict_sources["smoke"].get("reason", ""),
        "known_blockers": _known_blockers(
            dict_sources["check_env"],
            dict_matrix,
            dict_sources["coverage"],
            dict_sources["urg_matrix"],
        ),
        "host_fingerprint": _host_fingerprint(dict_environment, dict_sources["check_env"]),
        "urg_failure_signature": _urg_failure_signature(
            dict_sources["coverage"],
            dict_sources["urg_matrix"],
            dict_sources["ucapi_scan"],
            dict_sources["urg_probe"],
            dict_sources["urg_troubleshoot"],
        ),
        "supported_claims": _supported_claims(dict_delivery_matrix, dict_truth_matrix),
    }

# 返回 evidence CLI 需要声明的参数规格，供 parser 配置循环复用。
def _argument_specs() -> tuple[dict[str, Any], ...]:
    """
    返回 evidence CLI 的参数规格集合。

    参数：
    - 无业务参数；函数只返回静态参数规格。

    返回：
    - 返回由参数规格字典组成的元组。

    异常：
    - 无显式异常；参数规格是静态常量。
    """

    # 返回固定参数规格元组，避免 main 内部堆叠大量重复 add_argument 调用。
    return (
        {"flag": "--run-dir", "kwargs": {"type": Path, "required": True}},
        {"flag": "--smoke", "kwargs": {"type": Path, "required": True}},
        {"flag": "--check-env", "kwargs": {"type": Path, "required": True}},
        {"flag": "--report", "kwargs": {"type": Path}},
        {"flag": "--mixed-smoke", "kwargs": {"type": Path}},
        {"flag": "--coverage", "kwargs": {"type": Path}},
        {"flag": "--conversion", "kwargs": {"type": Path}},
        {"flag": "--ucapi-scan", "kwargs": {"type": Path}},
        {"flag": "--ucapi-manifest", "kwargs": {"type": Path}},
        {"flag": "--urg-probe", "kwargs": {"type": Path}},
        {"flag": "--urg-matrix", "kwargs": {"type": Path}},
        {"flag": "--urg-troubleshoot", "kwargs": {"type": Path}},
        {"flag": "--job-exit-code", "kwargs": {"type": int, "default": 0}},
        {"flag": "--output", "kwargs": {"type": Path}},
        {"flag": "--json", "kwargs": {"action": "store_true"}},
    )

# 把参数规格循环注册到 argparse 解析器，避免重复的 add_argument 模板代码。
def _register_cli_args(parser: argparse.ArgumentParser) -> None:
    """
    把 evidence CLI 的参数规格注册到解析器。

    参数：
    - parser: 需要写入参数定义的 argparse 解析器。

    返回：
    - 无业务返回值；函数只修改传入的解析器对象。

    异常：
    - 无显式异常；参数规格由静态 helper 保证完整。
    """

    # 逐条消费静态参数规格，把 CLI 所需选项写入解析器对象。
    for dict_argument_spec in _argument_specs():

        # 用规格字典驱动 add_argument，保证 main 保持轻量。
        parser.add_argument(dict_argument_spec["flag"], **dict_argument_spec["kwargs"])

# 从 argparse 结果中提取 collect_evidence 所需关键字。
def _build_cli_collect_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """
    从 CLI 参数命名空间构造 collect_evidence 关键字。

    参数：
    - args: `argparse` 解析得到的参数命名空间。

    返回：
    - 返回可以直接传给 `collect_evidence` 的关键字字典。

    异常：
    - 无显式异常；字段存在性由 argparse 解析器保证。
    """

    # 返回 collect_evidence 需要消费的关键字字典，避免 main 内部再维护大字面量。
    return {
        "run_dir": args.run_dir,
        "smoke_path": args.smoke,
        "check_env_path": args.check_env,
        "report_path": args.report,
        "mixed_smoke_path": args.mixed_smoke,
        "coverage_path": args.coverage,
        "conversion_path": args.conversion,
        "ucapi_scan_path": args.ucapi_scan,
        "ucapi_manifest_path": args.ucapi_manifest,
        "urg_probe_path": args.urg_probe,
        "urg_matrix_path": args.urg_matrix,
        "urg_troubleshoot_path": args.urg_troubleshoot,
        "job_exit_code": args.job_exit_code,
    }

# 汇总 smoke、coverage、URG 和环境检查输入，构造最终 evidence 字典。
def collect_evidence(
    *,
    run_dir: Path,
    smoke_path: Path,
    check_env_path: Path,
    **dict_optional_inputs: Any,
) -> dict[str, Any]:
    """
    汇总 VCS/Verdi 非 GUI 交付证据。

    参数：
    - run_dir: 本轮运行目录。
    - smoke_path: minimal smoke 结果 JSON 路径。
    - check_env_path: 环境检查结果 JSON 路径。
    - dict_optional_inputs: 允许的可选关键字包括
      `report_path`、`mixed_smoke_path`、`coverage_path`、`conversion_path`、
      `ucapi_scan_path`、`ucapi_manifest_path`、`urg_probe_path`、`urg_matrix_path`、
      `urg_troubleshoot_path`、`job_exit_code`、`env` 和 `timestamp_utc`。

    返回：
    - 返回可直接序列化成 JSON 的 evidence 字典。

    异常：
    - 当收到未支持的可选关键字时抛出 `TypeError`。
    """

    # 先把运行目录折算成绝对路径，避免最终 evidence 混入相对路径歧义。
    path_run_dir = run_dir.resolve()  # 当前 evidence 聚合使用的绝对运行目录

    # 再把可选关键字规范化成固定字典，统一后续读取路径和默认值。
    dict_options = _normalize_collect_options(dict_optional_inputs)  # collect_evidence 归一化后的可选参数字典

    # 统一读取全部 JSON 与文本输入，减少主流程里的重复赋值代码。
    dict_sources = _load_collect_sources(smoke_path, check_env_path, dict_options)  # collect_evidence 需要消费的输入载荷字典

    # 环境变量快照结合调用方覆盖值后，作为 host fingerprint 和 evidence 的共同来源。
    dict_environment = _collect_environment(dict_options["env"])  # evidence 要记录的环境变量快照

    # 矩阵集合统一由专门 helper 构造，集中处理 mixed 兼容回退逻辑。
    dict_matrix_bundle = _build_matrix_bundle(dict_sources)  # collect_evidence 要写入的矩阵集合字典

    # 返回最终 evidence 载荷，保持主流程只负责编排而不堆叠大字面量。
    return _build_evidence_payload(
        path_run_dir,
        smoke_path,
        dict_options,
        dict_environment,
        dict_sources,
        dict_matrix_bundle,
    )

# 提供 CLI 入口，把多份 JSON 与文本报告聚合成单一 evidence 输出。
def main() -> int:
    """
    运行 evidence 聚合 CLI。

    参数：
    - 无显式业务参数；函数直接消费命令行参数。

    返回：
    - 返回进程退出码；当前 CLI 正常完成时始终返回 `0`。

    异常：
    - 无显式异常；参数解析错误由 `argparse` 自行处理退出。
    """

    # 先创建参数解析器，统一声明 evidence 聚合 CLI 的全部输入选项。
    parser = argparse.ArgumentParser(description="Collect VCS/Verdi non-GUI execution evidence into one JSON object.")  # evidence 聚合 CLI 的参数解析器

    # 再把静态参数规格批量写入解析器，避免 main 堆叠重复模板代码。
    _register_cli_args(parser)

    # 解析命令行参数后，后续逻辑只消费这一份参数快照。
    args = parser.parse_args()  # 当前 CLI 调用解析得到的参数命名空间

    # 先整理传给 collect_evidence 的关键字参数，避免主调用点堆叠过长参数列表。
    dict_collect_kwargs = _build_cli_collect_kwargs(args)  # collect_evidence 需要消费的关键字参数字典

    # 再运行 evidence 聚合主流程，得到完整结构化结果。
    dict_evidence = collect_evidence(**dict_collect_kwargs)  # evidence 聚合主流程生成的结构化结果

    # output 存在时优先把完整 JSON 文本落盘，供上层后续流程直接读取。
    if args.output:

        # 写文件路径由调用方显式指定，因此这里直接落盘完整 evidence JSON。
        args.output.write_text(json.dumps(dict_evidence, indent=2, sort_keys=True), encoding="utf-8")

    # json 模式或未提供 output 时，stdout 都需要输出单一 JSON 载荷。
    if args.json or not args.output:

        # 按模块声明的 stdout_protocol 直接输出完整 JSON 文本。
        json.dump(dict_evidence, sys.stdout, indent=2, sort_keys=True)

        # JSON 协议输出补上换行，避免 shell 提示符直接拼接在末尾。
        sys.stdout.write("\n")

    # 当前 CLI 只负责聚合 evidence，流程正常结束时统一返回零退出码。
    return 0

# 直接脚本执行时，把 main 的返回码提升为进程退出状态。
if __name__ == "__main__":

    # CLI 返回码需要显式转换成进程退出码，供上层 shell 门禁直接判断。
    raise SystemExit(main())
