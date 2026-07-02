#!/usr/bin/env python3
"""扫描或修补验证 overlay 内的 `libucapi.so` 二进制补丁点。

本模块同时提供可导入的扫描/修补函数与命令行入口。

命令行标准输出协议：
- 默认只打印带前缀的人类可读状态摘要，不直接把结构化扫描或补丁结果输出到终端。
- 当传入 ``--json`` 时，标准输出会写出单个 JSON 对象，供自动化直接消费。
"""

# 启用延后求值注解，避免类型提示在运行期引入额外解析顺序要求。
from __future__ import annotations

# 提供命令行参数解析、哈希计算、JSON 序列化与路径处理能力。
import argparse
import hashlib
import json
import sys
from pathlib import Path

# 记录需要在 libucapi 二进制中搜索的原始字节模式。
BYTES_SEARCH_PATTERN = bytes.fromhex("e8 f3 fd ff ff ff c8")  # ucapi 校验点对应的原始机器码片段

# 记录命中后写回到目标偏移处的补丁字节序列。
BYTES_PATCH_BYTES = bytes.fromhex("90 90 90 90 90")  # 使用 NOP 覆盖原始调用指令的补丁字节

# 保留历史搜索常量名，避免既有测试和导入方找不到旧接口符号。
SEARCH_PATTERN = BYTES_SEARCH_PATTERN  # 兼容旧接口使用的搜索模式常量名

# 保留历史补丁常量名，避免既有测试和导入方找不到旧接口符号。
PATCH_BYTES = BYTES_PATCH_BYTES  # 兼容旧接口使用的补丁字节常量名

# 固定补丁清单文件名，供后续流程读取 activation path 与 loader 提示。
STR_MANIFEST_NAME = "ucapi_patch_manifest.json"  # 补丁执行后写回 overlay 根目录的清单文件名

# 按常见 overlay 目录布局列出候选 libucapi 相对路径。
TUPLE_CANDIDATE_RELS = (  # 可能出现 libucapi.so 的相对路径集合
    "linux64/lib/libucapi.so",  # 标准 VCS overlay 常见的库文件位置
    "linux64/bin/libucapi.so",  # 少数封装脚本把库文件放在 bin 目录旁
    "platform/linux64/bin/libucapi.so",  # 较旧平台目录布局下的候选位置
    "platform/LINUXAMD64/bin/libucapi.so",  # 大写平台目录布局下的候选位置
)

# 计算字节串的 SHA256 摘要，供扫描与补丁结果稳定比对。
def sha256_bytes(bytes_payload: bytes) -> str:
    """
    计算字节串对应的 SHA256 十六进制摘要。

    参数：
    - bytes_payload: 需要计算摘要的原始字节串。

    返回：
    - 返回 64 位十六进制 SHA256 摘要字符串。

    异常：
    - 无显式异常；哈希计算沿用标准库默认行为。
    """

    # 直接对原始字节串做哈希，避免扫描和补丁阶段重复实现摘要逻辑。
    return hashlib.sha256(bytes_payload).hexdigest()

# 判断给定目录是否像是本地验证 overlay，而不是供应商安装根目录。
def is_overlay_home(path_overlay_home: Path) -> bool:
    """
    判断目录是否符合验证 overlay 根目录特征。

    参数：
    - path_overlay_home: 待判断的 overlay 根目录路径。

    返回：
    - 符合 overlay 命名特征且不落在供应商 tools/synopsys 路径内时返回 ``True``。

    异常：
    - 无显式异常；路径解析沿用 ``Path.resolve`` 的默认行为。
    """

    # 先把路径各级目录统一规整成小写集合，便于检查是否落在供应商安装目录树里。
    set_lower_parts = {str_part.lower() for str_part in path_overlay_home.resolve(strict=False).parts}  # 当前路径逐级目录名的小写集合

    # 明确落在 synopsys/tools 层级中的路径按供应商安装目录处理，不允许直接打补丁。
    if "synopsys" in set_lower_parts and "tools" in set_lower_parts:

        # 遇到供应商目录时直接拒绝，防止把验证补丁误写回正式安装树。
        return False

    # 只有 overlay 常见命名才视为允许扫描或修补的验证目录。
    return path_overlay_home.name in {"vcs_overlay", "verdi_overlay"} or path_overlay_home.name.endswith("_overlay")

