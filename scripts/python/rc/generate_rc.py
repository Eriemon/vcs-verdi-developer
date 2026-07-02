#!/usr/bin/env python3
"""根据场景配置生成 Verdi signal restore RC 文本。"""
from __future__ import annotations

# 引入命令行、标准输出、路径与正则接口，支撑场景解析、RC 组装与 CLI 输出。
import argparse
import re
import sys
from pathlib import Path

# 维护单个信号条目的结构化类型别名。
ScenarioSignal = dict[str, str]  # 单个信号条目的路径、进制、颜色、高度与别名字段集合。

# 维护单个分组条目的结构化类型别名。
ScenarioGroup = dict[str, str | list[ScenarioSignal]]  # 单个分组条目的分组名、默认颜色与信号列表。

# 维护虚拟总线映射的结构化类型别名。
ScenarioBuses = dict[str, list[str]]  # 虚拟总线名到展开后信号路径列表的映射。

# 维护单个 marker 条目的结构化类型别名。
ScenarioMarker = dict[str, str]  # 单个 marker 条目的时间、名称与颜色字段集合。

# 维护 base 配置全集的结构化类型别名。
BaseEnvMap = dict[str, dict[str, str]]  # base 名称到别名映射表的完整配置集合。

# 统一维护支持的颜色到 Verdi 内部颜色枚举的映射。
COLOR_MAP = dict(  # 颜色文本到 Verdi 内部颜色 ID 的映射表。
    red="ID_RED5",  # 首个颜色条目也作为这张静态映射表的视觉锚点。
    green="ID_GREEN5",  # 绿色条目映射到 Verdi 的深绿色常量。
    blue="ID_BLUE5",  # 蓝色条目映射到 Verdi 的深蓝色常量。
    yellow="ID_YELLOW5",  # 黄色条目映射到 Verdi 的高亮黄色常量。
    cyan="ID_CYAN5",  # 青色条目映射到 Verdi 的深青色常量。
    magenta="ID_MAGENTA5",  # 品红条目映射到 Verdi 的深品红常量。
    white="ID_WHITE",  # 白色条目直接复用 Verdi 的白色常量。
    black="ID_BLACK",  # 黑色条目直接复用 Verdi 的黑色常量。
    gray="ID_GRAY5",  # 美式拼写 gray 映射到 Verdi 的灰色常量。
    grey="ID_GRAY5",  # 英式拼写 grey 与 gray 共用同一个灰色常量。
    orange="ID_ORANGE5",  # 橙色条目映射到 Verdi 的橙色常量。
    pink="ID_PINK5",  # 粉色条目映射到 Verdi 的粉色常量。
    brown="ID_BROWN5",  # 棕色条目映射到 Verdi 的棕色常量。
    purple="ID_PURPLE5",  # 紫色条目映射到 Verdi 的紫色常量。
    lightcyan="ID_CYAN3",  # 亮青色条目使用较浅一级的青色常量。
)  # 该表只做文本映射，不承担颜色合法性验证。

# 把默认配置目录集中成常量，避免 CLI 入口散落字面量。
DEFAULT_CONFIG_DIR = Path("config")  # CLI 未显式覆盖时，默认从当前目录下的 config 子目录读取配置。

# 把 marker 的缺省颜色集中成常量。
DEFAULT_MARKER_COLOR = "white"  # marker 未显式指定颜色时沿用白色，保持旧行为稳定。

# 把信号高度的缺省值集中成常量。
DEFAULT_SIGNAL_HEIGHT = "15"  # 未指定高度时沿用原始实现的默认波形高度。

# 把组头最少字段数集中成常量。
GROUP_HEADER_MIN_COLUMNS = 1  # 分组头至少需要给出组名。

# 把组内信号行最少字段数集中成常量。
GROUP_SIGNAL_MIN_COLUMNS = 3  # 分组信号行至少需要路径、进制和颜色三列。

# 把 marker 行最少字段数集中成常量。
MARKER_MIN_COLUMNS = 2  # marker 至少需要时间和值班名称两列。

# 预编译空白分割正则，避免逐行解析时重复编译。
RE_MULTISPACE = re.compile(r"\s+")  # GROUPS 与 MARKERS 段都按一个或多个空白切分字段。

# 统一维护受支持的数字显示格式到 Verdi 选项的映射。
RADIX_OPTION_MAP = dict(  # 数字显示格式到 Verdi 选项文本的映射表。
    hex="-HEX",  # 首个进制条目也作为这张静态映射表的视觉锚点。
    bin="-BIN",  # 二进制显示直接映射到 Verdi 的 BIN 选项。
    dec="-DEC",  # 十进制显示直接映射到 Verdi 的 DEC 选项。
    oct="-OCT",  # 八进制显示直接映射到 Verdi 的 OCT 选项。
)  # analog 作为特殊模式单独处理，不放进这张表里。

# 移除配置行中的 `#` 注释与首尾空白，后续解析统一基于净文本进行。
def _strip_comment(str_line: str) -> str:
    """
    去掉配置行中的注释片段与首尾空白。

    :param str_line: 原始配置行文本，dtype=str，unit=text
    :return: 返回去掉注释后的净文本，dtype=str，unit=text
    """

    # 这里只保留 `#` 之前的正文，再统一裁掉首尾空白。
    return str_line.split("#", 1)[0].strip()

# 尝试从净文本里识别 `[SECTION]` 形式的段头；不命中时返回空字符串。
def _section_name(str_raw_line: str) -> str:
    """
    识别净文本是否是配置段头。

    :param str_raw_line: 已去掉注释后的配置行文本，dtype=str，unit=text
    :return: 命中段头时返回段名，否则返回空字符串，dtype=str，unit=text
    """

    # 只有完整包裹在方括号中的文本才视为段头。
    if str_raw_line.startswith("[") and str_raw_line.endswith("]"):

        # 命中段头时直接返回去掉方括号后的段名。
        return str_raw_line[1:-1].strip()

    # 非段头行统一返回空字符串，调用方据此继续走普通数据行解析。
    return ""

