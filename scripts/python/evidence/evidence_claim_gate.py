#!/usr/bin/env python3
"""校验证据声明是否足以支撑事实性 readiness / release 说法。

本模块同时提供可导入的校验函数与命令行入口。

命令行标准输出协议：
- 默认输出带前缀的人类可读错误、告警和最终状态摘要，不直接把结构化结果打印到终端。
- 当传入 ``--json`` 时，标准输出会写出单个 JSON 对象，供上游自动化直接消费。
"""

# 启用延后求值注解，避免类型提示在运行期引入额外解析顺序要求。
from __future__ import annotations
# 提供命令行参数解析、JSON 序列化、标准输出与路径处理能力。
import argparse
import json
import sys
from pathlib import Path

# 声明必须按事实性 claim 规则硬失败处理的 claim 类型集合。
FACTUAL_CLAIM_TYPES = {"factual", "technical", "execution", "capability"}  # 需要证据硬门禁的 claim 类型集合

# 声明被视为已获支持的 support_status 合法值集合。
SUPPORTED_STATUSES = {"supported", "passed"}  # 可被事实性 claim 接受的 support_status 集合

# 声明需要 delivery_execution_confidence=passed 的 claim_scope 集合。
DELIVERY_SCOPES = {"delivery_mainline"}  # 交付主线 claim 允许使用的 scope 集合

# truth_full 代表真实执行闭环已经打通，只有 truth gate 通过后才允许拿来做强事实宣称。
TRUTH_SCOPES = {"truth_full"}  # 真实执行闭环 claim 允许使用的 scope 集合

# 声明在 URG 真实能力仍 blocked 时禁止出现在文本中的夸大语句片段。
TRUTH_BLOCKED_PHRASES = (  # truth gate 仍 blocked 时禁止出现的夸大表达片段
    "complete support",  # 直接宣称完整支持，必须已有 truth pass 才能使用
    "fully verified",  # 直接宣称已充分验证，必须已有 truth pass 才能使用
    "fully validated",  # 直接宣称已完成验证闭环，必须已有 truth pass 才能使用
    "only minor",  # 弱化未解决阻断的风险描述，容易掩盖真实 blocked 状态
    "just a small fix",  # 把 blocked 风险降格为小修小补，属于禁止夸大话术
    "已完整验证",  # 中文完整验证表述同样需要 truth gate 通过才能出现
    "只差少量",  # 中文弱化 blocked 风险的表述也应被 truth gate 拦住
)

# 从 claim 字典中提取稳定的 claim_id 文本，避免多处分支重复写回退逻辑。
def _claim_id(dict_claim: dict[str, object]) -> str:
    """
    返回当前 claim 的稳定标识文本。

    参数：
    - dict_claim: 当前待校验的 claim 字典。

    返回：
    - 若 claim 中存在 ``claim_id``，则返回其字符串值；否则返回占位标识。

    异常：
    - 无显式异常；字符串转换沿用 Python 默认行为。
    """

    # 把缺失 claim_id 的场景收敛成统一占位文本，便于错误列表保持稳定格式。
    return str(dict_claim.get("claim_id") or "<missing-claim-id>")

# 读取 payload 中的 gate_context，并把非法形态安全降级为空字典。
def _gate_context(dict_payload: dict[str, object]) -> dict[str, object]:
    """
    返回 payload 中可安全使用的 gate_context 字典。

    参数：
    - dict_payload: 当前证据声明门禁输入载荷。

    返回：
    - 若 ``gate_context`` 是字典则原样返回；否则返回空字典。

    异常：
    - 无显式异常；字典读取与类型判断沿用 Python 默认行为。
    """

    # 先读取 payload 中的 gate_context 原始值，供后续做字典形态校验。
    obj_gate_context = dict_payload.get("gate_context", {})  # payload 中声明的原始 gate_context 值

    # 只有 gate_context 真的是字典时才允许后续 scope 逻辑继续读取其中字段。
    if isinstance(obj_gate_context, dict):

        # 返回形态合法的 gate_context 字典，供 scope 规则直接消费。
        return obj_gate_context

    # 返回空字典，让上层按“没有 gate_context”语义继续处理。
    return {}

