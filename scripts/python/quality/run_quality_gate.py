#!/usr/bin/env python3
"""执行 vcs-verdi-developer 本地质量门并输出结构化结论。

当前模块属于 CLI 交付物。
当调用方显式传入 ``--json`` 时，stdout 协议固定为完整 JSON 对象，供上游脚本、测试和技能流程直接消费。
未传 ``--json`` 时，stdout 只输出带 ``> INFO/WARNING/ERR: [Python]`` 前缀的简短摘要。
"""
from __future__ import annotations

# 引入命令行、JSON、子进程和路径接口，支撑本地门禁计划构建、执行与结果汇总
import argparse
import importlib.util
import json

# 继续引入进程执行、路径和类型相关接口。
import subprocess
import sys
from importlib.machinery import ModuleSpec, SourceFileLoader
from pathlib import Path
from types import ModuleType
from typing import Any

# 为全部 JSON 风格返回对象声明统一别名，避免重复写长注解。
JsonDict = dict[str, Any]  # 质量门计划、步骤结果和置信度摘要都复用该别名。

# 列出 SKILL.md 必须保留的非 GUI 边界短语，后续会直接做诚实性核查。
NON_GUI_SCOPE_TERMS = (
    "non-gui",  # 首项直接锁定技能必须明确声明 non-gui 边界。
    "scripted",  # 第二项要求技能明确说明执行路径必须是脚本化流程。
    "does not claim complete coverage",  # 第三项要求技能禁止夸大为完整官方覆盖。
)  # 缺少其中任何一项都说明技能边界声明可能漂移。

# 维护当前技能受管的 Python 主脚本矩阵，后续既生成审计清单也生成质量门步骤。
SCRIPT_LAYOUT = (
    ("env", "check_env"),  # 首项固定为环境探针入口，便于后续步骤构造保持稳定顺序。
    ("rc", "generate_rc"),  # 第二项固定覆盖 Verdi RC 生成入口。
    ("validation", "vcs_verdi_check"),  # 验证入口负责汇总 VCS/Verdi dry-run 计划。
    ("diagnosis", "analyze_logs"),  # 日志诊断入口负责归纳常见 VCS/Verdi 失败症状。
    ("diagnosis", "fsdb_tools"),  # 波形诊断入口负责 FSDB 相关工具探测与转换。
    ("coverage", "coverage_flow"),  # 覆盖率主流程入口负责 urg/merge 流程编排。
    ("coverage", "patch_ucapi_overlay"),  # 覆盖率补丁入口负责 UCAPI 覆盖层生成。
    ("coverage", "urg_runtime_probe"),  # 运行时探针入口负责 urg 可执行环境检查。
    ("coverage", "urg_coverage_matrix"),  # 覆盖率矩阵入口负责不同覆盖组合的 dry-run。
    ("coverage", "urg_troubleshoot"),  # 覆盖率排障入口负责常见 urg 故障建议。
    ("import", "import_vcs_project"),  # 导入入口负责把 VCS 工程转换为技能定义。
    ("flows", "cocotb_vcs_flow"),  # 流程入口之一覆盖 cocotb 与 VCS 的协同流程。
    ("flows", "riscv_dv_flow"),  # 流程入口之一覆盖 riscv-dv 生成与仿真流程。
    ("flows", "kvips_vcs_flow"),  # 流程入口之一覆盖 kvips 端到端仿真流程。
    ("flows", "fpgen_vcs_flow"),  # 流程入口之一覆盖 fpgen 相关仿真流程。
    ("flows", "autoverifix_vcs_flow"),  # 流程入口之一覆盖 autoverifix 自动修复流程。
    ("flows", "aiss_vcs_flow"),  # 流程入口之一覆盖 aiss 项目接入流程。
    ("evidence", "collect_evidence"),  # 证据收集入口负责整理本地与远程执行事实。
    ("evidence", "evidence_claim_gate"),  # 声明门禁入口负责核对非 GUI 能力声明。
    ("regression", "run_regression"),  # 回归入口负责统一调度技能内的流程回归。
    ("remote", "remote_eda_gate"),  # 远程门禁入口负责读取远程 EDA 证据并判定新鲜度。
    ("quality", "run_quality_gate"),  # 质量门入口负责串联本文件定义的全部验证步骤。
)  # 每个元组依次给出脚本分组目录和基础文件名。

# 声明三种脚本包装层的目录名与扩展名，用来补齐跨终端入口矩阵。
WRAPPER_FAMILIES = (
    ("shell", ".sh"),  # 第一类 wrapper 明确覆盖类 Unix 终端入口。
    ("bat", ".bat"),  # 第二类 wrapper 覆盖 Windows 批处理入口。
    ("powershell", ".ps1"),  # 第三类 wrapper 覆盖 PowerShell 调用入口。
)  # wrapper 审计会逐项验证这三类入口是否全部存在。

# 把脚本矩阵项映射成 Python 相对路径，供后续复用。
def _python_script_rel(str_group: str, str_name: str) -> str:
    """
    生成 Python 主脚本的仓库内相对路径。

    :param str_group: 脚本所属分组目录名，dtype=str，unit=identifier
    :param str_name: 脚本基础文件名，不含扩展名，dtype=str，unit=identifier
    :return: 返回 ``scripts/python/<group>/<name>.py`` 形式的相对路径，dtype=str，unit=path
    """

    # 这里统一拼接标准 Python 脚本布局，避免别处再手写目录片段。
    return f"scripts/python/{str_group}/{str_name}.py"

# 把脚本矩阵项映射成某个 wrapper 族路径，后续审计可直接复用。
def _wrapper_script_rel(str_family: str, str_group: str, str_name: str, str_suffix: str) -> str:
    """
    生成某个 wrapper 脚本的仓库内相对路径。

    :param str_family: wrapper 脚本语言族目录名，dtype=str，unit=identifier
    :param str_group: 脚本所属分组目录名，dtype=str，unit=identifier
    :param str_name: 脚本基础文件名，不含扩展名，dtype=str，unit=identifier
    :param str_suffix: wrapper 文件扩展名，dtype=str，unit=text
    :return: 返回 ``scripts/<family>/<group>/<name><suffix>`` 形式的相对路径，dtype=str，unit=path
    """

    # 这里统一拼接 wrapper 目录和后缀，保持三类入口命名规则一致。
    return f"scripts/{str_family}/{str_group}/{str_name}{str_suffix}"

# 从脚本矩阵展开全部 Python 主脚本相对路径，审计函数会逐项遍历它们。
PYTHON_SCRIPT_RELS = tuple(  # 这里先打开 tuple 构造，随后逐项映射脚本矩阵。
    _python_script_rel(str_group, str_name) for str_group, str_name in SCRIPT_LAYOUT  # 把受管脚本矩阵展开成 Python 文件路径。
)

# 从同一矩阵再展开全部 wrapper 相对路径，避免人工维护重复清单。
WRAPPER_SCRIPT_RELS = tuple(  # 这里先打开 tuple 构造，随后逐项映射 wrapper 矩阵。
    _wrapper_script_rel(str_family, str_group, str_name, str_suffix)  # 逐项生成 shell、bat 和 PowerShell wrapper 路径。
    for str_group, str_name in SCRIPT_LAYOUT  # 外层循环逐项取出受管 Python 脚本矩阵。
    for str_family, str_suffix in WRAPPER_FAMILIES  # 内层循环把每个 Python 入口扩展成三类 wrapper。
)

# 补入两份不走 Python 主脚本矩阵的远程 shell 辅助脚本。
REMOTE_SHELL_RELS = (
    "scripts/shell/remote/make_shell_overlay.sh",  # 首项负责生成远程 shell 覆盖层。
    "scripts/shell/remote/run_remote_eda_smoke.sh",  # 第二项负责远程 EDA 最小冒烟执行。
)  # 这两份脚本仍属于最终交付面，不能漏审。

# 汇总技能最小交付面必须存在的文件，审计函数会拿它做完整性核对。
REQUIRED_SKILL_FILES = (
    "SKILL.md",  # 首项固定保留技能入口说明文件。
    "VERSION",  # 第二项保留技能版本文件，供发布门禁直接读取。
    "agents/openai.yaml",  # 代理定义文件用于本地安装后的路由入口。
    "evals/evals.json",  # 评测定义文件用于技能行为回归检查。
    "references/vcs-verdi-flow.md",  # 主流程参考文档约束对外说明边界。
    "references/non-gui-flow.md",  # 非 GUI 流程文档约束命令行能力声明。
    "references/capability-matrix.md",  # 能力矩阵文档约束主题覆盖面与边界表述。
    "references/third-party-extraction.md",  # 第三方抽取规则文档约束引用来源。
    "references/review-checklist.md",  # 审查清单文档用于人工收尾核对。
    "references/remote-eda-gate.md",  # 远程门禁文档用于说明远程验证契约。
    *PYTHON_SCRIPT_RELS,  # Python 主脚本矩阵必须完整进入最终交付面。
    *WRAPPER_SCRIPT_RELS,  # 三类 wrapper 入口矩阵必须与 Python 主脚本同步存在。
    *REMOTE_SHELL_RELS,  # 两份远程 shell 辅助脚本也属于安装后的运行面。
    "assets/evidence/non_gui_claims.json",  # 声明证据夹具供 claim gate 读取。
    "assets/minimal_vcs/top.sv",  # 最小 RTL 顶层夹具供本地 smoke dry-run 使用。
    "assets/minimal_vcs/coverage_top.sv",  # 覆盖率 smoke 夹具供 coverage 相关脚本使用。
    "assets/minimal_vcs/core.vhd",  # 混合语言夹具供 VHDL 路径验证使用。
    "assets/minimal_vcs/rtl.f",  # 文件列表夹具供 dry-run 计划验证使用。
    "assets/minimal_vcs/dump_ucli.tcl",  # UCLI 夹具供 FSDB 波形导出验证使用。
    "assets/minimal_vcs/manifest_matrix.json",  # manifest 矩阵夹具供多输入计划验证使用。
)  # 只要其中一项缺失，本地技能包就不完整。

