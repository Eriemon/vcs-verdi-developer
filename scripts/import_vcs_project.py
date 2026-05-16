#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
from pathlib import Path


VLOGAN_FLAG_PREFIXES = ("+v2k",)
VCS_FLAG_PREFIXES = ("+mindelays", "-negdelay", "+neg_tchk")
SOURCE_SUFFIXES = {".v", ".sv", ".vh", ".svh"}
EDALIZE_SOURCE_TYPES = ("verilogSource", "systemVerilogSource", "verilog2001Source", "cSource", "cppSource")


def _join_continuations(text: str) -> str:
    return text.replace("\\\r\n", " ").replace("\\\n", " ")


def _strip_make_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == "#" and not in_single and not in_double:
            return value[:index].rstrip()
    return value.strip()


def _read_make_var(makefile: Path, name: str) -> list[str]:
    text = _join_continuations(makefile.read_text(encoding="utf-8", errors="replace"))
    prefix = f"{name}"
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        left, sep, right = stripped.partition("=")
        if sep and left.strip() == name:
            return shlex.split(_strip_make_inline_comment(right), posix=True)
    return []


def _read_make_vars(makefile: Path) -> dict[str, str]:
    text = _join_continuations(makefile.read_text(encoding="utf-8", errors="replace"))
    variables: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("\t"):
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::=|\?=|\+=|=)\s*(.*)$", stripped)
        if match:
            name, value = match.groups()
            value = _strip_make_inline_comment(value)
            variables[name] = (variables.get(name, "") + " " + value).strip() if "+=" in stripped else value
    return variables


def _make_commands(makefile: Path) -> list[str]:
    commands = []
    for line in _join_continuations(makefile.read_text(encoding="utf-8", errors="replace")).splitlines():
        if line.startswith("\t"):
            commands.append(line.strip())
    return commands