# 按一个或多个空白切分配置字段，避免手写 `split` 与额外空列过滤逻辑。
def _split_columns(str_raw_line: str) -> list[str]:
    """
    按场景配置规则拆分字段列表。

    :param str_raw_line: 已去掉注释后的配置行文本，dtype=str，unit=text
    :return: 返回按空白切分后的字段列表，dtype=list[str]，unit=collection
    """

    # 这里只有非空净文本会传进来，因此切分结果天然至少包含一个字段。
    return RE_MULTISPACE.split(str_raw_line)

# 读取 `scn_base.lst` 并解析每个 `[base]` 段下的别名映射。
def _parse_base_file(path_base_file: Path) -> BaseEnvMap:
    """
    解析基础别名配置文件。

    :param path_base_file: `scn_base.lst` 文件路径，dtype=Path，unit=path
    :return: 返回 base 名称到别名映射表的全集，dtype=BaseEnvMap，unit=mapping
    :raises ValueError: 当基础配置文件缺失时抛出显式错误。
    """

    # 先准备完整 base 配置映射，后续会按段名逐项填充。
    dict_base_env_map: dict[str, dict[str, str]] = {}  # 每个 base 名字都会落成一个独立的键值映射。

    # 当前正在解析的 base 名称在未进入任何段前保持空字符串。
    str_current_base = ""  # 只有命中合法段头后，后续 `key=value` 才会写入这个段。

    # 基础配置文件缺失时直接阻断渲染流程，避免生成半成品 RC。
    if not path_base_file.exists():

        # 统一使用仓库要求的 `> ERR: [Python]` 前缀抛出显式错误。
        raise ValueError(f"> ERR: [Python] base file not found: {path_base_file}")

    # 逐行读取基础配置文本，按段头和 `key=value` 规则解析。
    for str_line in path_base_file.read_text(encoding="utf-8").splitlines():

        # 先去掉注释与首尾空白，空行会在下一步被统一跳过。
        str_raw_line = _strip_comment(str_line)  # 当前行去注释后的净文本。

        # 空行或纯注释行不参与任何配置解析。
        if not str_raw_line:

            # 当前行没有有效内容，直接进入下一行。
            continue

        # 优先尝试把当前净文本识别成新的 `[base]` 段头。
        str_section = _section_name(str_raw_line)  # 当前行若为段头，则这里会得到段名。

        # 命中段头时新建对应 base 映射并切换写入上下文。
        if str_section:

            # 当前段头名字会成为后续 `key=value` 写入的目标 base。
            str_current_base = str_section  # 后续直到下一个段头前都写入这个 base。

            # 为当前 base 准备独立映射，后续别名键值都落在这里。
            dict_base_env_map[str_current_base] = {}  # 每个 base 共享同一种 `str -> str` 映射结构。

            # 段头本身不再继续走 `key=value` 解析。
            continue

        # 只有已经进入某个 base 段且当前行含 `=` 时，才视为合法别名声明。
        if str_current_base and "=" in str_raw_line:

            # 这里把左值与右值拆开，保留右值中的剩余 `=` 文本。
            str_key, str_value = str_raw_line.split("=", 1)  # 当前别名项的键和值原文。

            # 去掉键值两侧空白后写入当前 base 映射。
            dict_base_env_map[str_current_base][str_key.strip()] = str_value.strip()  # 当前 base 的单条别名映射。

    # 返回完整的基础别名配置集合，供场景解析与路径展开复用。
    return dict_base_env_map

# 递归展开单个别名路径，兼容别名链和 `alias.member` 形式的层级引用。
def _resolve_alias(str_path: str, dict_env: dict[str, str], set_seen: set[str] | None = None) -> str:
    """
    根据 base 别名表展开单个逻辑路径。

    :param str_path: 待展开的逻辑路径或别名文本，dtype=str，unit=text
    :param dict_env: 当前 base 段的别名映射表，dtype=dict[str, str]，unit=mapping
    :param set_seen: 递归展开链路中的已访问别名集合，dtype=set[str] | None，unit=collection
    :return: 返回展开后的逻辑路径文本，dtype=str，unit=text
    """

    # 递归展开时复用调用链上的已访问集合，防止循环引用导致死递归。
    set_seen = set_seen or set()  # 同一轮解析里访问过的别名会落进这里。

    # 当当前路径已经访问过时，直接返回原文并中断递归链。
    if str_path in set_seen:

        # 循环引用场景下维持原文本返回，保持与旧实现一致的容错行为。
        return str_path

    # 完整路径直接命中 base 别名表时，继续递归展开它对应的真实值。
    if str_path in dict_env:

        # 递归前先登记当前别名，防止后续回到同一个名字。
        set_seen.add(str_path)

        # 继续递归展开别名指向的值，直到落成最终文本路径。
        return _resolve_alias(dict_env[str_path], dict_env, set_seen)

    # 不含层级点号时说明已没有进一步拆解空间，直接返回原文。
    if "." not in str_path:

        # 普通信号名或已经展开完成的路径会在这里直接返回。
        return str_path

    # 对 `alias.member` 形式只展开第一段别名，保留后缀成员路径。
    str_prefix, str_rest = str_path.split(".", 1)  # 别名前缀与剩余层级路径文本。

    # 只有第一段前缀命中别名表时，才继续做 `前缀展开 + 剩余层级` 的重组。
    if str_prefix in dict_env:

        # 递归前先登记命中的前缀别名，避免别名链在前缀层循环。
        set_seen.add(str_prefix)

        # 把展开后的前缀与原始剩余层级重新拼回完整层级路径。
        return f"{_resolve_alias(dict_env[str_prefix], dict_env, set_seen)}.{str_rest}"

    # 当前文本不命中任何可展开别名时，保留原样返回。
    return str_path