# script-matrix-only 只关心 Python 主脚本矩阵，不重复覆盖独立 shell 辅助脚本。
SCRIPT_MATRIX = PYTHON_SCRIPT_RELS  # 该矩阵专门服务 script-matrix-only 审计模式。

# 列出能力矩阵文档必须覆盖的主题词，后续据此检查文档承诺是否缩水。
CAPABILITY_MATRIX_TERMS = (
    "environment probe",  # 首项要求能力矩阵至少覆盖环境探针主题。
    "vcs_bin",  # 第二项要求文档覆盖 VCS 二进制能力面。
    "npi",  # 需要说明 NPI/Verdi 相关依赖探测能力。
    "edalize",  # 需要说明与 edalize 相关的集成边界。
    "fsdb",  # 需要说明 FSDB 波形相关能力。
    "coverage",  # 需要说明覆盖率流程能力。
    "delivery",  # 需要说明交付执行证据维度。
    "truth",  # 需要说明事实执行证据维度。
    "regression",  # 需要说明流程回归能力。
    "import",  # 需要说明工程导入能力。
    "evidence",  # 需要说明证据收集能力。
    "claim",  # 需要说明声明门禁能力。
    "cocotb",  # 需要说明 cocotb 流程能力。
    "autoverifix",  # 需要说明 autoverifix 自动修复链路如何接入 VCS/Verdi。
    "aiss",  # 需要说明 aiss 项目接入技能后的落地流程。
    "remote eda",  # 需要说明远程 EDA 主机验证能力。
    "riscv-dv",  # 需要说明 riscv-dv 生成与仿真链路。
    "kvips",  # 需要说明 kvips 项目级流程能力。
    "fp-gen",  # 需要说明 fpgen 相关生成流程能力。
)  # 每个主题词都对应一类对外宣称能力，缺失时必须报错。

# 从用户 home 目录恢复工具路径，避免在仓库里硬编码具体用户名。
def _home_path(*tuple_parts: str) -> Path:
    """
    在当前用户 home 目录下拼接目标路径。

    :param tuple_parts: 需要依次拼接到 home 目录后的路径片段，dtype=tuple[str, ...]，unit=collection
    :return: 返回 home 目录下的目标路径对象，dtype=Path，unit=path
    """

    # 统一以用户 home 为根拼路径，兼容不同机器上的用户名差异。
    return Path.home().joinpath(*tuple_parts)

# 把 Path 转成命令列表可直接使用的字符串。
def _script_if_exists(path_script: Path) -> str:
    """
    将脚本路径转换成命令行字符串。

    :param path_script: 候选脚本路径对象，dtype=Path，unit=path
    :return: 返回适合拼入命令列表的字符串路径，dtype=str，unit=path
    """

    # 当前调用方只需要字符串路径，因此这里直接返回稳定文本表示。
    return str(path_script)

# 根据技能根目录、分组和脚本名恢复 Python 主脚本绝对路径。
def _script_path(path_skill_dir: Path, str_group: str, str_name: str) -> Path:
    """
    生成技能内 Python 主脚本的绝对路径。

    :param path_skill_dir: 技能根目录，dtype=Path，unit=path
    :param str_group: 脚本所属分组目录名，dtype=str，unit=identifier
    :param str_name: 脚本基础文件名，不含扩展名，dtype=str，unit=identifier
    :return: 返回目标 Python 主脚本的绝对路径对象，dtype=Path，unit=path
    """

    # 这里按技能固定目录布局拼路径，避免调用方重复手写目录片段。
    return path_skill_dir / "scripts" / "python" / str_group / f"{str_name}.py"

# 根据脚本名反查标准相对路径，便于 gate 步骤回填来源定位。
def _script_rel_for_name(str_name: str) -> str:
    """
    根据脚本基础名查回其相对路径。

    :param str_name: 脚本基础文件名，不含扩展名，dtype=str，unit=identifier
    :return: 返回匹配的 Python 主脚本相对路径，dtype=str，unit=path
    :raises KeyError: 当脚本名不在受管矩阵中时抛出异常。
    """

    # 顺序扫描脚本矩阵，查找目标脚本名。
    for str_group, str_candidate in SCRIPT_LAYOUT:

        # 只有名字匹配时才返回脚本相对路径。
        if str_candidate == str_name:

            # 命中后立即返回标准相对路径。
            return _python_script_rel(str_group, str_candidate)

    # 未命中受管脚本名时抛出显式错误，阻止上层默默使用错误路径。
    raise KeyError(f"> ERR: [Python] unknown script: {str_name}")

# 审计技能目录是否具备最小交付面，并验证 SKILL.md 的边界声明保持准确
def audit_skill(path_skill_dir: Path) -> JsonDict:
    """
    审计技能目录中的必需文件与关键文案边界。

    :param path_skill_dir: 需要审计的技能根目录，dtype=Path，unit=path
    :return: 返回审计状态、检查过的路径和错误列表组成的结构化对象，dtype=dict[str, Any]，unit=mapping
    """

    # 先把技能目录规整成绝对路径，确保后续所有存在性检查都基于稳定位置
    # 先把技能根目录规整成绝对路径，避免脚本矩阵审计受当前工作目录影响。
    # 这里先把技能根目录钉成绝对路径，避免外部脚本从不同 cwd 调用时把审计目标算偏。
    # 把审计根目录固定成绝对路径，避免外层脚本在切换 cwd 后把矩阵检查指错位置。
    # 这里先把技能根目录固定为绝对路径，防止仓库外部调用时把审计目标解析到错误位置。
    path_skill_dir = path_skill_dir.resolve()  # 后续 capability-matrix 与脚本矩阵都以这个绝对根目录为基准。

    # 使用独立列表累计已检查项，便于调用方在 JSON 报告里追溯审计覆盖面
    list_checked: list[str] = []  # 已逐项检查过的相对路径列表

    # 用错误列表集中记录缺件或文案问题，让调用方一次性拿到全部阻塞原因
    list_errors: list[str] = []  # 当前技能审计发现的错误列表

    # 逐项确认最小交付面文件是否存在，缺少任何关键文件都应阻断交付
    for str_rel in REQUIRED_SKILL_FILES:

        # 无论文件是否存在，都先把相对路径纳入 checked 列表，便于完整审计留痕
        list_checked.append(str_rel)

        # 当目标文件缺失时，补入带相对路径的错误文本，方便用户快速定位
        if not (path_skill_dir / str_rel).exists():

            # 使用固定错误文本说明缺少哪份必需资源
            list_errors.append(f"missing required file: {str_rel}")

    # 进一步读取 SKILL.md，验证技能名与非 GUI 能力边界的关键承诺没有丢失
    path_skill_md = path_skill_dir / "SKILL.md"  # 技能入口说明文件路径

    # 先准备技能名占位，确保即便 SKILL.md 缺失也能返回稳定字段
    str_skill_name = ""  # 从 SKILL.md 解析得到的技能名

    # 只有 SKILL.md 存在时才尝试读取并做更细的文案审计
    if path_skill_md.exists():

        # 读取技能说明全文，后续既要抽取 name，也要做边界词检查
        str_text = path_skill_md.read_text(encoding="utf-8")  # SKILL.md 的完整文本内容

        # 按行扫描 ``name:`` 字段，保持与现有 metadata 约定一致
        for str_line in str_text.splitlines():

            # 命中 name 行后直接截取冒号右侧内容，避免引入额外 YAML 依赖
            if str_line.startswith("name:"):

                # 把命中的 name 字段保存为审计结果里的技能名。
                str_skill_name = str_line.split(":", 1)[1].strip()  # 该值稍后会直接回写到审计输出对象。

                # 找到技能名后即可退出循环，避免后续重复覆盖结果
                break

        # 技能名必须与仓库契约完全一致，否则说明元数据已漂移
        if str_skill_name != "vcs-verdi-developer":

            # 返回精确错误，帮助定位 SKILL.md 头部元数据不匹配问题
            list_errors.append("SKILL.md name must be vcs-verdi-developer")

        # 后续边界词检查统一在小写文本上进行，避免大小写差异导致误判
        str_lowered = str_text.lower()  # 统一为小写后的 SKILL.md 文本

        # 找出当前文本中缺失的范围边界短语，用来判断技能是否夸大 GUI 覆盖面
        list_missing_scope_terms = [str_term for str_term in NON_GUI_SCOPE_TERMS if str_term not in str_lowered]  # SKILL.md 中遗漏的关键范围边界短语

        # 只要边界词缺失，就说明当前技能说明没有明确声明非 GUI 脚本化边界
        if list_missing_scope_terms:

            # 保留历史测试契约中的固定错误文本，避免细节文案变化打断调用方
            list_errors.append("SKILL.md must state the non-GUI scripted scope and official-option boundary")

        # 额外检查技能文案是否明确否认“覆盖全部官方 Synopsys 选项”的夸大说法
        if "complete coverage of every official synopsys" not in str_lowered:

            # 这条错误专门约束技能不能对 Synopsys 官方选项覆盖面做过度承诺
            list_errors.append("SKILL.md must explicitly avoid claiming all official Synopsys options")

    # 返回结构化审计结果，供 --audit-only 和测试用例直接消费
    return {
        "status": "passed" if not list_errors else "failed",
        "skill_dir": str(path_skill_dir),
        "skill_name": str_skill_name,
        "checked": list_checked,
        "errors": list_errors,
    }

