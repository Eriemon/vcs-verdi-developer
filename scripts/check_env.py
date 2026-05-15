#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from typing import Callable, Mapping


TOOLS = ("vlogan", "vcs", "verdi", "python3", "python", "bash", "sh")
ENV_VARS = (
    "VCS_HOME",
    "VERDI_HOME",
    "NOVAS_HOME",
    "SNPSLMD_LICENSE_FILE",
    "LM_LICENSE_FILE",
    "DISPLAY",
    "XAUTHORITY",
    "VNC_DISPLAY",
    "SHELL",
    "PATH",
    "LD_LIBRARY_PATH",
)


def check_sh_compat(which_func: Callable[[str], str | None] | None = None) -> dict:
    which = which_func or shutil.which
    path = which("sh") or "/bin/sh"
    if not path:
        return {"available": False, "path": "", "supports_dash_h": False}
    try:
        completed = subprocess.run(
            [path, "-h", "-c", "exit 0"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "path": path, "supports_dash_h": False}
    return {
        "available": completed.returncode == 0,
        "path": path,
        "supports_dash_h": completed.returncode == 0,
        "stderr": completed.stderr.strip(),
    }


def check_environment(
    *,
    which_func: Callable[[str], str | None] | None = None,
    env: Mapping[str, str] | None = None,
    sh_compat_func: Callable[[], dict] | None = None,
) -> dict:
    which = which_func or shutil.which
    env_map = env or os.environ
    tools = {}
    for tool in TOOLS:
        path = which(tool)
        tools[tool] = {"available": bool(path), "path": path or ""}

    env_report = {}
    for name in ENV_VARS:
        value = env_map.get(name, "")
        env_report[name] = {"set": bool(value), "value": value}

    ready_for_vcs = tools["vlogan"]["available"] and tools["vcs"]["available"] and env_report["VCS_HOME"]["set"]
    ready_for_verdi = tools["verdi"]["available"] and env_report["VERDI_HOME"]["set"]
    ready_for_license = env_report["SNPSLMD_LICENSE_FILE"]["set"] or env_report["LM_LICENSE_FILE"]["set"]
    display_value = env_map.get("DISPLAY", "")
    display_report = {
        "available": bool(display_value),
        "value": display_value,
        "xauthority_set": bool(env_map.get("XAUTHORITY", "")),
        "vnc_display": env_map.get("VNC_DISPLAY", ""),
    }
    ld_library_path = env_map.get("LD_LIBRARY_PATH", "")
    novas_home = env_map.get("NOVAS_HOME", "")
    pli_report = {
        "novas_home": novas_home,
        "novas_hint_present": bool(novas_home) or "novas" in ld_library_path.lower() or "pli" in ld_library_path.lower(),
        "ld_library_path_mentions_pli": "pli" in ld_library_path.lower(),
    }
    path_value = env_map.get("PATH", "")
    split_token = os.pathsep
    if ":" in path_value and ";" not in path_value:
        split_token = ":"
    path_entries = [entry for entry in path_value.split(split_token) if entry]
    shell_report = {
        "SHELL": env_map.get("SHELL", ""),
        "PATH_entries": path_entries,
        "sh_compat": sh_compat_func() if sh_compat_func else check_sh_compat(which),
    }
    blockers: list[str] = []
    warnings: list[str] = []
    if not tools["vlogan"]["available"]:
        blockers.append("vlogan missing")
    if not tools["vcs"]["available"]:
        blockers.append("vcs missing")
    if not env_report["VCS_HOME"]["set"]:
        blockers.append("VCS_HOME unset")
    if not tools["verdi"]["available"]:
        blockers.append("verdi missing")
    if not env_report["VERDI_HOME"]["set"]:
        blockers.append("VERDI_HOME unset")
    if not display_report["available"]:
        blockers.append("DISPLAY unset")
    if not ready_for_license:
        warnings.append("license environment hint missing")
    if not pli_report["novas_hint_present"]:
        warnings.append("novas/PLI hint missing")
    if shell_report["sh_compat"].get("available") and not shell_report["sh_compat"].get("supports_dash_h"):
        warnings.append("POSIX sh rejects -h")

    return {
        "tools": tools,
        "env": env_report,
        "display": display_report,
        "pli": pli_report,
        "shell": shell_report,
        "overall": {
            "ready_for_vcs": ready_for_vcs,
            "ready_for_verdi": ready_for_verdi,
            "ready_for_gui_verdi": ready_for_verdi and display_report["available"],
            "license_hint_present": ready_for_license,
            "blockers": blockers,
            "warnings": warnings,
        },
    }


def text_report(report: dict) -> str:
    lines = ["VCS/Verdi environment check"]
    for name, item in report["tools"].items():
        state = "found" if item["available"] else "missing"
        suffix = f" at {item['path']}" if item["path"] else ""
        lines.append(f"- {name}: {state}{suffix}")
    for name, item in report["env"].items():
        state = "set" if item["set"] else "unset"
        lines.append(f"- {name}: {state}")
    lines.append(f"- DISPLAY available: {report['display']['available']}")
    lines.append(f"- novas/PLI hint present: {report['pli']['novas_hint_present']}")
    lines.append(f"- /bin/sh supports -h: {report['shell']['sh_compat'].get('supports_dash_h')}")
    lines.append(f"- ready_for_vcs: {report['overall']['ready_for_vcs']}")
    lines.append(f"- ready_for_verdi: {report['overall']['ready_for_verdi']}")
    lines.append(f"- ready_for_gui_verdi: {report['overall']['ready_for_gui_verdi']}")
    if report["overall"]["blockers"]:
        lines.append("- blockers: " + ", ".join(report["overall"]["blockers"]))
    if report["overall"]["warnings"]:
        lines.append("- warnings: " + ", ".join(report["overall"]["warnings"]))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check VCS and Verdi tool readiness.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    report = check_environment()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(text_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