# 生成 overlay 拒绝结果，统一对外报告为什么当前路径不能被修补。
def _reject(path_overlay_home: Path) -> dict[str, object]:
    """
    构建 overlay 拒绝结果字典。

    参数：
    - path_overlay_home: 被拒绝的 overlay 路径。

    返回：
    - 返回包含 ``status``、``overlay_home`` 与 ``reason`` 的拒绝结果字典。

    异常：
    - 无显式异常；字典构造与字符串转换沿用 Python 默认行为。
    """

    # 返回稳定拒绝结构，便于调用方和测试统一断言当前路径为何不可修补。
    return {
        "status": "rejected",
        "overlay_home": str(path_overlay_home),
        "reason": "refusing to patch outside a validation overlay home",
    }

# 在二进制字节串中查找全部补丁命中偏移，供扫描与真正补丁阶段共用。
def find_offsets(
    bytes_payload: bytes,
    bytes_pattern: bytes = BYTES_SEARCH_PATTERN,
) -> list[int]:
    """
    查找字节串中全部模式命中偏移。

    参数：
    - bytes_payload: 待扫描的原始二进制字节串。
    - bytes_pattern: 需要搜索的目标字节模式。

    返回：
    - 返回按出现顺序排列的全部命中偏移列表。

    异常：
    - 无显式异常；字节串查找沿用标准库默认行为。
    """

    # 先准备命中偏移列表，后续按查找顺序逐个累积结果。
    list_offsets: list[int] = []  # 当前字节串中全部模式命中偏移

    # 记录下一次搜索起点，确保可以继续查到后续重叠或相邻命中。
    int_search_start = 0  # 当前模式查找起始偏移

    # 反复搜索直到找不到下一处命中为止。
    while True:

        # 从当前搜索起点开始查找下一处模式命中。
        int_offset = bytes_payload.find(bytes_pattern, int_search_start)  # 本轮搜索命中的偏移位置

        # 没有下一处命中时直接返回当前累积列表。
        if int_offset < 0:

            # 返回全部已找到的模式偏移，供上层直接决定是否需要补丁。
            return list_offsets

        # 记录这一处命中偏移，供扫描结果和补丁结果共同复用。
        list_offsets.append(int_offset)

        # 把下一次搜索起点移到当前命中之后，继续寻找后续匹配。
        int_search_start = int_offset + 1  # 下一轮搜索从当前命中后一个字节继续