# 审计 Python 主脚本矩阵和 capability-matrix 文档是否同时覆盖关键脚本与主题词。
def script_matrix_audit(path_skill_dir: Path) -> JsonDict:
    """
    审计脚本矩阵与能力矩阵文档的一致性。

    :param path_skill_dir: 需要审计的技能根目录，dtype=Path，unit=path
    :return: 返回脚本矩阵审计状态、检查项和错误列表，dtype=dict[str, Any]，unit=mapping
    """

    # 在脚本矩阵审计里先锁定技能根目录，确保 capability-matrix 与脚本路径都从同一绝对位置解析。
    path_skill_dir = path_skill_dir.resolve()  # 待审计技能根目录的绝对路径

    # 使用独立错误列表记录脚本矩阵与文档矩阵中的所有阻塞项
    list_errors: list[str] = []  # 当前 script-matrix 审计发现的错误列表

    # 单独记录检查覆盖面，便于调用方或测试断言审计项完整性
    list_checked: list[str] = []  # 当前 script-matrix 审计遍历过的路径列表

    # 先验证 Python 主脚本矩阵是否完整，缺任何一项都说明主能力面不完整
    for str_rel in SCRIPT_MATRIX:

        # 先登记当前检查项，便于 JSON 输出完整反映覆盖范围
        list_checked.append(str_rel)

        # 目标脚本缺失时直接记录对应错误，帮助定位矩阵缺口
        if not (path_skill_dir / str_rel).exists():

            # 缺失 Python 主脚本时使用固定错误格式，便于测试与上游脚本匹配
            list_errors.append(f"missing script matrix entry: {str_rel}")

    # 再确认三种 wrapper 族的脚本都已补齐，否则本地安装后的多入口体验会不完整
    for str_rel in WRAPPER_SCRIPT_RELS:

        # wrapper 也要纳入 checked 列表，保持完整审计留痕
        list_checked.append(str_rel)

        # wrapper 缺失时追加对应错误，便于直接定位是 shell/bat/ps1 哪一份漏掉了
        if not (path_skill_dir / str_rel).exists():

            # 使用固定错误格式说明 wrapper 入口不完整
            list_errors.append(f"missing script wrapper entry: {str_rel}")

    # capability-matrix.md 既要存在，也要覆盖声明的关键主题词
    path_matrix = path_skill_dir / "references" / "capability-matrix.md"  # 能力矩阵文档路径

    # 无论存在与否，都把能力矩阵文档路径纳入检查列表，便于结果可追踪
    list_checked.append("references/capability-matrix.md")

    # 文档缺失时无需继续做主题词扫描，直接登记错误即可
    if not path_matrix.exists():

        # 能力矩阵是外部承诺文件，缺失时必须阻断交付
        list_errors.append("missing capability matrix")

    # 文档存在时，继续扫描主题词和边界声明是否齐备
    else:

        # 读取并统一成小写文本，方便关键主题词做大小写无关匹配
        str_text = path_matrix.read_text(encoding="utf-8").lower()  # 小写化后的能力矩阵全文

        # 逐个确认文档是否覆盖关键主题，避免文档更新时漏掉某个能力域
        for str_term in CAPABILITY_MATRIX_TERMS:

            # 缺失主题词时登记错误，帮助后续审查快速补齐能力矩阵描述
            if str_term not in str_text:

                # 使用固定错误格式给出缺失的主题词
                list_errors.append(f"capability matrix missing topic: {str_term}")

        # 文档同样必须保留“不覆盖全部官方选项”的边界文案，防止对外承诺失真
        if "complete coverage of every official synopsys" not in str_text:

            # 这条错误专门约束能力矩阵文档不得夸大官方选项覆盖面
            list_errors.append("capability matrix must keep the official-option boundary explicit")

    # 这里返回的是脚本矩阵与文档矩阵的一致性快照，便于 dry-run 与单测直接断言。
    return {
        "status": "passed" if not list_errors else "failed",
        "skill_dir": str(path_skill_dir),
        "checked": list_checked,
        "errors": list_errors,
    }

# 维护 flow fixture 汇总单测列表，避免在步骤构造函数里堆大段字符串表。
FLOW_FIXTURE_TESTS = (
    "tests.test_cocotb_vcs_flow",  # 首项覆盖 cocotb 集成流程夹具。
    "tests.test_autoverifix_vcs_flow",  # 第二项覆盖 autoverifix 流程夹具。
    "tests.test_kvips_vcs_flow",  # 第三项覆盖 kvips 流程夹具。
    "tests.test_riscv_dv_flow",  # 第四项覆盖 riscv-dv 流程夹具。
    "tests.test_fpgen_vcs_flow",  # 第五项覆盖 fpgen 流程夹具。
    "tests.test_aiss_vcs_flow",  # 第六项覆盖 aiss 流程夹具。
)  # 这些测试共同覆盖技能里所有流程型 Python 入口。

# 构造统一的步骤字典，减少本地 gate 计划里的重复键结构。
def _gate_step(
    str_name: str,
    list_cmd: list[str],
    path_cwd: Path,
    *,
    bool_required: bool,
    dict_json_contains: JsonDict | None = None,
) -> JsonDict:
    """
    构造单个质量门步骤定义。

    :param str_name: 步骤名，dtype=str，unit=identifier
    :param list_cmd: 子进程命令列表，dtype=list[str]，unit=collection
    :param path_cwd: 步骤执行目录，dtype=Path，unit=path
    :param bool_required: 当前步骤是否为必需步骤，dtype=bool，unit=flag
    :param dict_json_contains: 可选的 JSON 断言字典，dtype=dict[str, Any] | None，unit=mapping
    :return: 返回 run_step 可直接消费的步骤字典，dtype=dict[str, Any]，unit=mapping
    """

    # 先准备基础步骤结构，统一保存命令、目录和必需性。
    dict_step = {  # 该对象会直接进入本地质量门计划。
        "name": str_name,  # 步骤名称供输出与测试断言共同使用。
        "cmd": list_cmd,  # 命令列表会直接传入 subprocess.run。
        "cwd": str(path_cwd),  # 执行目录统一序列化成字符串，便于 JSON 输出。
        "required": bool_required,  # 必需性会决定失败后是否提前中止。
    }

    # 只有显式声明 JSON 断言时才补入 json_contains 键。
    if dict_json_contains:

        # 把结构化断言挂入步骤定义，供 run_step 在执行后校验。
        dict_step["json_contains"] = dict_json_contains  # 该键会在 run_step 里触发 JSON 路径断言。

    # 返回统一格式的步骤对象，方便后续集中执行。
    return dict_step

# 构造本地质量门的基础步骤，优先覆盖单测、技能结构、治理文档和环境探针。
def _base_local_gate_steps(
    path_repo_root: Path,
    *,
    path_skill_dir: Path,
    path_quick_validate: Path,
    path_agents_tools: Path,
) -> list[JsonDict]:
    """
    生成本地质量门的基础步骤列表。

    :param path_repo_root: 仓库根目录，dtype=Path，unit=path
    :param path_skill_dir: 技能根目录，dtype=Path，unit=path
    :param path_quick_validate: 系统 quick_validate.py 路径，dtype=Path，unit=path
    :param path_agents_tools: agents-md-generator 脚本目录，dtype=Path，unit=path
    :return: 返回基础步骤对象列表，dtype=list[dict[str, Any]]，unit=collection
    """

    # 公开发布仓库移除 tests/ 后，历史单测不再是默认公开 payload 的硬依赖。
    path_tests_dir = path_repo_root / "tests"  # 仍保留 tests/ 的仓库会继续执行完整单测集。

    # 基础步骤先验证仓库行为、技能结构与治理约束，避免后续功能 gate 建立在坏基线上。
    list_steps = [
        _gate_step(
            "unit_tests",
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            path_repo_root,
            bool_required=path_tests_dir.is_dir(),
        ),

        # 第二步开始检查技能结构与治理文件，避免功能 gate 在坏基线上继续执行。
        _gate_step(
            "skill_quick_validate",
            [sys.executable, _script_if_exists(path_quick_validate), str(path_skill_dir)],
            path_repo_root,
            bool_required=path_quick_validate.exists(),
        ),
        _gate_step(
            "agents_verify",
            [sys.executable, _script_if_exists(path_agents_tools / "verify_agents.py"), str(path_repo_root)],
            path_repo_root,
            bool_required=(path_agents_tools / "verify_agents.py").exists(),
        ),
        _gate_step(
            "docs_verify",
            [sys.executable, _script_if_exists(path_agents_tools / "manage_docs.py"), "verify", str(path_repo_root)],
            path_repo_root,
            bool_required=(path_agents_tools / "manage_docs.py").exists(),
        ),

        # 最后一项基础步骤专门确认本地环境探针自身可以稳定输出 JSON 结论。
        _gate_step(
            "env_probe",
            [sys.executable, str(_script_path(path_skill_dir, "env", "check_env")), "--json"],
            path_repo_root,
            bool_required=True,
        ),
    ]

    return list_steps

