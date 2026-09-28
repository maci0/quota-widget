from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Final

QML_PATH: Final = (
    Path(__file__).resolve().parent.parent / "package" / "contents" / "ui" / "main.qml"
)
QML_SOURCE: Final = QML_PATH.read_text()
DIMMED_LABEL: Final = re.compile(
    r"PlasmaComponents3\.Label \{[^}]*?opacity: 0\.[0-7]\d*"
)


UI_BINDING: Final = re.compile(
    r"(?:^|\s)(?:text|subtitle|title|label|detail|Accessible\.name"
    r"|Accessible\.description|ToolTip\.text|toolTip\w*Text)\s*:\s*(.*)$",
    re.MULTILINE,
)
PROSE_LITERAL: Final = re.compile(r'"([^"\\]*)"')


class MainQmlLocalizationTest(unittest.TestCase):
    """Guards the locale contract the fetcher tests cannot see."""

    @staticmethod
    def _untranslated_bindings() -> list[str]:
        # Drop every qsTr() call first, so what is left is text a translator
        # would never see. Brand names ("Claude", "Cursor", "Codex", "Grok")
        # and separators (" · ") carry no letters-plus-space, so they stay.
        source = re.sub(r'qsTr\("[^"]*"\)', "", QML_SOURCE)
        found: list[str] = []
        for match in UI_BINDING.finditer(source):
            for literal in PROSE_LITERAL.findall(match.group(1)):
                stripped = literal.strip()
                if " " in stripped and re.search(r"[A-Za-z]", stripped):
                    found.append(stripped)
        return found

    def test_ui_text_is_marked_for_translation(self) -> None:
        # A hardcoded label is a word a translator cannot reach, so the widget
        # stays English in every locale.
        self.assertEqual(self._untranslated_bindings(), [])

    def test_dates_and_times_use_the_locale(self) -> None:
        # "ddd h:mm AP" and friends are English patterns: they name the weekday
        # in English, order the fields the American way, and force a 12-hour
        # clock with a trailing meridiem.
        for args in re.findall(
            r"Qt\.format(?:Date|Time|DateTime)\(([^)]*)\)", QML_SOURCE
        ):
            self.assertNotIn('"', args, f"hardcoded date or time pattern: {args}")
        self.assertIn("Qt.DefaultLocaleShortDate", QML_SOURCE)

    def test_currency_is_formatted_by_the_locale(self) -> None:
        # "$" in front of an English-grouped number is wrong in most of the
        # world: symbol side, spacing, and decimal separator all differ.
        self.assertNotIn('"$" +', QML_SOURCE)
        self.assertIn('style: "currency"', QML_SOURCE)

    def test_numbers_use_the_locale(self) -> None:
        # toLocaleString() with no locale falls back to the C locale, so a
        # German user reads "12.5" instead of "12,5".
        self.assertNotIn("toLocaleString(undefined", QML_SOURCE)
        self.assertIn("Qt.locale().name", QML_SOURCE)

    def test_meter_fills_from_the_leading_edge(self) -> None:
        # Qt mirrors the left/right anchor lines in a right-to-left layout; a
        # physical edge would grow the meter from the wrong side.
        self.assertNotIn("anchors.leftToRight", QML_SOURCE)
        self.assertIn("anchors.left: parent.left", QML_SOURCE)


class MainQmlAccessibilityTest(unittest.TestCase):
    """Guards the QML accessibility contract the fetcher tests cannot see."""

    def test_text_uses_theme_point_sizes(self) -> None:
        # pixelSize ignores the user's KDE font scaling, so text stops growing
        # with the rest of the desktop (WCAG 1.4.4).
        self.assertNotIn("font.pixelSize", QML_SOURCE)

    def test_no_dimmed_labels(self) -> None:
        # Fading a label with opacity drops it below the 4.5:1 text contrast
        # floor on light themes (WCAG 1.4.3).
        self.assertEqual(DIMMED_LABEL.findall(QML_SOURCE), [])

    def test_meters_expose_a_spoken_summary(self) -> None:
        self.assertIn("Accessible.role: Accessible.ProgressBar", QML_SOURCE)
        self.assertIn("Accessible.description: [", QML_SOURCE)

    def test_panel_widget_is_keyboard_operable(self) -> None:
        self.assertIn("Keys.onSpacePressed: root.expanded = !root.expanded", QML_SOURCE)
        self.assertIn(
            "Keys.onReturnPressed: root.expanded = !root.expanded", QML_SOURCE
        )
        self.assertIn("border.color: Kirigami.Theme.focusColor", QML_SOURCE)

    def test_provider_titles_are_headings(self) -> None:
        self.assertIn("Accessible.role: Accessible.Heading", QML_SOURCE)

    def test_first_poll_shows_a_loading_state(self) -> None:
        # Cards stay hidden until the first poll answers, so without this the
        # popup is a bare heading above an empty area.
        self.assertIn("readonly property bool firstLoad", QML_SOURCE)
        self.assertIn("visible: root.firstLoad", QML_SOURCE)

    def test_refresh_reports_that_a_poll_is_running(self) -> None:
        # exec.poll() drops a second poll, so the button must not look clickable
        # while one is in flight, and a manual click must show progress.
        self.assertIn("property bool fetching: false", QML_SOURCE)
        self.assertIn("enabled: !root.fetching", QML_SOURCE)
        self.assertIn("root.fetching = true", QML_SOURCE)
        self.assertIn("root.fetching = false", QML_SOURCE)
        self.assertIn("PlasmaComponents3.BusyIndicator", QML_SOURCE)

    def test_failures_use_one_wording(self) -> None:
        # The banner and the cards described the same failure differently
        # ("Error" vs "Rate-limited"); both go through errText now.
        self.assertNotIn("errLabel", QML_SOURCE)
        self.assertIn("function errText(code, signIn)", QML_SOURCE)
        self.assertEqual(QML_SOURCE.count("function errText("), 1)

    def test_every_card_marks_a_cached_reading(self) -> None:
        # Only Claude and Cursor said "cached" before, so a stale Codex or Grok
        # reading looked live.
        for provider in ("claude", "cursor", "codex", "grok"):
            self.assertIn(
                f"stale: !!(root.{provider} && root.{provider}.stale)",
                QML_SOURCE,
            )

    def test_meters_state_the_reset_time_the_same_way(self) -> None:
        # Claude's weekly rows showed the absolute time first and every other
        # meter showed the countdown first, in the same two columns.
        self.assertNotIn('"Resets " + resetAtStr(', QML_SOURCE)


class MainQmlPollingTest(unittest.TestCase):
    """A poll holds a child process; it must always be released."""

    def test_every_started_run_records_its_start(self) -> None:
        self.assertIn(
            "root.pollStartedMs = root.nowMs\n            connectSource", QML_SOURCE
        )

    def test_completed_run_releases_the_source(self) -> None:
        self.assertIn(
            "disconnectSource(sourceName)\n            root.pollStartedMs = 0",
            QML_SOURCE,
        )

    def test_a_hung_run_is_dropped_so_polling_resumes(self) -> None:
        # Without this, one stalled fetcher holds the source and no later poll
        # ever starts.
        self.assertIn(
            "root.nowMs - root.pollStartedMs > root.pollTimeoutMs", QML_SOURCE
        )
        self.assertIn("disconnectSource(connectedSources[0])", QML_SOURCE)


if __name__ == "__main__":
    unittest.main()
