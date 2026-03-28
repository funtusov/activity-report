#!/usr/bin/env sh
set -eu

SCRIPT_PATH="$0"
while [ -L "$SCRIPT_PATH" ]; do
  LINK_DIR=$(cd -P -- "$(dirname -- "$SCRIPT_PATH")" && pwd)
  SCRIPT_PATH=$(readlink "$SCRIPT_PATH")
  case "$SCRIPT_PATH" in
    /*) ;;
    *) SCRIPT_PATH="$LINK_DIR/$SCRIPT_PATH" ;;
  esac
done

SCRIPT_DIR=$(cd -P -- "$(dirname -- "$SCRIPT_PATH")" && pwd)
TARGET_DIR="${1:-$HOME/.local/bin}"
TARGET_PATH="$TARGET_DIR/activity-report"

mkdir -p "$TARGET_DIR"
chmod +x "$SCRIPT_DIR/bin/activity-report"
ln -sf "$SCRIPT_DIR/bin/activity-report" "$TARGET_PATH"

echo "Installed: $TARGET_PATH -> $SCRIPT_DIR/bin/activity-report"
