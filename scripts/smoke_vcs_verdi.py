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
) -> list[str]:
    normalized = [str(Path(item).resolve()) for item in (sources or [])]
    if source:
        normalized.append(str(source.resolve()))
    if source_list:
        normalized.extend(["-f", str(source_list.resolve())])
    if not normalized:
        raise ValueError("at least one --source or --source-list is required")
    return normalized


def _log_path(log_dir: Path, step_name: str) -> str:
    return str(log_dir / f"{step_name}.log")


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
    workdir: Path,
    top: str,
    dump_name: str,
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
    source_args = normalize_sources(source=source, sources=sources, source_list=source_list)
    dump = workdir / dump_name
    simv = workdir / "simv"
    selected_pli_dir = pli_dir.resolve() if pli_dir else find_pli_dir() if auto_pli else None
    selected_pli_args, pli_artifacts = pli_args(selected_pli_dir)
    compile_cmd = ["vlogan", "-full64", "-sverilog", "-kdb", *source_args]
    elaborate_cmd = [
        "vcs",
        "-full64",
        "-kdb",
        "-sverilog",
        "-debug_access+all",
        *selected_pli_args,
        f"work.{top}",
        "-o",
        str(simv),
    ]
    simulate_cmd = ["./simv", "+fsdbfile+" + dump_name]
    if cmd_file:
        simulate_cmd.extend(["-ucli", "-do", str(cmd_file.resolve())])
    steps = [
        {
            "name": "compile",
            "cmd": compile_cmd,
            "cwd": str(workdir),
            "log": _log_path(log_dir, "compile"),
        },
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
    if verdi_check == "fsdbreport":
        signal = report_signal or f"/{top}/clk"
        verdi_cmd = ["fsdbreport", str(dump), "-s", signal]
        verdi_step_name = "verdi-fsdbreport-check"
    else:
        verdi_cmd = ["verdi", "-ssf", str(dump)]
        if rc_file:
            verdi_cmd.extend(["-sswr", str(rc_file.resolve())])
        verdi_cmd.extend(["-nologo", "-exit"])
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
        "source_list": str(source_list.resolve()) if source_list else "",
        "cmd_file": str(cmd_file.resolve()) if cmd_file else "",
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


def missing_tools(plan: dict) -> list[str]:
    required = []
    for step in plan["steps"]:
        exe = step["cmd"][0]
        if exe.startswith("./"):
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
            completed = subprocess.run(
                cmd,
                cwd=cwd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=step_timeout,
            )
            returncode = completed.returncode
            output = completed.stdout
        except subprocess.TimeoutExpired as exc:
            returncode = -9
            captured = exc.stdout or ""
            if isinstance(captured, bytes):
                captured = captured.decode("utf-8", errors="replace")
            output = captured + f"\nstep timed out after {step_timeout} seconds\n"
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
            dump_status = artifact_status({"dump": plan["artifacts"]["dump"]})["dump"]
            if dump_status["state"] != "present":
                msg = "FSDB dump is missing or zero bytes after simulate; skipping Verdi load"
                results.append(
                    {
                        "name": "artifact-check",
                        "returncode": 1,
                        "cmd": [],
                        "execution_cmd": [],
                        "cwd": str(cwd),
                        "log": "",
                        "output": msg,
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
    parser.add_argument("--source", action="append", type=Path, default=[])
    parser.add_argument("--source-list", type=Path)
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
        plan = build_smoke_plan(
            sources=[item.resolve() for item in args.source],
            source_list=_as_path(args.source_list.resolve()) if args.source_list else None,
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