# 构造本地质量门的功能性步骤，覆盖 dry-run、夹具单测和非 GUI 声明证据。
def _flow_local_gate_steps(
    path_repo_root: Path,
    *,
    path_skill_dir: Path,
    path_validation_script: Path,
    path_smoke_source: Path, path_manifest_matrix: Path,
    path_evidence_claims: Path,
) -> list[JsonDict]:
    """
    生成本地质量门的功能性步骤列表。

    :param path_repo_root: 仓库根目录，dtype=Path，unit=path
    :param path_skill_dir: 技能根目录，dtype=Path，unit=path
    :param path_validation_script: 主验证脚本路径，dtype=Path，unit=path
    :param path_smoke_source: 最小 RTL smoke 夹具路径，dtype=Path，unit=path
    :param path_manifest_matrix: manifest 矩阵夹具路径，dtype=Path，unit=path
    :param path_evidence_claims: 非 GUI 声明证据路径，dtype=Path，unit=path
    :return: 返回功能性步骤对象列表，dtype=list[dict[str, Any]]，unit=collection
    """

    # 公开发布仓库移除 tests/ 后，历史 fixture 单测会退到私有验证区，不再是公开门禁必跑项。
    path_tests_dir = path_repo_root / "tests"  # 仍保留公开 tests/ 时继续执行 fixture suite。

    # 功能性步骤专门验证技能的主流程入口、夹具矩阵和非 GUI 能力声明没有漂移。
    list_steps = [
        _gate_step(
            "smoke_dry_run",
            [
                sys.executable,
                str(path_validation_script),
                "--dry-run",
                "--json",
                "--source",
                str(path_smoke_source),
                "--workdir",
                str(path_repo_root / "build" / "vcs-verdi-smoke"),
                "--top",
                "top",
                "--dump-name",
                "waves.fsdb",
                "--verdi-check",
                "fsdbreport",
                "--report-signal",
                "/top/clk",
            ],
            path_repo_root,
            bool_required=True,
        ),

        # 第二项把 manifest 驱动路径单独拉出来，避免最小 smoke 覆盖不到多输入矩阵。
        _gate_step(
            "manifest_matrix_dry_run",
            [
                sys.executable,
                str(path_validation_script),
                "--manifest",
                str(path_manifest_matrix),
                "--workdir",
                str(path_repo_root / "build" / "vcs-verdi-manifest-matrix"),
                "--dry-run",
                "--json",
            ],
            path_repo_root,
            bool_required=True,
            dict_json_contains={"plan.plusargs": "+testcase=smoke"},
        ),

        # 接下来两项 fixture suite 分别覆盖导入链路和项目流程链路的 unittest 回归。
        _gate_step(
            "import_fixture_suite",
            [sys.executable, "-m", "unittest", "tests.test_import_vcs_project", "-v"],
            path_repo_root,
            bool_required=path_tests_dir.is_dir(),
        ),
        _gate_step(
            "flow_fixture_suites",
            [sys.executable, "-m", "unittest", *FLOW_FIXTURE_TESTS, "-v"],
            path_repo_root,
            bool_required=path_tests_dir.is_dir(),
        ),

        # 最后一项检查非 GUI 声明证据，确保能力描述没有越过技能真实执行面。
        _gate_step(
            "claim_evidence_gate",
            [
                sys.executable,
                str(_script_path(path_skill_dir, "evidence", "evidence_claim_gate")),
                "--claims-json",
                str(path_evidence_claims),
                "--json",
            ],
            path_repo_root,
            bool_required=path_evidence_claims.exists(),
            dict_json_contains={"status": "passed"},
        ),
    ]

    return list_steps

# 构造本地质量门的自审计步骤，最终回查脚本矩阵和技能交付面。
def _audit_local_gate_steps(path_repo_root: Path, *, path_quality_script: Path) -> list[JsonDict]:
    """
    生成本地质量门的自审计步骤列表。

    :param path_repo_root: 仓库根目录，dtype=Path，unit=path
    :param path_quality_script: 当前质量门脚本自身路径，dtype=Path，unit=path
    :return: 返回自审计步骤对象列表，dtype=list[dict[str, Any]]，unit=collection
    """

    # 自审计步骤最后回调当前脚本自身，确保脚本矩阵与技能交付面最终闭环。
    return [
        _gate_step(
            "script_matrix_audit",
            [sys.executable, str(path_quality_script), "--script-matrix-only", "--json"],
            path_repo_root,
            bool_required=True,
        ),
        _gate_step(
            "local_skill_audit",
            [sys.executable, str(path_quality_script), "--audit-only", "--json"],
            path_repo_root,
            bool_required=True,
        ),
    ]

# 构造本地质量门的执行计划，覆盖单元测试、冒烟 dry-run、导入夹具和技能自审计步骤。
def build_local_gate(path_repo_root: Path, *, skill_dir: Path | None = None) -> JsonDict:
    """
    构建 vcs-verdi-developer 的本地质量门执行计划。

    :param path_repo_root: 仓库根目录，dtype=Path，unit=path
    :param skill_dir: 可选的技能根目录；为空时按当前仓库布局推导默认位置，dtype=Path | None，unit=path
    :return: 返回包含 repo_root、skill_dir 和步骤列表的计划对象，dtype=dict[str, Any]，unit=mapping
    """

    # 先规整仓库根目录，避免后续命令混入相对路径歧义。
    path_repo_root = path_repo_root.resolve()  # 所有步骤默认在该仓库根目录下执行。

    # 调用方不传 skill_dir 时，默认回退到仓库里的标准技能目录。
    path_skill_dir = (skill_dir or path_repo_root / "skills" / "vcs-verdi-developer").resolve()  # 本地 gate 实际审计的技能根目录。

    # 定位 quick_validate.py，供技能包结构做快速一致性检查。
    path_quick_validate = _home_path(  # 该路径指向系统 quick_validate.py。
        ".codex",  # 用户主目录下的 Codex 技能根目录。
        "skills",  # 进入技能安装目录。
        ".system",  # 进入系统技能命名空间。
        "skill-creator",  # 定位提供 quick_validate.py 的系统技能。
        "scripts",  # 进入脚本子目录。
        "quick_validate.py",  # 最终定位质量门使用的 quick_validate.py。
    )

    # 定位 agents-md-generator 脚本目录，后续会调用 verify_agents 和 manage_docs。
    path_agents_tools = _home_path(".codex", "skills", "agents-md-generator", "scripts")  # 文档与 AGENTS 门禁都从这里发起。

    # 记录最小冒烟默认使用的 RTL 入口文件。
    path_smoke_source = path_skill_dir / "assets" / "minimal_vcs" / "top.sv"  # smoke_dry_run 会把它作为 --source 输入。

    # 记录 manifest 矩阵夹具路径，后续 dry-run 会验证多输入配置形状。
    path_manifest_matrix = path_skill_dir / "assets" / "minimal_vcs" / "manifest_matrix.json"  # 该夹具用于覆盖 plusargs 和多语言 manifest。

    # 记录非 GUI 声明证据路径，claim_evidence_gate 会从这里读取 claims。
    path_evidence_claims = path_skill_dir / "assets" / "evidence" / "non_gui_claims.json"  # 该证据文件决定声明门禁能否通过。

    # 记录当前质量门脚本自身路径，后续自审计步骤会回调它。
    path_quality_script = _script_path(path_skill_dir, "quality", "run_quality_gate")  # script_matrix_audit 和 local_skill_audit 都复用它。

    # 记录验证脚本路径，两个 dry-run 步骤都通过它构造执行计划。
    path_validation_script = _script_path(path_skill_dir, "validation", "vcs_verdi_check")  # 统一使用同一验证入口可减少流程漂移。

    # 先放入基础门禁步骤，后续再按执行顺序继续追加功能 gate 和自审计步骤。
    list_steps = _base_local_gate_steps(  # 当前执行计划的基础步骤列表。
        path_repo_root,  # 当前 gate 计划统一绑定的仓库根目录。
        path_skill_dir=path_skill_dir,  # 本地质量门实际审计的技能根目录。
        path_quick_validate=path_quick_validate,  # 系统 quick_validate.py 的绝对路径。
        path_agents_tools=path_agents_tools,  # AGENTS 与 docs 治理脚本目录。
    )

    # 先独立计算功能 gate 步骤列表，避免长参数调用直接挤在 list_steps.extend 里形成密集代码块。
    list_flow_steps = _flow_local_gate_steps(  # 中段功能 gate 的步骤列表。
        path_repo_root,  # 当前功能 gate 仍然共享同一个仓库根目录上下文。
        path_skill_dir=path_skill_dir,  # 这里继续沿用 build_local_gate 已解析出的技能根目录。

        # 下面两项决定最小 smoke dry-run 使用的验证入口与 RTL 夹具。
        path_validation_script=path_validation_script,  # 主验证脚本路径。
        path_smoke_source=path_smoke_source,  # 最小 RTL smoke 夹具路径。

        # 最后两项决定 manifest dry-run 和声明门禁读取的输入证据。
        path_manifest_matrix=path_manifest_matrix,  # manifest 矩阵夹具路径。
        path_evidence_claims=path_evidence_claims,  # 非 GUI 声明证据路径。
    )

    # 中段专门承接功能 dry-run、夹具单测和声明证据门禁。
    list_steps.extend(list_flow_steps)

    # 最后一段回调当前脚本做交付面和脚本矩阵自审计。
    list_steps.extend(_audit_local_gate_steps(path_repo_root, path_quality_script=path_quality_script))

    # 返回完整计划对象，供 dry-run 输出和真实执行共用同一份定义。
    return {
        "repo_root": str(path_repo_root),
        "skill_dir": str(path_skill_dir),
        "steps": list_steps,
    }