# 为单个候选 libucapi 路径收集存在性、哈希和命中偏移等扫描信息。
def candidate_info(path_overlay_home: Path) -> list[dict[str, object]]:
    """
    收集 overlay 候选 libucapi 路径的扫描信息。

    参数：
    - path_overlay_home: 当前验证 overlay 根目录。

    返回：
    - 返回每个候选相对路径对应的一条扫描信息字典列表。

    异常：
    - 无显式异常；文件读取失败会沿用底层异常行为。
    """

    # 累积全部候选路径的扫描信息，供 scan_overlay 统一决定最终状态。
    list_candidate_infos: list[dict[str, object]] = []  # 当前 overlay 全部候选库文件的扫描信息列表

    # 按候选相对路径顺序逐个构建扫描信息，保证输出顺序稳定。
    for str_candidate_rel in TUPLE_CANDIDATE_RELS:

        # 拼出当前候选库文件绝对路径，供 exists/read_bytes/sha256 共用。
        path_candidate = path_overlay_home / str_candidate_rel  # 当前候选 libucapi 文件路径

        # 先构建默认扫描信息，再在文件存在时补齐 ELF、哈希与命中偏移。
        dict_candidate_info = {
            "rel": str_candidate_rel,  # 当前候选库文件的相对路径
            "path": str(path_candidate),  # 当前候选库文件的绝对路径字符串
            "exists": path_candidate.exists(),  # 当前候选库文件是否真实存在
            "is_symlink": path_candidate.is_symlink(),  # 当前候选库文件是否仍是符号链接入口
            "is_elf": False,  # 默认先假定不是 ELF，存在时再按文件头修正
            "sha256": "",  # 默认未读取文件前没有可报告的摘要值
            "offsets": [],  # 默认没有发现任何补丁命中偏移
        }

        # 只有候选文件真实存在时，才继续读取二进制内容做更细扫描。
        if path_candidate.exists():

            # 读取候选库文件原始字节，供 ELF 识别、哈希与补丁命中搜索复用。
            bytes_candidate_image = path_candidate.read_bytes()  # 当前候选库文件的原始二进制内容

            # 把存在文件的扫描结果补写回候选信息字典，保证输出字段齐全。
            dict_candidate_info.update(
                {
                    "is_elf": bytes_candidate_image.startswith(b"\x7fELF"),
                    "sha256": sha256_bytes(bytes_candidate_image),
                    "offsets": find_offsets(bytes_candidate_image),
                }
            )

        # 把当前候选路径的扫描信息加入结果列表，供上层统一做状态归类。
        list_candidate_infos.append(dict_candidate_info)

    # 返回全部候选路径扫描结果，供 scan_overlay 决定最终 scan status。
    return list_candidate_infos

# 扫描 overlay 中的候选库文件，判断是否存在可打补丁的命中点。
def scan_overlay(path_overlay_home: Path | str) -> dict[str, object]:
    """
    扫描 overlay 中候选 libucapi 文件的补丁命中情况。

    参数：
    - path_overlay_home: 待扫描的 overlay 根目录路径或字符串。

    返回：
    - 返回包含扫描状态、候选文件列表与命中项列表的结构化结果字典。

    异常：
    - 无显式异常；文件读取失败会沿用底层异常行为。
    """

    # 先把输入统一规整成 Path，避免后续判断和路径拼接混用字符串。
    path_overlay = Path(path_overlay_home)  # 当前待扫描的 overlay 根目录

    # overlay 命名或路径边界不合法时，直接返回拒绝结果而不继续读文件。
    if not is_overlay_home(path_overlay):

        # 对非 overlay 目录统一返回拒绝结果，阻止误扫正式安装树。
        return _reject(path_overlay)

    # 重新抓取 overlay 候选库快照，后续要区分“没有库文件”和“库文件存在但不命中”两类诊断。
    list_candidate_infos = candidate_info(path_overlay)  # 按固定候选顺序收集到的原始扫描快照

    # 只保留真实存在的候选文件，供 no_match 与 no_candidate 状态区分复用。
    list_existing_candidates = [dict_candidate for dict_candidate in list_candidate_infos if dict_candidate["exists"]]  # 当前 overlay 中真实存在的候选文件列表

    # 只保留至少命中一个偏移的候选文件，供真正补丁阶段直接使用。
    list_matches = [dict_candidate for dict_candidate in list_existing_candidates if dict_candidate["offsets"]]  # 当前 overlay 中真正命中补丁点的候选文件列表

    # 命中候选存在时优先认定为 match，供 apply 模式继续进入真正打补丁流程。
    if list_matches:

        # 有命中偏移说明 overlay 中至少存在一个可以实际修补的候选文件。
        str_status = "match"  # 当前扫描状态为已找到可修补命中

    # 没有命中但存在候选文件时，说明文件在位只是补丁字节模式未出现。
    elif list_existing_candidates:

        # 候选文件存在但没有命中补丁位点，扫描状态归类为 no_match。
        str_status = "no_match"  # 当前扫描状态为候选存在但未命中

    # 一个候选文件都不存在时，说明 overlay 布局里根本没有目标库。
    else:

        # 所有候选路径都不存在时，把当前扫描状态归类为 no_candidate。
        str_status = "no_candidate"  # 当前扫描状态为不存在任何候选库文件

    # 返回完整扫描结果，供测试、CLI 与 apply 模式统一消费。
    return {
        "status": str_status,
        "overlay_home": str(path_overlay),
        "search_pattern": BYTES_SEARCH_PATTERN.hex(),
        "patch_bytes": BYTES_PATCH_BYTES.hex(),
        "candidates": list_candidate_infos,
        "matches": list_matches,
    }

