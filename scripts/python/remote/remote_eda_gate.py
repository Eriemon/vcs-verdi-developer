#!/usr/bin/env python3
"""规划并核验远端 VCS/Verdi 最小门禁所需的打包与证据规则。

本模块负责两类工作：
- 生成远端 smoke bundle 的本地打包计划与 zip 产物。
- 核验远端 smoke/coverage 运行后回传的结构化证据对象。

命令行标准输出协议：
- 默认模式只输出带前缀的人类可读摘要，不直接打印完整结构化对象。
- 当传入 ``--json`` 时，标准输出只写出单个 JSON 对象，供上游自动化消费。
"""

# 启用延后求值注解，避免类型提示在运行期额外干扰模块加载顺序。
from __future__ import annotations

# 引入参数解析、JSON 序列化与 zip 打包能力，支撑 CLI 与 bundle 产物生成。
import argparse
import json
import sys
import zipfile

# 补充 UTC 时间、路径对象与类型标注，供证据核验与结构化返回体复用。
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 统一描述本模块对外返回的结构化对象，避免重复书写宽泛字典类型。
JsonDict = dict[str, Any]  # 计划对象、证据对象与 CLI 返回体共用的映射类型

# 这份清单驱动远端 bundle 打包，覆盖环境检查、FSDB/URG 诊断和最小 HDL 回放所需的刚性资源。
MINIMAL_BUNDLE_FILES = (  # 缺失任一项都要阻断 bundle 子命令，因为远端 smoke 运行与证据回传都依赖这组固定文件
    "scripts/python/env/check_env.py",  # 远端前置环境探测脚本
    "scripts/python/validation/vcs_verdi_check.py",  # 远端 VCS/Verdi 核验脚本
    "scripts/python/diagnosis/fsdb_tools.py",  # 远端 FSDB 诊断辅助脚本
    "scripts/python/coverage/coverage_flow.py",  # 远端覆盖率主流程脚本
    "scripts/python/coverage/patch_ucapi_overlay.py",  # 远端 UCAPI 补丁辅助脚本
    "scripts/python/coverage/urg_runtime_probe.py",  # 远端 URG 运行时探针脚本
    "scripts/python/coverage/urg_coverage_matrix.py",  # 远端 URG 变体矩阵脚本
    "scripts/python/coverage/urg_troubleshoot.py",  # 远端 URG 故障诊断脚本
    "scripts/python/coverage/urg_troubleshoot_attempts.json",  # URG 故障诊断矩阵配置文件
    "scripts/python/evidence/collect_evidence.py",  # 远端证据采集脚本
    "scripts/shell/remote/make_shell_overlay.sh",  # 远端 shell 覆盖层生成脚本
    "scripts/shell/remote/run_remote_eda_smoke.sh",  # 远端 smoke 运行入口脚本
    "assets/minimal_vcs/top.sv",  # 最小 SystemVerilog 顶层夹具
    "assets/minimal_vcs/coverage_top.sv",  # 覆盖率场景使用的顶层夹具
    "assets/minimal_vcs/core.vhd",  # 混合语言场景的 VHDL 夹具
    "assets/minimal_vcs/rtl.f",  # 最小 filelist 夹具
    "assets/minimal_vcs/dump_ucli.tcl",  # 驱动 FSDB 波形导出检查的 UCLI Tcl 脚本
    "assets/minimal_vcs/manifest_matrix.json",  # 远端真值矩阵夹具
    "assets/minimal_vcs/include/.keep",  # include 目录占位文件
    "assets/waves/scn_base.lst",  # 基础波形信号列表
    "assets/waves/scn_basic.lst",  # 精简波形信号列表
)

# 远端 smoke 证据中必须全部成功的关键步骤名称。
REQUIRED_STEPS = (
    "compile",  # 编译步骤
    "elaborate",  # 展开步骤
    "simulate",  # 仿真步骤
    "verdi-fsdbreport-check",  # Verdi/FSDB 转换核验步骤
)

# 新鲜证据模式下必须回传的关键环境变量。
REQUIRED_ENV_KEYS = (
    "VCS_HOME",  # VCS 安装根目录
    "VERDI_HOME",  # 供 verdi/fsdbreport 定位可执行文件的目录锚点
    "SHELL",  # 远端执行 shell
)

# truth 模式要求回传并通过的远端矩阵条目。
REQUIRED_MATRIX = (
    "minimal_smoke",  # 最小 smoke 场景
    "mixed_vhdl_sv",  # 混合语言场景
    "coverage_urg",  # URG 覆盖率场景
    "fsdb_conversion",  # FSDB 转换场景
)

# delivery 模式允许跳过 truth-only 的 coverage_urg 真值断言。
DELIVERY_MATRIX = (
    "minimal_smoke",  # 交付前最小回归仍需通过的基础 smoke 场景
    "mixed_vhdl_sv",  # 交付前仍需保留的混合语言冒烟场景
    "fsdb_conversion",  # 交付前仍需保留的 FSDB 转换场景
)

# truth 模式默认要求通过的组合覆盖率变体名称。
DEFAULT_TRUTH_VARIANT = "line+cond+tgl__urg__auto64"  # truth 模式的默认 URG 组合变体名

# 记录单个 bundle 条目是否存在，供计划输出与缺失诊断共享。
def _bundle_file_record(path_skill_root: Path, str_relative_path: str) -> JsonDict:
    """
    构造单个 bundle 文件条目的结构化记录。

    参数：
    - path_skill_root: 技能根目录路径。
    - str_relative_path: 需要检查的相对文件路径。

    返回：
    - 返回包含相对路径、绝对路径与存在性的结构化记录。

    异常：
    - 无显式异常；路径拼接与存在性检查沿用 Python 默认行为。
    """

    # 先定位当前条目在技能目录下的真实文件位置，供存在性检查与计划输出复用。
    path_candidate = path_skill_root / str_relative_path  # 当前 bundle 条目对应的文件路径

    # 返回单条 bundle 记录，让调用方直接拼装 file_details 列表。
    return {
        "rel": str_relative_path,
        "path": str(path_candidate),
        "exists": path_candidate.exists(),
    }