# 统一封装步骤结果对象，避免 run_step 在 return 位置堆叠大块键值表。
def _step_result_payload(
    dict_step: JsonDict,
    *,
    int_returncode: int,
    str_status: str, list_json_errors: list[str] | None = None,
    str_stdout: str,
    str_stderr: str,
) -> JsonDict:
    """
    构造单个质量门步骤的结构化结果对象。

    :param dict_step: 原始步骤定义对象，dtype=dict[str, Any]，unit=mapping
    :param int_returncode: 子进程返回码，dtype=int，unit=exit code
    :param str_status: 步骤状态，通常是 ``passed``、``failed`` 或 ``skipped``，dtype=str，unit=identifier
    :param list_json_errors: 可选的 JSON 语义错误列表；为空时回退到空列表，dtype=list[str] | None，unit=collection
    :param str_stdout: 子进程 stdout 文本，dtype=str，unit=text
    :param str_stderr: 子进程 stderr 文本，dtype=str，unit=text
    :return: 返回 run_local_gate 可直接汇总的步骤结果对象，dtype=dict[str, Any]，unit=mapping
    """

    # 这里统一补齐 json_errors 字段，保证 passed/skipped/failed 三种分支输出形状一致。
    return {
        **dict_step,
        "returncode": int_returncode,
        "status": str_status,
        "json_errors": list_json_errors or [],
        "stdout": str_stdout,
        "stderr": str_stderr,
    }

# 执行单个计划步骤，并结合 JSON 语义检查生成最终 passed/failed 结论。
def run_step(dict_step: JsonDict, *, timeout: int) -> JsonDict:
    """
    执行单个质量门步骤。

    :param dict_step: 单个步骤定义对象，至少包含 name、cmd、cwd 和 required 字段，dtype=dict[str, Any]，unit=mapping
    :param timeout: 当前步骤的子进程超时秒数，dtype=int，unit=second
    :return: 返回带执行结果、stdout/stderr 和 JSON 检查错误的步骤结果对象，dtype=dict[str, Any]，unit=mapping
    """

    # 可选步骤依赖缺失时直接跳过，避免把环境差异误报成技能逻辑失败。
    if not dict_step.get("required", True):

        # 返回 skipped 结果，让上游仍能看到完整步骤形状。
        return _step_result_payload(
            dict_step,
            int_returncode=0,
            str_status="skipped",
            str_stdout="",
            str_stderr="optional tool missing",
        )

    # 启动子进程并保留 stdout/stderr，后续既要做 JSON 校验也要保留故障现场。
    completed_process_step: subprocess.CompletedProcess[str] = subprocess.run(  # 该对象保存子进程返回码和双通道文本。
        dict_step["cmd"],  # 实际执行的命令列表来自当前步骤定义。
        cwd=dict_step["cwd"],  # 子进程会在步骤声明的工作目录内执行。
        text=True,  # 打开文本模式，便于后续 JSON 解析与错误摘要输出。
        stdout=subprocess.PIPE,  # 标准输出会进入结构化结果对象。
        stderr=subprocess.PIPE,  # 标准错误同样要保留给最终诊断使用。
        timeout=timeout,  # 每个步骤统一遵守外层传入的超时上限。
    )  # 当前步骤对应的子进程执行结果

    # 汇总 JSON 语义层面的失败原因，稍后会并入最终步骤结果。
    list_json_errors: list[str] = []  # 该列表专门记录 errors 字段和 json_contains 断言失败。

    # 尝试把 stdout 解析成 JSON 对象，解析失败时只回退为空字典。
    try:

        # 很多子步骤承诺 ``--json`` 协议，因此这里优先解析结构化结果。
        dict_parsed_json = json.loads(completed_process_step.stdout)  # 解析成功时得到步骤 stdout 的 JSON 根对象。

    # stdout 不是 JSON 时无需额外报错，只保留空对象让 returncode 决定基础状态。
    except json.JSONDecodeError:

        # 非 JSON stdout 不参与结构化断言，因此在这里回退为空字典。
        dict_parsed_json = {}  # 这表示当前 stdout 不能作为 JSON 对象继续分析。

    # 子步骤主动上报 errors 字段时，要把它们转成字符串并纳入失败原因。
    if isinstance(dict_parsed_json, dict) and dict_parsed_json.get("errors"):

        # 统一拉平 errors 字段，方便上游直接显示或断言。
        list_json_errors = [str(obj_error) for obj_error in dict_parsed_json.get("errors", [])]  # 每项都转换成稳定字符串。

    # 解析结果确实是对象时，再校验调用方声明的 json_contains 断言。
    if isinstance(dict_parsed_json, dict):

        # 把字段路径断言失败继续追加到现有错误列表。
        list_json_errors.extend(_json_expectation_errors(dict_parsed_json, dict_step.get("json_contains", {})))

    # 同时满足返回码成功且没有 JSON 语义错误时，步骤才算真正通过。
    str_status = "passed" if completed_process_step.returncode == 0 and not list_json_errors else "failed"  # 该状态会决定 run_local_gate 是否提前终止。

    # 返回统一封装后的步骤结果，让上层保留命令、状态和原始输出证据。
    return _step_result_payload(
        dict_step,
        int_returncode=completed_process_step.returncode,
        str_status=str_status,
        list_json_errors=list_json_errors,
        str_stdout=completed_process_step.stdout,
        str_stderr=completed_process_step.stderr,
    )

# 读取嵌套 JSON 路径的值，供 json_contains 断言逻辑复用。
def _json_path_value(dict_data: JsonDict, str_path: str) -> Any:
    """
    从嵌套字典中按点分路径读取值。

    :param dict_data: 待读取的 JSON 风格对象，dtype=dict[str, Any]，unit=mapping
    :param str_path: 形如 ``plan.plusargs`` 的点分字段路径，dtype=str，unit=identifier path
    :return: 命中路径时返回对应值；任一路径段缺失时返回 None，dtype=Any，unit=object
    """

    # 使用动态游标沿着点分路径逐层下钻，直到命中目标字段或遇到缺失段
    obj_value: Any = dict_data  # 当前路径游标指向的对象

    # 逐段解析点分路径，每次都要求当前游标仍是字典且包含对应字段
    for str_part in str_path.split("."):

        # 任一路径段缺失或游标不再是字典时，直接返回 None 表示断言无法命中
        if not isinstance(obj_value, dict) or str_part not in obj_value:

            # 当前路径不存在，因此返回空值供调用方判断断言失败
            return None

        # 命中当前字段后，把游标推进到该字段对应的下一层值。
        obj_value = obj_value[str_part]  # 后续路径段都会基于这个新游标继续解析。

    # 所有路径段都命中后，返回最终读取到的对象值。
    return obj_value

