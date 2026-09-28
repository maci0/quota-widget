#!/usr/bin/env bash
# Install (or upgrade) the AI Quota plasmoid for the current user, or remove
# it again with --uninstall.
set -euo pipefail

# Everything this script creates belongs to one user and carries poll output,
# stderr, or the widget itself, so nothing it makes is group- or
# world-readable. A dir that already exists keeps the mode it had.
umask 077

# $0 is the path the caller typed, and a symlink is what a distro package or a
# convenience link in ~/bin hands this script. Walking up from the link's own
# directory then never reaches the project marker. readlink is spelled the same
# everywhere but has no -f, so the chain is followed by hand and bounded by the
# kernel's own ELOOP limit.
MAX_SYMLINK_HOPS=40

script_path() {
  local src="$1" dir hops=0
  while [[ -L "$src" ]]; do
    if ((hops++ >= MAX_SYMLINK_HOPS)); then
      echo "error: $src is a symlink loop" >&2
      return 1
    fi
    dir="$(cd -P "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    [[ "$src" == /* ]] || src="$dir/$src"
  done
  printf '%s\n' "$src"
}

find_root() {
  local dir
  dir="$(dirname "$(script_path "$0")")"
  dir="$(cd "$dir" && pwd)"
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

# The install directory is the manifest's KPlugin Id, so the two cannot drift:
# a name hardcoded here beside a different Id in package/metadata.json links
# the payload under a directory Plasma never reads, and the guard below then
# refuses to remove it again. The Id also lands in a path, so it has to be a
# plain reverse-DNS name.
PKG_ID="$(sed -n 's/.*"Id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
  "$ROOT/package/metadata.json" | head -n 1)"
if [[ ! "$PKG_ID" =~ ^[A-Za-z0-9]+([.-][A-Za-z0-9]+)*$ ]]; then
  echo "error: package/metadata.json has no usable KPlugin Id: '${PKG_ID}'" >&2
  exit 1
fi
# A literal Id in dest_is_ours is a regex, so the dots need escaping.
PKG_ID_RE="${PKG_ID//./\\.}"

# The floor for the runtime `python3` is `requires-python` in pyproject.toml,
# not a second literal here: a copy is a number nobody bumps.

# The XDG defaults below need $HOME, and a run without one (a service unit,
# `env -i`, a cron entry) would otherwise stop on the first expansion under
# `set -u` with the shell's own wording. `--help` reaches the usage text.
if [[ -z "${HOME:-}" ]]; then
  echo "error: HOME is not set; the widget installs under the user's data dir" >&2
  exit 1
fi

# XDG base dirs: a relative value is invalid, so the spec default stands.
# https://specifications.freedesktop.org/basedir-spec/latest/
xdg_data="$HOME/.local/share"
[[ "${XDG_DATA_HOME:-}" = /* ]] && xdg_data="$XDG_DATA_HOME"
xdg_config="$HOME/.config"
[[ "${XDG_CONFIG_HOME:-}" = /* ]] && xdg_config="$XDG_CONFIG_HOME"
xdg_cache="$HOME/.cache"
[[ "${XDG_CACHE_HOME:-}" = /* ]] && xdg_cache="$XDG_CACHE_HOME"

DEST="$xdg_data/plasma/plasmoids/${PKG_ID}"
PLASMOID_CONFIG="$xdg_config/plasmoids/org.kde.plasma.plasmoid/${PKG_ID}.json"
SCRATCH="$ROOT/.scratch"

# DEST may hold a copy put there by Plasma Discover, by a distro package, or
# unpacked by hand. Removing a directory this script did not create loses
# whatever the user keeps in it, so both install and uninstall stop instead.
dest_is_ours() {
  [[ -L "$DEST" ]] && return 0
  [[ -f "$DEST/metadata.json" ]] || return 1
  grep -Eq "\"Id\"[[:space:]]*:[[:space:]]*\"$PKG_ID_RE\"" "$DEST/metadata.json"
}

min_python_floor() {
  # The `>=` side of the `requires-python = ">=3.11"` declaration. A
  # specifier this shape cannot express (an upper bound, `!=`, a bare
  # version) matches nothing, and the empty result is refused below rather
  # than read as floor zero.
  sed -n 's/^requires-python = ">= *\([0-9][0-9.]*\)"\(.*\)$/\1/p' \
    "$ROOT/pyproject.toml"
}

check_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found on PATH; plasmashell runs the fetcher with it" >&2
    return 1
  fi
  # Refuse before linking rather than after a plasmashell restart: below the
  # floor the fetcher raises, and the panel shows a broken data source with no
  # explanation of why.
  local floor major minor ver have_major have_minor
  floor="$(min_python_floor)"
  if [[ -z "$floor" ]]; then
    echo "error: no >= floor in requires-python of $ROOT/pyproject.toml" >&2
    return 1
  fi
  major="${floor%%.*}"
  minor="${floor#*.}"
  minor="${minor%%.*}"
  ver="$(python3 -V 2>&1 | sed 's/^Python //')"
  have_major="${ver%%.*}"
  have_minor="${ver#*.}"
  have_minor="${have_minor%%.*}"
  case "$have_major$have_minor" in
    '' | *[!0-9]*) # unparsable banner: show it rather than guess
      echo "error: cannot read the python3 version from: $ver" >&2
      return 1
      ;;
  esac
  if ((have_major < major)) ||
    { ((have_major == major)) && ((have_minor < minor)); }; then
    echo "error: python3 $ver is below $major.$minor, which the fetcher needs" >&2
    return 1
  fi
}

usage() {
  # Same shape as the fetcher's help: a one-line summary, a usage line, then
  # the flags. Progress and results go to stdout, errors to stderr.
  cat <<'EOF'
usage: install.sh [-u | --uninstall] [-h | --help] [-V | --version]

Link this checkout's package/ into the user's Plasma plasmoid directory, or
remove it again. The link is followed by a smoke poll of the four providers,
whose summary is printed here and kept in .scratch/smoke.json.

options:
  -u, --uninstall  remove the installed widget, keep the cache and settings
  -h, --help       print this help and exit
  -V, --version    print the plasmoid version from package/metainfo.xml and exit
EOF
}

version() {
  # The released version lives in metainfo.xml, the same file Discover and
  # KNewStuff read, so the answer is a run-time read and not a second literal
  # that a release bumps without. The first <release> is the newest one.
  local release
  release="$(sed -n 's/.*<release[[:space:]][^>]*version="\([^"]*\)".*/\1/p' \
    "$ROOT/package/metainfo.xml" | head -n 1)"
  if [[ -z "$release" ]]; then
    echo "error: no <release version=...> in $ROOT/package/metainfo.xml" >&2
    return 1
  fi
  echo "$PKG_ID $release"
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
  echo "      erase them with: python3 '$ROOT/package/contents/code/fetch_quota.py' --clear-cache"
  echo "      $PLASMOID_CONFIG"
}

# One argument is a mode, and a second one is always a mistake. `--uninstall
# --help` is the case that matters: the mode is the destructive one, so a
# mistyped or misplaced flag on that line must not remove the widget while the
# word "uninstall" goes unread. Refusing is what the fetcher does too, and the
# two then answer the same line the same way.
if (( $# > 1 )); then
  echo "error: unexpected argument ${2@Q}" >&2
  usage >&2
  echo "try 'install.sh --help' for more information." >&2
  exit 2
fi

case "${1:-}" in
  --uninstall | -u) uninstall; exit $? ;;
  -h | --help)
    usage
    exit 0
    ;;
  -V | --version)
    version
    exit $?
    ;;
  "") ;;
  *)
    echo "error: unknown argument ${1@Q}" >&2
    usage >&2
    echo "try 'install.sh --help' for more information." >&2
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
