#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


STEP_NAMES = ("compile", "elaborate", "simulate", "verdi-load-check")


def _as_path(value: Path | str | None) -> Path | None:
    if value is None:
        return None
    return Path(value)


def normalize_sources(
    *,
    source: Path | None = None,
    sources: list[Path] | None = None,
    source_list: Path | None = None,
    source_lists: list[Path] | None = None,
) -> list[str]:
    normalized = [str(Path(item).resolve()) for item in (sources or [])]
    if source:
        normalized.append(str(source.resolve()))
    if source_list:
        normalized.extend(["-f", str(source_list.resolve())])
    for item in source_lists or []:
        normalized.extend(["-f", str(Path(item).resolve())])
    if not normalized:
        raise ValueError("at least one --source or --source-list is required")
    return normalized


def _resolve_many(values: list[Path] | None) -> list[str]:
    return [str(Path(item).resolve()) for item in (values or [])]


def _define_args(defines: dict[str, str] | None) -> list[str]:
    args: list[str] = []
    for key, value in (defines or {}).items():
        if value == "":
            args.append(f"+define+{key}")
        else:
            args.append(f"+define+{key}={value}")
    return args


def _include_args(include_dirs: list[Path] | None) -> list[str]:
    return [f"+incdir+{Path(item).resolve()}" for item in (include_dirs or [])]


def _coverage_arg(coverage: list[str] | None, coverage_db: Path | None = None) -> list[str]:
    if not coverage:
        return []
    args = ["-cm", "+".join(coverage)]
    if coverage_db is not None:
        args.extend(["-cm_dir", str(coverage_db)])
    return args


def _work_args(libraries: list[str] | None) -> list[str]:
    if not libraries:
        return []
    return ["-work", libraries[0]]


def _log_path(log_dir: Path, step_name: str) -> str:
    return str(log_dir / f"{step_name}.log")


def _stage_args(value: list[str] | tuple[str, ...] | None) -> list[str]:
    return [str(item) for item in (value or [])]


def _tool(tools: dict[str, str] | None, name: str, default: str) -> str:
    return str((tools or {}).get(name) or default)


def _simv_artifact(workdir: Path, simv_tool: str) -> Path:
    if simv_tool.startswith("./"):
        return workdir / simv_tool[2:]
    if simv_tool == "simv":
        return workdir / "simv"
    return workdir / "simv"


def _normalize_expected_artifacts(workdir: Path, expected: dict | None, default_dump: Path) -> dict:
    if not expected:
        return {"dump": {"path": str(default_dump), "min_bytes": 1}}
    normalized: dict[str, dict] = {}
    for name, spec in expected.items():
        if isinstance(spec, str):
            path = Path(spec)
            min_bytes = 1
        elif isinstance(spec, dict):
            path = Path(str(spec.get("path", name)))
            min_bytes = int(spec.get("min_bytes", 1))
        else:
            raise ValueError("expected_artifacts entries must be strings or objects")
        normalized[name] = {
            "path": str(path if path.is_absolute() else workdir / path),
            "min_bytes": min_bytes,
        }
    return normalized


def find_pli_dir(env: dict[str, str] | None = None) -> Path | None:
    env_map = env or os.environ
    candidates: list[Path] = []
    if env_map.get("NOVAS_HOME"):
        novas_home = Path(env_map["NOVAS_HOME"])
        candidates.extend(
            [
                novas_home,
                novas_home / "share" / "PLI" / "VCS" / "LINUX64",
                novas_home / "share" / "PLI" / "VCS" / "LINUX",
            ]
        )
    if env_map.get("VERDI_HOME"):
        verdi_home = Path(env_map["VERDI_HOME"])
        candidates.extend(
            [
                verdi_home / "share" / "PLI" / "VCS" / "LINUX64",
                verdi_home / "share" / "PLI" / "VCS" / "LINUX",
            ]
        )
    for candidate in candidates:
        if (candidate / "novas.tab").exists() and (candidate / "pli.a").exists():
            return candidate
    return None