# 生成远端 smoke bundle 的文件清单与远端执行计划。
def build_bundle_plan(skill_dir: Path | str, *, remote_dir: str) -> JsonDict:
    """
    构造远端 smoke bundle 的本地打包计划。

    参数：
    - skill_dir: 技能目录根路径；既支持 `Path` 也支持字符串路径。
    - remote_dir: 远端解压并执行 smoke 脚本的目标目录。

    返回：
    - 返回包含文件清单、缺失项与远端命令计划的结构化对象。

    异常：
    - 无显式异常；路径解析与存在性检查沿用 Python 默认行为。
    """

    # 先把技能目录统一解析成路径对象，避免后续重复判断字符串输入。
    path_skill_root = Path(skill_dir)  # 当前 bundle 计划使用的技能根目录

    # 收集所有需要进入 zip 的相对路径，保持与常量清单一致的输出顺序。
    list_bundle_files: list[str] = []  # 当前 bundle 计划包含的相对路径列表

    # 保存每个条目的绝对路径与存在性，便于调用方直查缺失项来源。
    list_file_details: list[JsonDict] = []  # 当前 bundle 计划的逐文件明细列表

    # 单独缓存缺失条目，供 blocked 状态与错误摘要直接复用。
    list_missing_paths: list[str] = []  # 当前 bundle 计划里缺失的相对路径列表

    # 顺序扫描所有必需条目，保持文件清单与缺失诊断可稳定复现。
    for str_relative_path in MINIMAL_BUNDLE_FILES:

        # 先生成单条文件记录，避免存在性检查逻辑在循环内散落复制。
        json_dict_file_record = _bundle_file_record(path_skill_root, str_relative_path)  # 当前 bundle 条目的检查结果

        # 把当前相对路径写入 files 列表，供远端 zip 校验与回显复用。
        list_bundle_files.append(str_relative_path)

        # 把当前检查结果收进 file_details，保留更细的排障上下文。
        list_file_details.append(json_dict_file_record)

        # 当前条目缺失时，把相对路径单独写入缺失列表便于 blocked 返回。
        if not json_dict_file_record["exists"]:

            # 缺失列表只保留相对路径，方便与发布目录中的实际布局对照。
            list_missing_paths.append(str_relative_path)

    # 远端侧需要依次完成建目录、解压 zip 与执行 smoke 三个动作。
    list_remote_commands = [  # 当前 bundle 计划推荐的远端执行命令列表
        f"mkdir -p {remote_dir}",  # 准备远端工作目录
        f"unzip -o remote-eda-vcs-verdi-bundle.zip -d {remote_dir}",  # 解压本地生成的 bundle
        (
            f"cd {remote_dir} && chmod +x scripts/shell/remote/*.sh && "
            "bash scripts/shell/remote/run_remote_eda_smoke.sh"
        ),  # 进入远端目录并执行 smoke 入口脚本
    ]

    # 返回当前 bundle 计划，让调用方据此决定是否继续打包或先修复缺失项。
    return {
        "status": "ready" if not list_missing_paths else "blocked",
        "skill_dir": str(path_skill_root),
        "remote_dir": remote_dir,
        "files": list_bundle_files,
        "file_details": list_file_details,
        "missing": list_missing_paths,
        "remote_commands": list_remote_commands,
    }

# 规范化 shell 脚本载荷，确保 zip 里的 shell 文件统一使用 LF 行尾。
def _normalized_shell_bytes(path_script: Path) -> bytes:
    """
    读取并规范化 shell 脚本字节内容。

    参数：
    - path_script: 需要写入 zip 的 shell 脚本路径。

    返回：
    - 返回已经统一成 LF 行尾的脚本字节串。

    异常：
    - 无显式异常；文件读取沿用 Python 默认行为。
    """

    # 先按二进制读取脚本内容，避免编码层额外改变可执行脚本的原始字节。
    bytes_script_payload = path_script.read_bytes()  # 当前 shell 脚本的原始字节内容

    # 把 Windows 风格 CRLF 统一折叠成 LF，保证远端解压后的脚本可直接执行。
    return bytes_script_payload.replace(b"\r\n", b"\n")

# 判定 zip 成员名是否保持了 POSIX 相对路径约束。
def _is_safe_archive_name(str_archive_name: str) -> bool:
    """
    判断 zip 成员名是否满足安全且稳定的相对路径约束。

    参数：
    - str_archive_name: 需要检查的 zip 成员名。

    返回：
    - 路径安全且保持 POSIX 相对形式时返回真；否则返回假。

    异常：
    - 无显式异常；路径分段解析沿用 Python 默认行为。
    """

    # 反斜杠、绝对路径前缀与父目录逃逸都会破坏 zip 成员名稳定性。
    return (
        "\\" not in str_archive_name
        and not str_archive_name.startswith("/")
        and ".." not in Path(str_archive_name).parts
    )