# 把 `top.sub.sig[0]` 一类层级路径转换成 Verdi 使用的 `/top/sub/sig\[0\]` 形式。
def _verdi_path(str_signal_path: str) -> str:
    """
    把逻辑层级路径转换成 Verdi RC 路径。

    :param str_signal_path: 点号分层的逻辑路径文本，dtype=str，unit=text
    :return: 返回适合写入 Verdi RC 的层级路径，dtype=str，unit=text
    """

    # 先把层级点号统一替换成 Verdi 所需的斜杠路径形式。
    str_converted = str_signal_path.replace(".", "/")  # 当前信号路径的斜杠化结果。

    # Verdi RC 路径统一要求从根层级 `/` 开始。
    if not str_converted.startswith("/"):

        # 非绝对层级路径时主动补出根斜杠。
        str_converted = f"/{str_converted}"  # 当前路径补齐根层级后的文本。

    # 返回同时完成根路径与方括号转义的最终 Verdi 路径。
    return str_converted.replace("[", "\\[").replace("]", "\\]")

# 把用户颜色文本转换成 Verdi `-color` 选项；未知颜色保持空字符串。
def _color_option(str_color: str) -> str:
    """
    把颜色文本转换成 Verdi `-color` 选项。

    :param str_color: 用户配置中的颜色文本，dtype=str，unit=text
    :return: 返回 Verdi 颜色选项；未知颜色时返回空字符串，dtype=str，unit=text
    """

    # 空颜色代表当前条目不输出任何颜色覆盖选项。
    if not str_color:

        # 调用方会据此决定是否继承组级默认颜色。
        return ""

    # 颜色表统一按小写键查询，兼容用户输入的大小写差异。
    str_color_id = COLOR_MAP.get(str_color.lower(), "")  # 当前颜色映射到的 Verdi 内部颜色标识。

    # 命中映射时输出 `-color <ID>`，未知颜色则保持空字符串。
    return f"-color {str_color_id}" if str_color_id else ""

# 把 `hex/bin/dec/oct/analog` 文本转换成 Verdi 的显示选项。
def _radix_option(str_radix: str) -> str:
    """
    把显示格式文本转换成 Verdi 选项。

    :param str_radix: 配置中的显示格式文本，dtype=str，unit=text
    :return: 返回 Verdi 进制或模拟显示选项，dtype=str，unit=text
    """

    # analog 走专门的 Verdi 选项，不与数字制式共用映射表。
    if str_radix.lower() == "analog":

        # 返回模拟波形专用选项，供后续 `addSignal` 直接拼接。
        return "-analog"

    # 其余受支持的数字制式统一走固定映射表。
    return RADIX_OPTION_MAP.get(str_radix.lower(), "")

# 解析 `GROUPS` 段中的分组头，格式为 `<index>. <group-name> [group-color]`。
def _append_group_header(list_groups: list[ScenarioGroup], list_columns: list[str]) -> None:
    """
    向场景分组列表追加一个新的组头定义。

    :param list_groups: 当前场景已解析出的分组列表，dtype=list[ScenarioGroup]，unit=collection
    :param list_columns: 组头行去掉层级索引后的字段列表，dtype=list[str]，unit=collection
    :return: 固定返回 None；当前 helper 只负责原地追加分组定义，dtype=None，unit=object
    :raises ValueError: 当组头缺少必要字段时抛出显式错误。
    """

    # 组头至少需要显式给出分组名，否则后续信号将无法归属。
    if len(list_columns) < GROUP_HEADER_MIN_COLUMNS:

        # 缺组名时直接报错，阻止生成缺少分组上下文的 RC。
        raise ValueError("> ERR: [Python] invalid group header row")

    # 当前组头的第一列固定是分组名。
    str_group_name = list_columns[0]  # 新分组的显示名称。

    # 第二列存在时作为组级默认颜色，不存在则保持空字符串。
    str_group_color = list_columns[1] if len(list_columns) > 1 else ""  # 新分组默认颜色。

    # 追加当前分组定义，并为后续信号条目预留空列表容器。
    list_groups.append(
        {
            "name": str_group_name,
            "color": str_group_color,
            "signals": [],
        }
    )

