#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def build_regression_plan(
    manifests: list[Path | str],
    *,
    workdir: Path | str,
    jobs: int = 1,
    timeout: int = 300,
) -> dict:
    script = Path(__file__).with_name("smoke_vcs_verdi.py")
    root = Path(workdir)
    cases = []
    for manifest in manifests:
        manifest_path = Path(manifest)
        name = manifest_path.stem
        case_workdir = root / name
        cases.append(
            {
                "name": name,
                "manifest": str(manifest_path),
                "workdir": str(case_workdir),
                "cmd": [
                    sys.executable,
                    str(script),
                    "--manifest",
                    str(manifest_path),
                    "--workdir",
                    str(case_workdir),
                    "--execute",
                    "--clean",
                    "--json",
                ],
            }
        )
    return {"workdir": str(root), "jobs": jobs, "timeout": timeout, "cases": cases}


def _dry_run_cmd(cmd: list[str]) -> list[str]:
    dry = [part for part in cmd if part not in {"--execute", "--clean"}]
    if "--dry-run" not in dry:
        dry.insert(2, "--dry-run")
    return dry


def _json_from_stdout(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
    return {}


def summarize_case_result(result: dict) -> dict:
    parsed = _json_from_stdout(result.get("stdout", ""))
    diagnostics = parsed.get("diagnostics", {}) if isinstance(parsed, dict) else {}
    artifacts = diagnostics.get("artifacts", {}) or parsed.get("artifact_status", {}) if isinstance(parsed, dict) else {}
    missing_tools = parsed.get("missing_tools", []) if isinstance(parsed, dict) else []
    tool_confidence = "blocked" if missing_tools else "passed" if result.get("returncode") == 0 else "failed"
    return {
        "name": result.get("name", ""),
        "status": result.get("status", ""),
        "returncode": result.get("returncode"),
        "missing_tools": missing_tools,
        "artifacts": artifacts,
        "tool_confidence": tool_confidence,
        "inner_status": parsed.get("status", "") if isinstance(parsed, dict) else "",
    }


def run_case(case: dict, *, timeout: int, dry_run: bool = False) -> dict:
    cmd = _dry_run_cmd(case["cmd"]) if dry_run else case["cmd"]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        status = "passed" if completed.returncode == 0 else "failed"
        result = {
            **case,
            "cmd": cmd,
            "status": status,
            "returncode": completed.returncode,
            "elapsed_sec": round(time.monotonic() - started, 3),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        result["summary"] = summarize_case_result(result)
        return result
    except subprocess.TimeoutExpired as exc:
        result = {
            **case,
            "cmd": cmd,
            "status": "timeout",
            "returncode": None,
            "elapsed_sec": round(time.monotonic() - started, 3),
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or f"timeout after {timeout}s",
        }
        result["summary"] = summarize_case_result(result)
        return result


def junit_xml(results: list[dict]) -> str:
    failures = [result for result in results if result["status"] != "passed"]
    suite = ET.Element("testsuite", tests=str(len(results)), failures=str(len(failures)))
    for result in results:
        case = ET.SubElement(suite, "testcase", name=result["name"])
        if result["status"] != "passed":
            failure = ET.SubElement(case, "failure", message=result.get("reason") or result["status"])
            failure.text = result.get("stderr") or result.get("stdout") or result.get("reason") or result["status"]
    return ET.tostring(suite, encoding="unicode")


def run_regression(plan: dict, *, dry_run: bool = False) -> dict:
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(plan["jobs"]))) as executor:
        futures = [
            executor.submit(run_case, case, timeout=int(plan["timeout"]), dry_run=dry_run)
            for case in plan["cases"]
        ]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["name"])
    return {
        **plan,
        "results": results,
        "status": "passed" if all(item["status"] == "passed" for item in results) else "failed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a manifest-driven VCS/Verdi non-GUI regression.")
    parser.add_argument("manifests", nargs="+", type=Path)
    parser.add_argument("--workdir", type=Path, default=Path("build/vcs-verdi-regression"))
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--junit", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    plan = build_regression_plan(args.manifests, workdir=args.workdir, jobs=args.jobs, timeout=args.timeout)
    result = run_regression(plan, dry_run=args.dry_run)
    if args.junit:
        args.junit.parent.mkdir(parents=True, exist_ok=True)
        args.junit.write_text(junit_xml(result["results"]), encoding="utf-8")
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result["status"])
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
