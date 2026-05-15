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
      tail -n +2 "$file"
    } > "$dst/bin/$base"
    chmod +x "$dst/bin/$base"
  else
    ln -s "$file" "$dst/bin/$base"
  fi
done

echo "$dst"
