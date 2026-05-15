#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Mapping


def fsdb_status(path: Path | str) -> dict:
    fsdb = Path(path)
    if not fsdb.exists():
        return {"state": "missing", "status": "missing", "path": str(fsdb), "bytes": 0}
    size = fsdb.stat().st_size
    state = "present" if size > 0 else "zero"
    return {"state": state, "status": state, "path": str(fsdb), "bytes": size}


def require_nonzero_fsdb(path: Path | str) -> dict:
    status = fsdb_status(path)
    if status["state"] == "present":
        return {**status, "status": "passed"}
    reason = "FSDB is missing" if status["state"] == "missing" else "FSDB is zero bytes"
    return {**status, "status": "failed", "reason": reason}


def build_fsdbreport_cmd(fsdb: Path | str, signal: str | None = None) -> list[str]:
    cmd = ["fsdbreport", str(Path(fsdb))]
    if signal:
        cmd.extend(["-s", signal])
    return cmd


NPI_BANNER_KEYWORDS = (
    "NPI -",
    "Version V-",
    "Copyright",
    "Synopsys",
    "Licensed Products",
    "Native Programming",
    "solvnetplus",
    "License Key",
)


def filter_npi_banner(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(keyword in stripped for keyword in NPI_BANNER_KEYWORDS):
            continue
        lines.append(line)
    return "\n".join(lines)


def find_verdi_python(
    *,
    env: Mapping[str, str] | None = None,
    path_exists_func: Callable[[str], bool] | None = None,
) -> str:
    env_map = env or os.environ
    path_exists = path_exists_func or (lambda path: Path(path).exists())
    if env_map.get("VERDI_PYTHON") and path_exists(env_map["VERDI_PYTHON"]):
        return env_map["VERDI_PYTHON"]
    verdi_home = env_map.get("VERDI_HOME", "")
    if not verdi_home:
        return ""
    candidates = [
        Path(verdi_home) / "platform" / "linux64" / "Python" / "bin" / "python3.6",
        Path(verdi_home) / "platform" / "linux64" / "Python" / "bin" / "python3",
    ]
    for candidate in candidates:
        text = str(candidate)
        if path_exists(text):
            return text
    return ""


def normalize_npi_signal_path(signal: str) -> str:
    return signal.strip().strip("/").replace("/", ".")


def normalize_cli_signal_path(signal: str) -> str:
    value = signal.strip()
    if value.startswith("/"):
        value = value[1:]
    return "/" + value.replace(".", "/")


def _npi_script(action: str, fsdb: Path, *, signal: str | None = None, scope: str | None = None, depth: int = 2) -> str:
    fsdb_text = str(fsdb.resolve()).replace("\\", "\\\\")
    if action == "read-signal":
        npi_signal = normalize_npi_signal_path(signal or "")
        return (
            "from pynpi import npisys, waveform\n"
            "npisys.init([''])\n"
            f"fh = waveform.open('{fsdb_text}')\n"
            f"sig = fh.sig_by_name('{npi_signal}')\n"
            "fh.add_to_sig_list(sig)\n"
            "fh.load_vc_by_range(0, fh.max_time())\n"
            "vct = sig.create_vct()\n"
            "print('Time(1ps) ' + sig.full_name())\n"
            "ret = vct.goto_first()\n"
            "while ret:\n"
            "    print('%d %s' % (vct.time(), vct.value(waveform.VctFormat_e.HexStrVal)))\n"
            "    ret = vct.goto_next()\n"
            "npisys.end()\n"
        )
    if action == "list-signals":
        scope_filter = normalize_npi_signal_path(scope or "")
        return (
            "from pynpi import npisys, waveform\n"
            "npisys.init([''])\n"
            f"fh = waveform.open('{fsdb_text}')\n"
            f"scope_name = '{scope_filter}'\n"
            f"max_depth = {depth}\n"
            "print('LIST_SIGNALS depth=%d scope=%s' % (max_depth, scope_name))\n"
            "npisys.end()\n"
        )
    return (
        "from pynpi import npisys, waveform\n"
        "npisys.init([''])\n"
        f"fh = waveform.open('{fsdb_text}')\n"
        "print('FSDB_INFO')\n"
        "print('max_time=%s' % fh.max_time())\n"
        "npisys.end()\n"
    )


def build_fsdb_read_plan(
    fsdb: Path | str,
    *,
    action: str,
    signal: str | None = None,
    scope: str | None = None,
    depth: int = 2,
    output: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    path_exists_func: Callable[[str], bool] | None = None,
) -> dict:
    fsdb_path = Path(fsdb)
    status = require_nonzero_fsdb(fsdb_path)
    if status["status"] != "passed":
        return status
    verdi_python = find_verdi_python(env=env, path_exists_func=path_exists_func)
    if action == "convert-vcd":
        if output is None:
            raise ValueError("output is required for convert-vcd")
        return {
            "status": "planned",
            "mode": "cli",
            "action": action,
            "cmd": build_convert_cmd(fsdb_path, output),
            "non_gui": True,
        }
    if verdi_python:
        script = _npi_script(action, fsdb_path, signal=signal, scope=scope, depth=depth)
        return {
            "status": "planned",
            "mode": "npi",
            "action": action,
            "fsdb": str(fsdb_path),
            "cmd": [verdi_python, "-c", script],
            "script": script,
            "npi_signal": normalize_npi_signal_path(signal or "") if signal else "",
            "cli_signal": normalize_cli_signal_path(signal or "") if signal else "",
            "non_gui": True,
        }
    if action == "read-signal":
        if not signal:
            raise ValueError("signal is required for read-signal")
        cmd = build_fsdbreport_cmd(fsdb_path, normalize_cli_signal_path(signal))
    elif action == "list-signals":
        cmd = ["fsdb2vcd", str(fsdb_path), "-bt", "0", "-et", "0"]
    elif action == "info":
        cmd = ["fsdb2vcd", str(fsdb_path), "-bt", "0", "-et", "0"]
    else:
        raise ValueError(f"unsupported FSDB reader action: {action}")
    return {
        "status": "planned",
        "mode": "cli",
        "action": action,
        "fsdb": str(fsdb_path),
        "cmd": cmd,
        "notes": "CLI fallback; scope=%s depth=%s" % (scope or "", depth),
        "npi_signal": normalize_npi_signal_path(signal or "") if signal else "",
        "cli_signal": normalize_cli_signal_path(signal or "") if signal else "",
        "non_gui": True,
    }


def build_convert_cmd(src: Path | str, dst: Path | str) -> list[str]:
    src_path = Path(src)
    dst_path = Path(dst)
    if src_path.suffix.lower() == ".fsdb" and dst_path.suffix.lower() == ".vcd":
        return ["fsdb2vcd", str(src_path), "-o", str(dst_path)]
    if src_path.suffix.lower() == ".vcd" and dst_path.suffix.lower() == ".fsdb":
        return ["vcd2fsdb", str(src_path), "-o", str(dst_path)]
    if src_path.suffix.lower() == ".vpd" and dst_path.suffix.lower() == ".fsdb":
        return ["vpd2fsdb", str(src_path), "-o", str(dst_path)]
    raise ValueError(f"unsupported conversion: {src_path.suffix} -> {dst_path.suffix}")


def build_vcd_first_debug_plan(vcd: Path | str, fsdb: Path | str, signal: str | None = None) -> dict:
    vcd_path = Path(vcd)
    fsdb_path = Path(fsdb)
    commands = [
        build_convert_cmd(vcd_path, fsdb_path),
        build_fsdbreport_cmd(fsdb_path, signal),
        ["verdi", "-ssf", str(fsdb_path), "-nologo", "-exit"],
    ]
    return {
        "status": "planned",
        "input": str(vcd_path),
        "output": str(fsdb_path),
        "commands": commands,
        "non_gui": True,
    }


def execute_command(cmd: list[str], *, timeout: int = 300, cwd: Path | str | None = None) -> dict:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            cwd=Path(cwd) if cwd is not None else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return {
            "cmd": cmd,
            "cwd": str(Path(cwd)) if cwd is not None else "",
            "returncode": completed.returncode,
            "status": "passed" if completed.returncode == 0 else "failed",
            "elapsed_sec": round(time.monotonic() - started, 3),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "cmd": cmd,
            "cwd": str(Path(cwd)) if cwd is not None else "",
            "returncode": None,
            "status": "timeout",
            "elapsed_sec": round(time.monotonic() - started, 3),
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or f"timeout after {timeout}s",
        }


def parse_fsdbreport(text: str) -> dict:
    header: list[str] | None = None
    samples: list[dict] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("=") or set(line) <= {"-", " "}:
            continue
        parts = line.split()
        if header is None and parts and parts[0].lower().startswith("time"):
            header = parts
            continue
        if header is None:
            continue
        if len(parts) < len(header):
            continue
        values = {signal: value for signal, value in zip(header[1:], parts[1:])}
        samples.append({"time": parts[0], "values": values})
    return {"signals": header[1:] if header else [], "samples": samples}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and inspect non-GUI FSDB utility commands.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("fsdb", type=Path)
    status_parser.add_argument("--require-nonzero", action="store_true")
    status_parser.add_argument("--json", action="store_true")

    report_parser = subparsers.add_parser("fsdbreport-cmd")
    report_parser.add_argument("fsdb", type=Path)
    report_parser.add_argument("--signal")
    report_parser.add_argument("--json", action="store_true")

    convert_parser = subparsers.add_parser("convert-cmd")
    convert_parser.add_argument("src", type=Path)
    convert_parser.add_argument("dst", type=Path)
    convert_parser.add_argument("--json", action="store_true")

    vcd_plan_parser = subparsers.add_parser("vcd-debug-plan")
    vcd_plan_parser.add_argument("vcd", type=Path)
    vcd_plan_parser.add_argument("fsdb", type=Path)
    vcd_plan_parser.add_argument("--signal")
    vcd_plan_parser.add_argument("--json", action="store_true")

    execute_parser = subparsers.add_parser("execute")
    execute_parser.add_argument("cmd", nargs="+")
    execute_parser.add_argument("--cwd", type=Path)
    execute_parser.add_argument("--timeout", type=int, default=300)
    execute_parser.add_argument("--json", action="store_true")

    parse_parser = subparsers.add_parser("parse-report")
    parse_parser.add_argument("report", type=Path)
    parse_parser.add_argument("--json", action="store_true")

    read_parser = subparsers.add_parser("read-plan")
    read_parser.add_argument("fsdb", type=Path)
    read_parser.add_argument("--action", choices=("info", "list-signals", "read-signal", "convert-vcd"), default="info")
    read_parser.add_argument("--signal")
    read_parser.add_argument("--scope")
    read_parser.add_argument("--depth", type=int, default=2)
    read_parser.add_argument("--output", type=Path)
    read_parser.add_argument("--json", action="store_true")

    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.command == "status":
        result = require_nonzero_fsdb(args.fsdb) if args.require_nonzero else fsdb_status(args.fsdb)
    elif args.command == "fsdbreport-cmd":
        result = {"cmd": build_fsdbreport_cmd(args.fsdb, args.signal)}
    elif args.command == "convert-cmd":
        result = {"cmd": build_convert_cmd(args.src, args.dst)}
    elif args.command == "vcd-debug-plan":
        result = build_vcd_first_debug_plan(args.vcd, args.fsdb, signal=args.signal)
    elif args.command == "execute":
        result = execute_command(args.cmd, timeout=args.timeout, cwd=args.cwd)
    elif args.command == "read-plan":
        result = build_fsdb_read_plan(
            args.fsdb,
            action=args.action,
            signal=args.signal,
            scope=args.scope,
            depth=args.depth,
            output=args.output,
        )
    else:
        result = parse_fsdbreport(args.report.read_text(encoding="utf-8", errors="replace"))

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