# 针对单条 claim 校验 scope 与 truth gate 是否被夸大表述越权使用。
def _scope_error(dict_claim: dict[str, object], *, dict_gate_context: dict[str, object]) -> str:
    """
    返回当前 claim 在 scope / truth gate 维度上的首条错误文本。

    参数：
    - dict_claim: 当前待校验的 claim 字典。
    - dict_gate_context: 当前载荷中已经净化后的 gate_context 字典。

    返回：
    - 若未发现 scope 违规，则返回空字符串；否则返回单条稳定错误文本。

    异常：
    - 无显式异常；字符串处理与字典读取沿用 Python 默认行为。
    """

    # 先读取 claim_scope 并规整成小写文本，供后续 delivery/truth 规则复用。
    str_scope_name = str(dict_claim.get("claim_scope") or "").strip().lower()  # 当前 claim 的 scope 名称

    # 再读取 claim 文本并转成小写，供夸大用语与 truth scope 关键字匹配复用。
    str_claim_text = str(dict_claim.get("text", "")).lower()  # 当前 claim 文本的小写版本

    # 缓存当前 claim 的稳定标识文本，避免每条错误消息都重复拼接回退逻辑。
    str_claim_id = _claim_id(dict_claim)  # 当前 claim 的稳定标识文本

    # 没有 gate_context 时不做 scope 依赖的硬判定，让纯静态 evidence 校验继续进行。
    if not dict_gate_context:

        # 返回空错误，表示当前 claim 在缺少 gate_context 的前提下不追加 scope 阻断。
        return ""

    # boundary scope 只是边界声明，不要求 delivery/truth gate 通过。
    if str_scope_name == "boundary":

        # 返回空错误，允许 boundary scope 继续作为非执行性边界描述存在。
        return ""

    # delivery scope 只有在 delivery_execution_confidence 已通过时才允许作为事实 claim 输出。
    if str_scope_name in DELIVERY_SCOPES:

        # 当前 gate_context 没有交付执行通过时，禁止输出 delivery_mainline 级别事实说法。
        if dict_gate_context.get("delivery_execution_confidence") != "passed":

            # 返回 delivery scope 违规错误，保持与现有测试断言兼容。
            return f"{str_claim_id} delivery scope requires delivery_execution_confidence=passed"

    # truth scope 或“every official / complete support”类文本都要求 truth gate 真正通过。
    if str_scope_name in TRUTH_SCOPES or "complete support" in str_claim_text or "every official" in str_claim_text:

        # 当前 truth gate 未通过时，禁止输出 truth_full 或同等强度的事实说法。
        if dict_gate_context.get("truth_execution_confidence") != "passed":

            # 这里复用现有断言文案，避免测试与上游流程失去稳定的错误关键字。
            return f"{str_claim_id} truth scope requires truth_execution_confidence=passed"

    # 当 URG 供应商或主机状态明确 blocked 时，禁止使用“只差少量修复”之类夸大话术。
    if (
        dict_gate_context.get("truth_execution_confidence") == "blocked"
        and dict_gate_context.get("urg_vendor_or_host_blocked")
    ):

        # 只有 claim 文本命中任一夸大短语时，才把它标记为 truth-blocked 夸大陈述。
        if any(str_phrase in str_claim_text for str_phrase in TRUTH_BLOCKED_PHRASES):

            # 返回夸大 truth-blocked URG 状态的错误文本，保持与现有测试断言兼容。
            return f"{str_claim_id} text overstates a truth-blocked URG state"

    # 返回空错误，表示当前 claim 在 scope / truth gate 维度没有新增阻断。
    return ""

