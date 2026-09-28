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
        self.assertIn(
            "Keys.onSpacePressed: root.expanded = !root.expanded", QML_SOURCE
        )
        self.assertIn(
            "Keys.onReturnPressed: root.expanded = !root.expanded", QML_SOURCE
        )
        self.assertIn("border.color: Kirigami.Theme.focusColor", QML_SOURCE)

    def test_provider_titles_are_headings(self) -> None:
        self.assertIn("Accessible.role: Accessible.Heading", QML_SOURCE)


if __name__ == "__main__":
    unittest.main()