# 判断目标文件实际解析路径是否仍落在 overlay 内，避免越界写回供应商目录。
def _is_inside(path_target: Path, path_root: Path) -> bool:
    """
    判断路径解析后是否位于指定根目录内部。

    参数：
    - path_target: 待检查的目标路径。
    - path_root: 允许的根目录路径。

    返回：
    - ``path_target`` 解析后仍位于 ``path_root`` 内部时返回 ``True``。

    异常：
    - 无显式异常；相对路径判断失败会被收敛为 ``False``。
    """

    # 尝试把目标路径解析后相对于根目录取相对路径，以验证是否越界。
    try:

        # 解析后仍能成功 relative_to 根目录时，说明目标仍处在 overlay 边界内。
        path_target.resolve(strict=False).relative_to(path_root.resolve(strict=False))

        # 解析路径位于根目录内部时返回 True，允许直接在 overlay 中改写。
        return True

    # 无法相对于根目录取相对路径时，说明目标已经跳出 overlay 边界。
    except ValueError:

        # 越界目标必须走物化副本路径，不能原位改写正式库文件。
        return False

# 决定真正要写回补丁的文件路径，必要时把越界目标重定向到 overlay 私有副本。
def _materialized_patch_path(path_candidate: Path, path_overlay_home: Path) -> Path:
    """
    计算当前候选库文件真正应写入补丁的位置。

    参数：
    - path_candidate: 扫描命中的候选库文件路径。
    - path_overlay_home: 当前 overlay 根目录。

    返回：
    - 若候选文件仍位于 overlay 内则返回原路径；否则返回 overlay 私有补丁副本路径。

    异常：
    - 解析真实目标时可能抛出底层文件系统异常。
    """

    # 解析候选路径当前指向的真实文件，供后续判断是否越出 overlay 边界。
    path_resolved_target = path_candidate.resolve(strict=True)  # 当前候选路径解析后的真实目标文件路径

    # 符号链接需要保留逻辑入口路径，后续物化函数会把它替换成 overlay 本地副本。
    if path_candidate.is_symlink():

        # 对符号链接返回原入口路径，便于后续物化逻辑就地替换链接。
        return path_candidate

    # 非符号链接且真实文件仍在 overlay 内时，可以直接在原位置写回补丁。
    if _is_inside(path_resolved_target, path_overlay_home):

        # 真正位于 overlay 内部的常规文件允许直接原位改写。
        return path_candidate

    # 准备 overlay 私有补丁目录，用于容纳从供应商或外部目录物化出来的副本。
    path_patch_dir = path_overlay_home / "ucapi_patch_lib"  # overlay 内部用于放置补丁副本的目录路径

    # 确保私有补丁目录存在，后续才能把外部目标物化成 overlay 自己的副本。
    path_patch_dir.mkdir(parents=True, exist_ok=True)

    # 返回 overlay 私有副本路径，避免把补丁写回到 overlay 外部真实文件。
    return path_patch_dir / "libucapi.so"