# 生成远端 smoke bundle zip，并额外核验 zip 内部路径是否满足 POSIX 约束。
def create_bundle_zip(skill_dir: Path | str, output: Path | str) -> JsonDict:
    """
    创建远端 smoke bundle zip 文件。

    参数：
    - skill_dir: 技能目录根路径；既支持 `Path` 也支持字符串路径。
    - output: 目标 zip 文件路径；既支持 `Path` 也支持字符串路径。

    返回：
    - 返回包含输出路径、归档成员列表与路径安全结果的结构化对象。

    异常：
    - 无显式异常；zip 写入与路径检查沿用 Python 默认行为。
    """

    # 先统一收口技能根目录，确保 plan 与实际写包阶段引用同一份目录语义。
    path_skill_root = Path(skill_dir)  # 当前 zip 打包使用的技能根目录

    # 把输出路径也解析成 `Path`，便于创建父目录并回填最终路径字符串。
    path_output_zip = Path(output)  # 当前 bundle zip 的目标输出路径

    # 打包前先复用计划检查逻辑，避免缺文件时仍然生成半成品归档。
    json_dict_plan = build_bundle_plan(path_skill_root, remote_dir=".")  # 当前 zip 打包前复用的 bundle 计划

    # 计划仍有缺失项时，直接返回 blocked，让调用方先修复源目录。
    if json_dict_plan["missing"]:

        # 返回 blocked 结果时仍然带上文件清单，方便调用方定位缺失项。
        return {
            "status": "blocked",
            "output": str(path_output_zip),
            "missing": json_dict_plan["missing"],
            "files": json_dict_plan["files"],
        }

    # 先补齐输出 zip 的父目录层级，避免目标目录缺失导致写包失败。
    path_output_zip.parent.mkdir(parents=True, exist_ok=True)

    # 顺序写入 bundle 所需条目，保持 zip 内部路径与计划清单完全一致。
    with zipfile.ZipFile(path_output_zip, "w", compression=zipfile.ZIP_DEFLATED) as obj_archive:

        # 按计划清单逐项落入 zip，避免额外文件混入当前远端 smoke bundle。
        for str_relative_path in json_dict_plan["files"]:

            # 先定位当前需要入档的真实源文件，便于根据后缀切换写入策略。
            path_source_file = path_skill_root / str_relative_path  # 当前 zip 成员对应的本地源文件路径

            # shell 脚本需要统一 LF 行尾并补上可执行位，保证远端直接执行成功。
            if path_source_file.suffix == ".sh":

                # 先准备统一 LF 行尾后的脚本字节载荷，避免 CRLF 污染远端执行环境。
                bytes_script_payload = _normalized_shell_bytes(path_source_file)  # 当前 shell 脚本的归档字节内容

                # 单独构造 zip 成员头，确保 shell 脚本解压后保留可执行权限。
                zip_info_obj_zip_info: zipfile.ZipInfo = zipfile.ZipInfo(str_relative_path)  # 当前 shell 脚本的 zip 成员信息

                # 通过 Unix 权限位显式保留 shell 脚本的可执行属性。
                zip_info_obj_zip_info.external_attr = 0o755 << 16  # 当前 shell 脚本归档后的 Unix 权限位

                # 直接把规范化后的字节写入归档，避免文本模式改变脚本内容。
                obj_archive.writestr(zip_info_obj_zip_info, bytes_script_payload)

            # 其余普通资源文件可以直接按路径归档。
            else:

                # 保留计划中的相对路径作为归档成员名，确保 zip 布局稳定。
                obj_archive.write(path_source_file, arcname=str_relative_path)

    # 重新打开 zip 读取成员名，便于在返回体中附带最终归档结果。
    with zipfile.ZipFile(path_output_zip, "r") as obj_archive:

        # 读取 zip 内部所有成员名，供路径安全与回归测试复用。
        list_archive_names = obj_archive.namelist()  # 当前 bundle zip 的成员名列表

    # 过滤出违反 POSIX 相对路径约束的成员名，让调用方能立即发现异常归档布局。
    list_bad_archive_names = [  # 当前 zip 中不安全或不稳定的成员名列表
        str_archive_name  # 当前待检查的归档成员名
        for str_archive_name in list_archive_names  # 顺序扫描所有归档成员名
        if not _is_safe_archive_name(str_archive_name)  # 仅保留违反 POSIX 相对路径约束的成员名
    ]

    # 返回写包结果，供本地核验与后续安装/部署流程继续消费。
    return {
        "status": "ready" if not list_bad_archive_names else "blocked",
        "output": str(path_output_zip),
        "files": list_archive_names,
        "bad_names": list_bad_archive_names,
    }

# 解析 ISO8601 时间戳文本，并统一折算到 UTC 时区。
def _parse_timestamp(str_value: str) -> datetime | None:
    """
    把时间戳文本解析成 UTC `datetime` 对象。

    参数：
    - str_value: 原始时间戳字符串；允许带 `Z` 后缀。

    返回：
    - 解析成功时返回 UTC `datetime`；解析失败时返回 `None`。

    异常：
    - 无显式异常；非法文本会被当前 helper 吞掉并回退为 `None`。
    """

    # 空字符串无法形成有效时间戳，因此这里直接回退为空值。
    if not str_value:

        # 返回空值，让调用方把时间戳缺失当成证据不充分处理。
        return None

    # 先把 `Z` 标准化成显式 `+00:00` 偏移，兼容 `fromisoformat` 的输入要求。
    str_text = str_value.replace("Z", "+00:00")  # 当前时间戳文本的 ISO8601 标准化版本

    # 先尝试按 ISO8601 文本解析时间戳，非法文本会直接回退为空值。
    try:

        # 读取当前时间戳文本对应的 `datetime` 对象。
        datetime_parsed_timestamp_utc: datetime = datetime.fromisoformat(str_text)  # 当前时间戳解析得到的时间对象

    # 解析失败时说明输入不满足 ISO8601 约束。
    except ValueError:

        # 返回空值，让调用方把时间戳视为缺失或非法。
        return None

    # 缺少时区信息时，按 UTC 解释以保持远端证据比较口径稳定。
    if datetime_parsed_timestamp_utc.tzinfo is None:

        # 把无时区时间戳显式标成 UTC，避免后续时间差计算报错。
        datetime_normalized_timestamp_utc: datetime = datetime_parsed_timestamp_utc.replace(tzinfo=UTC)  # 当前时间戳补齐 UTC 时区后的时间对象

        # 返回补齐时区后的 UTC 时间对象，保持缺省时区输入也能参与后续年龄计算。
        return datetime_normalized_timestamp_utc.astimezone(UTC)

    # 统一折算成 UTC，保证新鲜度计算不受原始时区表示影响。
    return datetime_parsed_timestamp_utc.astimezone(UTC)