# 解析 `GROUPS` 段中的信号条目，格式为 `<index> <path> <radix> <color> [height|alias...]`。
def _append_group_signal(list_groups: list[ScenarioGroup], list_columns: list[str], str_raw_line: str) -> None:
    """
    向当前分组追加一个信号条目。

    :param list_groups: 当前场景已解析出的分组列表，dtype=list[ScenarioGroup]，unit=collection
    :param list_columns: 信号行去掉层级索引后的字段列表，dtype=list[str]，unit=collection
    :param str_raw_line: 原始净文本配置行，dtype=str，unit=text
    :return: 固定返回 None；当前 helper 只负责原地追加信号条目，dtype=None，unit=object
    :raises ValueError: 当信号行缺少分组上下文或必要字段时抛出显式错误。
    """

    # 只有已经出现至少一个组头时，信号条目才有合法归属。
    if not list_groups:

        # 缺少组头时无法推断信号归属，直接给出显式错误。
        raise ValueError(f"> ERR: [Python] invalid signal row: {str_raw_line}")

    # 信号行至少需要路径、进制和颜色三列。
    if len(list_columns) < GROUP_SIGNAL_MIN_COLUMNS:

        # 字段不足时直接阻断，避免生成结构不完整的信号定义。
        raise ValueError(f"> ERR: [Python] invalid signal row: {str_raw_line}")

    # 当前信号路径始终来自第一列。
    str_signal_path = list_columns[0]  # 信号或虚拟总线引用的原始路径文本。

    # 当前信号的显示进制来自第二列。
    str_signal_radix = list_columns[1]  # Verdi 要使用的显示格式文本。

    # 第三列是信号级颜色；`-` 会在下一步统一转成空字符串。
    str_signal_color = list_columns[2]  # 当前信号条目的颜色原文。

    # 高度缺省时保持空字符串，后续组装阶段再回退到默认高度。
    str_signal_height = ""  # 当前信号显式声明的高度文本。

    # 别名缺省为空字符串，表示直接使用真实路径或虚拟总线名。
    str_signal_alias = ""  # 当前信号显式声明的显示别名。

    # 第四列及以后作为可选扩展字段，兼容原始配置语法。
    list_optional_columns = list_columns[3:]  # 当前信号条目的高度/别名扩展列。

    # 扩展列存在时需要区分“第一个扩展列是高度”还是“第一个扩展列就是别名”。
    if list_optional_columns:

        # 第一扩展列为纯数字时，视为显式高度。
        if list_optional_columns[0].isdigit():

            # 命中纯数字时直接记录当前信号的显式高度。
            str_signal_height = list_optional_columns[0]  # 当前信号的波形高度文本。

            # 高度后若还有额外字段，则把下一列视为显示别名。
            str_signal_alias = list_optional_columns[1] if len(list_optional_columns) > 1 else ""  # 当前信号的显式别名。

        # 第一扩展列不是数字时，沿用旧语义把它视为别名。
        else:

            # 这种简写通常用于只调整波形窗口标签，不额外关心单条信号高度。
            str_signal_alias = list_optional_columns[0]  # 这里保留配置作者写下的显示标签原文。

    # 当前组记录在列表末尾，因此这里直接取最后一个分组承接信号条目。
    dict_current_group = list_groups[-1]  # 当前信号应追加到的目标分组对象。

    # 分组对象中的 `signals` 字段固定维护为信号条目列表。
    list_signals = dict_current_group["signals"]  # 当前分组已经累计的信号条目列表。

    # 这里把条目构造成稳定字典结构，后续 RC 组装阶段会直接复用这些字段。
    list_signals.append(
        {
            "path": str_signal_path,
            "radix": str_signal_radix,
            "color": "" if str_signal_color == "-" else str_signal_color,
            "height": str_signal_height,
            "alias": str_signal_alias,
        }
    )

# 解析 `GROUPS` 段中的单行文本，并把它归约成组头或信号条目。
def _parse_group_row(str_raw_line: str, list_groups: list[ScenarioGroup]) -> None:
    """
    解析单条 `GROUPS` 段配置记录。

    :param str_raw_line: 原始净文本配置行，dtype=str，unit=text
    :param list_groups: 当前场景已解析出的分组列表，dtype=list[ScenarioGroup]，unit=collection
    :return: 固定返回 None；当前 helper 只负责原地更新分组列表，dtype=None，unit=object
    """

    # 先按配置规则切分字段，后续根据索引列形态决定解析分支。
    list_columns = _split_columns(str_raw_line)  # 当前组配置行拆出的字段列表。

    # 当前行首列固定承载层级索引，例如 `1.`、`1.2`。
    str_index = list_columns[0]  # 当前配置行的层级索引文本。

    # 其余字段统一作为组头或信号条目的有效载荷。
    list_payload_columns = list_columns[1:]  # 当前配置行去掉层级索引后的字段列表。

    # 以 `.` 结尾的索引代表组头；其余索引代表组内信号条目。
    if str_index.endswith("."):

        # 组头只负责新建分组定义，不消耗后续信号解析逻辑。
        _append_group_header(list_groups, list_payload_columns)

        # 组头解析完成后可直接结束当前行处理。
        return

    # 非组头行统一按组内信号条目解析。
    _append_group_signal(list_groups, list_payload_columns, str_raw_line)

# 解析 `VIRTUAL_BUSES` 段中的一条总线定义。
def _parse_virtual_bus_row(str_raw_line: str, dict_buses: ScenarioBuses, dict_env: dict[str, str]) -> None:
    """
    解析单条虚拟总线配置。

    :param str_raw_line: 原始净文本配置行，dtype=str，unit=text
    :param dict_buses: 当前场景的虚拟总线映射，dtype=ScenarioBuses，unit=mapping
    :param dict_env: 当前 base 段的别名映射表，dtype=dict[str, str]，unit=mapping
    :return: 固定返回 None；当前 helper 只负责原地更新虚拟总线映射，dtype=None，unit=object
    """

    # 虚拟总线定义固定使用 `name = sig0, sig1, ...` 形式。
    str_bus_name, str_bus_values = str_raw_line.split("=", 1)  # 总线名与成员列表原文。

    # 当前总线名需要去掉两侧空白后作为最终字典键。
    str_bus_key = str_bus_name.strip()  # 当前虚拟总线的稳定名称。

    # 先准备当前虚拟总线的成员缓冲区，后续按声明顺序逐项追加。
    list_bus_members: list[str] = []  # 当前虚拟总线展开后的成员路径列表。

    # 顺序扫描原始成员文本，逐项完成去空白与别名展开。
    for str_member in str_bus_values.split(","):

        # 当前成员去掉两侧空白后才参与后续判定。
        str_member_name = str_member.strip()  # 当前虚拟总线成员的净文本名称。

        # 过滤空成员，避免把多余逗号扩展成空路径。
        if not str_member_name:

            # 当前成员为空白时不追加任何路径。
            continue

        # 把当前成员展开成最终逻辑路径，并按声明顺序写回成员列表。
        list_bus_members.append(_resolve_alias(str_member_name, dict_env))

    # 当前总线键写回后，后续 `addBusSignal` 会按这个顺序展开成员路径。
    dict_buses[str_bus_key] = list_bus_members  # 当前虚拟总线最终保存的成员路径列表。

