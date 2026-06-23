#!/usr/bin/env bash
set -euo pipefail

WPS_DIR="${1:-WPS}"

if [ ! -d "$WPS_DIR" ]; then
  echo "Cannot find WPS directory: $WPS_DIR"
  exit 1
fi

find "$WPS_DIR" -type f \( -name '*.F' -o -name '*.c' \) -size -128c | while read -r file; do
  target="$(tr -d '\r\n' < "$file")"
  case "$target" in
    ../*|./*)
      if [ -e "$(dirname "$file")/$target" ]; then
        rm -f "$file"
        ln -s "$target" "$file"
        echo "linked $file -> $target"
      fi
      ;;
  esac
done