# 对解析后的 JSON 对象执行字段期望断言，并返回所有失败信息。
def _json_expectation_errors(dict_parsed: JsonDict, dict_expectations: JsonDict) -> list[str]:
    """
    检查 JSON 对象是否满足字段值期望。

    :param dict_parsed: 已解析的 JSON 风格对象，dtype=dict[str, Any]，unit=mapping
    :param dict_expectations: 键为点分路径、值为期望值的断言映射，dtype=dict[str, Any]，unit=mapping
    :return: 返回所有不满足期望的错误文本列表，dtype=list[str]，unit=collection
    """

    # 使用独立错误列表承接路径断言失败信息，便于 run_step 与测试直接消费
    list_errors: list[str] = []  # JSON 字段值断言失败信息列表

    # 逐项校验期望路径和值，兼容“字段值是列表且必须包含某元素”的场景
    for str_path, obj_expected in dict_expectations.items():

        # 先按路径取出当前实际值，后续再根据值形态决定比较方式。
        value_actual = _json_path_value(dict_parsed, str_path)  # 这是当前路径在 JSON 对象中的真实内容。

        # 实际值是列表时，当前断言语义变成“列表必须包含目标元素”。
        if isinstance(value_actual, list):

            # 缺少期望元素时登记断言失败，便于上游快速定位遗漏字段。
            if obj_expected not in value_actual:

                # 保持测试依赖的错误文案格式，便于上游断言和人工排查。
                list_errors.append(f"{str_path} missing expected value {obj_expected!r}")

        # 其余类型按普通相等判断，保持 JSON 断言语义直接可读。
        elif value_actual != obj_expected:

            # 把期望值和实际值同时写入错误文本，便于复盘具体差异。
            list_errors.append(f"{str_path} expected {obj_expected!r}, got {value_actual!r}")

    # 返回所有路径断言失败信息，供 run_step 汇总为步骤失败原因。
    return list_errors

# 执行完整本地质量门计划，并在首个 required 失败步骤后停止。
def run_local_gate(path_repo_root: Path, *, skill_dir: Path | None = None, timeout: int = 300) -> JsonDict:
    """
    执行完整的本地质量门计划。

    :param path_repo_root: 仓库根目录，dtype=Path，unit=path
    :param skill_dir: 可选的技能根目录覆盖值，dtype=Path | None，unit=path
    :param timeout: 每个步骤的执行超时秒数，dtype=int，unit=second
    :return: 返回包含计划、步骤结果和置信度分类的结构化对象，dtype=dict[str, Any]，unit=mapping
    """

    # 先构建完整计划，保证 dry-run 与真实执行共享同一份步骤定义
    json_dict_plan = build_local_gate(path_repo_root, skill_dir=skill_dir)  # 当前本地质量门的完整执行计划

    # 使用列表按顺序承接已执行步骤结果，便于后续停止策略和汇总结论复用
    list_results: list[JsonDict] = []  # 已执行步骤的结构化结果列表

    # 依次执行每个 gate 步骤，并在首个 required 失败步骤后立即停止
    for dict_step in json_dict_plan["steps"]:

        # 当前步骤的执行结果会保留 stdout/stderr 和 JSON 错误，供上层直接消费。
        json_dict_result = run_step(dict_step, timeout=timeout)  # 当前步骤执行得到的结构化结果。

        # 无论步骤成功、失败还是 skipped，都先把结果收集起来。
        list_results.append(json_dict_result)

        # required 步骤失败后继续执行没有意义，因此在这里尽早中止计划。
        if json_dict_result["status"] == "failed" and dict_step.get("required", True):

            # 保留首个失败现场后退出循环，避免后续步骤噪声掩盖根因。
            break

    # 只有全部步骤都执行完且没有 required 失败时，整个本地 gate 才算通过。
    bool_all_steps_passed = all(  # 该布尔值同时要求每一步都是 passed/skipped。
        json_dict_item["status"] in {"passed", "skipped"} for json_dict_item in list_results  # 逐项核对每个步骤是否至少达到通过或跳过状态。
    )

    # 计划中若有步骤因为 required 失败而提前中止，这里会把整体状态压回 failed。
    if bool_all_steps_passed and len(list_results) == len(json_dict_plan["steps"]):

        # 计划已完整执行且每步状态都合格，因此整体状态可判为 passed。
        str_status = "passed"  # 当前本地质量门计划的整体状态。

    # 只要出现提前中止或失败步骤，就必须把总状态留在 failed。
    else:

        # 这里显式保留 failed，方便调用方区分“全部步骤跑完且通过”和“中途短路或已有失败”两类结果。
        str_status = "failed"  # 只要计划未完整通过，就用 failed 固定表达本地 gate 未达标。

    # 先汇总计划和步骤结果，再在同一个对象上补齐置信度字段。
    dict_gate_output = {
        **json_dict_plan,  # 先保留原始计划元数据，便于后续输出能追溯执行上下文。
        "results": list_results,  # 这里保留按顺序采集的步骤结果列表。
        "status": str_status,  # 这里保留本地 gate 的总体通过/失败状态。
    }  # 这是 run_local_gate 的基础结构化输出对象。

    # 基于当前执行结果派生 local、delivery 和 truth 三类置信度。
    dict_gate_output.update(classify_confidence(dict_gate_output))

    # 返回带置信度字段的完整本地 gate 输出对象。
    return dict_gate_output

# 从步骤结果的 stdout 中提取 JSON 对象，便于 classify_confidence 按需读取 missing_tools 等字段。
def _json_from_step(dict_step: JsonDict) -> JsonDict:
    """
    从步骤结果的 stdout 中解析 JSON 对象。

    :param dict_step: 单个步骤结果对象，dtype=dict[str, Any]，unit=mapping
    :return: 成功解析时返回 JSON 对象；解析失败时返回空字典，dtype=dict[str, Any]，unit=mapping
    """

    # 步骤 stdout 可能为空或非 JSON，这里统一先取文本再做解析尝试
    str_text = dict_step.get("stdout", "")  # 当前步骤 stdout 文本

    # 把 stdout 当作 JSON 解析成功时，后续调用方可以直接读取结构化字段
    try:

        # 仅当 stdout 是完整 JSON 时，这里才会得到可继续分析的对象。
        dict_parsed_json = json.loads(str_text)  # 成功时保存当前步骤 stdout 的 JSON 根对象。

    # 非 JSON stdout 并不属于当前 helper 的错误，只需回退空对象即可
    except json.JSONDecodeError:

        # 返回空字典让上层继续按“未提供结构化字段”的分支处理
        return {}

    # 顶层确实是对象时才返回；其余 JSON 根类型对当前调用方没有帮助。
    return dict_parsed_json if isinstance(dict_parsed_json, dict) else {}

# 生成可执行的远程 gate 模块导入规格，并在缺失 loader 时尽早失败。
def _remote_gate_spec(path_gate: Path) -> ModuleSpec:
    """
    根据远程门禁脚本路径生成可靠的导入规格。

    :param path_gate: 远程门禁脚本路径，dtype=Path，unit=path
    :return: 返回带有效 loader 的模块导入规格，dtype=ModuleSpec，unit=object
    :raises RuntimeError: 当目标脚本无法生成可执行导入规格时抛出显式错误。
    """

    # 这里直接构造带 loader 的模块规格，后续会据此执行 remote_eda_gate.py。
    return ModuleSpec("remote_eda_gate", SourceFileLoader("remote_eda_gate", str(path_gate)))

# 读取远程 EDA 证据文件，并调用 remote_eda_gate.py 中的验证函数生成 delivery/truth 双结论。
def load_remote_evidence(path_evidence: Path, *, max_age_hours: int | None) -> JsonDict:
    """
    读取并验证远程 EDA 证据文件。

    :param path_evidence: 远程证据 JSON 文件路径，dtype=Path，unit=path
    :param max_age_hours: 允许的证据最大时效小时数；为空时由下游验证器自行决定，dtype=int | None，unit=hour
    :return: 返回同时包含 delivery 与 truth 验证结果的结构化对象，dtype=dict[str, Any]，unit=mapping
    :raises RuntimeError: 当远程门禁脚本无法加载时抛出显式错误。
    """

    # 先定位 remote_eda_gate.py，后续会按文件路径动态导入它。
    path_gate = Path(__file__).resolve().parents[1] / "remote" / "remote_eda_gate.py"  # 远程 EDA 门禁脚本与当前文件同属一个技能目录。

    # 先拿到带有效 loader 的导入规格，避免在本函数里重复 loader 判空逻辑。
    module_spec_gate = _remote_gate_spec(path_gate)  # 该规格已经保证具备可执行 loader。

    # 根据导入规格实例化模块对象，稍后会把脚本代码执行进这个命名空间。
    module_type_remote_gate: ModuleType = importlib.util.module_from_spec(module_spec_gate)  # delivery 和 truth 验证函数都会从这里暴露出来。

    # 执行模块加载，让远程门禁脚本里的验证函数真正变成可调用对象。
    module_spec_gate.loader.exec_module(module_type_remote_gate)

    # 读取并解析远程证据 JSON，后续会分别走 delivery 和 truth 两条验证路径。
    dict_evidence_json = json.loads(path_evidence.read_text(encoding="utf-8"))  # 该对象会被直接传给 remote_eda_gate 的验证函数。

    # 返回双通路验证结果，保持与 classify_confidence 期望的 remote_evidence 结构一致。
    return {
        "delivery": module_type_remote_gate.validate_delivery_evidence(
            dict_evidence_json,
            max_age_hours=max_age_hours,
        ),
        "truth": module_type_remote_gate.validate_evidence(
            dict_evidence_json,
            max_age_hours=max_age_hours,
            mode="truth",
        ),
    }