# 解析 `MARKERS` 段中的一条 marker 记录。
def _append_marker_row(str_raw_line: str, list_markers: list[ScenarioMarker]) -> None:
    """
    解析单条 marker 配置。

    :param str_raw_line: 原始净文本配置行，dtype=str，unit=text
    :param list_markers: 当前场景的 marker 列表，dtype=list[ScenarioMarker]，unit=collection
    :return: 固定返回 None；当前 helper 只负责原地追加 marker 条目，dtype=None，unit=object
    :raises ValueError: 当 marker 行缺少必要字段时抛出显式错误。
    """

    # marker 行也按空白切分字段，顺序分别是时间、名称和可选颜色。
    list_columns = _split_columns(str_raw_line)  # 当前 marker 行拆出的字段列表。

    # marker 至少需要时间和值班名称两列，缺失时当前行不可恢复。
    if len(list_columns) < MARKER_MIN_COLUMNS:

        # 字段不足时直接报错，避免后续生成无效 marker 指令。
        raise ValueError(f"> ERR: [Python] invalid marker row: {str_raw_line}")

    # 第一列固定是 marker 的时间值。
    str_marker_time = list_columns[0]  # 当前 marker 的时间文本。

    # 第二列固定是 marker 的显示名称。
    str_marker_name = list_columns[1]  # 当前 marker 的名称文本。

    # 第三列存在时作为颜色；否则回退到默认白色。
    str_marker_color = list_columns[2] if len(list_columns) > 2 else DEFAULT_MARKER_COLOR  # 当前 marker 的颜色文本。

    # 当前 marker 结构会在 RC 输出阶段直接转成 `addMarker` 指令。
    list_markers.append(
        {
            "time": str_marker_time,
            "name": str_marker_name,
            "color": str_marker_color,
        }
    )

# 读取 `scn_<scenario>.lst` 并解析分组、虚拟总线与 marker 三类配置。
def _parse_scenario_file(
    path_scenario_file: Path,
    dict_env: dict[str, str],
) -> tuple[list[ScenarioGroup], ScenarioBuses, list[ScenarioMarker]]:
    """
    解析场景配置文件中的分组、虚拟总线与 marker。

    :param path_scenario_file: 目标场景配置文件路径，dtype=Path，unit=path
    :param dict_env: 当前 base 段的别名映射表，dtype=dict[str, str]，unit=mapping
    :return: 返回分组列表、虚拟总线映射与 marker 列表三元组，
        dtype=tuple[list[ScenarioGroup], ScenarioBuses, list[ScenarioMarker]]，unit=collection
    :raises ValueError: 当场景文件缺失时抛出显式错误。
    """

    # 场景文件缺失时直接阻断渲染流程，避免生成与请求场景不一致的 RC。
    if not path_scenario_file.exists():

        # 错误文本带上缺失文件路径，便于调用方直接定位配置问题。
        raise ValueError(f"> ERR: [Python] scenario file not found: {path_scenario_file}")

    # 先准备分组列表，后续 `GROUPS` 段会按声明顺序逐项填充。
    list_groups: list[ScenarioGroup] = []  # 当前场景下的全部分组定义列表。

    # 再准备虚拟总线映射，后续 `VIRTUAL_BUSES` 段会往这里追加成员展开结果。
    dict_scenario_buses = {}  # 虚拟总线名到展开后成员路径列表的映射。

    # marker 列表按声明顺序保留，供最终 RC 文本按同样顺序输出。
    list_markers: list[ScenarioMarker] = []  # 当前场景中声明的全部 marker。

    # 当前解析游标所在的段名在进入任何段前保持空字符串。
    str_section = ""  # 只有命中合法段头后，后续数据行才会按该段规则解析。

    # 顺序扫描场景配置文件中的每一行，并按当前段名路由到不同解析分支。
    for str_line in path_scenario_file.read_text(encoding="utf-8").splitlines():

        # 先移除注释与首尾空白，空行会在下一步统一跳过。
        str_raw_line = _strip_comment(str_line)  # 当前场景配置行的净文本。

        # 空行或纯注释行不参与任何场景对象构建。
        if not str_raw_line:

            # 当前行没有有效内容，直接进入下一轮。
            continue

        # 这里先提取段头控制信息，避免后面的业务分支把控制行当成普通数据记录。
        str_new_section = _section_name(str_raw_line)  # 命中控制段时，这里保存后续路由要切换到的目标段名。

        # 命中新的段头后，需要切换解析上下文。
        if str_new_section:

            # 统一把段名转成大写，避免大小写差异影响后续路由。
            str_section = str_new_section.upper()  # 当前生效的场景配置段名。

            # 段头本身不再参与任何数据对象构建。
            continue

        # `GROUPS` 段负责构建分组与组内信号条目。
        if str_section == "GROUPS":

            # 当前净文本会被进一步拆成组头或信号条目。
            _parse_group_row(str_raw_line, list_groups)

            # 当前行已经被完全处理，不再继续检查其他段分支。
            continue

        # `VIRTUAL_BUSES` 段只处理带 `=` 的总线定义行。
        if str_section == "VIRTUAL_BUSES" and "=" in str_raw_line:

            # 当前总线定义会被解析并写入虚拟总线映射。
            _parse_virtual_bus_row(str_raw_line, dict_scenario_buses, dict_env)

            # 总线定义已完成处理，不再继续落到 marker 分支。
            continue

        # `MARKERS` 段中的每一条净文本都按 marker 规则解析。
        if str_section == "MARKERS":

            # 当前 marker 会按声明顺序追加到 marker 列表。
            _append_marker_row(str_raw_line, list_markers)

    # 返回场景文件中解析出的三类结构，供 RC 文本渲染阶段直接复用。
    return list_groups, dict_scenario_buses, list_markers