# 把候选库文件物化成 overlay 内部可安全改写的实际文件，并保留原始权限位。
def _materialize_overlay_file(path_candidate: Path, path_overlay_home: Path) -> Path:
    """
    把候选库文件物化为 overlay 内可写补丁副本。

    参数：
    - path_candidate: 当前扫描命中的候选库文件路径。
    - path_overlay_home: 当前 overlay 根目录。

    返回：
    - 返回最终可以安全写入补丁的 overlay 内文件路径。

    异常：
    - 文件读取、写入、chmod 或 unlink 失败时沿用底层异常行为。
    """

    # 解析候选路径真实指向的文件，确保物化时复制的是当前生效库文件内容。
    path_resolved_target = path_candidate.resolve(strict=True)  # 当前候选路径解析后的真实库文件路径

    # 计算应该把补丁写到哪里，可能是原位文件，也可能是 overlay 私有副本。
    path_materialized = _materialized_patch_path(path_candidate, path_overlay_home)  # 当前候选文件最终要物化到的路径

    # 先读取真实目标文件全部字节，供后续写入 overlay 内可改写副本。
    bytes_target_image = path_resolved_target.read_bytes()  # 当前真实库文件的原始二进制内容

    # 记录真实目标文件现有权限位，保证物化后可执行属性不会意外丢失。
    int_target_mode = path_resolved_target.stat().st_mode  # 当前真实库文件原始权限位

    # 若需要在原候选路径处替换符号链接，就先删除链接本身再写入真实文件副本。
    if path_materialized == path_candidate and path_candidate.is_symlink():

        # 移除 overlay 中原有符号链接，后续才能在同路径写入普通文件副本。
        path_candidate.unlink()

    # 把真实目标内容写入最终物化路径，形成 overlay 内可安全改写的普通文件。
    path_materialized.write_bytes(bytes_target_image)

    # 把原始权限位复制到物化文件，保持 loader 对该库文件的可执行属性预期。
    path_materialized.chmod(int_target_mode)

    # 返回最终可安全写回补丁的 overlay 内文件路径。
    return path_materialized

# 按给定偏移把补丁字节写入目标文件，并返回补丁前后哈希与激活路径信息。
def _patch_file(
    path_candidate: Path,
    path_overlay_home: Path,
    list_offsets: list[int],
) -> dict[str, object]:
    """
    对单个候选库文件执行补丁写回。

    参数：
    - path_candidate: 当前扫描命中的候选库文件路径。
    - path_overlay_home: 当前 overlay 根目录。
    - list_offsets: 需要写入补丁字节的偏移列表。

    返回：
    - 返回包含补丁路径、激活路径、原始/补丁后哈希与偏移列表的结果字典。

    异常：
    - 物化、文件读写或偏移改写失败时沿用底层异常行为。
    """

    # 先把候选文件物化成 overlay 内真正可写的普通文件，避免越界改写正式安装树。
    path_materialized = _materialize_overlay_file(path_candidate, path_overlay_home)  # 当前候选文件在 overlay 内的可写补丁路径

    # 读取补丁前文件内容，供原始哈希计算与 bytearray 原地修改复用。
    bytes_original_image = path_materialized.read_bytes()  # 当前补丁目标文件的原始二进制内容

    # 使用 bytearray 进行原位偏移覆盖，避免每次改写都重新拼接大量字节串。
    bytearray_patched_image = bytearray(bytes_original_image)  # 当前补丁目标文件的可修改字节数组

    # 逐个命中偏移写入补丁字节，确保所有命中点都被一致替换。
    for int_offset in list_offsets:

        # 在当前命中偏移处写入固定补丁字节，覆盖原始调用序列。
        bytearray_patched_image[int_offset : int_offset + len(BYTES_PATCH_BYTES)] = BYTES_PATCH_BYTES  # 当前命中偏移对应的原始调用序列被替换为 NOP 补丁

    # 把补丁后的完整字节数组写回物化文件，使 overlay 内目标库真正生效。
    path_materialized.write_bytes(bytes(bytearray_patched_image))

    # 返回单文件补丁结果，供 apply 模式汇总 activation path 和 loader 提示。
    return {
        "source_path": str(path_candidate),
        "path": str(path_materialized),
        "activation_path": str(path_materialized.parent),
        "offsets": list_offsets,
        "original_sha256": sha256_bytes(bytes_original_image),
        "patched_sha256": sha256_bytes(bytes(bytearray_patched_image)),
        "patch_bytes": BYTES_PATCH_BYTES.hex(),
    }