# 对单条事实性 claim 追加 support_status、evidence_ids、scope 三类硬门禁错误。
def _validate_factual_claim(
    dict_claim: dict[str, object],
    *,
    set_evidence_ids: set[str],
    dict_gate_context: dict[str, object],
    list_errors: list[str],
) -> None:
    """
    把单条事实性 claim 的校验错误追加到错误列表。

    参数：
    - dict_claim: 当前待校验的事实性 claim 字典。
    - set_evidence_ids: 当前 payload 中有效 evidence_id 的集合。
    - dict_gate_context: 当前载荷中已经净化后的 gate_context 字典。
    - list_errors: 用于累积错误文本的列表。

    返回：
    - 无业务返回值；错误会直接追加到 ``list_errors``。

    异常：
    - 无显式异常；列表追加和字典读取沿用 Python 默认行为。
    """

    # 缓存当前 claim 的稳定标识，避免多条错误文本各自重复做回退拼接。
    str_claim_id = _claim_id(dict_claim)  # 当前事实性 claim 的稳定标识文本

    # 读取当前 claim 的 support_status，并规整成小写文本供硬门禁判断使用。
    str_support_status = str(dict_claim.get("support_status") or "unverified").lower()  # 当前事实性 claim 的支持状态

    # 读取当前 claim 声明的 evidence_ids，并保证后续总是得到一个列表形态。
    list_evidence_refs = list(dict_claim.get("evidence_ids") or [])  # 当前事实性 claim 声明关联的 evidence_id 列表

    # 事实性 claim 只有 supported / passed 两种状态才允许继续向外宣称。
    if str_support_status not in SUPPORTED_STATUSES:

        # 记录 support_status 非法错误，保持与现有测试断言兼容。
        list_errors.append(f"{str_claim_id} support_status is {str_support_status}")

    # 事实性 claim 没有任何 evidence_id 时必须硬失败，不能靠文本兜底。
    if not list_evidence_refs:

        # 记录 evidence_ids 缺失错误，保持与现有测试断言兼容。
        list_errors.append(f"{str_claim_id} has no evidence_ids")

    # 逐条检查 claim 引用的 evidence_id 是否真实存在于 payload evidence 集合里。
    for str_evidence_ref in list_evidence_refs:

        # 只有引用了不存在的 evidence_id 才登记错误，存在的引用直接通过。
        if str_evidence_ref not in set_evidence_ids:

            # 记录 evidence_id 缺失错误，便于调用方直接定位无效引用。
            list_errors.append(f"{str_claim_id} references missing evidence_id {str_evidence_ref}")

    # 检查当前 claim 是否越过 delivery/truth gate 的边界去做过强事实陈述。
    str_scope_error = _scope_error(dict_claim, dict_gate_context=dict_gate_context)  # 当前 claim 的 scope 校验错误文本

    # 只有 scope 校验真的返回错误文本时，才把它追加到最终错误列表中。
    if str_scope_error:

        # 记录当前 claim 的 scope 违规错误，保持错误列表语义稳定。
        list_errors.append(str_scope_error)

# 对单条非事实性 claim 追加 warning，不把它升级成硬失败。
def _validate_non_factual_claim(dict_claim: dict[str, object], *, list_warnings: list[str]) -> None:
    """
    把单条非事实性 claim 的 warning 追加到告警列表。

    参数：
    - dict_claim: 当前待校验的非事实性 claim 字典。
    - list_warnings: 用于累积 warning 文本的列表。

    返回：
    - 无业务返回值；warning 会直接追加到 ``list_warnings``。

    异常：
    - 无显式异常；列表追加和字典读取沿用 Python 默认行为。
    """

    # 缓存当前 claim 的稳定标识，避免告警文本重复拼接回退逻辑。
    str_claim_id = _claim_id(dict_claim)  # 当前非事实性 claim 的稳定标识文本

    # 读取当前 claim 类型文本，供 warning 文案继续保留原语义分类。
    str_claim_type = str(dict_claim.get("claim_type") or "factual").lower()  # 当前非事实性 claim 的类型文本

    # 读取当前 claim 的 support_status，供 recommendation 等弱 claim 生成 warning 文案。
    str_support_status = str(dict_claim.get("support_status") or "unverified").lower()  # 当前非事实性 claim 的支持状态

    # 只有 support_status 不在支持集合里时，才把它作为 warning 保留给调用方。
    if str_support_status not in SUPPORTED_STATUSES:

        # 记录当前非事实性 claim 的 warning，保持与现有测试断言兼容。
        list_warnings.append(f"{str_claim_id} {str_claim_type} is {str_support_status}")