# 为命中的信号别名生成 `addRenameSig` 指令列表。
def _alias_lines(list_groups: list[ScenarioGroup], dict_env: dict[str, str], dict_buses: ScenarioBuses) -> list[str]:
    """
    生成非总线信号别名对应的 RC 行列表。

    :param list_groups: 当前场景的分组列表，dtype=list[ScenarioGroup]，unit=collection
    :param dict_env: 当前 base 段的别名映射表，dtype=dict[str, str]，unit=mapping
    :param dict_buses: 当前场景的虚拟总线映射，dtype=ScenarioBuses，unit=mapping
    :return: 返回 `addRenameSig` 指令行列表，dtype=list[str]，unit=collection
    """

    # 先准备别名行缓冲区，只有命中别名的非虚拟总线条目才会往里追加内容。
    list_alias_lines: list[str] = []  # 当前场景所有 `addRenameSig` 指令的顺序列表。

    # 逐组扫描全部信号条目，保留原始组内顺序不变。
    for dict_group in list_groups:

        # 当前分组的 `signals` 字段固定维护为信号条目列表。
        list_signals = dict_group["signals"]  # 当前分组承载的全部信号条目。

        # 逐项判断当前信号是否需要生成别名指令。
        for dict_signal in list_signals:

            # 只有存在别名且当前路径不是虚拟总线时，才需要输出 rename 指令。
            if dict_signal["alias"] and dict_signal["path"] not in dict_buses:

                # 先把真实路径按 base 别名表展开，再转成 Verdi 层级路径。
                str_real_path = _verdi_path(_resolve_alias(dict_signal["path"], dict_env))  # 当前信号的最终真实 Verdi 路径。

                # 别名路径共享同一个父层级，只把末级信号名替换成别名。
                str_parent_path = str_real_path.rsplit("/", 1)[0]  # 当前信号真实路径的父层级路径。

                # 当前别名会作为新的末级名称写进 `addRenameSig` 指令。
                str_alias_target = f'{str_parent_path}/{dict_signal["alias"]}'  # RC 中展示给用户的别名路径。

                # 追加当前信号的 rename 指令，保持与声明顺序一致。
                list_alias_lines.append(f'addRenameSig "{str_alias_target}" "{str_real_path}"')

    # 返回全部别名指令文本，供主 RC 渲染流程按段落拼接。
    return list_alias_lines

# 为场景中的 marker 生成 `addMarker` 指令列表。
def _marker_lines(list_markers: list[ScenarioMarker]) -> list[str]:
    """
    生成 marker 对应的 RC 行列表。

    :param list_markers: 当前场景的 marker 列表，dtype=list[ScenarioMarker]，unit=collection
    :return: 返回 `addMarker` 指令行列表，dtype=list[str]，unit=collection
    """

    # 逐项保留 marker 顺序并把颜色统一转成大写 RC 文本。
    return [
        f'addMarker -time {dict_marker["time"]} -name "{dict_marker["name"]}" -color {dict_marker["color"].upper()}'
        for dict_marker in list_markers
    ]

# 生成单个信号或总线条目的高度、颜色与进制选项文本。
def _signal_options(dict_signal: ScenarioSignal, str_group_color: str) -> str:
    """
    生成单个信号或总线条目的 Verdi 选项串。

    :param dict_signal: 当前信号条目对象，dtype=ScenarioSignal，unit=mapping
    :param str_group_color: 当前分组的默认颜色文本，dtype=str，unit=text
    :return: 返回拼好的高度、颜色与进制选项串，dtype=str，unit=text
    """

    # 高度选项始终存在；未显式指定时回退到默认波形高度。
    list_options = [f'-h {dict_signal["height"] or DEFAULT_SIGNAL_HEIGHT}']  # 当前条目的基础高度选项列表。

    # 信号级颜色优先，其次才回退到组级默认颜色。
    str_signal_color = _color_option(dict_signal["color"] or str_group_color)  # 当前条目的最终颜色选项文本。

    # 进制选项由当前条目的 `radix` 字段决定；未知值时保持空字符串。
    str_signal_radix = _radix_option(dict_signal["radix"])  # 当前条目的最终进制选项文本。

    # 命中合法颜色选项时再追加到结果列表，避免把空字符串拼进 RC。
    if str_signal_color:

        # 当前颜色选项会出现在高度选项之后。
        list_options.append(str_signal_color)

    # 命中合法进制选项时再追加到结果列表。
    if str_signal_radix:

        # 当前进制选项会跟在颜色选项后面，保持与旧实现一致。
        list_options.append(str_signal_radix)

    # 返回拼好的选项串，供 `addSignal` 与 `addBus` 直接复用。
    return " ".join(list_options)