# 把证据中的步骤字段统一映射成按步骤名索引的字典结构。
def _steps_map(obj_raw_steps: Any) -> JsonDict:
    """
    规范化远端证据中的步骤集合表示。

    参数：
    - obj_raw_steps: 证据里原始的 `steps` 字段；可能是列表或字典。

    返回：
    - 返回以步骤名为键的字典表示；缺失时返回空字典。

    异常：
    - 无显式异常；非预期结构会保守回退为空字典。
    """

    # 列表形态需要按 `name` 字段重建映射，方便后续按步骤名直接索引。
    if isinstance(obj_raw_steps, list):

        # 把步骤列表折叠成字典，保持 `validate_evidence` 的读取逻辑统一。
        return {
            json_dict_step.get("name"): json_dict_step
            for json_dict_step in obj_raw_steps
            if isinstance(json_dict_step, dict)
        }

    # 已经是字典形态时直接复用，避免不必要的结构复制。
    if isinstance(obj_raw_steps, dict):

        # 返回原有步骤映射，让调用方继续按步骤名读取。
        return obj_raw_steps

    # 其余非预期结构都回退为空字典，保持证据缺失时的保守判定。
    return {}

# 计算整份证据是否仍处于允许的时间新鲜度窗口内。
def _freshness(
    json_dict_evidence: JsonDict,
    *,
    max_age_hours: int | None,
    now_utc: str | None,
) -> tuple[bool, str]:
    """
    判断远端证据是否满足时间新鲜度门禁。

    参数：
    - json_dict_evidence: 当前待核验的远端证据对象。
    - max_age_hours: 允许的最大证据年龄；为空时表示跳过新鲜度门禁。
    - now_utc: 供测试或回放显式指定的当前 UTC 时间文本。

    返回：
    - 返回 `(是否新鲜, 错误说明)` 二元组；通过时错误说明为空字符串。

    异常：
    - 无显式异常；时间戳非法时会转成失败原因文本返回。
    """

    # 证据显式自证为 fresh 时，优先沿用该事实并跳过时间差计算。
    if json_dict_evidence.get("fresh"):

        # 返回 fresh，让调用方继续处理其他门禁而不再重复推算年龄。
        return True, ""

    # 未开启新鲜度门禁时，直接视为通过，避免对历史回放强加时效要求。
    if max_age_hours is None:

        # 返回通过结果，让调用方继续检查步骤、矩阵与产物证据。
        return True, ""

    # 先解析证据自身携带的时间戳，后续年龄计算都依赖这一个锚点。
    datetime_evidence_timestamp_utc: datetime | None = _parse_timestamp(  # 当前证据声明的 UTC 时间戳
        str(json_dict_evidence.get("timestamp_utc", ""))  # 证据里原始的时间戳文本
    )

    # 时间戳缺失或格式非法时，当前证据不足以支撑事实性通过。
    if datetime_evidence_timestamp_utc is None:

        # 返回失败原因，让调用方把时间戳缺陷纳入最终 errors 列表。
        return False, "timestamp_utc missing or invalid"

    # 先确定当前时刻的文本锚点，便于把输入异常和解析异常拆开处理。
    str_now_timestamp_utc: str = now_utc or datetime.now(UTC).isoformat()  # 当前比较使用的 UTC 时间文本

    # 再把当前时刻文本解析成时间对象，供证据年龄计算复用。
    datetime_reference_now_utc: datetime | None = _parse_timestamp(  # 当前比较使用的 UTC 现在时刻
        str_now_timestamp_utc  # 已确定来源的当前 UTC 时间文本
    )

    # 当前时刻文本非法时，无法完成新鲜度计算，只能把它视为输入错误。
    if datetime_reference_now_utc is None:

        # 返回失败原因，让调用方显式知道 `now_utc` 输入无效。
        return False, "now_utc invalid"

    # 计算证据距当前时刻的小时数，供后续比较是否超过允许窗口。
    float_age_hours = (datetime_reference_now_utc - datetime_evidence_timestamp_utc).total_seconds() / 3600  # 当前证据相对现在的年龄小时数

    # 超出允许窗口时，当前证据不应被当成事实性通过。
    if float_age_hours > max_age_hours:

        # 返回过期原因，保持错误文本里直接携带具体小时数。
        return False, f"evidence is stale: {float_age_hours:.1f}h old"

    # 证据时间戳落在未来时，同样不能视为可信的新鲜证据。
    if float_age_hours < 0:

        # 返回失败原因，让调用方明确这是时间锚点方向异常。
        return False, "evidence timestamp is in the future"

    # 其余情况说明证据年龄落在允许窗口内，可以继续通过后续门禁。
    return True, ""

# 根据 truth/delivery 模式选择应当读取的矩阵对象与必需条目。
def _matrix_for_mode(json_dict_evidence: JsonDict, str_mode: str) -> tuple[JsonDict, tuple[str, ...]]:
    """
    为给定核验模式挑选矩阵对象与必需条目集合。

    参数：
    - json_dict_evidence: 当前待核验的远端证据对象。
    - str_mode: 当前核验模式，只允许 `truth` 或 `delivery`。

    返回：
    - 返回 `(矩阵对象, 必需条目元组)` 二元组。

    异常：
    - 无显式异常；缺失矩阵时会保守回退到空字典。
    """

    # delivery 模式优先读取 `delivery_matrix`，缺失时回退到通用 `matrix`。
    if str_mode == "delivery":

        # 返回 delivery 模式应当核验的矩阵对象与条目集合。
        return json_dict_evidence.get("delivery_matrix", json_dict_evidence.get("matrix", {})), DELIVERY_MATRIX

    # truth 模式要保留 coverage_urg 真值语义，因此这里优先读取 `truth_matrix` 视图。
    return json_dict_evidence.get("truth_matrix", json_dict_evidence.get("matrix", {})), REQUIRED_MATRIX

