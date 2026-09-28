#!/usr/bin/env python3
"""Print a one-line summary of a fetch_quota.py JSON dump."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# JSON dump from fetch_quota.py: provider payloads are unversioned dicts.
Payload = dict[str, Any]

USAGE_LINE = "usage: print_smoke.py [-h] [DUMP]"

HELP = f"""{USAGE_LINE}

Summarize a fetch_quota.py JSON dump: one line per provider on stdout, or
exit 1 when the dump carries a config error (detail on stderr).

positional arguments:
  DUMP   dump to read; defaults to .scratch/smoke.json under the project root

options:
  -h, --help  print this help and exit
"""


def project_root() -> Path:
    start = Path(__file__).resolve().parent
    for path in [start, *start.parents]:
        if (path / "package" / "metadata.json").is_file():
            return path
    raise SystemExit("print_smoke: package/metadata.json not found")


def _dict(value: object) -> Payload:
    return value if isinstance(value, dict) else {}


def _rows(value: object) -> list[Payload]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _join(*parts: str) -> str:
    return " ".join(part for part in parts if part)


def _period_bits(payload: Payload) -> str:
    rows = _rows(payload.get("periods"))
    return _join(*(f"{row.get('label', '')}={row.get('util')}%" for row in rows))


def _ok_line(name: str, payload: Payload, extra: str) -> None:
    if payload.get("ok"):
        print(f"  {name}: ok {extra}".rstrip())
    else:
        print(f"  {name}: {payload.get('error') or 'empty'}")


def _load(path: Path) -> Payload:
    if not path.is_file():
        raise SystemExit(
            f"print_smoke: no fetch_quota.py dump at {path}\n"
            "run: mkdir -p .scratch && "
            "python3 package/contents/code/fetch_quota.py > .scratch/smoke.json"
        )
    try:
        return _dict(json.loads(path.read_text(encoding="utf-8")))
    except OSError as exc:
        raise SystemExit(f"print_smoke: {path} could not be read: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise SystemExit(f"print_smoke: {path} is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"print_smoke: {path} is not valid JSON: {exc}") from exc


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args in (["-h"], ["--help"]):
        print(HELP, end="")
        raise SystemExit(0)
    if len(args) > 1:
        print(
            f"print_smoke: unexpected argument {args[1]!r}\n{USAGE_LINE}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    dump = Path(args[0]) if args else project_root() / ".scratch" / "smoke.json"
    data = _load(dump)
    config_error = data.get("config_error")
    if config_error:
        print(f"print_smoke: config error: {config_error}", file=sys.stderr)
        raise SystemExit(1)
    claude = _dict(data.get("claude"))
    cursor = _dict(data.get("cursor"))
    grok = _dict(data.get("grok"))
    codex = _dict(data.get("codex"))

    session = _dict(claude.get("session")).get("util")
    _ok_line(
        "claude",
        claude,
        _join(
            str(claude.get("plan") or ""),
            f"session={session}%" if claude.get("ok") and session is not None else "",
            "stale" if claude.get("stale") else "",
        ),
    )

    _ok_line(
        "cursor", cursor, _join(str(cursor.get("plan") or ""), _period_bits(cursor))
    )

    windows = len(_rows(codex.get("windows")))
    _ok_line(
        "codex",
        codex,
        _join(
            str(codex.get("plan") or ""),
            f"windows={windows}" if codex.get("ok") else "",
        ),
    )

    _ok_line("grok", grok, _period_bits(grok))


if __name__ == "__main__":
    main()