# 为当前分组生成 `addGroup`、`addBus`、`addBusSignal` 与 `addSignal` 指令。
def _group_lines(
    dict_group: ScenarioGroup,
    dict_buses: ScenarioBuses,
    dict_env: dict[str, str],
) -> list[str]:
    """
    生成单个分组对应的全部 RC 行。

    :param dict_group: 当前分组对象，dtype=ScenarioGroup，unit=mapping
    :param dict_buses: 当前场景的虚拟总线映射，dtype=ScenarioBuses，unit=mapping
    :param dict_env: 当前 base 段的别名映射表，dtype=dict[str, str]，unit=mapping
    :return: 返回当前分组展开后的 RC 行列表，dtype=list[str]，unit=collection
    """

    # 先准备当前分组的输出缓冲区，后续会按 Verdi 期望顺序逐项追加。
    list_group_lines: list[str] = []  # 当前分组对应的全部 RC 行文本。

    # 先把分组级默认颜色转换成 Verdi `-color` 选项。
    str_group_color_option = _color_option(str(dict_group["color"]))  # 当前分组默认颜色对应的 Verdi 选项文本。

    # 分组头只有在存在颜色选项时才需要额外补一个尾随空格。
    str_group_color_prefix = f"{str_group_color_option} " if str_group_color_option else ""  # 分组头中颜色参数的最终前缀文本。

    # 当前分组头总是先于组内信号输出。
    list_group_lines.append(f'addGroup {str_group_color_prefix}"{dict_group["name"]}"')

    # 当前分组的 `signals` 字段固定承载按声明顺序排列的信号条目列表。
    list_signals = dict_group["signals"]  # 当前分组中需要逐项展开的信号条目。

    # 逐项生成普通信号或虚拟总线对应的 RC 指令。
    for dict_signal in list_signals:

        # 先生成当前条目的高度、颜色与进制选项串。
        str_options = _signal_options(dict_signal, str(dict_group["color"]))  # 当前条目在 RC 中要共用的选项串。

        # 当前路径命中虚拟总线时，需要输出 `addBus` 和多行 `addBusSignal`。
        if dict_signal["path"] in dict_buses:

            # 虚拟总线存在别名时优先用别名，否则继续使用总线名本身。
            str_bus_name = dict_signal["alias"] or dict_signal["path"]  # 当前虚拟总线在 RC 中显示的名称。

            # 先输出总线头，再逐个挂接组成总线的真实信号路径。
            list_group_lines.append(f'addBus {str_options} -name "{str_bus_name}"')

            # 按声明顺序逐个输出虚拟总线成员。
            for str_bus_signal in dict_buses[dict_signal["path"]]:

                # 当前总线成员路径已经在解析阶段完成别名展开，这里只需转成 Verdi 路径。
                list_group_lines.append(f"  addBusSignal {_verdi_path(str_bus_signal)}")

            # 当前条目作为总线已处理完成，不再继续走普通信号分支。
            continue

        # 普通信号要先展开别名路径，再转成 Verdi 层级路径。
        str_real_signal_path = _verdi_path(_resolve_alias(dict_signal["path"], dict_env))  # 当前普通信号的真实 Verdi 路径。

        # 输出普通信号指令，保持选项顺序与旧实现一致。
        list_group_lines.append(f"addSignal {str_options} {str_real_signal_path}")

    # 分组末尾保留一个空行，方便后续与下一个分组做稳定分段。
    list_group_lines.append("")

    # 返回当前分组的完整 RC 行列表。
    return list_group_lines

# 根据解析结果拼出完整 RC 文本，并保持与历史实现兼容的段落顺序。
def _render_rc_text(
    list_groups: list[ScenarioGroup],
    dict_buses: ScenarioBuses,
    list_markers: list[ScenarioMarker],
    dict_env: dict[str, str],
) -> str:
    """
    根据解析结果渲染完整 RC 文本。

    :param list_groups: 当前场景的分组列表，dtype=list[ScenarioGroup]，unit=collection
    :param dict_buses: 当前场景的虚拟总线映射，dtype=ScenarioBuses，unit=mapping
    :param list_markers: 当前场景的 marker 列表，dtype=list[ScenarioMarker]，unit=collection
    :param dict_env: 当前 base 段的别名映射表，dtype=dict[str, str]，unit=mapping
    :return: 返回完整 Verdi RC 文本，dtype=str，unit=text
    """

    # RC 文本固定以 Verdi 文件头起始，并在其后保留一个空行。
    list_lines = ["# Verdi Signal Save File", ""]  # 当前输出 RC 的完整文本行缓冲区。

    # 先渲染所有非总线别名指令，保持与旧实现一致的顶部布局。
    list_alias_lines = _alias_lines(list_groups, dict_env, dict_buses)  # 全部 `addRenameSig` 指令行列表。

    # 存在别名指令时先整体追加，再补一个空行与后续段落分隔。
    if list_alias_lines:

        # 当前 RC 文本头部追加全部别名指令。
        list_lines.extend(list_alias_lines)

        # 别名段非空时在其后补一个空行，保持视觉分段稳定。
        list_lines.append("")

    # marker 不是层级信号本体，所以这里把它们独立汇总成专门的波形标记段。
    list_marker_lines = _marker_lines(list_markers)  # 这里单独保存 marker 段要写出的全部标记语句。

    # 存在 marker 时整体追加，并在其后补一个空行。
    if list_marker_lines:

        # 当前 RC 文本继续追加全部 marker 指令。
        list_lines.extend(list_marker_lines)

        # marker 段非空时在其后补一个空行，便于与分组段区分。
        list_lines.append("")

    # 最后按声明顺序逐组渲染分组头、总线与信号行。
    for dict_group in list_groups:

        # 当前分组展开出的全部 RC 行会原样接到最终缓冲区尾部。
        list_lines.extend(_group_lines(dict_group, dict_buses, dict_env))

    # 返回去掉尾随空白行后再补一个换行的稳定 RC 文本。
    return "\n".join(list_lines).rstrip() + "\n"

