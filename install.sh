#!/usr/bin/env bash
# Install (or upgrade) the AI Quota plasmoid for the current user.
set -euo pipefail

find_root() {
  local dir
  dir="$(cd "$(dirname "$0")" && pwd)"
  while [[ "$dir" != "/" ]]; do
    if [[ -f "$dir/package/metadata.json" ]]; then
      printf '%s\n' "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  echo "error: package/metadata.json not found above $0" >&2
  return 1
}

ROOT="$(find_root)"
PKG_ID="com.maci.quota-widget"

# XDG base dirs: a relative value is invalid, so the spec default stands.
# https://specifications.freedesktop.org/basedir-spec/latest/
xdg_data="$HOME/.local/share"
[[ "${XDG_DATA_HOME:-}" = /* ]] && xdg_data="$XDG_DATA_HOME"
xdg_cache="$HOME/.cache"
[[ "${XDG_CACHE_HOME:-}" = /* ]] && xdg_cache="$XDG_CACHE_HOME"

DEST="$xdg_data/plasma/plasmoids/${PKG_ID}"
SCRATCH="$ROOT/.scratch"

chmod +x "$ROOT/package/contents/code/fetch_quota.py"
mkdir -p "$SCRATCH"

if ! python3 "$ROOT/package/contents/code/fetch_quota.py" \
    >"$SCRATCH/smoke.json" 2>"$SCRATCH/smoke.err"; then
  echo "warning: fetch_quota.py exited non-zero (see $SCRATCH/smoke.err)" >&2
else
  echo "data source ok:"
  python3 "$ROOT/scripts/print_smoke.py" "$SCRATCH/smoke.json" \
    || echo "warning: invalid configuration (detail in $SCRATCH/smoke.err)" >&2
fi

# Bytecode caches are build residue, not content: the whole package/ tree is
# what gets linked into the plasmoid dir.
find "$ROOT/package" -type d -name __pycache__ -prune -exec rm -rf {} +

mkdir -p "$(dirname "$DEST")"
rm -rf "$DEST"
ln -sfn "$ROOT/package" "$DEST"
echo "installed -> $DEST"

rm -rf "$xdg_cache/plasmashell/qmlcache" 2>/dev/null || true

echo
echo "Add the widget: right-click desktop or panel -> Add Widgets -> search \"AI Quota\""
echo "If it does not appear, restart plasmashell:"
echo "  systemctl --user restart plasma-plasmashell.service"
echo "  # or:  kquitapp6 plasmashell; plasmashell --replace &"