# 核验关键步骤是否全部成功，并在 fresh 模式下补充命令证据约束。
def _append_step_errors(
    list_errors: list[str],
    json_dict_steps: JsonDict,
    *,
    bool_require_commands: bool,
) -> None:
    """
    把步骤级失败信息追加到错误列表中。

    参数：
    - list_errors: 当前累计的错误文本列表。
    - json_dict_steps: 按步骤名索引的步骤字典。
    - bool_require_commands: 为真时还要求每个步骤提供 `cmd` 证据。

    返回：
    - 当前 helper 只会向错误列表追加文本，不返回业务结果。

    异常：
    - 无显式异常；字典读取沿用 Python 默认行为。
    """

    # 顺序检查远端 smoke 关键步骤，保持错误输出顺序与门禁顺序一致。
    for str_step_name in REQUIRED_STEPS:

        # 先读取当前步骤记录；缺失时按空字典处理，维持失败语义保守一致。
        json_dict_step: JsonDict = json_dict_steps.get(str_step_name, {})  # 当前关键步骤对应的证据条目

        # returncode 非零时，当前步骤没有形成可接受的成功证据。
        if json_dict_step.get("returncode") != 0:

            # 记录具体步骤失败信息，便于直接定位远端 smoke 卡在何处。
            list_errors.append(f"{str_step_name} returncode is {json_dict_step.get('returncode')}")

        # fresh 门禁开启时，还必须保留可追溯的命令证据。
        if bool_require_commands and not json_dict_step.get("cmd"):

            # 命令证据缺失会削弱事实性核验，因此要单独登记为错误。
            list_errors.append(f"{str_step_name} command evidence missing")

# 核验 FSDB 产物与 report 文本是否满足最小真值要求。
def _append_artifact_errors(list_errors: list[str], json_dict_evidence: JsonDict) -> None:
    """
    把产物与 report 相关失败信息追加到错误列表中。

    参数：
    - list_errors: 当前累计的错误文本列表。
    - json_dict_evidence: 当前待核验的远端证据对象。

    返回：
    - 当前 helper 只会向错误列表追加文本，不返回业务结果。

    异常：
    - 无显式异常；字典与字符串读取沿用 Python 默认行为。
    """

    # 先读取 artifacts 字段，后续会重点核验波形文件是否真实产出。
    json_dict_artifacts: JsonDict = json_dict_evidence.get("artifacts", {})  # 当前证据对象中的产物字典

    # waves.fsdb 是最小 smoke 的关键真值产物，字节数必须大于零。
    json_dict_fsdb: JsonDict = json_dict_artifacts.get("waves.fsdb", {})  # 当前证据对象里的 FSDB 产物记录

    # FSDB 缺失或零字节都说明远端仿真并未形成有效波形证据。
    if json_dict_fsdb.get("bytes", 0) <= 0:

        # 把波形缺失记录成显式错误，避免调用方误把步骤成功当成事实性通过。
        list_errors.append("waves.fsdb is missing or zero bytes")

    # 读取文本报告内容，后续要确认关键探针信号是否实际出现在 report 中。
    str_report_text = str(json_dict_evidence.get("report_text", ""))  # 当前证据对象里的 report 文本

    # `/top/clk` 是最小 smoke 真值核验的核心信号，缺失时不应判定通过。
    if "/top/clk" not in str_report_text:

        # 记录 report 文本缺失关键信号，提示上游重新检查 fsdbreport 输出。
        list_errors.append("fsdbreport output does not include /top/clk")

# fresh 模式下核验环境变量、许可证提示与远端矩阵状态。
def _append_remote_context_errors(
    list_errors: list[str],
    json_dict_evidence: JsonDict,
    *,
    str_mode: str,
) -> None:
    """
    把环境与远端矩阵相关失败信息追加到错误列表中。

    参数：
    - list_errors: 当前累计的错误文本列表。
    - json_dict_evidence: 当前待核验的远端证据对象。
    - str_mode: 当前核验模式，只允许 `truth` 或 `delivery`。

    返回：
    - 当前 helper 只会向错误列表追加文本，不返回业务结果。

    异常：
    - 无显式异常；字典读取与字符串拼接沿用 Python 默认行为。
    """

    # 先读取环境字典，逐项核验工具安装与 shell 事实是否都已回传。
    json_dict_environment: JsonDict = json_dict_evidence.get("environment", {})  # 当前证据对象里的远端环境字典

    # 关键环境变量缺失时，当前证据还不足以支撑事实性通过。
    for str_env_key in REQUIRED_ENV_KEYS:

        # 单项环境变量为空时，当前远端环境证据不完整。
        if not json_dict_environment.get(str_env_key):

            # 记录缺失的环境键名，便于快速补齐远端 smoke 采样内容。
            list_errors.append(f"environment {str_env_key} missing")

    # 许可证提示至少要回传一个常见变量，避免远端授权上下文完全缺失。
    if not (
        json_dict_environment.get("SNPSLMD_LICENSE_FILE")
        or json_dict_environment.get("LM_LICENSE_FILE")
    ):

        # 当前证据没有提供许可证线索，不满足 remote gate 的最小环境可解释性。
        list_errors.append("license environment hint missing")

    # 根据 truth/delivery 模式选择实际需要核验的矩阵对象与条目集合。
    tuple_matrix_selection: tuple[JsonDict, tuple[str, ...]] = _matrix_for_mode(json_dict_evidence, str_mode)  # 当前模式选中的矩阵对象与条目集合

    # 把矩阵对象单独取出，后续每一项状态核验都基于这一个视图展开。
    json_dict_matrix = tuple_matrix_selection[0]  # 当前模式实际需要核验的矩阵对象

    # 把必需条目集合单独取出，保持循环里只关注矩阵项本身。
    tuple_required_matrix = tuple_matrix_selection[1]  # 当前模式要求通过的矩阵条目元组

    # 顺序检查矩阵条目状态，让错误文本与门禁定义保持一致顺序。
    for str_matrix_name in tuple_required_matrix:

        # 先读取当前矩阵项；缺失时按空字典处理，保持错误逻辑保守一致。
        json_dict_matrix_item: JsonDict = json_dict_matrix.get(str_matrix_name, {})  # 当前矩阵条目的结构化状态对象

        # 非 `passed` 状态都必须转成显式错误，不能被静默吞掉。
        if json_dict_matrix_item.get("status") != "passed":

            # 当前矩阵项若附带 reason，就把它并入错误文本提高可读性。
            str_reason = str(json_dict_matrix_item.get("reason", ""))  # 当前矩阵项的失败原因文本

            # 仅在 reason 非空时补充后缀，避免错误文本多出空冒号。
            str_suffix = f": {str_reason}" if str_reason else ""  # 当前矩阵项失败原因后缀

            # 记录矩阵项失败状态与原因，让调用方直接看到哪一项没有通过。
            list_errors.append(
                f"remote matrix {str_matrix_name} is {json_dict_matrix_item.get('status', 'missing')}{str_suffix}"
            )