def pli_args(pli_dir: Path | None) -> tuple[list[str], dict[str, str]]:
    if not pli_dir:
        return [], {}
    tab = (pli_dir / "novas.tab").resolve()
    lib = (pli_dir / "pli.a").resolve()
    if not tab.exists() or not lib.exists():
        raise ValueError(f"novas PLI files not found under {pli_dir}")
    return ["-P", str(tab), str(lib)], {"pli_tab": str(tab), "pli_lib": str(lib)}


def build_smoke_plan(
    *,
    source: Path | None = None,
    sources: list[Path] | None = None,
    source_list: Path | None = None,
    source_lists: list[Path] | None = None,
    vhdl_sources: list[Path] | None = None,
    include_dirs: list[Path] | None = None,
    defines: dict[str, str] | None = None,
    libraries: list[str] | None = None,
    timescale: str | None = None,
    debug: str = "all",
    kdb: bool = True,
    coverage: list[str] | None = None,
    sv_libs: list[Path] | None = None,
    plusargs: list[str] | None = None,
    seed: int | None = None,
    workdir: Path,
    top: str,
    dump_name: str,
    tools: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    vhdlan_args: list[str] | None = None,
    vlogan_args: list[str] | None = None,
    vcs_args: list[str] | None = None,
    simv_args: list[str] | None = None,
    fsdbreport_args: list[str] | None = None,
    verdi_args: list[str] | None = None,
    expected_artifacts: dict | None = None,
    step_timeout: int | None = None,
    rc_file: Path | None = None,
    cmd_file: Path | None = None,
    pli_dir: Path | None = None,
    auto_pli: bool = True,
    verdi_check: str = "verdi",
    report_signal: str | None = None,
    clean: bool = False,
    log_dir: Path | None = None,
) -> dict:
    workdir = workdir.resolve()
    log_dir = (log_dir or workdir / "logs").resolve()
    coverage_db = workdir / "simv.vdb" if coverage else None
    coverage_args = _coverage_arg(coverage, coverage_db)
    vhdl_coverage_args: list[str] = []
    tool_map = tools or {}
    vhdl_source_args = _resolve_many(vhdl_sources)
    if source or sources or source_list or source_lists:
        source_args = normalize_sources(source=source, sources=sources, source_list=source_list, source_lists=source_lists)
    else:
        source_args = []
    if not source_args and not vhdl_source_args:
        raise ValueError("at least one --source, --source-list, --vhdl-source, or manifest source is required")
    dump = workdir / dump_name
    simv_tool = _tool(tool_map, "simv", "./simv")
    simv = _simv_artifact(workdir, simv_tool)
    selected_pli_dir = pli_dir.resolve() if pli_dir else find_pli_dir() if auto_pli else None
    selected_pli_args, pli_artifacts = pli_args(selected_pli_dir)
    sv_lib_args = []
    for lib in sv_libs or []:
        sv_lib_args.extend(["-sv_lib", str(Path(lib).resolve())])
    kdb_args = ["-kdb"] if kdb else []
    timescale_args = [f"-timescale={timescale}"] if timescale else []
    common_compile_args = [*_work_args(libraries), *kdb_args]
    vhdl_cmd = [
        _tool(tool_map, "vhdlan", "vhdlan"),
        "-full64",
        *common_compile_args,
        *vhdl_coverage_args,
        *_stage_args(vhdlan_args),
        *vhdl_source_args,
    ]
    compile_cmd = [
        _tool(tool_map, "vlogan", "vlogan"),
        "-full64",
        "-sverilog",
        *common_compile_args,
        *coverage_args,
        *timescale_args,
        *_include_args(include_dirs),
        *_define_args(defines),
        *_stage_args(vlogan_args),
        *source_args,
    ]
    elaborate_cmd = [
        _tool(tool_map, "vcs", "vcs"),
        "-full64",
        *kdb_args,
        "-sverilog",
        f"-debug_access+{debug}",
        *coverage_args,
        *selected_pli_args,
        *sv_lib_args,
        *_stage_args(vcs_args),
        f"work.{top}",
        "-o",
        str(simv),
    ]
    simulate_cmd = [simv_tool, "+fsdbfile+" + dump_name]
    simulate_cmd.extend(coverage_args)
    if seed is not None:
        simulate_cmd.append(f"+ntb_random_seed={seed}")
    simulate_cmd.extend(plusargs or [])
    simulate_cmd.extend(_stage_args(simv_args))
    if cmd_file:
        simulate_cmd.extend(["-ucli", "-do", str(cmd_file.resolve())])
    steps = []
    if vhdl_source_args:
        steps.append(
            {
                "name": "compile-vhdl",
                "cmd": vhdl_cmd,
                "cwd": str(workdir),
                "log": _log_path(log_dir, "compile-vhdl"),
            }
        )
    if source_args:
        compile_step_name = "compile-verilog" if vhdl_source_args else "compile"
        steps.append(
            {
                "name": compile_step_name,
                "cmd": compile_cmd,
                "cwd": str(workdir),
                "log": _log_path(log_dir, compile_step_name),
            }
        )
    steps.extend(
        [
            {
                "name": "elaborate",
                "cmd": elaborate_cmd,
                "cwd": str(workdir),
                "log": _log_path(log_dir, "elaborate"),
            },
            {
                "name": "simulate",
                "cmd": simulate_cmd,
                "cwd": str(workdir),
                "log": _log_path(log_dir, "simulate"),
            },
        ]
    )
    if verdi_check == "fsdbreport":
        signal = report_signal or f"/{top}/clk"
        verdi_cmd = [_tool(tool_map, "fsdbreport", "fsdbreport"), str(dump), "-s", signal, *_stage_args(fsdbreport_args)]
        verdi_step_name = "verdi-fsdbreport-check"
    else:
        verdi_cmd = [_tool(tool_map, "verdi", "verdi"), "-ssf", str(dump)]
        if rc_file:
            verdi_cmd.extend(["-sswr", str(rc_file.resolve())])
        verdi_cmd.extend(["-nologo", "-exit", *_stage_args(verdi_args)])
        verdi_step_name = "verdi-load-check"
    steps.append(
        {
            "name": verdi_step_name,
            "cmd": verdi_cmd,
            "cwd": str(workdir),
            "log": _log_path(log_dir, verdi_step_name),
        }
    )
    return {
        "sources": [str(Path(item).resolve()) for item in (sources or ([source] if source else []))],
        "vhdl_sources": _resolve_many(vhdl_sources),
        "source_list": str(source_list.resolve()) if source_list else "",
        "source_lists": [str(Path(item).resolve()) for item in (source_lists or [])],
        "cmd_file": str(cmd_file.resolve()) if cmd_file else "",
        "include_dirs": _resolve_many(include_dirs),
        "defines": defines or {},
        "libraries": libraries or [],
        "timescale": timescale or "",
        "debug": debug,
        "kdb": kdb,
        "coverage": coverage or [],
        "coverage_db": str(coverage_db) if coverage_db else "",
        "coverage_args": {
            "vhdlan": vhdl_coverage_args,
            "vlogan": coverage_args,
            "compile": coverage_args,
            "vhdl_compile": vhdl_coverage_args,
            "verilog_compile": coverage_args,
            "elaborate": coverage_args,
            "simulate": coverage_args,
        },
        "sv_libs": _resolve_many(sv_libs),
        "plusargs": plusargs or [],
        "seed": seed,
        "tools": tool_map,
        "env": {str(key): str(value) for key, value in (env or {}).items()},
        "stage_args": {
            "vhdlan": _stage_args(vhdlan_args),
            "vlogan": _stage_args(vlogan_args),
            "vcs": _stage_args(vcs_args),
            "simv": _stage_args(simv_args),
            "fsdbreport": _stage_args(fsdbreport_args),
            "verdi": _stage_args(verdi_args),
        },
        "expected_artifacts": _normalize_expected_artifacts(workdir, expected_artifacts, dump),
        "step_timeout": step_timeout,
        "verdi_check": verdi_check,
        "report_signal": report_signal or "",
        "workdir": str(workdir),
        "log_dir": str(log_dir),
        "top": top,
        "clean": clean,
        "steps": steps,
        "artifacts": {
            "simv": str(simv),
            "dump": str(dump),
            "rc": str(rc_file.resolve()) if rc_file else "",
            **pli_artifacts,
        },
    }


