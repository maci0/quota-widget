#!/usr/bin/env bash
# Install (or upgrade) the AI Quota plasmoid for the current user.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PKG_ID="com.maci.quota-widget"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/plasma/plasmoids/${PKG_ID}"

chmod +x "$ROOT/package/contents/code/fetch_quota.py"

# Smoke-test the data source before installing.
if ! python3 "$ROOT/package/contents/code/fetch_quota.py" >/tmp/quota-widget-smoke.json 2>/tmp/quota-widget-smoke.err; then
  echo "warning: fetch_quota.py exited non-zero (see /tmp/quota-widget-smoke.err)" >&2
else
  echo "data source ok:"
  python3 - <<'PY'
import json
p=json.load(open("/tmp/quota-widget-smoke.json"))
c=p.get("claude") or {}
g=p.get("grok") or {}
x=p.get("codex") or {}
print("  claude:", "ok" if c.get("ok") else c.get("error"),
      (c.get("plan") or ""),
      ("session="+str((c.get("session") or {}).get("util"))+"%") if c.get("ok") else "")
print("  codex: ", "ok" if x.get("ok") else x.get("error"),
      (x.get("plan") or ""),
      ("windows="+str(len(x.get("windows") or []))) if x.get("ok") else "")
print("  grok:  ", "ok" if g.get("ok") else g.get("error"),
      (" ".join(p.get("label","")+"="+str(p.get("util"))+"%"
                for p in (g.get("periods") or [])) if g.get("ok") else ""))
PY
fi

mkdir -p "$(dirname "$DEST")"
rm -rf "$DEST"
# Symlink so edits in this repo show up live (after qml cache clear).
ln -sfn "$ROOT/package" "$DEST"
echo "installed → $DEST"

# Drop compiled QML so Plasma picks up source changes.
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/plasmashell/qmlcache" 2>/dev/null || true

echo
echo "Add the widget: right-click desktop or panel → Add Widgets → search \"AI Quota\""
echo "If it does not appear, restart plasmashell:"
echo "  systemctl --user restart plasma-plasmashell.service"
echo "  # or:  kquitapp6 plasmashell; plasmashell --replace &"