# truth 模式下补充 coverage_urg 相关的证据充分性约束。
def _append_truth_coverage_errors(list_errors: list[str], json_dict_evidence: JsonDict) -> None:
    """
    把 truth 模式下的 coverage_urg 细化门禁追加到错误列表中。

    参数：
    - list_errors: 当前累计的错误文本列表。
    - json_dict_evidence: 当前待核验的远端证据对象。

    返回：
    - 当前 helper 只会向错误列表追加文本，不返回业务结果。

    异常：
    - 无显式异常；字典读取沿用 Python 默认行为。
    """

    # truth 模式统一从 truth_matrix 回退读取 coverage_urg 条目，保持老证据兼容。
    json_dict_truth_matrix = json_dict_evidence.get("truth_matrix", json_dict_evidence.get("matrix", {}))  # 当前证据对象里的 truth 矩阵

    # 取出 coverage_urg 条目，后续会按 passed/failed 两条路径分开核验。
    json_dict_coverage_item: JsonDict = json_dict_truth_matrix.get("coverage_urg", {})  # 当前 truth 矩阵中的 coverage_urg 条目

    # coverage_urg 明确失败时，必须额外回传 probe、矩阵与诊断证据。
    if json_dict_coverage_item.get("status") not in ("passed", None):

        # 失败却没有 urg_runtime_probe 时，当前 coverage 故障没有基本运行时上下文。
        if not json_dict_evidence.get("urg_runtime_probe"):

            # 记录缺失 probe 证据，提醒调用方补齐运行时探测结果。
            list_errors.append("urg runtime probe evidence missing for failed coverage_urg")

        # 失败却没有 urg_coverage_matrix 时，调用方无法看到变体矩阵真实结果。
        if not json_dict_evidence.get("urg_coverage_matrix"):

            # 记录缺失矩阵证据，提醒调用方补齐 URG 变体矩阵输出。
            list_errors.append("urg coverage matrix evidence missing for failed coverage_urg")

        # 读取 coverage_summary，后续需要确认至少保留了一侧 stdout/stderr 尾部。
        json_dict_coverage_summary: JsonDict = json_dict_evidence.get("coverage_summary", {})  # 当前 failed coverage 对应的摘要对象

        # 失败却没有 stdout/stderr 尾部，会让 coverage 故障缺少最小文本上下文。
        if not json_dict_coverage_summary.get("stderr_tail") and not json_dict_coverage_summary.get("stdout_tail"):

            # 记录缺失日志尾部证据，提醒调用方补齐 coverage 输出摘要。
            list_errors.append("coverage stdout/stderr tail evidence missing for failed coverage_urg")

        # 失败却没有 urg_troubleshoot 时，当前 coverage 故障缺少下游诊断结论。
        if not json_dict_evidence.get("urg_troubleshoot"):

            # 记录缺失 troubleshoot 证据，提醒调用方补齐 URG 故障分析结果。
            list_errors.append("urg troubleshoot evidence missing for failed coverage_urg")

    # coverage_urg 已通过时，还要补充校验报告目录与默认组合变体是否真实存在。
    if json_dict_coverage_item.get("status") == "passed":

        # 先读取 coverage 摘要，确认 urgReport 是否真的产出并且包含文件。
        json_dict_coverage_summary: JsonDict = json_dict_evidence.get("coverage_summary", {})  # 当前 passed coverage 用来证明 urgReport 落盘的摘要对象

        # 覆盖率详细状态通常放在 `coverage_summary.coverage` 子对象里。
        json_dict_coverage_status: JsonDict = json_dict_coverage_summary.get("coverage", {})  # 当前 coverage 摘要里的报告状态对象

        # 报告目录不存在或文件数为空时，不应把 coverage_urg 当成事实性通过。
        if (
            not json_dict_coverage_status.get("report_exists")
            or json_dict_coverage_status.get("report_file_count", 0) <= 0
        ):

            # 记录通过却无报告目录的矛盾状态，提醒调用方重新检查 URG 产物。
            list_errors.append("coverage_urg passed without nonempty urgReport evidence")

        # 继续读取 URG 变体矩阵里的默认组合条目，核验最关键的 truth 变体是否真的通过。
        json_dict_default_variant: JsonDict = (
            json_dict_evidence.get("urg_coverage_matrix", {}).get("default_variant", {})  # 当前证据回传的默认 URG 变体对象
        )  # 当前 URG 矩阵里的默认 truth 变体

        # 默认组合名称、状态或报告文件数任一不满足时，都不应接受 truth 通过结论。
        if (
            json_dict_default_variant.get("name") != DEFAULT_TRUTH_VARIANT
            or json_dict_default_variant.get("status") != "passed"
            or json_dict_default_variant.get("report_file_count", 0) <= 0
        ):

            # 记录默认 truth 变体不达标，让调用方直接看到 coverage_urg 的关键缺口。
            list_errors.append("default line+cond+tgl URG matrix variant did not pass")