def _manifest_path_list(base: Path, values: list[str]) -> list[Path]:
    return [(base / item).resolve() for item in values]


def _manifest_env(base: Path, values: dict[str, str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in values.items():
        text = str(value)
        if key == "LD_LIBRARY_PATH":
            separator = ";" if ";" in text else ":"
            parts = []
            for item in text.split(separator):
                if not item:
                    continue
                path = Path(item)
                parts.append(str(path if path.is_absolute() else (base / path).resolve()))
            env[str(key)] = os.pathsep.join(parts)
        else:
            env[str(key)] = text
    return env


def validate_manifest(data: dict) -> None:
    list_fields = (
        "sources",
        "source_lists",
        "include_dirs",
        "libraries",
        "coverage",
        "sv_libs",
        "plusargs",
        "vhdlan_args",
        "vlogan_args",
        "vcs_args",
        "simv_args",
        "fsdbreport_args",
        "verdi_args",
    )
    for field in list_fields:
        if field in data and not isinstance(data[field], list):
            raise ValueError(f"manifest {field} must be a list")
    for field in ("defines", "tools", "env", "expected_artifacts"):
        if field in data and not isinstance(data[field], dict):
            raise ValueError(f"manifest {field} must be an object")
    for item in data.get("sources", []):
        if not isinstance(item, (str, dict)):
            raise ValueError("manifest sources entries must be strings or objects")
        if isinstance(item, dict) and "path" not in item:
            raise ValueError("manifest source objects must include path")


def build_smoke_plan_from_manifest(*, manifest: Path, workdir: Path | None = None, **overrides) -> dict:
    manifest = manifest.resolve()
    manifest_dir = manifest.parent
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest root must be an object")
    validate_manifest(data)
    sv_sources: list[Path] = []
    vhdl_sources: list[Path] = []
    for item in data.get("sources", []):
        if isinstance(item, str):
            path = (manifest_dir / item).resolve()
            language = path.suffix.lower().lstrip(".")
        else:
            path = (manifest_dir / item["path"]).resolve()
            language = str(item.get("language", path.suffix.lower().lstrip("."))).lower()
        if language in {"vhdl", "vhd"} or path.suffix.lower() in {".vhd", ".vhdl"}:
            vhdl_sources.append(path)
        else:
            sv_sources.append(path)

    source_lists = _manifest_path_list(manifest_dir, data.get("source_lists", []))

    def option(name: str, default=None):
        value = overrides.get(name)
        if value is not None:
            return value
        return data.get(name, default)

    plan = build_smoke_plan(
        sources=sv_sources,
        source_lists=source_lists,
        vhdl_sources=vhdl_sources,
        include_dirs=_manifest_path_list(manifest_dir, data.get("include_dirs", [])),
        defines={str(key): str(value) for key, value in data.get("defines", {}).items()},
        libraries=[str(item) for item in data.get("libraries", [])],
        timescale=data.get("timescale"),
        debug=str(data.get("debug", "all")),
        kdb=bool(data.get("kdb", True)),
        coverage=[str(item) for item in data.get("coverage", [])],
        sv_libs=_manifest_path_list(manifest_dir, data.get("sv_libs", [])),
        plusargs=[str(item) for item in data.get("plusargs", [])],
        seed=data.get("seed"),
        workdir=(workdir or (manifest_dir / data.get("workdir", "run"))).resolve(),
        top=str(option("top", "top")),
        dump_name=str(option("dump_name", "waves.fsdb")),
        tools={str(key): str(value) for key, value in data.get("tools", {}).items()},
        env=_manifest_env(manifest_dir, data.get("env", {})),
        vhdlan_args=[str(item) for item in data.get("vhdlan_args", [])],
        vlogan_args=[str(item) for item in data.get("vlogan_args", [])],
        vcs_args=[str(item) for item in data.get("vcs_args", [])],
        simv_args=[str(item) for item in data.get("simv_args", [])],
        fsdbreport_args=[str(item) for item in data.get("fsdbreport_args", [])],
        verdi_args=[str(item) for item in data.get("verdi_args", [])],
        expected_artifacts=data.get("expected_artifacts"),
        step_timeout=option("step_timeout"),
        rc_file=(manifest_dir / data["rc_file"]).resolve() if data.get("rc_file") else None,
        cmd_file=(manifest_dir / data["cmd_file"]).resolve() if data.get("cmd_file") else None,
        pli_dir=(manifest_dir / data["pli_dir"]).resolve() if data.get("pli_dir") else None,
        auto_pli=bool(option("auto_pli", True)),
        verdi_check=str(option("verdi_check", "verdi")),
        report_signal=option("report_signal"),
        clean=bool(option("clean", False)),
    )
    plan["manifest"] = str(manifest)
    plan["manifest_dir"] = str(manifest_dir)
    return plan


def parse_define(value: str) -> tuple[str, str]:
    if "=" in value:
        key, val = value.split("=", 1)
        return key, val
    return value, ""


def missing_tools(plan: dict) -> list[str]:
    required = []
    for step in plan["steps"]:
        exe = step["cmd"][0]
        if exe.startswith("./"):
            continue
        if Path(exe).is_absolute():
            if not Path(exe).exists():
                required.append(exe)
            continue
        required.append(exe)
    return sorted({tool for tool in required if shutil.which(tool) is None})


def wrapper_info(cmd: list[str]) -> dict:
    if not cmd:
        return {"path": "", "exists": False, "first_line": ""}
    exe = cmd[0]
    if exe.startswith("./"):
        return {"path": exe, "exists": False, "first_line": ""}
    resolved = shutil.which(exe) or exe
    path = Path(resolved)
    info = {"path": str(path), "exists": path.exists(), "first_line": ""}
    if path.exists():
        try:
            info["first_line"] = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        except (IndexError, OSError, UnicodeDecodeError):
            info["first_line"] = ""
    return info


def execution_command(cmd: list[str], shell_which=shutil.which) -> list[str]:
    if not cmd or cmd[0].startswith("./"):
        return cmd
    info = wrapper_info(cmd)
    first = info["first_line"].strip()
    path_text = info["path"].replace("\\", "/").lower()
    if first == "#!/bin/sh -h" or (first == "#!/bin/sh" and "synopsys" in path_text):
        shell = shell_which("bash") or "bash"
        return [shell, info["path"], *cmd[1:]]
    return cmd


def _safe_to_clean(path: Path) -> bool:
    resolved = path.resolve()
    return resolved.name not in {"", ".", ".."} and len(resolved.parts) >= 3


def artifact_status(artifacts: dict[str, Path | str]) -> dict:
    status = {}
    for name, value in artifacts.items():
        if not value:
            continue
        path = Path(value)
        if not path.exists():
            status[name] = {"path": str(path), "state": "missing", "bytes": 0}
            continue
        size = path.stat().st_size
        status[name] = {"path": str(path), "state": "present" if size > 0 else "zero", "bytes": size}
    return status


def expected_artifact_status(expected_artifacts: dict[str, dict]) -> dict:
    status = {}
    for name, spec in expected_artifacts.items():
        path = Path(spec["path"])
        min_bytes = int(spec.get("min_bytes", 1))
        if not path.exists():
            status[name] = {"path": str(path), "state": "missing", "bytes": 0, "min_bytes": min_bytes, "status": "failed"}
            continue
        size = path.stat().st_size
        passed = size >= min_bytes
        status[name] = {
            "path": str(path),
            "state": "present" if size > 0 else "zero",
            "bytes": size,
            "min_bytes": min_bytes,
            "status": "passed" if passed else "failed",
        }
    return status


def plan_diagnostics(plan: dict) -> dict:
    return {
        "missing_tools": missing_tools(plan),
        "steps": [
            {
                "name": step["name"],
                "cwd": step.get("cwd", plan["workdir"]),
                "wrapper": wrapper_info(step["cmd"]),
                "execution_cmd": execution_command(step["cmd"]),
                "log": step["log"],
            }
            for step in plan["steps"]
        ],
        "artifacts": artifact_status(plan["artifacts"]),
        "shell": {"SHELL": os.environ.get("SHELL", ""), "COMSPEC": os.environ.get("COMSPEC", "")},
    }


def run_plan(plan: dict, *, step_timeout: int = 300) -> list[dict]:
    results = []
    workdir = Path(plan["workdir"])
    log_dir = Path(plan["log_dir"])
    if plan.get("clean") and workdir.exists():
        if not _safe_to_clean(workdir):
            raise RuntimeError(f"refusing to clean unsafe workdir: {workdir}")
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    for step in plan["steps"]:
        cmd = execution_command(step["cmd"])
        cwd = Path(step.get("cwd", plan["workdir"]))
        cwd.mkdir(parents=True, exist_ok=True)
        try:
            run_env = os.environ.copy()
            run_env.update(plan.get("env", {}))
            completed = subprocess.run(
                cmd,
                cwd=cwd,
                env=run_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=plan.get("step_timeout") or step_timeout,
            )
            returncode = completed.returncode
            output = completed.stdout
        except subprocess.TimeoutExpired as exc:
            returncode = -9
            captured = exc.stdout or ""
            if isinstance(captured, bytes):
                captured = captured.decode("utf-8", errors="replace")
            effective_timeout = plan.get("step_timeout") or step_timeout
            output = captured + f"\nstep timed out after {effective_timeout} seconds\n"
        log_path = Path(step["log"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8", errors="replace")
        results.append(
            {
                "name": step["name"],
                "returncode": returncode,
                "cmd": step["cmd"],
                "execution_cmd": cmd,
                "cwd": str(cwd),
                "log": str(log_path),
                "output": output,
            }
        )
        if returncode != 0:
            break
        if step["name"] == "simulate":
            artifact_check = expected_artifact_status(plan.get("expected_artifacts") or {"dump": {"path": plan["artifacts"]["dump"], "min_bytes": 1}})
            failed = {name: item for name, item in artifact_check.items() if item["status"] != "passed"}
            if failed:
                msg = "FSDB dump is missing or zero bytes after simulate; skipping Verdi load"
                results.append(
                    {
                        "name": "artifact-check",
                        "returncode": 1,
                        "cmd": [],
                        "execution_cmd": [],
                        "cwd": str(cwd),
                        "log": "",
                        "output": msg + "\n" + json.dumps(failed, sort_keys=True),
                    }
                )
                break
    return results


def summarize_status(output: dict) -> str:
    if output["status"] != "passed":
        return output["status"]
    artifacts = output.get("artifact_status", {})
    dump = artifacts.get("dump", {})
    if dump.get("state") != "present":
        output["reason"] = "FSDB dump is missing or zero bytes"
        return "failed"
    return "passed"


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan or run a minimal VCS/Verdi smoke flow.")
    parser.add_argument("--manifest", type=Path, help="JSON manifest describing sources and common non-GUI simulation options.")
    parser.add_argument("--source", action="append", type=Path, default=[])
    parser.add_argument("--vhdl-source", action="append", type=Path, default=[])
    parser.add_argument("--source-list", type=Path)
    parser.add_argument("--include-dir", action="append", type=Path, default=[])
    parser.add_argument("--define", action="append", default=[], help="Verilog define as NAME or NAME=VALUE.")
    parser.add_argument("--library", action="append", default=[])
    parser.add_argument("--timescale")
    parser.add_argument("--debug", default="all")
    parser.add_argument("--no-kdb", action="store_true")
    parser.add_argument("--coverage", action="append", default=[], help="Coverage item, for example line or cond.")
    parser.add_argument("--sv-lib", action="append", type=Path, default=[], help="DPI shared library passed to VCS with -sv_lib.")
    parser.add_argument("--plusarg", action="append", default=[])
    parser.add_argument("--seed", type=int)
    parser.add_argument("--workdir", type=Path, default=Path("build/vcs-verdi-smoke"))
    parser.add_argument("--top", default="top")
    parser.add_argument("--dump-name", default="waves.fsdb")
    parser.add_argument("--rc-file", type=Path)
    parser.add_argument("--cmd-file", type=Path)
    parser.add_argument("--pli-dir", type=Path, help="Directory containing novas.tab and pli.a.")
    parser.add_argument("--no-auto-pli", action="store_true", help="Do not infer Verdi novas PLI from VERDI_HOME/NOVAS_HOME.")
    parser.add_argument("--clean", action="store_true", help="Remove the work directory before executing.")
    parser.add_argument("--execute", action="store_true", help="Run commands after planning.")
    parser.add_argument("--dry-run", action="store_true", help="Print the command plan without executing. This is the default.")
    parser.add_argument("--step-timeout", type=int, default=300, help="Per-step execution timeout in seconds.")
    parser.add_argument("--verdi-check", choices=("verdi", "fsdbreport"), default="verdi")
    parser.add_argument("--report-signal", default=None, help="Signal path for fsdbreport mode, for example /top/clk.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args()

    try:
        if args.manifest:
            manifest_overrides = {}
            if args.no_auto_pli:
                manifest_overrides["auto_pli"] = False
            plan = build_smoke_plan_from_manifest(
                manifest=args.manifest,
                workdir=args.workdir.resolve(),
                **manifest_overrides,
            )
        else:
            plan = build_smoke_plan(
                sources=[item.resolve() for item in args.source],
                source_list=_as_path(args.source_list.resolve()) if args.source_list else None,
                vhdl_sources=[item.resolve() for item in args.vhdl_source],
                include_dirs=[item.resolve() for item in args.include_dir],
                defines=dict(parse_define(item) for item in args.define),
                libraries=[str(item) for item in args.library],
                timescale=args.timescale,
                debug=args.debug,
                kdb=not args.no_kdb,
                coverage=[str(item) for item in args.coverage],
                sv_libs=[item.resolve() for item in args.sv_lib],
                plusargs=[str(item) for item in args.plusarg],
                seed=args.seed,
                workdir=args.workdir.resolve(),
                top=args.top,
                dump_name=args.dump_name,
                rc_file=args.rc_file.resolve() if args.rc_file else None,
                cmd_file=args.cmd_file.resolve() if args.cmd_file else None,
                pli_dir=args.pli_dir.resolve() if args.pli_dir else None,
                auto_pli=not args.no_auto_pli,
                verdi_check=args.verdi_check,
                report_signal=args.report_signal,
                clean=args.clean,
            )
    except ValueError as exc:
        parser.error(str(exc))

    output = {"plan": plan, "diagnostics": plan_diagnostics(plan), "missing_tools": missing_tools(plan)}
    if args.execute:
        if output["missing_tools"]:
            output["status"] = "skipped"
            output["reason"] = "missing required tools"
        else:
            try:
                output["results"] = run_plan(plan, step_timeout=args.step_timeout)
            except RuntimeError as exc:
                output["status"] = "failed"
                output["reason"] = str(exc)
            else:
                last_ok = bool(output["results"]) and output["results"][-1]["returncode"] == 0
                output["artifact_status"] = artifact_status(plan["artifacts"])
                output["status"] = "passed" if last_ok else "failed"
                output["status"] = summarize_status(output)
    else:
        output["status"] = "dry-run"

    if args.json:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        for step in plan["steps"]:
            print(f"{step['name']}: {' '.join(step['cmd'])}")
            print(f"  cwd: {step.get('cwd', plan['workdir'])}")
            print(f"  log: {step['log']}")
        if output["missing_tools"]:
            print("missing tools: " + ", ".join(output["missing_tools"]))
        if output.get("reason"):
            print("reason: " + output["reason"])
        print("status: " + output["status"])
    return 0 if output["status"] in {"dry-run", "passed", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