def _rel(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _resolve_from(base: Path, token: str, project_root: Path) -> str:
    path = Path(token)
    if not path.is_absolute():
        path = base / path
    return _rel(path, project_root)


def _resolve_source_token(base: Path, token: str, project_root: Path) -> str:
    path = Path(token)
    if path.is_absolute():
        return _rel(path, project_root)
    project_relative = project_root / path
    if project_relative.exists():
        return _rel(project_relative, project_root)
    return _resolve_from(base, token, project_root)


def _normalize_arg_path(value: str, *, base: Path, project_root: Path) -> str:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return _rel(path, project_root)


def _shell_find_result(command: str, *, project_root: Path) -> list[str]:
    if command.strip() == "pwd":
        return [project_root.as_posix()]
    tokens = shlex.split(command, posix=True)
    if len(tokens) < 2 or tokens[0] != "find":
        return []
    root = Path(tokens[1])
    if not root.is_absolute():
        root = project_root / root
    include_patterns: list[str] = []
    exclude_patterns: list[str] = []
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if token == "-name" and index + 1 < len(tokens):
            include_patterns.append(tokens[index + 1])
            index += 2
            continue
        if token == "!" and index + 2 < len(tokens) and tokens[index + 1] == "-name":
            exclude_patterns.append(tokens[index + 2])
            index += 3
            continue
        index += 1
    if not include_patterns or not root.exists():
        return []
    matches = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        if not any(fnmatch.fnmatch(name, pattern) for pattern in include_patterns):
            continue
        if any(fnmatch.fnmatch(name, pattern) for pattern in exclude_patterns):
            continue
        matches.append(_rel(path, project_root))
    return matches


def _expand_make_value(
    raw: str,
    *,
    variables: dict[str, str],
    make_dir: Path,
    project_root: Path,
    seen: set[str] | None = None,
) -> str:
    seen = seen or set()
    value = raw.replace("$(PWD)", make_dir.as_posix()).replace("$(CURDIR)", make_dir.as_posix())

    def shell_repl(match: re.Match[str]) -> str:
        command = match.group(1).strip()
        if command == "pwd":
            return make_dir.as_posix()
        return " ".join(_shell_find_result(command, project_root=project_root))

    value = re.sub(r"\$\(shell\s+([^)]*)\)", shell_repl, value)

    def var_repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in seen:
            return ""
        if name in variables:
            return _expand_make_value(
                variables[name],
                variables=variables,
                make_dir=make_dir,
                project_root=project_root,
                seen={*seen, name},
            )
        return os.environ.get(name, "")

    value = re.sub(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)", var_repl, value)
    value = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", var_repl, value)
    return value


def _expanded_tokens(
    raw: str,
    *,
    variables: dict[str, str],
    make_dir: Path,
    project_root: Path,
) -> list[str]:
    expanded = _expand_make_value(raw, variables=variables, make_dir=make_dir, project_root=project_root)
    return shlex.split(expanded, posix=True)


def _source_tokens(tokens: list[str]) -> list[str]:
    sources = []
    for token in tokens:
        suffix = Path(token).suffix.lower()
        if suffix in SOURCE_SUFFIXES and not _is_uvm_token(token):
            sources.append(token)
    return sources


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _is_uvm_token(token: str) -> bool:
    return "uvm" in token.replace("\\", "/").lower()


def _mark_uvm_dependency(diagnostics: list[str], optional_deps: list[str]) -> None:
    if "uvm" not in optional_deps:
        optional_deps.append("uvm")
    message = "detected UVM dependency; skipped UVM_FLAGS for core non-GUI import"
    if message not in diagnostics:
        diagnostics.append(message)


def _optional_dependency_var_names(variables: dict[str, str]) -> set[str]:
    names: set[str] = set()
    for name, value in variables.items():
        upper = name.upper()
        lower_value = value.lower()
        if upper.startswith("UVM") or "uvm.sv" in lower_value or "uvm_dpi.cc" in lower_value:
            names.add(name)
    return names


def _references_optional_var(token: str, var_names: set[str]) -> bool:
    return any(f"$({name})" in token or f"${{{name}}}" in token for name in var_names)


def _strip_optional_dependency_tokens(
    command_piece: str,
    *,
    variables: dict[str, str],
    diagnostics: list[str],
    optional_deps: list[str],
) -> str:
    optional_vars = _optional_dependency_var_names(variables)
    kept_tokens = []
    skipped = False
    for token in shlex.split(command_piece, posix=True):
        if _references_optional_var(token, optional_vars) or _is_uvm_token(token):
            skipped = True
            continue
        kept_tokens.append(token)
    if skipped:
        _mark_uvm_dependency(diagnostics, optional_deps)
    return " ".join(kept_tokens)


def _add_define(defines: dict[str, str], define: str) -> None:
    if "=" in define:
        name, value = define.split("=", 1)
    else:
        name, value = define, "1"
    if name:
        defines[name] = value


def _coverage_metrics(value: str) -> list[str]:
    return [item for item in value.split("+") if item]


def parse_filelist_details(filelist: Path, *, project_root: Path, base: Path | None = None) -> dict:
    base = base or filelist.parent
    entries: list[str] = []
    include_dirs: list[str] = []
    defines: dict[str, str] = {}
    vlogan_args: list[str] = []
    vcs_args: list[str] = []
    coverage_metrics: list[str] = []
    for raw_line in filelist.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        tokens = shlex.split(line, posix=True)
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if _is_uvm_token(token):
                index += 1
                continue
            if token.startswith("+define+"):
                _add_define(defines, token.split("+define+", 1)[1])
            elif token.startswith("+incdir+"):
                include_dirs.append(_resolve_from(base, token.split("+incdir+", 1)[1], project_root))
            elif token == "-sverilog":
                vlogan_args.append(token)
            elif token == "-cm":
                metrics = _take_next(tokens, index)
                coverage_metrics.extend(_coverage_metrics(metrics))
                vcs_args.extend(["-cm", metrics])
                index += 2
                continue
            elif Path(token).suffix.lower() in SOURCE_SUFFIXES and not token.startswith(("+", "-")):
                entries.append(_resolve_from(base, token, project_root))
            elif token.startswith("-") or token.startswith("+"):
                vcs_args.append(token)
            index += 1
    return {
        "entries": _dedupe(entries),
        "include_dirs": _dedupe(include_dirs),
        "defines": defines,
        "vlogan_args": _dedupe(vlogan_args),
        "vcs_args": _dedupe(vcs_args),
        "coverage_metrics": _dedupe(coverage_metrics),
    }


def parse_filelist(filelist: Path, *, project_root: Path) -> list[str]:
    return parse_filelist_details(filelist, project_root=project_root)["entries"]


def _take_next(tokens: list[str], index: int) -> str:
    return tokens[index + 1] if index + 1 < len(tokens) else ""


def _take_value(tokens: list[str], index: int) -> str:
    value = _take_next(tokens, index)
    if not value or value.startswith("-") or value.startswith("+"):
        return ""
    return value


def _parse_vcs_tokens(
    tokens: list[str],
    *,
    make_dir: Path,
    project_root: Path,
    source_lists: list[str],
    include_dirs: list[str],
    vlogan_args: list[str],
    vcs_args: list[str],
    original_vcs_args: list[str],
    diagnostics: list[str] | None = None,
    defines: dict[str, str] | None = None,
    coverage: dict | None = None,
) -> tuple[str, str, str, str]:
    timescale = ""
    debug = "all"
    top = "top"
    output = ""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _is_uvm_token(token):
            index += 1
            continue
        original_vcs_args.append(token)
        if token in {"vcs", "vlogan", "iverilog", "vlog"}:
            index += 1
            continue
        if token == "-f":
            rel = _resolve_from(make_dir, _take_next(tokens, index), project_root)
            source_lists.append(rel)
            original_vcs_args.append(_take_next(tokens, index))
            index += 2
            continue
        if token == "-o":
            output = _normalize_arg_path(_take_next(tokens, index), base=make_dir, project_root=project_root)
            vcs_args.extend(["-o", output])
            original_vcs_args.append(_take_next(tokens, index))
            index += 2
            continue
        if token == "-cm":
            metrics = _take_next(tokens, index)
            if coverage is not None:
                coverage["metrics"] = _dedupe([*coverage.get("metrics", []), *_coverage_metrics(metrics)])
            vcs_args.extend(["-cm", metrics])
            original_vcs_args.append(metrics)
            index += 2
            continue
        if token == "-cm_dir":
            cm_dir = _normalize_arg_path(_take_next(tokens, index), base=make_dir, project_root=project_root)
            if coverage is not None:
                coverage["compile_dir"] = cm_dir
            vcs_args.extend(["-cm_dir", cm_dir])
            original_vcs_args.append(_take_next(tokens, index))
            index += 2
            continue
        if token.startswith("-Mdir="):
            vcs_args.append("-Mdir=" + _normalize_arg_path(token.split("=", 1)[1], base=make_dir, project_root=project_root))
        elif token.startswith("+define+"):
            if defines is not None:
                _add_define(defines, token.split("+define+", 1)[1])
        if token.startswith("+incdir+"):
            include_dirs.append(_resolve_from(make_dir, token.split("+incdir+", 1)[1], project_root))
        elif token.startswith("-timescale="):
            timescale = token.split("=", 1)[1]
        elif token == "-timescale":
            timescale = _take_next(tokens, index)
            original_vcs_args.append(timescale)
            index += 2
            continue
        elif token.startswith("-debug_access+"):
            debug = token.split("+", 1)[1]
        elif token.startswith("-debug_acc+"):
            debug = token.split("+", 1)[1]
        elif token in VLOGAN_FLAG_PREFIXES or token == "-sverilog":
            vlogan_args.append(token)
        elif token in VCS_FLAG_PREFIXES or token.startswith("+vcs+dumpvars+") or token in {"-full64", "-R", "-lca"}:
            vcs_args.append(token)
        elif token.startswith("-msg_config="):
            vcs_args.extend(["-msg_config", _normalize_arg_path(token.split("=", 1)[1], base=make_dir, project_root=project_root)])
        elif token == "-msg_config":
            vcs_args.extend(["-msg_config", _normalize_arg_path(_take_next(tokens, index), base=make_dir, project_root=project_root)])
            original_vcs_args.append(_take_next(tokens, index))
            index += 2
            continue
        elif token == "-top":
            value = _take_value(tokens, index)
            if value:
                top = value
                original_vcs_args.append(value)
                index += 2
            else:
                if diagnostics is not None:
                    diagnostics.append("ignored -top without a value")
                index += 1
            continue
        elif token in {"-l", "-notice", "-lca", "+notimingcheck", "+nospecify", "-assert"}:
            vcs_args.append(token)
            if token in {"-l", "-assert"}:
                vcs_args.append(_take_next(tokens, index))
                original_vcs_args.append(_take_next(tokens, index))
                index += 2
                continue
        elif token.startswith("-debug_region+"):
            vcs_args.append(token)
        index += 1
    return timescale, debug, top, output


def _extract_vcs_command_tokens(
    *,
    makefile: Path,
    variables: dict[str, str],
    make_dir: Path,
    project_root: Path,
    diagnostics: list[str],
    optional_deps: list[str],
) -> list[str]:
    for command in _make_commands(makefile):
        if "vcs" not in command:
            continue
        pieces = [part.strip() for part in command.split("&&")]
        for piece in pieces:
            if not piece.startswith("vcs "):
                continue
            cleaned_piece = _strip_optional_dependency_tokens(
                piece,
                variables=variables,
                diagnostics=diagnostics,
                optional_deps=optional_deps,
            )
            return _expanded_tokens(cleaned_piece, variables=variables, make_dir=make_dir, project_root=project_root)
    return []


def _vcs_workdir_from_commands(makefile: Path, *, project_root: Path, variables: dict[str, str]) -> str:
    for command in _make_commands(makefile):
        pieces = [part.strip() for part in command.split("&&")]
        if len(pieces) < 2 or not any(piece.startswith("vcs ") for piece in pieces[1:]):
            continue
        first = pieces[0]
        if first.startswith("cd "):
            raw = shlex.split(first, posix=True)[1]
            expanded = _expand_make_value(raw, variables=variables, make_dir=makefile.parent, project_root=project_root)
            path = Path(expanded)
            if not path.is_absolute():
                path = makefile.parent / path
            return _rel(path, project_root)
    return "run"


def _expand_source_glob(base: Path, token: str, *, project_root: Path, diagnostics: list[str]) -> list[str]:
    path = Path(token)
    if not path.is_absolute():
        path = base / path
    pattern_for_diag = _rel(path, project_root).replace("\\", "/")
    matches = sorted(path.parent.glob(path.name)) if any(mark in path.name for mark in "*?[") else [path]
    resolved = []
    for match in matches:
        if match.exists() and match.is_file() and match.suffix.lower() in SOURCE_SUFFIXES:
            resolved.append(_rel(match, project_root))
    if not resolved and any(mark in token for mark in "*?["):
        diagnostics.append(f"source glob matched no files: {pattern_for_diag}")
    return resolved


def _parse_design_tokens(
    tokens: list[str],
    *,
    base: Path,
    project_root: Path,
    sources: list[str],
    include_dirs: list[str],
    defines: dict[str, str],
    vlogan_args: list[str],
    diagnostics: list[str],
) -> str:
    top = ""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"vlog", "vcs", "iverilog"}:
            index += 1
            continue
        if token in {"-vlog01compat", "-g2005"}:
            vlogan_args.append("+v2k")
        elif token == "-s":
            top = _take_next(tokens, index)
            index += 2
            continue
        elif token.startswith("-D") and token != "-D":
            _add_define(defines, token[2:])
        elif token == "-D":
            _add_define(defines, _take_next(tokens, index))
            index += 2
            continue
        elif token.startswith("+define+"):
            _add_define(defines, token.split("+define+", 1)[1])
        elif token.startswith("-I") and token != "-I":
            include_dirs.append(_resolve_from(base, token[2:], project_root))
        elif token == "-I":
            include_dirs.append(_resolve_from(base, _take_next(tokens, index), project_root))
            index += 2
            continue
        elif token.startswith("+incdir+"):
            include_dirs.append(_resolve_from(base, token.split("+incdir+", 1)[1], project_root))
        elif Path(token).suffix.lower() in SOURCE_SUFFIXES or any(mark in token for mark in "*?["):
            sources.extend(_expand_source_glob(base, token, project_root=project_root, diagnostics=diagnostics))
        index += 1
    return top