# 从 remote_evidence 对象中安全读取指定 gate，避免非 dict 形态污染后续置信度判断。
def _extract_remote_gate(dict_remote: JsonDict, str_name: str) -> JsonDict:
    """
    安全读取远程证据中的指定 gate 对象。

    :param dict_remote: remote_evidence 顶层对象，dtype=dict[str, Any]，unit=mapping
    :param str_name: 需要读取的 gate 名称，例如 ``delivery`` 或 ``truth``，dtype=str，unit=identifier
    :return: 返回命中的 gate 对象；字段缺失或不是对象时返回空字典，dtype=dict[str, Any]，unit=mapping
    """

    # 先从顶层对象里取出命中的 gate 值，后续再检查它是否真的是对象。
    dict_gate_candidate: Any = dict_remote.get(str_name, {})  # 这里先接受任意值，再显式过滤成对象。

    # 只有对象形态才可继续参与 status、errors 和 fresh 判断。
    return dict_gate_candidate if isinstance(dict_gate_candidate, dict) else {}

# 把同一批执行阻塞项同步追加到 delivery 与 truth 两侧原因列表，避免重复裸调用。
def _extend_execution_reasons(
    list_delivery_reasons: list[str],
    list_truth_reasons: list[str],
    list_reasons: list[str],
) -> None:
    """
    把同一批阻塞原因同步追加到 delivery 与 truth 列表。

    :param list_delivery_reasons: delivery 侧阻塞原因列表，dtype=list[str]，unit=collection
    :param list_truth_reasons: truth 侧阻塞原因列表，dtype=list[str]，unit=collection
    :param list_reasons: 需要同时追加到两侧的原因列表，dtype=list[str]，unit=collection
    :return: 返回值固定为空；该 helper 只承担原地扩展两个列表的副作用，dtype=None，unit=object
    """

    # delivery 侧需要原样保留这批执行阻塞项。
    list_delivery_reasons.extend(list_reasons)

    # truth 侧也要保留同一批执行阻塞项，保持两种置信度输入一致。
    list_truth_reasons.extend(list_reasons)

# 根据本地步骤结果和可选远程证据，分别给出 local、delivery、truth 三类执行置信度
def classify_confidence(dict_output: JsonDict) -> JsonDict:
    """
    从本地 gate 输出中归纳多层执行置信度。

    :param dict_output: 本地 gate 执行输出对象，dtype=dict[str, Any]，unit=mapping
    :return: 返回 local、delivery、truth 与 EDA 执行置信度字段组成的结构化对象，dtype=dict[str, Any]，unit=mapping
    """

    # 本地置信度只取决于整体 status 是否 passed，不混入远程 truth/delivery 维度
    str_local_confidence = "passed" if dict_output.get("status") == "passed" else "failed"  # 本地质量门层面的执行置信度

    # 先收集 delivery 维度的阻塞原因，后面会根据远程证据或本地步骤结果逐步追加。
    list_delivery_reasons: list[str] = []  # 这里累计 delivery 维度的执行阻塞原因。

    # truth 维度专门承接“是否真的执行过”这一层证据，后续 stale 和 missing_tools 都会落到这里。
    list_truth_reasons: list[str] = []  # 这里只收“缺少真实执行证据”这一侧的阻塞项，例如 stale 证据或缺工具。

    # remote_evidence 存在时优先相信远程结果；否则回退到本地步骤派生阻塞原因。
    dict_remote = dict_output.get("remote_evidence") or {}  # 这里承接调用方附加的远程证据汇总。

    # 安全读取 delivery 和 truth 两个 gate，避免字段缺失时抛异常。
    json_dict_delivery_gate = _extract_remote_gate(dict_remote, "delivery")  # delivery 证据 gate 会决定交付执行置信度。

    # truth gate 独立承接“是否真实执行过”的判断，因此单独拆一行方便后续维护。
    json_dict_truth_gate = _extract_remote_gate(dict_remote, "truth")  # truth 证据 gate 会决定事实执行置信度。

    # 先读取 delivery gate 的 fresh 字段，后续既判断通过态也判断 stale。
    bool_delivery_fresh = bool(json_dict_delivery_gate.get("fresh", True))  # 缺省时按 fresh 处理，只有显式 False 才算 stale。

    # 先读取 truth gate 的 fresh 字段，后续复用同一份新鲜度判断。
    bool_truth_fresh = bool(json_dict_truth_gate.get("fresh", True))  # 缺省时同样按 fresh 处理。

    # delivery 证据必须同时通过 gate 且仍然 fresh，才算真正可用。
    bool_delivery_passed = json_dict_delivery_gate.get("status") == "passed" and bool_delivery_fresh  # 这个布尔值会直接驱动 delivery_execution_confidence。

    # truth 证据沿用同一规则，但它的语义比 delivery 更严格。
    bool_truth_passed = json_dict_truth_gate.get("status") == "passed" and bool_truth_fresh  # 这个布尔值同时驱动 truth 和 eda 两层执行置信度。

    # 当远程证据存在时，优先从 delivery/truth gate 的 errors/fresh 字段提取阻塞原因
    if dict_remote:

        # delivery gate 已经产出的错误直接进入 delivery 阻塞原因列表。
        list_delivery_reasons.extend(json_dict_delivery_gate.get("errors", []))

        # truth gate 的错误同样直接进入 truth 阻塞原因列表。
        list_truth_reasons.extend(json_dict_truth_gate.get("errors", []))

        # delivery 证据过期时，必须把 stale 风险显式写入原因列表。
        if not bool_delivery_fresh:

            # 使用稳定文案明确指出 delivery 远程证据已经过期。
            list_delivery_reasons.append("remote evidence is stale")

        # truth 证据过期时，也必须显式记录 stale 风险。
        if not bool_truth_fresh:

            # 这里复用同一条稳定错误文本，让调用方能统一识别 stale 原因。
            list_truth_reasons.append("remote evidence is stale")

    # 没有远程证据时，从本地 env_probe 和各步骤的 missing_tools 字段派生 blocked 原因
    else:

        # 顺序扫描本地执行结果，把环境阻塞和缺工具信息分别归入 delivery/truth 两侧
        for dict_step in dict_output.get("results", []):

            # 尝试解析步骤 stdout 中的 JSON 内容，便于读取 blockers/missing_tools 字段
            json_dict_parsed = _json_from_step(dict_step)  # 当前步骤 stdout 解析出的 JSON 对象

            # env_probe 的 blockers 代表本地环境尚不具备继续 VCS/Verdi 执行的前提
            if dict_step.get("name") == "env_probe":

                # env_probe 输出里的 blockers 会同时阻断 delivery 和 truth。
                list_blockers = json_dict_parsed.get("overall", {}).get("blockers", [])  # env_probe 报告的环境阻塞项

                # 环境阻塞会同时影响交付证据与事实证据，因此统一同步到两侧列表。
                _extend_execution_reasons(list_delivery_reasons, list_truth_reasons, list_blockers)

            # 子步骤上报的 missing_tools 同样属于执行阻塞。
            list_missing_tools = json_dict_parsed.get("missing_tools", [])  # 当前步骤 JSON 报告的缺失工具列表

            # 缺工具与环境阻塞一样，会同时阻断两侧执行置信度。
            _extend_execution_reasons(list_delivery_reasons, list_truth_reasons, list_missing_tools)

    # 先按首次出现顺序压缩 delivery 原因，避免重复 blocker 把交付侧输出刷屏。
    list_delivery_deduped = list(dict.fromkeys(str(obj_item) for obj_item in list_delivery_reasons if obj_item))  # 去重后保留 delivery 原因的首次出现顺序。

    # truth 原因去重后会直接暴露给最终 JSON，因此这里要稳定保留首次出现顺序，便于人工比对“真实执行”证据变化。
    list_truth_deduped = list(dict.fromkeys(str(obj_item) for obj_item in list_truth_reasons if obj_item))  # 这个列表会直接变成 truth/eda 输出原因，因此要稳定去重。

    # 返回完整的多层置信度分类结果，保持与 SKILL.md 和测试中的字段契约一致。
    return {
        "local_confidence": str_local_confidence,
        "delivery_execution_confidence": (
            "passed" if bool_delivery_passed else "blocked" if list_delivery_deduped else "not_executed"
        ),
        "delivery_execution_reasons": [] if bool_delivery_passed else list_delivery_deduped,
        "truth_execution_confidence": (
            "passed" if bool_truth_passed else "blocked" if list_truth_deduped else "not_executed"
        ),
        "truth_execution_reasons": [] if bool_truth_passed else list_truth_deduped,
        "eda_execution_confidence": (
            "passed" if bool_truth_passed else "blocked" if list_truth_deduped else "not_executed"
        ),
        "eda_execution_reasons": [] if bool_truth_passed else list_truth_deduped,
    }