# 对 overlay 中全部命中候选执行补丁，并写出供后续加载器配置使用的 manifest。
def apply_overlay_patch(path_overlay_home: Path | str) -> dict[str, object]:
    """
    对 overlay 中命中的 libucapi 候选文件执行补丁。

    参数：
    - path_overlay_home: 待修补的 overlay 根目录路径或字符串。

    返回：
    - 返回包含补丁结果列表、激活路径和 loader 提示的 manifest 字典。

    异常：
    - 文件扫描、物化、读写或 manifest 写入失败时沿用底层异常行为。
    """

    # 先把输入统一规整成 Path，供扫描、物化与 manifest 写回共用。
    path_overlay = Path(path_overlay_home)  # 当前待修补的 overlay 根目录

    # 先执行扫描流程，只有命中状态才允许真正进入补丁写回阶段。
    dict_scan_result = scan_overlay(path_overlay)  # 当前 overlay 的预扫描结果字典

    # 没有实际命中时直接复用扫描结果返回，避免无意义地写 manifest 或改动文件。
    if dict_scan_result["status"] != "match":

        # 只有真正命中补丁点的 overlay 才允许进入 apply 流程。
        return dict_scan_result

    # 累积全部补丁后的单文件结果，供 activation path 汇总与 manifest 写回复用。
    list_patched_results: list[dict[str, object]] = []  # 当前 overlay 全部命中候选的补丁结果列表

    # 对每个命中候选逐条执行真正补丁，保证多副本场景也能全部同步改写。
    for dict_match in list(dict_scan_result["matches"]):

        # 把当前命中候选交给补丁函数，生成单文件补丁结果并加入汇总列表。
        list_patched_results.append(
            _patch_file(
                Path(str(dict_match["path"])),
                path_overlay,
                [int(obj_offset) for obj_offset in list(dict_match["offsets"])],
            )
        )

    # 先去重收集所有补丁目标的父目录，保证后续 loader 提示不会重复同一路径。
    set_activation_paths = {
        str(Path(str(dict_patched["path"])).parent)  # 当前补丁文件所在的激活目录
        for dict_patched in list_patched_results  # 当前单文件补丁结果
    }  # 当前补丁结果对应的去重激活目录集合

    # 汇总全部补丁目标父目录，供调用方设置 LD_LIBRARY_PATH 或其他 loader 搜索路径。
    list_activation_paths = sorted(set_activation_paths)  # 当前补丁结果对应的稳定激活目录列表

    # 只要有任一补丁文件落在 ucapi_patch_lib，就提示调用方把该目录放到更高优先级。
    bool_uses_patch_lib = any(  # 当前补丁结果是否触发了私有补丁目录优先级提示
        Path(str(dict_patched["path"])).parent.name == "ucapi_patch_lib"  # 当前补丁文件是否落在私有补丁目录
        for dict_patched in list_patched_results  # 逐条检查每个补丁结果最终写入的父目录
    )

    # 预先整理 loader 额外提示文本，避免在 manifest 字典里嵌入难读的多行条件表达式。
    str_loader_warning = (
        "LD_LIBRARY_PATH must place ucapi_patch_lib before the VCS wrapper BASE_STRING/lib"  # 使用私有补丁目录时必须提升其 loader 搜索优先级
        if bool_uses_patch_lib  # 当前补丁结果确实落到了 overlay 私有补丁目录
        else ""  # 没有使用私有补丁目录时无需附加任何 loader 警告
    )  # manifest 中记录给调用方的 loader 额外提示文本

    # 组装最终 manifest，供测试、CLI 与下游 loader 配置步骤共同消费。
    dict_manifest = {
        "status": "patched",  # 当前 apply 流程已经完成实际补丁写回
        "overlay_home": str(path_overlay),  # 当前被修补的 overlay 根目录
        "search_pattern": BYTES_SEARCH_PATTERN.hex(),  # 本次扫描命中的原始搜索模式十六进制文本
        "patched": list_patched_results,  # 当前 overlay 内每个命中库文件的补丁结果
        "activation_paths": list_activation_paths,  # 调用方需要优先加入 loader 搜索路径的目录列表
        "ld_library_paths": list_activation_paths,  # 与 activation_paths 同步的 LD_LIBRARY_PATH 推荐值
        "effective_loader_warning": str_loader_warning,  # 私有补丁目录需要高优先级时给出的额外 loader 提示
    }

    # 把补丁 manifest 写回 overlay 根目录，便于调用方后续直接读取 loader 指引。
    (path_overlay / STR_MANIFEST_NAME).write_text(
        json.dumps(dict_manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # 返回已写回 manifest 的结构化结果，供测试与 CLI 直接消费。
    return dict_manifest

# 解析命令行参数并按需执行 scan/apply，再输出摘要或显式 JSON 协议。
def main(argv: list[str] | None = None) -> int:
    """
    运行 overlay 扫描/补丁命令行入口并返回退出码。

    参数：
    - argv: 可选命令行参数列表；传入 ``None`` 时使用进程默认参数。

    返回：
    - 扫描得到 ``match``、``no_match``、``no_candidate``，或补丁得到 ``patched`` 时返回 ``0``；其余状态返回 ``1``。

    异常：
    - 参数解析失败时由 ``argparse`` 抛出并终止进程；文件系统异常沿用底层行为。
    """

    # 创建命令行解析器，集中声明 overlay 根目录、执行模式和 JSON 输出协议。
    parser = argparse.ArgumentParser(description="Scan or patch libucapi.so inside a VCS/Verdi validation overlay.")  # 当前 CLI 的参数解析器

    # 调用方必须显式给出 overlay 根目录，避免脚本隐式猜目录并误改错误位置。
    parser.add_argument("--overlay-home", type=Path, required=True)

    # 通过 mode 选择只扫描还是实际补丁，默认保持最保守的 scan 模式。
    parser.add_argument("--mode", choices=("scan", "apply"), default="scan")

    # 显式请求 JSON 协议时，标准输出允许写出单个结构化 JSON 对象。
    parser.add_argument("--json", action="store_true")

    # 解析当前命令行参数，得到 overlay 路径、执行模式与输出模式。
    args = parser.parse_args(argv)  # 当前 CLI 解析得到的参数对象

    # scan 模式只做结构化扫描，apply 模式则在命中后真正写回补丁。
    dict_result = scan_overlay(args.overlay_home) if args.mode == "scan" else apply_overlay_patch(args.overlay_home)  # 当前 CLI 生成的结构化执行结果

    # 先把结构化结果里的状态抽成普通字符串，避免摘要输出阶段直接访问字典结构。
    str_status = str(dict_result["status"])  # 当前扫描或补丁结果的终端摘要状态

    # JSON 模式下按模块文档声明输出单个结构化对象，供上游自动化直接消费。
    if args.json:

        # 把完整结构化结果写到标准输出，避免混入其他人类可读摘要文本。
        json.dump(dict_result, sys.stdout, indent=2, sort_keys=True)

        # 为 JSON 协议输出补一个换行，避免 shell 提示符紧贴在 JSON 末尾。
        sys.stdout.write("\n")

    # 默认模式下只输出带前缀的短状态摘要，不把结构化 payload 直接刷到终端。
    elif str_status in {"match", "patched"}:

        # 成功找到命中或成功打补丁时输出 INFO 摘要，便于终端快速判断进展。
        print(f"> INFO: [Python] overlay status {str_status}")

    # 拒绝或其他失败状态使用 ERR 摘要，提醒调用方不要继续把当前 overlay 当作可用目标。
    else:

        # 非成功状态输出 ERR 摘要，细节应改用 --json 查看结构化结果。
        print(f"> ERR: [Python] overlay status {str_status}")

    # 只有扫描/补丁结果落在可接受状态集合里时，CLI 才返回零退出码。
    return 0 if str_status in {"match", "no_match", "no_candidate", "patched"} else 1

# 只有脚本被直接执行时才启动 CLI，避免导入测试模块时意外运行扫描或补丁逻辑。
if __name__ == "__main__":

    # 把 main 返回值转换成进程退出码，供 shell 与自动化流程直接判断成败。
    raise SystemExit(main())
