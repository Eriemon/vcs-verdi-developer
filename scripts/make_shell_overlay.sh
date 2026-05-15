#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: make_shell_overlay.sh <source-tool-home> <overlay-tool-home>" >&2
  exit 2
fi

src="$1"
dst="$2"

if [ ! -d "$src/bin" ]; then
  echo "source tool home has no bin directory: $src" >&2
  exit 2
fi

rm -rf "$dst"
mkdir -p "$dst/bin"

for entry in "$src"/*; do
  base="$(basename "$entry")"
  if [ "$base" != "bin" ]; then
    ln -s "$entry" "$dst/$base"
  fi
done

for file in "$src"/bin/*; do
  base="$(basename "$file")"
  if [ -f "$file" ] && head -n 1 "$file" | grep -q '^#!/bin/sh'; then
    {
      printf '#!/usr/bin/env bash\n'
      if [ "$base" = "urg" ]; then
        cat <<'SNPS_URG_PATCH_LOADER'
if [ "${VCS_VERDI_ALLOW_UCAPI_PATCH:-0}" = "1" ] && [ -n "${VCS_HOME:-}" ] && [ -d "$VCS_HOME/ucapi_patch_lib" ]; then
  case ":${LD_LIBRARY_PATH:-}:" in
    *":$VCS_HOME/ucapi_patch_lib:"*) ;;
    *) export LD_LIBRARY_PATH="$VCS_HOME/ucapi_patch_lib:${LD_LIBRARY_PATH:-}" ;;
  esac
fi
SNPS_URG_PATCH_LOADER
      fi
      tail -n +2 "$file"
    } > "$dst/bin/$base"
    chmod +x "$dst/bin/$base"
  else
    ln -s "$file" "$dst/bin/$base"
  fi
done

echo "$dst"
