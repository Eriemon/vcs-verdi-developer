#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Iterable


RULES = (
    ("license", "error", re.compile(r"license|checkout failed|snpslmd|lm_license", re.IGNORECASE)),
    ("platform", "warning", re.compile(r"unsupported linux|unsupported .*kernel|LNX_OS_VERUN|LINX_KRNL", re.IGNORECASE)),
    ("pli", "warning", re.compile(r"novas\.tab|pli\.a|pli.*missing|no such file or directory.*pli", re.IGNORECASE)),
    ("fsdb", "error", re.compile(r"fsdb.*(missing|zero|failed|error)|failed to open FSDB", re.IGNORECASE)),
    ("compile_error", "error", re.compile(r"(^|\s)(Error-\[|\*E,|error:|syntax error)", re.IGNORECASE)),
    ("warning", "warning", re.compile(r"(^|\s)(Warning-\[|\*W,|warning:)", re.IGNORECASE)),
)


def _line_issue(line: str, *, line_no: int, source: str) -> dict | None:
    for category, severity, pattern in RULES:
        if pattern.search(line):
            return {
                "source": source,
                "line": line_no,
                "category": category,
                "severity": severity,
                "text": line.strip(),
            }
    return None


def summarize(issues: Iterable[dict], *, files: int = 1) -> dict:
    issue_list = list(issues)
    errors = sum(1 for issue in issue_list if issue["severity"] == "error")
    warnings = sum(1 for issue in issue_list if issue["severity"] == "warning")
    categories = sorted({issue["category"] for issue in issue_list})
    return {
        "files": files,
        "issues": len(issue_list),
        "errors": errors,
        "warnings": warnings,
        "categories": categories,
    }


def analyze_text(text: str, *, source: str = "<text>") -> dict:
    issues = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        issue = _line_issue(line, line_no=line_no, source=source)
        if issue is not None:
            issues.append(issue)
    summary = summarize(issues)
    return {
        "status": "failed" if summary["errors"] else "passed",
        "summary": summary,
        "issues": issues,
    }


def analyze_paths(paths: Iterable[Path | str]) -> dict:
    all_issues: list[dict] = []
    path_list = [Path(path) for path in paths]
    for path in path_list:
        text = path.read_text(encoding="utf-8", errors="replace")
        result = analyze_text(text, source=str(path))
        all_issues.extend(result["issues"])
    summary = summarize(all_issues, files=len(path_list))
    return {
        "status": "failed" if summary["errors"] else "passed",
        "summary": summary,
        "issues": all_issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze VCS/Verdi logs for common non-GUI failures.")
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = analyze_paths(args.logs)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result["status"])
        for issue in result["issues"]:
            print(f"{issue['source']}:{issue['line']}: {issue['category']}: {issue['text']}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
