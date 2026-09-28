"""The shipped manifest: package/metadata.json is what Plasma reads.

Every field here is a promise the panel makes to the user, so a stale or
missing one ships a broken widget.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "package"

Manifest = dict[str, Any]


def metadata() -> Manifest:
    return cast(
        Manifest, json.loads((PKG / "metadata.json").read_text(encoding="utf-8"))
    )


def test_version_matches_pyproject() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata()["KPlugin"]["Version"] == pyproject["project"]["version"]


def test_structure_is_a_plasma_applet() -> None:
    assert metadata()["KPackageStructure"] == "Plasma/Applet"
    assert metadata()["X-Plasma-API-Minimum-Version"] == "6.0"


def test_id_names_the_install_directory_and_the_icon() -> None:
    plugin = metadata()["KPlugin"]
    assert plugin["Id"] == "com.maci.quota-widget"
    assert plugin["Icon"] == f"{plugin['Id']}.svg"
    assert (PKG / "contents" / "icons" / plugin["Icon"]).is_file()


def test_license_matches_the_license_file() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["project"]["license"]["text"]
    assert metadata()["KPlugin"]["License"] == declared
    assert license_text.startswith("MIT License")
    assert declared in license_text


def test_shipped_contents_are_the_applet_and_nothing_else() -> None:
    shipped = sorted(
        str(path.relative_to(PKG))
        for path in PKG.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    assert shipped == [
        "contents/code/fetch_quota.py",
        "contents/config/main.xml",
        "contents/icons/com.maci.quota-widget.svg",
        "contents/ui/main.qml",
        "metadata.json",
    ]