# 校验证据与 claim 的对应关系，并返回可供 CLI 或调用方消费的结构化结果。
def validate_claim_evidence(dict_payload: dict[str, object]) -> dict[str, object]:
    """
    校验证据声明载荷中的 factual / recommendation claim 是否满足门禁。

    参数：
    - dict_payload: 包含 ``evidence``、``claims`` 与可选 ``gate_context`` 的输入载荷字典。

    返回：
    - 返回包含 ``status``、``claim_count``、``evidence_count``、``errors`` 和 ``warnings`` 的结构化结果字典。

    异常：
    - 无显式异常；若输入字典结构严重异常，底层键读取和列表转换异常沿用默认行为。
    """

    # 读取 payload 中的 evidence 列表，供 evidence_id 索引与数量统计共同复用。
    list_evidence = list(dict_payload.get("evidence", []))  # 当前载荷声明的 evidence 列表

    # claims 同时用于逐条门禁校验和结果计数，先统一取出可避免后续重复读取 payload。
    list_claims = list(dict_payload.get("claims", []))  # 本次门禁需要逐条分发校验的全部 claim 条目

    # 提取并净化 gate_context，避免非法结构把后续 scope 判断拖成类型错误。
    dict_gate_context = _gate_context(dict_payload)  # 当前载荷中可安全消费的 gate_context 字典

    # 收集全部有效 evidence_id，供 factual claim 逐条检查引用是否合法。
    set_evidence_ids = {  # 当前载荷中实际存在的 evidence_id 集合
        str(dict_evidence["evidence_id"])  # 规范化单条 evidence 的 evidence_id 文本
        for dict_evidence in list_evidence  # 逐条查看原始 evidence 记录，提取其中真实存在的标识
        if dict_evidence.get("evidence_id")  # 只保留真的声明了 evidence_id 的条目
    }

    # 初始化错误列表，用于累积 facts 级别的硬失败原因。
    list_errors: list[str] = []  # 当前载荷累计得到的硬失败错误列表

    # 初始化告警列表，用于保留 recommendation 等非事实性 claim 的弱风险。
    list_warnings: list[str] = []  # 当前载荷累计得到的 warning 列表

    # 逐条校验 claim，根据类型把它们路由到 factual 硬门禁或弱 warning 分支。
    for dict_claim in list_claims:

        # 先读取 claim_type 并规整成小写文本，供 factual / non-factual 分支判断复用。
        str_claim_type = str(dict_claim.get("claim_type") or "factual").lower()  # 当前 claim 的类型文本

        # 事实性 claim 走硬门禁路径，必须校验状态、证据和 scope 边界。
        if str_claim_type in FACTUAL_CLAIM_TYPES:

            # 对当前事实性 claim 执行 support_status、evidence_ids 与 scope 校验。
            _validate_factual_claim(
                dict_claim,
                set_evidence_ids=set_evidence_ids,
                dict_gate_context=dict_gate_context,
                list_errors=list_errors,
            )

        # 其余 claim 只在 support_status 不足时保留 warning，不把整单结果打成 failed。
        else:

            # 对当前非事实性 claim 执行 warning 路径校验。
            _validate_non_factual_claim(dict_claim, list_warnings=list_warnings)

    # 返回结构化校验结果，供单元测试、CLI 与上游自动化共同消费。
    return {
        "status": "failed" if list_errors else "passed",
        "claim_count": len(list_claims),
        "evidence_count": len(list_evidence),
        "errors": list_errors,
        "warnings": list_warnings,
    }

