#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


SEARCH_PATTERN = bytes.fromhex("e8 f3 fd ff ff ff c8")
PATCH_BYTES = bytes.fromhex("90 90 90 90 90")
MANIFEST_NAME = "ucapi_patch_manifest.json"

CANDIDATE_RELS = (
    "linux64/lib/libucapi.so",
    "linux64/bin/libucapi.so",
    "platform/linux64/bin/libucapi.so",
    "platform/LINUXAMD64/bin/libucapi.so",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_overlay_home(path: Path) -> bool:
    parts = {part.lower() for part in path.resolve(strict=False).parts}
    if "synopsys" in parts and "tools" in parts:
        return False
    return path.name in {"vcs_overlay", "verdi_overlay"} or path.name.endswith("_overlay")


def _reject(path: Path) -> dict:
    return {
        "status": "rejected",
        "overlay_home": str(path),
        "reason": "refusing to patch outside a validation overlay home",
    }


def find_offsets(data: bytes, pattern: bytes = SEARCH_PATTERN) -> list[int]:
    offsets: list[int] = []
    start = 0
    while True:
        offset = data.find(pattern, start)
        if offset < 0:
            return offsets
        offsets.append(offset)
        start = offset + 1


def candidate_info(overlay_home: Path) -> list[dict]:
    infos = []
    for rel in CANDIDATE_RELS:
        path = overlay_home / rel
        info = {
            "rel": rel,
            "path": str(path),
            "exists": path.exists(),
            "is_symlink": path.is_symlink(),
            "is_elf": False,
            "sha256": "",
            "offsets": [],
        }
        if path.exists():
            data = path.read_bytes()
            info.update(
                {
                    "is_elf": data.startswith(b"\x7fELF"),
                    "sha256": sha256_bytes(data),
                    "offsets": find_offsets(data),
                }
            )
        infos.append(info)
    return infos


def scan_overlay(overlay_home: Path | str) -> dict:
    overlay = Path(overlay_home)
    if not is_overlay_home(overlay):
        return _reject(overlay)
    infos = candidate_info(overlay)
    existing = [info for info in infos if info["exists"]]
    matches = [info for info in existing if info["offsets"]]
    if matches:
        status = "match"
    elif existing:
        status = "no_match"
    else:
        status = "no_candidate"
    return {
        "status": status,
        "overlay_home": str(overlay),
        "search_pattern": SEARCH_PATTERN.hex(),
        "patch_bytes": PATCH_BYTES.hex(),
        "candidates": infos,
        "matches": matches,
    }


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _materialized_patch_path(path: Path, overlay: Path) -> Path:
    target = path.resolve(strict=True)
    if path.is_symlink():
        return path
    if _is_inside(target, overlay):
        return path
    patch_dir = overlay / "ucapi_patch_lib"
    patch_dir.mkdir(parents=True, exist_ok=True)
    return patch_dir / "libucapi.so"


def _materialize_overlay_file(path: Path, overlay: Path) -> Path:
    target = path.resolve(strict=True)
    materialized = _materialized_patch_path(path, overlay)
    data = target.read_bytes()
    mode = target.stat().st_mode
    if materialized == path and path.is_symlink():
        path.unlink()
    materialized.write_bytes(data)
    materialized.chmod(mode)
    return materialized


def _patch_file(path: Path, overlay: Path, offsets: list[int]) -> dict:
    patch_path = _materialize_overlay_file(path, overlay)
    original = patch_path.read_bytes()
    patched = bytearray(original)
    for offset in offsets:
        patched[offset : offset + len(PATCH_BYTES)] = PATCH_BYTES
    patch_path.write_bytes(bytes(patched))
    return {
        "source_path": str(path),
        "path": str(patch_path),
        "activation_path": str(patch_path.parent),
        "offsets": offsets,
        "original_sha256": sha256_bytes(original),
        "patched_sha256": sha256_bytes(bytes(patched)),
        "patch_bytes": PATCH_BYTES.hex(),
    }


def apply_overlay_patch(overlay_home: Path | str) -> dict:
    overlay = Path(overlay_home)
    scan = scan_overlay(overlay)
    if scan["status"] != "match":
        return scan
    patched = []
    for match in scan["matches"]:
        patched.append(_patch_file(Path(match["path"]), overlay, match["offsets"]))
    activation_paths = sorted({str(Path(item["path"]).parent) for item in patched})
    uses_patch_lib = any(Path(item["path"]).parent.name == "ucapi_patch_lib" for item in patched)
    manifest = {
        "status": "patched",
        "overlay_home": str(overlay),
        "search_pattern": SEARCH_PATTERN.hex(),
        "patched": patched,
        "activation_paths": activation_paths,
        "ld_library_paths": activation_paths,
        "effective_loader_warning": (
            "LD_LIBRARY_PATH must place ucapi_patch_lib before the VCS wrapper BASE_STRING/lib"
            if uses_patch_lib
            else ""
        ),
    }
    (overlay / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan or patch libucapi.so inside a VCS/Verdi validation overlay.")
    parser.add_argument("--overlay-home", type=Path, required=True)
    parser.add_argument("--mode", choices=("scan", "apply"), default="scan")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = scan_overlay(args.overlay_home) if args.mode == "scan" else apply_overlay_patch(args.overlay_home)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result["status"])
    return 0 if result["status"] in {"match", "no_match", "no_candidate", "patched"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
