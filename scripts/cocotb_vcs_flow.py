#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path


ASSIGN_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*([:+?]?=)\s*(.*)$")


def _strip_comment(line: str) -> str:
    if "#" not in line:
        return line.rstrip()
    escaped = False
    out: list[str] = []
    for char in line:
        if char == "#" and not escaped:
            break
        out.append(char)
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
    return "".join(out).rstrip()


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        lines.append(pending + line)
        pending = ""
    if pending:
        lines.append(pending)
    return lines


def _split_condition_args(expr: str) -> tuple[str, str] | None:
    start = expr.find("(")
    end = expr.rfind(")")
    if start < 0 or end <= start:
        return None
    payload = expr[start + 1 : end]
    depth = 0
    for idx, char in enumerate(payload):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            return payload[:idx].strip(), payload[idx + 1 :].strip()
    return None


def _expand_vars(value: str, variables: dict[str, str], make_dir: Path) -> str:
    def replace_shell(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        if body == "pwd":
            return str(make_dir)
        if body == "echo $(SIM) | tr A-Z a-z":
            return variables.get("SIM", "").lower()
        return f"$(shell {body})"

    value = re.sub(r"\$\(shell\s+([^)]*(?:\)[^)]*)?)\)", replace_shell, value)

    def replace_filter(match: re.Match[str]) -> str:
        first = _expand_vars(match.group(1).strip(), variables, make_dir)
        second = _expand_vars(match.group(2).strip(), variables, make_dir)
        return first if first in second.split() else ""

    value = re.sub(r"\$\(filter\s+([^,]+),([^)]+)\)", replace_filter, value)

    def replace_var(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "PWD":
            return variables.get("PWD", str(make_dir))
        return variables.get(name, "")

    previous = None
    while previous != value:
        previous = value
        value = re.sub(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)", replace_var, value)
        value = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace_var, value)
    return value.strip()


def _eval_condition(line: str, variables: dict[str, str], make_dir: Path, *, negated: bool) -> bool:
    parts = _split_condition_args(line)
    if not parts:
        return False
    left = _expand_vars(parts[0], variables, make_dir)
    right = _expand_vars(parts[1], variables, make_dir)
    result = left == right
    return not result if negated else result


def _current_active(stack: list[dict[str, bool]]) -> bool:
    return all(frame["active"] for frame in stack)


def parse_cocotb_makefile(
    makefile: Path,
    *,
    toplevel_lang: str = "verilog",
    make_vars: dict[str, str] | None = None,
) -> dict[str, str]:
    makefile = makefile.resolve()
    make_dir = makefile.parent
    variables: dict[str, str] = {
        "SIM": "vcs",
        "TOPLEVEL_LANG": toplevel_lang,
        "PWD": str(make_dir),
        "COCOTB_HDL_TIMEUNIT": "1ns",
        "COCOTB_HDL_TIMEPRECISION": "1ps",
    }
    variables.update(make_vars or {})
    stack: list[dict[str, bool]] = []

    for raw in _logical_lines(makefile.read_text(encoding="utf-8")):
        stripped = _strip_comment(raw).strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith("else ifeq"):
            frame = stack[-1]
            cond = _eval_condition(stripped[len("else ") :], variables, make_dir, negated=False)
            active = frame["parent"] and (not frame["matched"]) and cond
            frame["active"] = active
            frame["matched"] = frame["matched"] or active
            continue
        if lowered.startswith("else ifneq"):
            frame = stack[-1]
            cond = _eval_condition(stripped[len("else ") :], variables, make_dir, negated=True)
            active = frame["parent"] and (not frame["matched"]) and cond
            frame["active"] = active
            frame["matched"] = frame["matched"] or active
            continue
        if lowered == "else":
            frame = stack[-1]
            active = frame["parent"] and not frame["matched"]
            frame["active"] = active
            frame["matched"] = True
            continue
        if lowered.startswith("ifeq"):
            parent = _current_active(stack)
            active = parent and _eval_condition(stripped, variables, make_dir, negated=False)
            stack.append({"parent": parent, "active": active, "matched": active})
            continue
        if lowered.startswith("ifneq"):
            parent = _current_active(stack)
            active = parent and _eval_condition(stripped, variables, make_dir, negated=True)
            stack.append({"parent": parent, "active": active, "matched": active})
            continue
        if lowered == "endif":
            if stack:
                stack.pop()
            continue
        if not _current_active(stack):
            continue
        if stripped.startswith("\t") or stripped.startswith("include ") or stripped.startswith("$("):
            continue
        match = ASSIGN_RE.match(stripped)
        if not match:
            continue
        name, op, raw_value = match.groups()
        value = _expand_vars(raw_value, variables, make_dir)
        if op == "?=" and name in variables and variables[name]:
            continue
        if op == "+=":
            variables[name] = " ".join(item for item in (variables.get(name, ""), value) if item).strip()
        else:
            variables[name] = value
    return variables


def _split_words(value: str) -> list[str]:
    if not value:
        return []
    return shlex.split(value, posix=False)


def _rel(path_text: str, project_root: Path) -> str:
    path = Path(path_text)
    if not path.is_absolute():
        path = (project_root / path).resolve()
    else:
        path = path.resolve()
    try:
        return path.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _source_list(value: str, project_root: Path) -> list[str]:
    return [_rel(item, project_root) for item in _split_words(value)]


def _has_gui_request(*values: list[str]) -> bool:
    for value in values:
        if any(item.lower() in {"-gui", "+gui", "gui=1"} for item in value):
            return True
    return False


def _base_plan(variables: dict[str, str], project_root: Path, diagnostics: list[str]) -> dict:
    return {
        "tool": "vcs",
        "frontend": "cocotb",
        "scope": "non-gui scripted subset",
        "top": variables.get("TOPLEVEL", ""),
        "module": variables.get("MODULE", ""),
        "testcase": variables.get("TESTCASE", ""),
        "toplevel_lang": variables.get("TOPLEVEL_LANG", "verilog"),
        "sources": {
            "verilog": _source_list(variables.get("VERILOG_SOURCES", ""), project_root),
            "vhdl": _source_list(variables.get("VHDL_SOURCES", ""), project_root),
        },
        "diagnostics": diagnostics,
        "unsupported_official_scope": [
            "GUI/DVE/interactive Verdi launch",
            "VCS cocotb VHDL/VHPI execution",
            "complete coverage of every official Synopsys option",
        ],
    }


def build_cocotb_vcs_plan(
    *,
    makefile: Path,
    project_root: Path,
    toplevel_lang: str = "verilog",
    make_vars: dict[str, str] | None = None,
    cocotb_lib: str | None = None,
    dry_run: bool = True,
) -> dict:
    project_root = project_root.resolve()
    variables = parse_cocotb_makefile(makefile, toplevel_lang=toplevel_lang, make_vars=make_vars)
    diagnostics: list[str] = [
        "cocotb VCS support is planned through VPI-only Verilog/SystemVerilog access; VHDL/VHPI is guarded unsupported for this flow.",
    ]
    plan = _base_plan(variables, project_root, diagnostics)

    compile_args = _split_words(variables.get("COMPILE_ARGS", ""))
    sim_args = _split_words(variables.get("SIM_ARGS", ""))
    extra_args = _split_words(variables.get("EXTRA_ARGS", ""))
    plusargs = _split_words(variables.get("PLUSARGS", ""))
    if _has_gui_request(compile_args, sim_args, extra_args, plusargs) or variables.get("GUI", "").lower() in {"1", "true", "yes"}:
        return {**plan, "status": "blocked", "reason": "gui_requested"}

    if plan["sources"]["vhdl"]:
        return {
            **plan,
            "status": "unsupported",
            "reason": "vcs_cocotb_vhdl_unsupported",
        }
    if not plan["sources"]["verilog"]:
        return {**plan, "status": "blocked", "reason": "no_verilog_sources"}
    if not plan["top"] or not plan["module"]:
        return {**plan, "status": "blocked", "reason": "missing_toplevel_or_module"}

    sim_build = variables.get("SIM_BUILD") or "sim_build"
    pli_tab = f"{sim_build}/pli.tab"
    timescale = f"{variables.get('COCOTB_HDL_TIMEUNIT', '1ns')}/{variables.get('COCOTB_HDL_TIMEPRECISION', '1ps')}"
    cocotb_vpi_lib = cocotb_lib or "$(cocotb-config --lib-name-path vpi vcs)"
    compile_cmd = [
        "vcs",
        "-top",
        plan["top"],
        *plusargs,
        "+acc+1",
        "+vpi",
        "-P",
        pli_tab,
        "+define+COCOTB_SIM=1",
        "-sverilog",
        f"-timescale={timescale}",
        *extra_args,
        "-debug",
        "-load",
        cocotb_vpi_lib,
        *compile_args,
        *plan["sources"]["verilog"],
    ]
    simv = f"{sim_build}/simv" if sim_build else "simv"
    simulate_cmd = [simv, "+define+COCOTB_SIM=1", *sim_args, *extra_args]
    env = {
        "MODULE": plan["module"],
        "TESTCASE": plan["testcase"],
        "TOPLEVEL": plan["top"],
        "TOPLEVEL_LANG": plan["toplevel_lang"],
    }
    if variables.get("PYTHONPATH"):
        env["PYTHONPATH"] = variables["PYTHONPATH"]
    results = variables.get("COCOTB_RESULTS_FILE") or "results.xml"

    return {
        **plan,
        "status": "dry-run" if dry_run else "planned",
        "required_external_dependencies": ["vcs", "cocotb VPI library"],
        "write_pli_tab": {"path": pli_tab, "content": "acc+=rw,wn:*"},
        "compile": {"cwd": str(project_root), "cmd": compile_cmd},
        "simulate": {"cwd": str(project_root), "env": env, "cmd": simulate_cmd},
        "expected_artifacts": [results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan a non-GUI cocotb VCS/VPI simulation flow.")
    parser.add_argument("--makefile", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--toplevel-lang", default="verilog")
    parser.add_argument("--make-var", action="append", default=[])
    parser.add_argument("--cocotb-lib")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    make_vars = {}
    for item in args.make_var:
        if "=" not in item:
            parser.error(f"--make-var must be NAME=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        make_vars[key] = value
    plan = build_cocotb_vcs_plan(
        makefile=args.makefile,
        project_root=args.project_root,
        toplevel_lang=args.toplevel_lang,
        make_vars=make_vars,
        cocotb_lib=args.cocotb_lib,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print("\n".join(plan.get("compile", {}).get("cmd", [])))
    return 0 if plan["status"] in {"dry-run", "planned", "unsupported", "blocked"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