# 解析命令行参数并输出证据 claim gate 的摘要或 JSON 协议。
def main(argv: list[str] | None = None) -> int:
    """
    运行证据声明门禁命令行入口并返回进程退出码。

    参数：
    - argv: 可选的命令行参数列表；传入 ``None`` 时使用进程默认参数。

    返回：
    - 当 gate 结果为 ``passed`` 时返回 ``0``，否则返回 ``1``。

    异常：
    - 参数解析失败时由 ``argparse`` 抛出并终止进程；文件读取与 JSON 解析异常沿用底层行为。
    """

    # 创建命令行参数解析器，统一声明脚本用途与支持的 JSON 输出协议。
    parser = argparse.ArgumentParser(description="Hard-fail unsupported factual claims without evidence.")  # 当前 CLI 的参数解析器

    # 注册 claims-json 输入参数，要求调用方显式提供待校验的载荷文件路径。
    parser.add_argument("--claims-json", type=Path, required=True)

    # 注册 JSON 输出开关，启用后按模块文档约定输出单个结构化 JSON 对象。
    parser.add_argument("--json", action="store_true")

    # 解析命令行参数，得到本次 gate 执行所需的输入路径与输出模式。
    args = parser.parse_args(argv)  # 当前 CLI 解析得到的参数对象

    # 读取 claims-json 文件全文，供后续 JSON 反序列化和门禁校验复用。
    str_payload_text = args.claims_json.read_text(encoding="utf-8")  # 当前 claims-json 文件的原始文本

    # 解析输入载荷 JSON，供 claim/evidence 结构化门禁逻辑直接消费。
    dict_payload = json.loads(str_payload_text)  # 当前 CLI 读取到的结构化输入载荷

    # 运行证据声明门禁逻辑，得到最终结构化结果供 JSON 或人类可读摘要复用。
    dict_result = validate_claim_evidence(dict_payload)  # 当前 CLI 生成的结构化门禁结果

    # 当调用方显式请求 JSON 协议时，输出单个结构化对象供自动化直接消费。
    if args.json:

        # 按模块文档约定把单个 JSON 对象写到标准输出，避免混入额外终端文本。
        json.dump(dict_result, sys.stdout, indent=2, sort_keys=True)

        # 为 JSON 协议输出补一个换行，避免 shell 提示符直接接在 JSON 末尾。
        sys.stdout.write("\n")

    # 未请求 JSON 协议时，只输出带前缀的人类可读错误、告警与最终状态摘要。
    else:

        # 逐条输出错误摘要，便于调用方直接看到哪条 claim 触发了硬失败。
        for str_error in dict_result["errors"]:

            # 首参保持静态协议前缀，便于 current-project 门禁静态验证 CLI 输出格式。
            print("> ERR: [Python] claim validation error:", str_error)

        # 逐条输出 warning 摘要，便于调用方区分 recommendation 等弱风险。
        for str_warning in dict_result["warnings"]:

            # 首参保持静态协议前缀，避免动态拼接导致输出协议无法被静态证明。
            print("> WARNING: [Python] claim validation warning:", str_warning)

        # 通过结果输出 INFO 摘要，说明当前 evidence claim gate 已全部通过。
        if dict_result["status"] == "passed":

            # 输出通过摘要，避免把结构化结果直接打印到终端。
            print("> INFO: [Python] evidence claim gate passed")

        # 失败结果输出 ERR 摘要，说明当前 evidence claim gate 至少存在一条硬失败。
        else:

            # 输出失败摘要，提醒调用方改用 --json 查看结构化细节。
            print("> ERR: [Python] evidence claim gate failed")

    # passed 返回零退出码，其余状态返回一，供 shell 与 CI 直接判定是否通过门禁。
    return 0 if dict_result["status"] == "passed" else 1

# 只有以脚本方式直接执行时才启动 CLI，避免导入测试模块时立刻退出当前 Python 进程。
if __name__ == "__main__":

    # 把 main 返回值转换为进程退出码，供 shell、CI 与上游自动化直接判定成败。
    raise SystemExit(main())