def _parse_icarus_makefile(
    *,
    variables: dict[str, str],
    make_dir: Path,
    project_root: Path,
    sources: list[str],
    include_dirs: list[str],
    defines: dict[str, str],
    vlogan_args: list[str],
    diagnostics: list[str],
) -> tuple[str, str]:
    if not variables.get("IVARG"):
        return "", ""
    tokens = _expanded_tokens(variables["IVARG"], variables=variables, make_dir=make_dir, project_root=project_root)
    top = _parse_design_tokens(
        tokens,
        base=make_dir / "sim",
        project_root=project_root,
        sources=sources,
        include_dirs=include_dirs,
        defines=defines,
        vlogan_args=vlogan_args,
        diagnostics=diagnostics,
    )
    return top, _rel(make_dir / "sim", project_root)


def _parse_modelsim_tcl(
    modelsim_tcl: Path,
    *,
    project_root: Path,
    sources: list[str],
    include_dirs: list[str],
    defines: dict[str, str],
    vlogan_args: list[str],
    diagnostics: list[str],
) -> tuple[str, str]:
    tcl_vars: dict[str, str] = {}
    top = ""
    text = modelsim_tcl.read_text(encoding="utf-8", errors="replace")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("set "):
            parts = shlex.split(line, posix=True)
            if len(parts) >= 3:
                tcl_vars[parts[1]] = " ".join(parts[2:])
            continue
        if line.startswith("vlog "):
            expanded = line
            for name, value in tcl_vars.items():
                expanded = expanded.replace(f"${name}", value)
            tokens = shlex.split(expanded, posix=True)
            _parse_design_tokens(
                tokens,
                base=modelsim_tcl.parent / "sim",
                project_root=project_root,
                sources=sources,
                include_dirs=include_dirs,
                defines=defines,
                vlogan_args=vlogan_args,
                diagnostics=diagnostics,
            )
            continue
        if line.startswith("vsim "):
            parts = shlex.split(line, posix=True)
            if len(parts) >= 2:
                top = parts[1].split(".")[-1]
    return top, _rel(modelsim_tcl.parent / "sim", project_root)