# 推断当前脚本在公开仓库还是技能源码仓布局下运行。
def detect_repo_root_default(path_quality_script: Path) -> Path:
    """根据当前脚本位置推断质量门默认 repo root。

    :param path_quality_script: `run_quality_gate.py` 的绝对路径。
    :return: 返回默认 repo root 路径；公开仓库布局优先返回技能根目录，源码仓布局回退到历史父级层数。
    """

    # 当前技能根目录对应 `<skill-root>/scripts/python/quality/run_quality_gate.py` 的三级父目录。
    path_skill_dir_default = path_quality_script.resolve().parents[3]  # 公开仓库布局下这里就是仓库根目录。

    # 公开仓库根目录同时拥有 README、pyproject、references 与 scripts。
    tuple_public_repo_markers = (
        path_skill_dir_default / "README.md",
        path_skill_dir_default / "pyproject.toml",
        path_skill_dir_default / "references",
        path_skill_dir_default / "scripts",
    )  # 这些标记共同说明当前技能根目录本身就是可发布仓库根目录。

    # 命中公开仓库标记时，直接把技能根目录视作 repo root。
    if all(path_marker.exists() for path_marker in tuple_public_repo_markers):
        return path_skill_dir_default

    # 否则沿用历史源码仓布局：`<repo>/skills/<skill>/scripts/python/quality/run_quality_gate.py`。
    return path_quality_script.resolve().parents[5]

# 构造 CLI 参数规格清单，避免 main 里连续堆叠裸 add_argument 调用。
def _cli_argument_specs(
    path_repo_root_default: Path,
    path_skill_dir_default: Path,
) -> tuple[tuple[tuple[str, ...], JsonDict], ...]:
    """
    生成本地质量门 CLI 使用的参数规格清单。

    :param path_repo_root_default: ``--repo-root`` 的默认值，dtype=Path，unit=path
    :param path_skill_dir_default: ``--skill-dir`` 的默认值，dtype=Path，unit=path
    :return: 返回参数位置串与关键字字典组成的元组序列，dtype=tuple[tuple[tuple[str, ...], dict[str, Any]], ...]，unit=collection
    """

    # 这里把 CLI 协议集中成数据结构，便于 main 统一注册参数。
    return (
        (("--repo-root",), {"type": Path, "default": path_repo_root_default}),  # 仓库根目录覆盖参数。
        (("--skill-dir",), {"type": Path, "default": path_skill_dir_default}),  # 技能根目录覆盖参数。
        (("--audit-only",), {"action": "store_true"}),  # 仅做技能交付面与边界审计。
        (("--script-matrix-only",), {"action": "store_true"}),  # 仅做脚本矩阵与能力矩阵一致性审计。
        (("--dry-run",), {"action": "store_true"}),  # 仅输出计划，不执行任何步骤。
        (("--json",), {"action": "store_true"}),  # 输出机器可读 JSON 协议。
        (("--timeout",), {"type": int, "default": 300}),  # 控制每个步骤的统一超时秒数。
        (
            ("--remote-evidence",),
            {"type": Path, "help": "Fresh remote EDA host/equivalent evidence JSON."},
        ),  # 远程证据文件路径。
        (("--remote-max-age-hours",), {"type": int, "default": 24}),  # 远程证据时效上限小时数。
    )

# 解析 CLI 参数，按模式运行本地 gate、技能审计或远程证据补充流程
def main() -> int:
    """
    运行本地质量门 CLI 入口。

    :param 无: 当前入口函数不接收显式 Python 位置参数，全部输入都来自命令行解析结果。
    :return: 当最终状态为 passed 时返回 0，其余情况返回 1，dtype=int，unit=exit code
    """

    # 先创建统一的参数解析器，保证人工调用与测试调用共享同一套 CLI 协议。
    parser = argparse.ArgumentParser(description="Run the vcs-verdi-developer local quality gate.")  # 本地质量门 CLI 会通过该解析器接收全部参数。

    # 再计算技能根目录默认值，保持它与当前技能安装布局同步。
    path_skill_dir_default = Path(__file__).resolve().parents[3]  # 默认技能根目录对应当前技能安装布局。

    # 公开仓库布局与技能源码仓布局的 repo root 层级不同，这里统一做一次判定。
    path_repo_root_default = detect_repo_root_default(Path(__file__).resolve())  # 默认仓库根目录按当前布局自动推断。

    # 顺序注册全部 CLI 参数，保持 main 内部只保留一处裸 add_argument 调用。
    for tuple_cli_args, dict_cli_kwargs in _cli_argument_specs(path_repo_root_default, path_skill_dir_default):

        # 把当前参数规格注册到解析器，确保 CLI 协议与规格清单保持单一事实来源。
        parser.add_argument(*tuple_cli_args, **dict_cli_kwargs)

    # 在这里一次性解析全部 CLI 输入，后续只按模式分发执行路径。
    args = parser.parse_args()  # 所有入口模式都从这个命名空间读取参数。

    # audit-only 模式只做技能文件与文案边界审计，不执行完整本地质量门计划
    if args.audit_only:

        # audit-only 模式只跑技能交付面和文案边界审计。
        json_dict_cli_output = audit_skill(args.skill_dir)  # 这里直接复用 audit_skill 的结构化结果。

        # 当前模式的最终状态直接来自 audit_skill 的 status 字段。
        str_status = json_dict_cli_output["status"]  # audit-only 模式只需要沿用审计结论。

    # script-matrix-only 模式只做脚本矩阵与能力矩阵文档一致性审计
    elif args.script_matrix_only:

        # script-matrix-only 模式只检查脚本矩阵和能力矩阵文档。
        json_dict_cli_output = script_matrix_audit(args.skill_dir)  # 这里直接复用脚本矩阵审计结果。

        # 这里的最终状态专门来自脚本矩阵审计结论，不再混入其他模式判断。
        str_status = json_dict_cli_output["status"]  # 脚本矩阵审计会给出 passed 或 failed。

    # dry-run 模式只返回计划本身，不真正执行测试与脚本
    elif args.dry_run:

        # dry-run 模式只返回计划本身，不实际执行任何步骤。
        json_dict_cli_output = build_local_gate(args.repo_root, skill_dir=args.skill_dir)  # 这里输出的是还未执行的本地 gate 计划。

        # 只要计划能构造出来，就先把 dry-run 结果标记成 passed。
        json_dict_cli_output["status"] = "passed"  # dry-run 只验证计划构造成功，因此状态固定为 passed。

        # dry-run 只验证计划构造本身，因此初始状态保持 passed
        str_status = "passed"  # dry-run 模式下的初始最终状态

    # 默认模式执行完整本地质量门步骤序列
    else:

        # 默认模式执行完整本地 gate，并返回包含步骤结果与置信度的完整对象。
        json_dict_cli_output = run_local_gate(args.repo_root, skill_dir=args.skill_dir, timeout=args.timeout)  # 这里承接 run_local_gate 的完整执行结果。

        # 当前模式的最终状态直接来自 run_local_gate 汇总后的 status 字段。
        str_status = json_dict_cli_output["status"]  # 默认执行模式会先给出本地 gate 总体状态。

    # 调用方补充远程证据时，需要在现有输出对象上再挂接 delivery/truth 远程执行结论
    if args.remote_evidence:

        # 读取远程证据文件，并生成 delivery 与 truth 两条验证结果分支。
        json_dict_cli_output["remote_evidence"] = load_remote_evidence(  # 远程证据会被挂到同一个输出对象上。
            args.remote_evidence,  # 远程证据 JSON 路径来自调用方传入。
            max_age_hours=args.remote_max_age_hours,  # 该值约束远程证据的新鲜度阈值。
        )

        # 把远程证据带来的置信度升级或阻塞结果再次回写到输出对象。
        json_dict_cli_output.update(classify_confidence(json_dict_cli_output))

        # truth_execution_confidence 只要不是 passed，就必须把整体结果压回 failed。
        if json_dict_cli_output.get("truth_execution_confidence") != "passed":

            # 保持与测试契约一致：truth 被阻断时整体 CLI 退出码必须失败。
            str_status = "failed"  # truth 只要未通过，CLI 退出码就必须切回失败。

            # 同步把输出对象里的 status 改成 failed，避免 JSON 与退出码不一致。
            json_dict_cli_output["status"] = "failed"  # 结构化 JSON 状态也必须与退出码保持一致。

    # JSON 模式走机器可读协议输出，供测试和上游脚本直接消费。
    if args.json:

        # 把完整 JSON 对象写到 stdout，维持稳定的机器可读协议。
        sys.stdout.write(f"{json.dumps(json_dict_cli_output, indent=2, sort_keys=True)}\n")

    # 非 JSON 模式只输出简短摘要，不把结构化载荷直接打印到终端。
    else:

        # 先写整体状态摘要，便于人工终端快速识别 passed 或 failed。
        sys.stdout.write(f"> INFO: [Python] local quality gate status: {str_status}\n")

        # 当输出对象带 errors 字段时，再逐条补充人类可读错误摘要。
        if json_dict_cli_output.get("errors"):

            # 逐条写出错误摘要，保持非 JSON 模式仍具备基本可诊断性。
            for str_error in json_dict_cli_output["errors"]:

                # 每次循环都按统一格式写一条人类可读错误消息。
                sys.stdout.write(f"> ERR: [Python] local quality gate error: {str_error}\n")

    # 只有最终状态为 passed 时返回 0，其余任何阻塞都让 CLI 以失败退出。
    return 0 if str_status == "passed" else 1

# 作为独立脚本运行时，把 main 返回值透传为进程退出码
if __name__ == "__main__":

    # 让 CLI 调用方直接依赖标准退出码判断本地质量门是否通过
    raise SystemExit(main())
