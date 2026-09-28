"""The shipped manifests: package/metadata.json is what Plasma reads, and
package/metainfo.xml is what Discover and KNewStuff read.

Every field here is a promise the panel makes to the user, so a stale or
missing one ships a broken widget, and the two files must name the same
version or an update is hidden or advertised wrongly.
"""

from __future__ import annotations

import json
import re
import tomllib
import xml.etree.ElementTree as ET
from typing import Any, cast

from project_paths import project_root

ROOT = project_root()
PKG = ROOT / "package"

Manifest = dict[str, Any]


def text(root: ET.Element, tag: str, **attrib: str) -> str:
    """The text of `root/tag` selected by attributes, empty when absent."""
    predicate = "".join(f"[@{k}={v!r}]" for k, v in attrib.items())
    found = root.find(f"{tag}{predicate}")
    return (found.text or "").strip() if found is not None else ""


def metadata() -> Manifest:
    return cast(
        "Manifest", json.loads((PKG / "metadata.json").read_text(encoding="utf-8"))
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
        "metainfo.xml",
    ]


def metainfo() -> ET.Element:
    """The AppStream component Plasma Discover and KNewStuff read to list the
    widget. KPackage looks for `metainfo.xml` at the package root. It is parsed
    with the stdlib, not defusedxml: the file is this repository's own, never
    a download, and a test must run under the declared dev extra alone."""
    root = ET.parse(PKG / "metainfo.xml").getroot()  # noqa: S314  # own file
    assert root is not None, "metainfo.xml is empty"
    return root


def test_metainfo_describes_the_manifest() -> None:
    plugin = metadata()["KPlugin"]
    root = metainfo()
    assert root.tag == "component"
    assert root.get("type") == "addon"
    assert text(root, "id") == plugin["Id"]
    assert text(root, "name") == plugin["Name"]
    assert text(root, "project_license") == plugin["License"]
    assert text(root, "url", type="homepage") == plugin["Website"]
    assert text(root, "summary"), "Discover lists the widget by its summary"


def test_metainfo_release_is_the_shipped_version() -> None:
    # A metainfo release is how Discover decides there is an update; one that
    # names a version the package does not ship leaves users on a version they
    # cannot get, and one that lags it hides an update that exists.
    shipped = metadata()["KPlugin"]["Version"]
    releases = metainfo().findall("./releases/release")
    assert [release.get("version") for release in releases] == [shipped]
    # An empty or malformed date is dropped by the catalog parser, so the
    # release is listed with no date at all.
    assert releases[0].get("date") is not None
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", releases[0].get("date") or "")


def test_metainfo_declares_its_own_metadata_license() -> None:
    # The file is metadata about the package, not part of it: CC0-1.0 is what
    # AppStream requires, and MIT here would claim the MIT terms over the
    # descriptive text.
    assert text(metainfo(), "metadata_license") == "CC0-1.0"


def kcfg_entries() -> dict[str, dict[str, str]]:
    """Each <entry> in main.xml by name, with its type and default/min/max."""
    text = (PKG / "contents" / "config" / "main.xml").read_text(encoding="utf-8")
    entries: dict[str, dict[str, str]] = {}
    for block in re.findall(r"<entry\b.*?</entry>", text, re.DOTALL):
        name = re.search(r'name="([^"]+)"', block)
        if name is None:
            continue
        bounds = dict(re.findall(r"<(default|min|max)>([^<]+)</\1>", block))
        kind = re.search(r'type="([^"]+)"', block)
        entries[name.group(1)] = {"type": kind.group(1) if kind else "", **bounds}
    return entries


def test_qml_fallbacks_match_the_shipped_kcfg_defaults() -> None:
    # intSetting() falls back to a literal when the config file has no value
    # for a key. A literal that drifts from main.xml is a widget that reads one
    # number on a fresh install and a different one everywhere else.
    qml = (PKG / "contents" / "ui" / "main.qml").read_text(encoding="utf-8")
    mismatches = []
    for name, values in kcfg_entries().items():
        if values["type"] != "Int":
            continue
        call = re.search(
            rf"intSetting\(\s*Plasmoid\.configuration\.{name},\s*(-?\d+),\s*"
            rf"(-?\d+),\s*(-?\d+)\s*\)",
            qml,
        )
        if call is None:
            mismatches.append(f"{name}: not read through intSetting()")
            continue
        fallback, low, high = (int(group) for group in call.groups())
        if (fallback, low, high) != (
            int(values["default"]),
            int(values["min"]),
            int(values["max"]),
        ):
            mismatches.append(
                f"{name}: QML {fallback}/{low}/{high} vs "
                f"kcfg {values['default']}/{values['min']}/{values['max']}"
            )
    assert mismatches == []
