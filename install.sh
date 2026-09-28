#!/usr/bin/env bash
# Install (or upgrade) the AI Quota plasmoid for the current user, or remove
# it again with --uninstall.
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

# The floor in pyproject.toml (`requires-python`): the fetcher uses dt.UTC, so
# a 3.10 `python3` on PATH means plasmashell shows a broken data source with no
# explanation of why. Refuse before linking rather than after a restart.
MIN_PY_MAJOR=3
MIN_PY_MINOR=11

# XDG base dirs: a relative value is invalid, so the spec default stands.
# https://specifications.freedesktop.org/basedir-spec/latest/
xdg_data="$HOME/.local/share"
[[ "${XDG_DATA_HOME:-}" = /* ]] && xdg_data="$XDG_DATA_HOME"
xdg_cache="$HOME/.cache"
[[ "${XDG_CACHE_HOME:-}" = /* ]] && xdg_cache="$XDG_CACHE_HOME"

DEST="$xdg_data/plasma/plasmoids/${PKG_ID}"
SCRATCH="$ROOT/.scratch"

# DEST may hold a copy put there by Plasma Discover, by a distro package, or
# unpacked by hand. Removing a directory this script did not create loses
# whatever the user keeps in it, so both install and uninstall ask first.
dest_is_ours() {
  [[ -L "$DEST" ]] && return 0
  [[ -f "$DEST/metadata.json" ]] || return 1
  grep -q "$PKG_ID" "$DEST/metadata.json"
}

check_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found on PATH; plasmashell runs the fetcher with it" >&2
    return 1
  fi
  local ver major minor
  ver="$(python3 -V 2>&1 | sed 's/^Python //')"
  major="${ver%%.*}"
  minor="${ver#*.}"
  minor="${minor%%.*}"
  case "$major$minor" in
    '' | *[!0-9]*) # unparsable banner: show it rather than guess
      echo "error: cannot read the python3 version from: $ver" >&2
      return 1
      ;;
  esac
  if ((major < MIN_PY_MAJOR)) ||
    { ((major == MIN_PY_MAJOR)) && ((minor < MIN_PY_MINOR)); }; then
    echo "error: python3 $ver is below $MIN_PY_MAJOR.$MIN_PY_MINOR, which the" \
      "fetcher needs" >&2
    return 1
  fi
}

usage() {
  cat <<'EOF'
usage: install.sh [--uninstall]

  (no argument)  link package/ into the user's Plasma plasmoid directory
  --uninstall    remove the installed widget, keep the cache and settings
EOF
}

uninstall() {
  if [[ -e "$DEST" || -L "$DEST" ]]; then
    if ! dest_is_ours; then
      echo "error: $DEST is not this widget, left in place" >&2
      return 1
    fi
    rm -rf "$DEST"
    echo "removed -> $DEST"
  else
    echo "not installed -> $DEST"
  fi
  rm -rf "$xdg_cache/plasmashell/qmlcache" 2>/dev/null || true
  echo "kept: $xdg_cache/quota-widget (last good readings)"
  echo "      ~/.config/plasmoids/org.kde.plasma.plasmoid/com.maci.quota-widget.json"
}

case "${1:-}" in
  --uninstall | -u) uninstall; exit $? ;;
  -h | --help)
    usage
    exit 0
    ;;
  "") ;;
  *)
    echo "error: unknown argument ${1@Q}" >&2
    usage >&2
    exit 2
    ;;
esac

# Both checks run before anything is fetched or written: a refused install
# must leave the checkout and the plasmoid dir exactly as it found them.
if [[ -e "$DEST" || -L "$DEST" ]] && ! dest_is_ours; then
  echo "error: $DEST exists and is not this widget; remove it by hand" >&2
  exit 1
fi

check_python || exit 1

chmod +x "$ROOT/package/contents/code/fetch_quota.py"
mkdir -p "$SCRATCH"

if ! python3 "$ROOT/package/contents/code/fetch_quota.py" \
    >"$SCRATCH/smoke.json" 2>"$SCRATCH/smoke.err"; then
  echo "warning: fetch_quota.py exited non-zero (see $SCRATCH/smoke.err)" >&2
else
  echo "data source ok:"
  python3 "$ROOT/scripts/print_smoke.py" "$SCRATCH/smoke.json" \
    || echo "warning: smoke check failed (detail above)" >&2
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