# 核验一份远端 smoke/coverage 证据是否满足 truth 或 delivery 门禁。
def validate_evidence(
    evidence: JsonDict,
    *,
    max_age_hours: int | None = None,
    now_utc: str | None = None,
    mode: str = "truth",
) -> JsonDict:
    """
    核验远端 smoke/coverage 证据是否满足给定模式的门禁要求。

    参数：
    - evidence: 当前待核验的远端证据对象。
    - max_age_hours: 允许的最大证据年龄；为空时跳过新鲜度门禁。
    - now_utc: 供测试或回放显式指定的当前 UTC 时间文本。
    - mode: 当前核验模式，只允许 `truth` 或 `delivery`。

    返回：
    - 返回包含通过状态、fresh 标记、错误列表与回显证据的结构化对象。

    异常：
    - 无显式异常；非法输入会被折叠成失败错误文本。
    """

    # 累计所有失败原因，最后再统一折叠成 `passed` 或 `failed` 状态。
    list_errors: list[str] = []  # 当前证据核验过程中累计出的错误文本列表

    # truth 模式要求远端作业本身零退出；delivery 模式则允许 coverage_urg 留在软阻断。
    if mode == "truth" and evidence.get("job_exit_code") != 0:

        # 把整体作业退出码异常显式记录下来，避免调用方忽略第一层失败事实。
        list_errors.append(f"job exit code is {evidence.get('job_exit_code')}")

    # 先把 `steps` 字段统一收口成字典，后续步骤核验才能按固定键名直接读取。
    json_dict_steps = _steps_map(evidence.get("steps", {}))  # 当前证据对象里的步骤映射

    # 核验 compile/elaborate/simulate/fsdbreport 四个关键步骤是否都提供成功证据。
    _append_step_errors(
        list_errors,
        json_dict_steps,
        bool_require_commands=max_age_hours is not None,
    )

    # 核验 FSDB 产物与 report 文本里的关键信号是否满足最小真值要求。
    _append_artifact_errors(list_errors, evidence)

    # 新鲜度门禁开启时，还需要补充时间、环境和远端矩阵的事实性核验。
    bool_fresh = True  # 当前证据默认视为 fresh，除非新鲜度计算明确给出失败

    # 只有显式开启时间窗口时，才把 fresh/环境/矩阵作为事实性通过条件。
    if max_age_hours is not None:

        # 先计算当前证据是否仍在允许的时间窗口内。
        tuple_freshness_result = _freshness(  # 当前证据的新鲜度判定结果二元组
            evidence,  # 当前待核验的远端证据对象
            max_age_hours=max_age_hours,  # 当前允许的最大证据年龄小时数
            now_utc=now_utc,  # 当前比较使用的 UTC 时间锚点
        )

        # 先取出是否 fresh 的布尔判定，供后续 errors 与返回体同时复用。
        bool_fresh = tuple_freshness_result[0]  # 当前证据是否仍在允许的新鲜度窗口内

        # 再取出失败说明文本，只有新鲜度失败时才会被并入错误列表。
        str_freshness_error = tuple_freshness_result[1]  # 当前新鲜度门禁失败时的原因文本

        # 新鲜度不通过时，把错误原因直接并入总错误列表。
        if not bool_fresh:

            # 时间锚点失效意味着这份证据不再能支撑事实性通过。
            list_errors.append(str_freshness_error)

        # 继续核验远端环境、许可证线索与矩阵状态，让 fresh 模式保持强事实门禁。
        _append_remote_context_errors(list_errors, evidence, str_mode=mode)

        # truth 模式还要附加 coverage_urg 的深入证据充分性检查。
        if mode == "truth":

            # 继续检查 failed/passed 两条 coverage_urg 路径下的补充证据要求。
            _append_truth_coverage_errors(list_errors, evidence)

    # 返回最终核验结果，让调用方直读状态、fresh 标记与错误明细。
    return {
        "status": "passed" if not list_errors else "failed",
        "fresh": bool_fresh,
        "errors": list_errors,
        "evidence": evidence,
        "validation_mode": mode,
    }

# 用 delivery 模式复用主核验器，避免调用方手写 `mode="delivery"`。
def validate_delivery_evidence(
    evidence: JsonDict,
    *,
    max_age_hours: int | None = None,
    now_utc: str | None = None,
) -> JsonDict:
    """
    使用 delivery 模式核验远端证据。

    参数：
    - evidence: 当前待核验的远端证据对象。
    - max_age_hours: 允许的最大证据年龄；为空时跳过新鲜度门禁。
    - now_utc: 供测试或回放显式指定的当前 UTC 时间文本。

    返回：
    - 返回 delivery 模式下的结构化核验结果。

    异常：
    - 无显式异常；具体失败会折叠到返回体里的错误列表。
    """

    # 直接复用主核验器，让 delivery 模式与 truth 模式共享同一套核心逻辑。
    return validate_evidence(
        evidence,
        max_age_hours=max_age_hours,
        now_utc=now_utc,
        mode="delivery",
    )

