#!/usr/bin/env python3
"""Print a one-line summary of a fetch_quota.py JSON dump."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# JSON dump from fetch_quota.py: provider payloads are unversioned dicts.
Payload = dict[str, Any]


def project_root() -> Path:
    start = Path(__file__).resolve().parent
    for path in [start, *start.parents]:
        if (path / "package" / "metadata.json").is_file():
            return path
    raise SystemExit("package/metadata.json not found")


def _dict(value: object) -> Payload:
    return value if isinstance(value, dict) else {}


def _rows(value: object) -> list[Payload]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _ok_line(name: str, payload: Payload, extra: str) -> None:
    if payload.get("ok"):
        print(f"  {name}: ok {extra}".rstrip())
    else:
        print(f"  {name}: {payload.get('error') or 'empty'}")


def _load(path: Path) -> Payload:
    if not path.is_file():
        raise SystemExit(
            f"no fetch_quota.py dump at {path}\n"
            "run: mkdir -p .scratch && "
            "python3 package/contents/code/fetch_quota.py > .scratch/smoke.json"
        )
    try:
        return _dict(json.loads(path.read_text()))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}") from exc


def main() -> None:
    dump = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else project_root() / ".scratch" / "smoke.json"
    )
    data = _load(dump)
    claude = _dict(data.get("claude"))
    cursor = _dict(data.get("cursor"))
    grok = _dict(data.get("grok"))
    codex = _dict(data.get("codex"))

    session = _dict(claude.get("session")).get("util")
    claude_extra = " ".join(
        part
        for part in (
            str(claude.get("plan") or ""),
            f"session={session}%" if claude.get("ok") and session is not None else "",
            "stale" if claude.get("stale") else "",
        )
        if part
    )
    _ok_line("claude", claude, claude_extra)

    cursor_bits = [
        str(cursor.get("plan") or ""),
        *(
            f"{row.get('label', '')}={row.get('util')}%"
            for row in _rows(cursor.get("periods"))
        ),
    ]
    _ok_line("cursor", cursor, " ".join(bit for bit in cursor_bits if bit))

    windows = len(_rows(codex.get("windows")))
    _ok_line(
        "codex",
        codex,
        " ".join(
            part
            for part in (
                str(codex.get("plan") or ""),
                f"windows={windows}" if codex.get("ok") else "",
            )
            if part
        ),
    )

    grok_bits = [
        f"{row.get('label', '')}={row.get('util')}%"
        for row in _rows(grok.get("periods"))
    ]
    _ok_line("grok", grok, " ".join(grok_bits))


if __name__ == "__main__":
    main()
