#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import re


COLOR_MAP = {
    "red": "ID_RED5",
    "green": "ID_GREEN5",
    "blue": "ID_BLUE5",
    "yellow": "ID_YELLOW5",
    "cyan": "ID_CYAN5",
    "magenta": "ID_MAGENTA5",
    "white": "ID_WHITE",
    "black": "ID_BLACK",
    "gray": "ID_GRAY5",
    "grey": "ID_GRAY5",
    "orange": "ID_ORANGE5",
    "pink": "ID_PINK5",
    "brown": "ID_BROWN5",
    "purple": "ID_PURPLE5",
    "lightcyan": "ID_CYAN3",
}


def strip_comment(line: str) -> str:
    return line.split("#", 1)[0].strip()


def parse_base(path: Path) -> dict[str, dict[str, str]]:
    envs: dict[str, dict[str, str]] = {}
    current = None
    if not path.exists():
        raise ValueError(f"base file not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = strip_comment(line)
        if not raw:
            continue
        if raw.startswith("[") and raw.endswith("]"):
            current = raw[1:-1].strip()
            envs[current] = {}
            continue
        if current and "=" in raw:
            key, value = raw.split("=", 1)
            envs[current][key.strip()] = value.strip()
    return envs


def resolve(path: str, env: dict[str, str], seen: set[str] | None = None) -> str:
    seen = seen or set()
    if path in seen:
        return path
    if path in env:
        seen.add(path)
        return resolve(env[path], env, seen)
    if "." not in path:
        return path
    prefix, rest = path.split(".", 1)
    if prefix in env:
        seen.add(prefix)
        return f"{resolve(env[prefix], env, seen)}.{rest}"
    return path


def verdi_path(path: str) -> str:
    converted = path.replace(".", "/")
    if not converted.startswith("/"):
        converted = "/" + converted
    return converted.replace("[", "\\[").replace("]", "\\]")


def color_option(color: str) -> str:
    if not color:
        return ""
    mapped = COLOR_MAP.get(color.lower())
    return f"-color {mapped}" if mapped else ""


def parse_scenario(path: Path, env: dict[str, str]) -> tuple[list[dict], dict[str, list[str]], list[dict]]:
    if not path.exists():
        raise ValueError(f"scenario file not found: {path}")
    groups: list[dict] = []
    buses: dict[str, list[str]] = {}
    markers: list[dict] = []
    section = ""

    for line in path.read_text(encoding="utf-8").splitlines():
        raw = strip_comment(line)
        if not raw:
            continue
        if raw.startswith("[") and raw.endswith("]"):
            section = raw[1:-1].strip().upper()
            continue
        if section == "GROUPS":
            parts = re.split(r"\s+", raw)
            if not parts:
                continue
            index = parts[0]
            if index.endswith("."):
                groups.append({"name": parts[1], "color": parts[2] if len(parts) > 2 else "", "signals": []})
                continue
            if not groups or len(parts) < 3:
                raise ValueError(f"invalid signal row: {raw}")
            height = ""
            alias = ""
            if len(parts) > 4:
                if parts[4].isdigit():
                    height = parts[4]
                    alias = parts[5] if len(parts) > 5 else ""
                else:
                    alias = parts[4]
            groups[-1]["signals"].append(
                {
                    "path": parts[1],
                    "radix": parts[2],
                    "color": parts[3] if len(parts) > 3 and parts[3] != "-" else "",
                    "height": height,
                    "alias": alias,
                }
            )
        elif section == "VIRTUAL_BUSES" and "=" in raw:
            name, values = raw.split("=", 1)
            buses[name.strip()] = [resolve(item.strip(), env) for item in values.split(",") if item.strip()]
        elif section == "MARKERS":
            parts = re.split(r"\s+", raw)
            if len(parts) < 2:
                raise ValueError(f"invalid marker row: {raw}")
            markers.append({"time": parts[0], "name": parts[1], "color": parts[2] if len(parts) > 2 else "white"})
    return groups, buses, markers


def radix_option(radix: str) -> str:
    lower = radix.lower()
    if lower == "analog":
        return "-analog"
    if lower in {"hex", "bin", "dec", "oct"}:
        return f"-{lower.upper()}"
    return ""


def generate_rc(groups: list[dict], buses: dict[str, list[str]], markers: list[dict], env: dict[str, str]) -> str:
    lines = ["# Verdi Signal Save File", ""]

    for group in groups:
        for signal in group["signals"]:
            if signal["alias"] and signal["path"] not in buses:
                real = verdi_path(resolve(signal["path"], env))
                parent = real.rsplit("/", 1)[0]
                lines.append(f'addRenameSig "{parent}/{signal["alias"]}" "{real}"')
    if len(lines) > 2:
        lines.append("")

    for marker in markers:
        lines.append(f'addMarker -time {marker["time"]} -name "{marker["name"]}" -color {marker["color"].upper()}')
    if markers:
        lines.append("")

    for group in groups:
        group_color = color_option(group["color"])
        color_prefix = f"{group_color} " if group_color else ""
        lines.append(f'addGroup {color_prefix}"{group["name"]}"')
        for signal in group["signals"]:
            options = [f'-h {signal["height"] or "15"}']
            chosen_color = color_option(signal["color"] or group["color"])
            chosen_radix = radix_option(signal["radix"])
            if chosen_color:
                options.append(chosen_color)
            if chosen_radix:
                options.append(chosen_radix)
            opts = " ".join(options)
            if signal["path"] in buses:
                name = signal["alias"] or signal["path"]
                lines.append(f'addBus {opts} -name "{name}"')
                for bus_signal in buses[signal["path"]]:
                    lines.append(f"  addBusSignal {verdi_path(bus_signal)}")
            else:
                lines.append(f"addSignal {opts} {verdi_path(resolve(signal['path'], env))}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_rc(*, config_dir: Path, scenario: str, base: str) -> str:
    bases = parse_base(config_dir / "scn_base.lst")
    if base not in bases:
        raise ValueError(f"BASE '{base}' not found")
    scenario_file = config_dir / f"scn_{scenario}.lst"
    groups, buses, markers = parse_scenario(scenario_file, bases[base])
    return generate_rc(groups, buses, markers, bases[base])


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Verdi signal restore RC file.")
    parser.add_argument("-c", "--config-dir", type=Path, default=Path("config"))
    parser.add_argument("-s", "--scenario", required=True)
    parser.add_argument("-b", "--base", required=True)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()

    try:
        rc = render_rc(config_dir=args.config_dir, scenario=args.scenario, base=args.base)
    except ValueError as exc:
        parser.error(str(exc))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rc, encoding="utf-8")
        print(args.output)
    else:
        print(rc, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
