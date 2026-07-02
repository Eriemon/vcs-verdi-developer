from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
PACKAGE_NAME = "erie-vcs-verdi-developer"
RELEASE_DIR = DIST_DIR / f"{PACKAGE_NAME}-{VERSION}"
ZIP_PATH = DIST_DIR / f"{PACKAGE_NAME}-{VERSION}.zip"
MANIFEST_PATH = DIST_DIR / "manifest.json"

ALLOWED_TOP_LEVEL_FILES = {
    ".gitignore",
    "CITATION.cff",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "README-CN.md",
    "RELEASE_RECEIPT.json",
    "SECURITY.md",
    "SKILL.md",
    "VERSION",
    "build_release.py",
    "pyproject.toml",
}
ALLOWED_TOP_LEVEL_DIRS = {
    "agents",
    "assets",
    "docs",
    "evals",
    "references",
    "scripts",
}
EXCLUDED_PARTS = {
    ".git",
    ".settings",
    "__pycache__",
    "_smoke_runs",
    "dist",
    "downloads",
    "logs",
    "reports",
    "requests",
    "test",
    "tests",
    "tmp",
}
EXCLUDED_SUFFIXES = {".bak", ".log", ".pyc", ".pyo"}


def run_git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def is_excluded(path: Path) -> bool:
    rel_parts = set(path.relative_to(ROOT).parts)
    if rel_parts & EXCLUDED_PARTS:
        return True
    if path.name.endswith(".local.json"):
        return True
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    return False


def verify_tracked_content_policy() -> None:
    tracked_output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    violations: list[str] = []
    for raw_entry in tracked_output.split(b"\x00"):
        if not raw_entry:
            continue
        rel_path = raw_entry.decode("utf-8").replace("\\", "/")
        parts = rel_path.split("/")
        top_level = parts[0]
        if top_level in EXCLUDED_PARTS:
            violations.append(rel_path)
            continue
        if rel_path.endswith(".local.json"):
            violations.append(rel_path)
            continue
        if Path(rel_path).suffix.lower() in EXCLUDED_SUFFIXES:
            violations.append(rel_path)
            continue
        if top_level not in ALLOWED_TOP_LEVEL_FILES and top_level not in ALLOWED_TOP_LEVEL_DIRS:
            violations.append(rel_path)
    if violations:
        joined = "\n".join(f"- {item}" for item in sorted(violations))
        raise SystemExit("> ERR: [Python] public release policy violations:\n" + joined)


def copy_public_tree() -> None:
    if RELEASE_DIR.exists():
        shutil.rmtree(RELEASE_DIR)
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)

    for name in sorted(ALLOWED_TOP_LEVEL_FILES):
        path_source = ROOT / name
        if path_source.is_file():
            shutil.copy2(path_source, RELEASE_DIR / name)

    for name in sorted(ALLOWED_TOP_LEVEL_DIRS):
        path_source = ROOT / name
        path_target = RELEASE_DIR / name
        if path_source.is_dir():
            shutil.copytree(
                path_source,
                path_target,
                ignore=shutil.ignore_patterns(
                    "__pycache__",
                    "*.pyc",
                    "*.pyo",
                    "*.bak",
                    "*.log",
                    "*.local.json",
                ),
            )


def create_zip() -> None:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with ZipFile(ZIP_PATH, "w", compression=ZIP_DEFLATED) as zip_file:
        for path_file in sorted(RELEASE_DIR.rglob("*")):
            if path_file.is_file():
                arcname = f"{RELEASE_DIR.name}/{path_file.relative_to(RELEASE_DIR).as_posix()}"
                zip_file.write(path_file, arcname)


def sha256_file(path_file: Path) -> str:
    digest = hashlib.sha256()
    with path_file.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def count_release_files() -> int:
    return sum(1 for path_file in RELEASE_DIR.rglob("*") if path_file.is_file())


def write_manifest() -> None:
    manifest = {
        "name": PACKAGE_NAME,
        "version": VERSION,
        "source_branch": run_git("rev-parse", "--abbrev-ref", "HEAD"),
        "source_commit": run_git("rev-parse", "HEAD"),
        "source_dirty": bool(run_git("status", "--short")),
        "directory_artifact": RELEASE_DIR.name,
        "zip_artifact": ZIP_PATH.name,
        "zip_sha256": sha256_file(ZIP_PATH),
        "file_count": count_release_files(),
        "release_created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "validation_commands": [
            "python .\\scripts\\python\\quality\\run_quality_gate.py --json",
            "python .\\scripts\\python\\validation\\vcs_verdi_check.py --dry-run --json --source .\\assets\\minimal_vcs\\top.sv --top top",
            "python .\\build_release.py",
        ],
        "excludes": [
            "dist/",
            ".settings/",
            "reports/",
            "requests/",
            "downloads/",
            "logs/",
            "tmp/",
            "test/",
            "tests/",
            "__pycache__/",
            "_smoke_runs/",
            "*.local.json",
            "*.bak",
            "*.log",
            "*.pyc",
            "*.pyo",
        ],
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    verify_tracked_content_policy()
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    copy_public_tree()
    create_zip()
    write_manifest()
    print(f"> INFO: [Python] release directory: {RELEASE_DIR}")
    print(f"> INFO: [Python] release zip: {ZIP_PATH}")
    print(f"> INFO: [Python] manifest: {MANIFEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