def _readmemh_artifacts(sources: list[str], *, project_root: Path) -> list[str]:
    artifacts: list[str] = []
    pattern = re.compile(r"\$readmemh\s*\(\s*\"([^\"]+)\"")
    for rel_source in sources:
        path = project_root / rel_source
        if not path.exists() or not path.is_file():
            continue
        for match in pattern.finditer(path.read_text(encoding="utf-8", errors="replace")):
            artifacts.append(Path(match.group(1)).name)
    return _dedupe(artifacts)


def _external_tool_dependencies(variables: dict[str, str]) -> list[str]:
    deps: list[str] = []
    for name in ("CC", "LD", "OC", "OD", "SZ", "GDB"):
        value = variables.get(name, "").strip()
        if value.startswith("mips-mti-elf-"):
            deps.append(value)
    return _dedupe(deps)


def _edalize_param_groups(edam: dict) -> dict:
    params = edam.get("parameters", {})
    groups = {"plusarg": {}, "vlogdefine": {}, "vlogparam": {}}
    for group in groups:
        if isinstance(params.get(group), dict):
            groups[group].update(params[group])
        if isinstance(edam.get(group), dict):
            groups[group].update(edam[group])
    if isinstance(params, dict):
        for name, spec in params.items():
            if name in groups or not isinstance(spec, dict):
                continue
            paramtype = str(spec.get("paramtype", "")).strip()
            if paramtype in groups and "default" in spec:
                groups[paramtype][name] = spec["default"]
    return groups