# 构造当前 CLI 使用的参数解析器，集中声明三种工作模式的输入开关。
def _build_argument_parser() -> argparse.ArgumentParser:
    """
    构造远端 gate CLI 的参数解析器。

    参数：
    - 无业务参数：当前 helper 不接收外部业务参数。

    返回：
    - 返回已经完成参数注册的解析器对象。

    异常：
    - 无显式异常；参数注册沿用 `argparse` 默认行为。
    """

    # 先构造解析器对象，并写入这条 CLI 的最小用途说明。
    parser = argparse.ArgumentParser(  # 当前远端 gate CLI 使用的参数解析器
        description="Plan or validate the minimal remote EDA host VCS/Verdi gate.",  # 当前 CLI 的简要用途说明
    )

    # 注册技能目录输入；默认回退到当前脚本向上三级的技能根目录。
    parser.add_argument("--skill-dir", type=Path, default=Path(__file__).resolve().parents[3])

    # 注册远端工作目录输入，供 bundle 计划模式直接构造远端命令。
    parser.add_argument("--remote-dir", default="validation/vcs-verdi-nongui")

    # 注册目标 zip 输出路径；提供该参数时进入打包模式。
    parser.add_argument("--bundle-zip", type=Path)

    # 注册证据文件路径；提供该参数时进入证据核验模式。
    parser.add_argument("--evidence", type=Path)

    # 注册新鲜度门禁窗口；为空时跳过事实性时间约束。
    parser.add_argument("--max-age-hours", type=int)

    # 注册显式当前 UTC 时间，便于回放测试与稳定复测。
    parser.add_argument("--now-utc")

    # 注册机器可读输出开关；开启后标准输出只写出单个 JSON 对象。
    parser.add_argument("--json", action="store_true")

    # 返回配置完成的解析器对象，供主入口统一复用。
    return parser

# 执行 CLI 主入口，并根据输入形态选择计划、打包或证据核验路径。
def main(argv: list[str] | None = None) -> int:
    """
    执行远端 gate CLI 主入口。

    参数：
    - argv: 可选的命令行参数列表；为空时由 `argparse` 直接读取进程参数。

    返回：
    - 成功状态返回 0，阻断状态返回 1。

    异常：
    - 参数格式错误时由 `argparse` 直接终止并输出错误。
    """

    # 先构造当前 CLI 的参数解析器，集中收口本地计划与远端证据核验入口。
    parser = _build_argument_parser()  # 当前 CLI 入口使用的参数解析器

    # 解析当前命令行输入，得到后续模式分派需要的参数对象。
    args = parser.parse_args(argv)  # 当前 CLI 请求解析得到的参数对象

    # 提供 `--bundle-zip` 时，优先进入本地 zip 打包模式。
    if args.bundle_zip:

        # 生成 bundle zip，并返回归档成员与路径安全检查结果。
        json_dict_result = create_bundle_zip(args.skill_dir, args.bundle_zip)  # 当前 CLI 请求对应的打包结果

    # 提供 `--evidence` 时，进入远端证据核验模式。
    elif args.evidence:

        # 先读取证据 JSON，再按 truth 模式执行远端门禁核验。
        json_dict_result = validate_evidence(  # 当前 CLI 请求对应的证据核验结果
            json.loads(args.evidence.read_text(encoding="utf-8")),  # 当前 CLI 读取出的远端证据 JSON 对象
            max_age_hours=args.max_age_hours,  # 当前 CLI 指定的证据新鲜度窗口
            now_utc=args.now_utc,  # 当前 CLI 指定的 UTC 现在时刻
        )

    # 其余情况默认进入 bundle 计划模式，只做本地清单与远端命令规划。
    else:

        # 构造远端 bundle 计划，供调用方决定是否继续打包与部署。
        json_dict_result = build_bundle_plan(args.skill_dir, remote_dir=args.remote_dir)  # 当前 CLI 请求对应的 bundle 计划结果

    # 显式请求 JSON 协议时，标准输出只允许写出单个 JSON 对象。
    if args.json:

        # 按模块文档声明把结构化结果直接写到标准输出，供上游自动化读取。
        json.dump(json_dict_result, sys.stdout, indent=2, sort_keys=True)

        # 为 JSON 协议输出补一个换行，避免 shell 提示符粘在 JSON 末尾。
        sys.stdout.write("\n")

    # 默认模式只输出短摘要，避免终端直接泄漏完整结构化载荷。
    else:

        # 先读取最终状态，后续摘要文本只围绕这个状态给出最小提示。
        str_status = str(json_dict_result.get("status", "unknown"))  # 当前 CLI 请求得到的最终状态

        # 结果通过时输出 INFO 摘要，提醒调用方如需结构化详情可改用 `--json`。
        if str_status in {"ready", "passed"}:

            # 输出通过摘要，保持默认 CLI 日志短小且可读。
            print(f"> INFO: [Python] remote EDA gate {str_status}; rerun with --json for structured details")

        # 结果阻断时输出 WARNING 摘要，提醒调用方转看 JSON 细节定位缺口。
        elif str_status == "blocked":

            # 输出阻断摘要，提示调用方使用 JSON 协议查看缺失项或坏路径列表。
            print("> WARNING: [Python] remote EDA gate blocked; rerun with --json for structured details")

        # 其余失败状态统一走 ERR 摘要，让调用方明确这不是可接受的成功结果。
        else:

            # 输出失败摘要，提示调用方使用 JSON 协议查看错误列表。
            print("> ERR: [Python] remote EDA gate failed; rerun with --json for structured details")

    # ready 与 passed 都视为成功退出，其余状态统一返回非零退出码。
    return 0 if json_dict_result.get("status") in {"ready", "passed"} else 1

# 只有脚本被直接执行时才触发 CLI，避免测试导入模块时提前退出当前 Python 进程。
if __name__ == "__main__":

    # 把主入口返回值转成交给 shell 的进程退出码。
    raise SystemExit(main())