# 对外暴露稳定的 RC 渲染入口，供测试、脚本包装层与技能工作流复用。
def render_rc(*, config_dir: Path, scenario: str, base: str) -> str:
    """
    按给定配置目录、场景名和基础别名集渲染 Verdi RC 文本。

    :param config_dir: 包含 `scn_base.lst` 与 `scn_<scenario>.lst` 的配置目录，dtype=Path，unit=path
    :param scenario: 目标场景名，不含 `scn_` 前缀与 `.lst` 后缀，dtype=str，unit=identifier
    :param base: 需要使用的基础别名段名称，dtype=str，unit=identifier
    :return: 返回最终 Verdi RC 文本，dtype=str，unit=text
    :raises ValueError: 当 base 或 scenario 配置缺失、或者配置语法非法时抛出显式错误。
    """

    # 先读取全部基础别名配置，后续会按 `base` 名称选取具体映射。
    dict_base_env_map: dict[str, dict[str, str]] = _parse_base_file(config_dir / "scn_base.lst")  # 配置目录中的全部基础别名映射集合。

    # 指定 base 不存在时直接阻断，避免错误地退回到其他配置段。
    if base not in dict_base_env_map:

        # 这里复用明确的 base 缺失错误文本，便于测试断言稳定匹配。
        raise ValueError(f"> ERR: [Python] BASE '{base}' not found")

    # 目标场景文件路径固定按 `scn_<scenario>.lst` 命名规则拼接。
    path_scenario_file = config_dir / f"scn_{scenario}.lst"  # 当前请求场景对应的配置文件路径。

    # 按选中的 base 映射解析目标场景文件。
    tuple_scenario = _parse_scenario_file(path_scenario_file, dict_base_env_map[base])  # 当前场景解析出的分组、总线与 marker 三元组。

    # 返回最终 RC 文本，供测试与 CLI 直接消费。
    return _render_rc_text(tuple_scenario[0], tuple_scenario[1], tuple_scenario[2], dict_base_env_map[base])

# 构建 CLI 参数解析器，保持 `main` 只关注调度与输出。
def _build_parser() -> argparse.ArgumentParser:
    """
    构建当前脚本使用的命令行参数解析器。

    :param 无: 当前 helper 不接收显式 Python 位置参数。
    :return: 返回配置完成的参数解析器，dtype=argparse.ArgumentParser，unit=object
    """

    # 当前解析器负责统一承接配置目录、场景名、base 名称与输出路径。
    parser = argparse.ArgumentParser(description="Generate a Verdi signal restore RC file.")  # RC 生成脚本的统一 CLI 入口解析器。

    # 配置目录可由调用方覆盖；未覆盖时沿用当前目录下的 `config`。
    parser.add_argument("-c", "--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)

    # 目标场景名是 RC 生成的必填输入。
    parser.add_argument("-s", "--scenario", required=True)

    # 基础别名段名同样是 RC 生成的必填输入。
    parser.add_argument("-b", "--base", required=True)

    # 输出路径存在时把 RC 文本写文件；否则直接写到 stdout。
    parser.add_argument("-o", "--output", type=Path)

    # 返回构造完成的解析器实例，供 `main` 统一复用。
    return parser

# 按 CLI 约定把 RC 文本写到文件或标准输出。
def _emit_rc(str_rc: str, path_output: Path | None) -> None:
    """
    按 CLI 约定输出 RC 文本。

    :param str_rc: 已渲染完成的 RC 文本，dtype=str，unit=text
    :param path_output: 可选输出文件路径；为空时写到 stdout，dtype=Path | None，unit=path
    :return: 固定返回 None；当前 helper 只负责输出副作用，dtype=None，unit=object
    """

    # 调用方显式传入输出路径时，优先把 RC 文本落盘并回显目标路径。
    if path_output:

        # 先确保目标父目录存在，避免写文件阶段因中间目录缺失失败。
        path_output.parent.mkdir(parents=True, exist_ok=True)

        # 当前 RC 文本以 UTF-8 编码写入目标文件。
        path_output.write_text(str_rc, encoding="utf-8")

        # 保持与旧实现兼容：成功落盘后把目标路径写回 stdout。
        sys.stdout.write(f"{path_output}\n")

        # 当前输出路径分支已经完成，直接结束 helper。
        return

    # 未传输出路径时，直接把完整 RC 文本写到标准输出。
    sys.stdout.write(str_rc)

# 解析 CLI 参数并驱动完整的 RC 生成流程。
def main() -> int:
    """
    运行 Verdi RC 生成脚本的命令行入口。

    :param 无: 当前入口函数不接收显式 Python 位置参数，全部输入来自命令行。
    :return: 当 RC 文本成功生成并完成输出时返回 0，dtype=int，unit=exit code
    """

    # 先构建统一的参数解析器，确保人工调用与测试调用共享同一协议。
    parser = _build_parser()  # 当前脚本的 CLI 协议在这里集中定义。

    # 在进入主流程前一次性解析全部命令行输入。
    args = parser.parse_args()  # 解析后的参数命名空间会驱动下游渲染与输出。

    # RC 渲染阶段统一把配置异常转成 parser.error，让 CLI 以标准方式失败退出。
    try:

        # 当前 CLI 请求对应的 RC 文本会在这里一次性渲染完成。
        str_rc = render_rc(config_dir=args.config_dir, scenario=args.scenario, base=args.base)  # 最终要输出到文件或 stdout 的 RC 文本。

    # 仅把本模块定义的配置错误转成命令行参数错误提示。
    except ValueError as exc:

        # parser.error 会输出统一错误信息并以非零退出。
        parser.error(str(exc))

    # RC 文本生成成功后，按是否存在 `--output` 选择输出目标。
    _emit_rc(str_rc, args.output)

    # 成功完成渲染与输出时返回零退出码。
    return 0

# 作为独立脚本运行时，把 `main` 的返回值直接透传成进程退出码。
if __name__ == "__main__":

    # 让 shell、bat 与 PowerShell 包装层都能直接依赖标准退出码。
    raise SystemExit(main())