def _edalize_value(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _edalize_tool_options(edam: dict) -> dict:
    options = edam.get("tool_options", {})
    if "vcs" in options and isinstance(options["vcs"], dict):
        return options["vcs"]
    return options if isinstance(options, dict) else {}


def import_edalize_project(edam: dict, *, project_root: Path | None = None) -> dict:
    project_root = (project_root or Path.cwd()).resolve()
    sources: list[str] = []
    include_dirs: list[str] = []
    vlogan_args: list[str] = []
    vcs_args: list[str] = []
    optional_deps: list[str] = []
    diagnostics: list[str] = []
    saw_systemverilog = False
    saw_verilog2001 = False

    for item in edam.get("files", []):
        if isinstance(item, str):
            name = item
            file_type = "verilogSource"
            include_path = []
        else:
            name = str(item.get("name", item.get("file", "")))
            file_type = str(item.get("file_type", ""))
            include_path = item.get("include_path", item.get("include_paths", [])) or []
        if not name:
            continue
        if file_type.startswith("systemVerilog"):
            saw_systemverilog = True
        if file_type == "verilog2001Source":
            saw_verilog2001 = True
        if file_type.startswith(EDALIZE_SOURCE_TYPES) or Path(name).suffix.lower() in {".v", ".sv", ".c", ".cc", ".cpp"}:
            path = Path(name)
            sources.append(path.as_posix() if not path.is_absolute() else _rel(path, project_root))
        if isinstance(include_path, str):
            include_path = [include_path]
        for inc in include_path:
            inc_path = Path(str(inc))
            include_dirs.append(inc_path.as_posix() if not inc_path.is_absolute() else _rel(inc_path, project_root))

    if saw_systemverilog:
        vlogan_args.append("-sverilog")
    if saw_verilog2001:
        vlogan_args.append("+v2k")

    groups = _edalize_param_groups(edam)
    defines: dict[str, str] = {}
    for name, value in groups["vlogdefine"].items():
        defines[str(name)] = _edalize_value(value)
    plusargs = [f"+{name}={_edalize_value(value)}" for name, value in groups["plusarg"].items()]
    for name, value in groups["vlogparam"].items():
        vcs_args.append(f"-pvalue+{name}={_edalize_value(value)}")

    options = _edalize_tool_options(edam)
    vcs_args.extend(str(item) for item in options.get("vcs_options", []) or [])
    simv_args = [str(item) for item in options.get("run_options", []) or []]

    return {
        "sources": _dedupe(sources),
        "source_lists": [],
        "include_dirs": _dedupe(include_dirs),
        "defines": dict(sorted(defines.items())),
        "libraries": ["work"],
        "top": str(edam.get("toplevel", edam.get("top", "top"))),
        "timescale": str(edam.get("timescale", "")),
        "debug": "all",
        "kdb": True,
        "vlogan_args": _dedupe(vlogan_args),
        "vcs_args": _dedupe(vcs_args),
        "coverage": {"metrics": []},
        "output": str(edam.get("name", "simv")),
        "simv_args": simv_args,
        "plusargs": plusargs,
        "original_vcs_args": [],
        "tools": {},
        "dump_name": "waves.fsdb",
        "workdir": "run",
        "expected_artifacts": {"dump": {"path": "waves.fsdb", "min_bytes": 1}},
        "verdi_check": "fsdbreport",
        "report_signal": "/top/clk",
        "filelist_entries": [],
        "pre_sim_artifacts": [],
        "optional_external_dependencies": optional_deps,
        "diagnostics": diagnostics,
    }


def import_project(
    *,
    makefile: Path,
    filelist: Path | None = None,
    project_root: Path | None = None,
    modelsim_tcl: Path | None = None,
    make_vars: dict[str, str] | None = None,
) -> dict:
    makefile = makefile.resolve()
    project_root = (project_root or makefile.parent).resolve()
    make_dir = makefile.parent
    variables = _read_make_vars(makefile)
    if make_vars:
        variables.update({str(key): str(value) for key, value in make_vars.items()})
    vcs_tokens = _read_make_var(makefile, "VCS")
    verdi_tokens = _read_make_var(makefile, "VERDI")
    source_lists: list[str] = []
    include_dirs: list[str] = []
    vlogan_args: list[str] = []
    vcs_args: list[str] = []
    original_vcs_args: list[str] = []
    simv_args: list[str] = []
    post_compile_checks: list[str] = []
    post_sim_checks: list[str] = []
    sources: list[str] = []
    diagnostics: list[str] = []
    optional_deps: list[str] = []
    tools: dict[str, str] = {}
    defines: dict[str, str] = {}
    coverage: dict = {"metrics": []}
    timescale = ""
    debug = "all"
    top = "top"
    output = ""
    dump_name = "waves.fsdb"
    workdir = _vcs_workdir_from_commands(makefile, project_root=project_root, variables=variables)
    flag_base = project_root / workdir if workdir != "run" else make_dir
    parsed_expanded_vcs_command = False

    if vcs_tokens and vcs_tokens[0] != "vcs":
        tools["vcs"] = vcs_tokens[0]
    if vcs_tokens:
        timescale, debug, top, parsed_output = _parse_vcs_tokens(
            vcs_tokens[1:] if vcs_tokens and vcs_tokens[0] == "vcs" else vcs_tokens,
            make_dir=make_dir,
            project_root=project_root,
            source_lists=source_lists,
            include_dirs=include_dirs,
            vlogan_args=vlogan_args,
            vcs_args=vcs_args,
            original_vcs_args=original_vcs_args,
            diagnostics=diagnostics,
            defines=defines,
            coverage=coverage,
        )
        output = parsed_output or output
    else:
        command_tokens = _extract_vcs_command_tokens(
            makefile=makefile,
            variables=variables,
            make_dir=make_dir,
            project_root=project_root,
            diagnostics=diagnostics,
            optional_deps=optional_deps,
        )
        if command_tokens:
            parsed_expanded_vcs_command = True
            if command_tokens[0] != "vcs":
                tools["vcs"] = command_tokens[0]
            command_body = command_tokens[1:] if command_tokens and command_tokens[0] == "vcs" else command_tokens
            raw_sources = _source_tokens(command_body)
            sources = [_resolve_source_token(make_dir, item, project_root) for item in raw_sources]
            timescale, debug, top, parsed_output = _parse_vcs_tokens(
                command_body,
                make_dir=flag_base,
                project_root=project_root,
                source_lists=source_lists,
                include_dirs=include_dirs,
                vlogan_args=vlogan_args,
                vcs_args=vcs_args,
                original_vcs_args=original_vcs_args,
                diagnostics=diagnostics,
                defines=defines,
                coverage=coverage,
            )
            output = parsed_output or output
        elif variables.get("SRCS"):
            raw_sources = _source_tokens(_expanded_tokens(variables["SRCS"], variables=variables, make_dir=make_dir, project_root=project_root))
            sources = [_resolve_source_token(make_dir, item, project_root) for item in raw_sources]

        if variables.get("VCS_FLAGS") and not parsed_expanded_vcs_command:
            flag_tokens = _expanded_tokens(variables["VCS_FLAGS"], variables=variables, make_dir=make_dir, project_root=project_root)
            parsed_timescale, parsed_debug, parsed_top, parsed_output = _parse_vcs_tokens(
                flag_tokens,
                make_dir=flag_base,
                project_root=project_root,
                source_lists=source_lists,
                include_dirs=include_dirs,
                vlogan_args=vlogan_args,
                vcs_args=vcs_args,
                original_vcs_args=original_vcs_args,
                diagnostics=diagnostics,
                defines=defines,
                coverage=coverage,
            )
            timescale = parsed_timescale or timescale
            debug = parsed_debug or debug
            top = parsed_top or top
            output = parsed_output or output

        if variables.get("UVM_FLAGS"):
            _mark_uvm_dependency(diagnostics, optional_deps)

    if modelsim_tcl is None and (make_dir / "modelsim_script.tcl").exists():
        modelsim_tcl = make_dir / "modelsim_script.tcl"
    if modelsim_tcl is not None and modelsim_tcl.exists():
        parsed_top, parsed_workdir = _parse_modelsim_tcl(
            modelsim_tcl,
            project_root=project_root,
            sources=sources,
            include_dirs=include_dirs,
            defines=defines,
            vlogan_args=vlogan_args,
            diagnostics=diagnostics,
        )
        top = parsed_top or top
        workdir = parsed_workdir or workdir
    elif variables.get("IVARG"):
        parsed_top, parsed_workdir = _parse_icarus_makefile(
            variables=variables,
            make_dir=make_dir,
            project_root=project_root,
            sources=sources,
            include_dirs=include_dirs,
            defines=defines,
            vlogan_args=vlogan_args,
            diagnostics=diagnostics,
        )
        top = parsed_top or top
        workdir = parsed_workdir or workdir

    for command in _make_commands(makefile):
        if re.search(r"\./[A-Za-z0-9_.-]+", command):
            pieces = [part.strip() for part in command.split("&&")]
            for piece in pieces:
                if re.match(r"^\./[A-Za-z0-9_.-]+", piece):
                    sim_tokens = shlex.split(piece, posix=True)
                    simv_args = sim_tokens[1:]
        if "check_compile_error.sh" in command:
            post_compile_checks.append(command)
        if "check_sim_error.sh" in command:
            post_sim_checks.append(command)
        if re.search(r"\burg\b", command) and "-dir" in command:
            pieces = [part.strip() for part in command.split("&&")]
            for piece in pieces:
                if not re.search(r"\burg\b", piece):
                    continue
                tokens = shlex.split(piece, posix=True)
                if "-dir" in tokens:
                    raw_vdb = _take_next(tokens, tokens.index("-dir"))
                    vdb_dir = _normalize_arg_path(raw_vdb, base=project_root / workdir, project_root=project_root)
                    coverage["vdb_dir"] = vdb_dir
                    coverage["report_dir"] = _rel((project_root / vdb_dir).parent / "urgReport", project_root)
        if re.search(r"\bverdi\b", command):
            pieces = [part.strip() for part in command.split("&&")]
            for piece in pieces:
                verdi_command_tokens = _expanded_tokens(piece, variables=variables, make_dir=make_dir, project_root=project_root)
                if not verdi_command_tokens:
                    continue
                tool_name = Path(verdi_command_tokens[0]).name.lower()
                if tool_name not in {"verdi", "verdi.exe"}:
                    continue
                if verdi_command_tokens[0].lower() not in {"verdi", "verdi.exe"}:
                    tools["verdi"] = verdi_command_tokens[0]
                index = 1
                while index < len(verdi_command_tokens):
                    token = verdi_command_tokens[index]
                    if token == "-ssf":
                        dump_name = Path(_take_next(verdi_command_tokens, index)).name
                        index += 2
                        continue
                    if token == "-f" and not source_lists:
                        source_lists.append(_resolve_from(make_dir, _take_next(verdi_command_tokens, index), project_root))
                        index += 2
                        continue
                    index += 1

    if verdi_tokens:
        if verdi_tokens[0] != "verdi":
            tools["verdi"] = verdi_tokens[0]
        index = 1
        while index < len(verdi_tokens):
            token = verdi_tokens[index]
            if token == "-ssf":
                dump_name = Path(_take_next(verdi_tokens, index)).name
                index += 2
                continue
            if token == "-f" and not source_lists:
                source_lists.append(_resolve_from(make_dir, _take_next(verdi_tokens, index), project_root))
                index += 2
                continue
            index += 1

    if filelist is None and source_lists:
        filelist = project_root / source_lists[0]
    filelist_entries: list[str] = []
    if filelist and filelist.exists():
        filelist_base = project_root / workdir if workdir != "run" else filelist.resolve().parent
        details = parse_filelist_details(filelist.resolve(), project_root=project_root, base=filelist_base)
        filelist_entries = details["entries"]
        include_dirs.extend(details["include_dirs"])
        defines.update(details["defines"])
        vlogan_args.extend(details["vlogan_args"])
        vcs_args.extend(details["vcs_args"])
        coverage["metrics"] = _dedupe([*coverage.get("metrics", []), *details["coverage_metrics"]])
    for arg in vcs_args:
        if arg.startswith("+vcs+dumpvars+"):
            dump_name = arg.rsplit("+", 1)[-1]

    optional_deps.extend(_external_tool_dependencies(variables))
    pre_sim_artifacts = _readmemh_artifacts(_dedupe([*sources, *filelist_entries]), project_root=project_root)
    for artifact in pre_sim_artifacts:
        if not (make_dir / artifact).exists():
            diagnostics.append(f"pre-sim artifact missing before simulation: {artifact}")

    manifest = {
        "sources": _dedupe(sources),
        "source_lists": _dedupe(source_lists),
        "include_dirs": _dedupe(include_dirs),
        "defines": dict(sorted(defines.items())),
        "libraries": ["work"],
        "top": top,
        "timescale": timescale,
        "debug": debug,
        "kdb": True,
        "vlogan_args": _dedupe(vlogan_args),
        "vcs_args": _dedupe(vcs_args),
        "coverage": {"metrics": _dedupe(coverage.get("metrics", [])), **{k: v for k, v in coverage.items() if k != "metrics"}},
        "output": output,
        "simv_args": simv_args,
        "post_compile_checks": _dedupe(post_compile_checks),
        "post_sim_checks": _dedupe(post_sim_checks),
        "original_vcs_args": original_vcs_args,
        "tools": tools,
        "dump_name": dump_name,
        "workdir": workdir,
        "expected_artifacts": {"dump": {"path": dump_name, "min_bytes": 1}},
        "verdi_check": "fsdbreport",
        "report_signal": "/top/clk",
        "filelist_entries": filelist_entries,
        "pre_sim_artifacts": pre_sim_artifacts,
        "optional_external_dependencies": _dedupe(optional_deps),
        "diagnostics": _dedupe(diagnostics),
    }
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a simple VCS/Verdi Makefile and filelist into a smoke manifest.")
    parser.add_argument("--makefile", type=Path)
    parser.add_argument("--filelist", type=Path)
    parser.add_argument("--modelsim-tcl", type=Path)
    parser.add_argument("--edalize-json", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument(
        "--make-var",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Override or inject a Make variable before import, for example --make-var TOP=tb_top.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.edalize_json:
        manifest = import_edalize_project(
            json.loads(args.edalize_json.read_text(encoding="utf-8")),
            project_root=args.project_root,
        )
    else:
        if args.makefile is None:
            parser.error("--makefile is required unless --edalize-json is used")
        make_vars = {}
        for item in args.make_var:
            if "=" not in item:
                parser.error(f"--make-var must be NAME=VALUE, got {item!r}")
            name, value = item.split("=", 1)
            make_vars[name] = value
        manifest = import_project(
            makefile=args.makefile,
            filelist=args.filelist,
            project_root=args.project_root,
            modelsim_tcl=args.modelsim_tcl,
            make_vars=make_vars,
        )
    text = json.dumps(manifest, indent=2, sort_keys=True)
    print(text if args.json else text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
