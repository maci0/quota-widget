from __future__ import annotations

import ast
import math
import re
import unittest
from typing import TYPE_CHECKING, Final

from project_paths import project_root

if TYPE_CHECKING:
    from collections.abc import Iterator

QML_PATH: Final = project_root() / "package" / "contents" / "ui" / "main.qml"
QML_SOURCE: Final = QML_PATH.read_text(encoding="utf-8")
FETCHER_SOURCE: Final = (QML_PATH.parent.parent / "code" / "fetch_quota.py").read_text(
    encoding="utf-8"
)
LABEL_OPEN: Final = "PlasmaComponents3.Label {"
DIMMED_OPACITY: Final = re.compile(r"opacity:\s*0\.[0-7]\d*")
ROSTER_RE: Final = re.compile(r"readonly property var providerNames: \[([^\]]*)\]")


def block_bodies(source: str, opener: str) -> Iterator[str]:
    """The body of every block `opener` opens, braces balanced.

    A pattern that stops at the first `}` never sees the rest of a block whose
    body is nested, and splitting on a fixed closing column widens to the rest
    of the file the moment a reformat moves that column. Both make an
    assertion about the block true by accident, so the depth is counted.
    Comments and string literals are stepped over: a brace in either is text,
    not nesting, and counting one leaves the block open for good.
    """
    start = source.find(opener)
    while start != -1:
        body = start + len(opener)
        if not opener.endswith("{"):
            # A signature: the block opens at the brace after the parameter
            # list, not at the end of the opener itself.
            body = source.index("{", body) + 1
        depth = 1
        index = body
        while index < len(source) and depth:
            char = source[index]
            if char == "/" and source.startswith("//", index):
                index = source.find("\n", index)
                if index == -1:
                    break
            elif char in {'"', "'", "`"}:
                index = source.find(char, index + 1)
                if index == -1:
                    break
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
        if depth:
            return
        yield source[body : index - 1]
        start = source.find(opener, index)


def block_body(source: str, opener: str) -> str:
    """The body of the first block `opener` opens, or a failed assertion.

    A missing opener is an `assert`, not an `IndexError` from the split that
    used to stand here: a renamed or reformatted block is a finding, and the
    reader should be told which one.
    """
    start = source.find(opener)
    assert start != -1, f"no block opens with {opener!r}"
    bodies = list(block_bodies(source[start:], opener))
    assert bodies, f"unbalanced braces after {opener!r}"
    return bodies[0]


def fetcher_int(name: str) -> int:
    """The value of a module-level integer constant in the fetcher."""
    match = re.search(rf"^{name} = (-?\d+)$", FETCHER_SOURCE, re.MULTILINE)
    assert match is not None, f"the fetcher defines no {name}"
    return int(match.group(1))


def label_blocks(source: str) -> Iterator[str]:
    """The body of every PlasmaComponents3.Label, braces balanced."""
    return block_bodies(source, LABEL_OPEN)


def dimmed_labels(source: str) -> list[str]:
    found = []
    for block in label_blocks(source):
        match = DIMMED_OPACITY.search(block)
        if match is not None:
            found.append(match.group(0))
    return found


PROSE_LITERAL: Final = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
# A line comment, not the "//" inside a URL or a relative path: those sit
# behind a ":" or a "." and must stay so the literals around them still pair up.
LINE_COMMENT: Final = re.compile(r"(?:^|(?<=\s))//[^\n]*", re.MULTILINE)
# String literals that are not prose: a vendor brand, a shell quote, or a key
# the fetcher envelope uses. Anything else with a word in it is a sentence a
# translator must reach.
NOT_PROSE: Final = frozenset(
    {
        "Claude",
        "Cursor",
        "Codex",
        "Grok",
        "python3 '",
        "'\\''",
        "exit code",
    }
)


class MainQmlLocalizationTest(unittest.TestCase):
    """Guards the locale contract the fetcher tests cannot see."""

    @staticmethod
    def _stripped_source() -> str:
        # Comments are not UI text, and a qsTr() call is already marked, so
        # neither can reach a reader. What is left is prose nobody can translate.
        return re.sub(r"//[^\n]*", "", re.sub(r'qsTr\("[^"]*"\)', "", QML_SOURCE))

    @classmethod
    def _untranslated_strings(cls) -> list[str]:
        # Drop every qsTr() call first, so what is left is text a translator
        # would never see. Comments and single-character separators carry no
        # letters-plus-space, so they stay out of the result on their own.
        source = re.sub(r'qsTr\("[^"]*"\)', "", QML_SOURCE)
        source = re.sub(LINE_COMMENT, "", source)
        found: list[str] = []
        for literal in PROSE_LITERAL.findall(source):
            stripped = literal.strip()
            if stripped in NOT_PROSE:
                continue
            if " " in stripped and re.search(r"[A-Za-z]", stripped):
                found.append(stripped)
        return found

    @staticmethod
    def _prose(text: str) -> list[str]:
        found: list[str] = []
        for literal in PROSE_LITERAL.findall(text):
            stripped = literal.strip()
            if (
                " " in stripped
                and re.search(r"[A-Za-z]", stripped)
                and stripped not in NOT_PROSE
            ):
                found.append(stripped)
        return found

    def test_user_facing_strings_are_marked_for_translation(self) -> None:
        # A hardcoded label is a word a translator cannot reach, so the widget
        # stays English in every locale. The scan covers the whole file, not
        # just one-line property bindings: a sentence returned from a helper
        # or wrapped across lines is the same defect.
        self.assertEqual(self._untranslated_strings(), [])

    def test_returned_wording_is_marked_for_translation(self) -> None:
        # A helper that answers in a hardcoded literal stays English wherever
        # its result lands. One word is enough to be a sentence, so this does
        # not wait for a space to appear. An empty return is the one way out.
        found = [
            literal
            for literal in re.findall(r'return "([^"]*)"', QML_SOURCE)
            if literal != ""
        ]
        self.assertEqual(found, [])

    def test_helper_returned_text_is_marked_for_translation(self) -> None:
        # errText() and the cached-card tooltip return their wording into a
        # label, so no `text:` binding ever names the literal and the sweep
        # above reads the call site, not the string.
        # A literal never spans a line here, and matching across one would pair
        # a stray quote with the next line's.
        self.assertEqual(
            [
                literal
                for line in self._stripped_source().splitlines()
                for literal in self._prose(line)
            ],
            [],
        )

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

    def test_percentages_use_the_locale(self) -> None:
        # "%1%" glues an English suffix on: French wants "12 %" with a space
        # and its own digits, and some locales lead with the sign.
        self.assertNotIn('qsTr("%1%")', QML_SOURCE)
        self.assertIn('style: "percent"', QML_SOURCE)
        self.assertIn("function percentStr(fraction, maxDigits)", QML_SOURCE)
        self.assertIn("Qt.locale().percent", QML_SOURCE)

    def test_counts_in_durations_use_the_locale(self) -> None:
        # qsTr().arg() on a raw number splices a JS number, so an Arabic
        # locale would read Latin digits beside a localized percentage.
        self.assertIn('qsTr("%1 %2 %3 %4")', QML_SOURCE)
        self.assertIn('qsTr("%1 %2")', QML_SOURCE)
        for count in ("d", "h", "m"):
            self.assertIn(f"numStr({count}, 0)", QML_SOURCE)

    def test_duration_units_are_words_a_translator_owns(self) -> None:
        # "2d 3h" welds an English unit letter to a Latin digit: no catalog
        # entry exists for it, so a locale that spells the unit out, writes it
        # behind the count, or uses another script has nothing to change.
        for welded in ('qsTr("%1d %2h")', 'qsTr("%1h %2m")', 'qsTr("%1 min")'):
            self.assertNotIn(welded, QML_SOURCE)
        for unit in ("dayUnit", "hourUnit", "minuteUnit"):
            self.assertIn(f"function {unit}(n)", QML_SOURCE)

    def test_a_count_picks_its_own_plural_form(self) -> None:
        # QML's qsTr() takes no plural argument, so the singular and the
        # plural have to be two entries the count chooses between. A language
        # with more than two forms (Polish, Russian, Arabic) then has a
        # string to translate rather than an English spelling to work around.
        for unit, forms in (
            ("dayUnit", ("day", "days")),
            ("hourUnit", ("hour", "hours")),
            ("minuteUnit", ("minute", "minutes")),
        ):
            self.assertIn(
                f"function {unit}(n) {{ return n === 1 "
                f'? qsTr("{forms[0]}") : qsTr("{forms[1]}") }}',
                QML_SOURCE,
            )

    def test_composed_sentences_are_one_pattern(self) -> None:
        # A translated phrase glued to a hardcoded " · " or joined on a
        # literal ", " cannot be reordered: the catalog holds half a
        # sentence. Every composition goes through a pattern whose
        # placeholders a translator can move.
        self.assertNotIn('" · "', QML_SOURCE)
        self.assertNotIn('join(", ")', QML_SOURCE)
        self.assertIn('qsTr("%1 · %2").arg(value).arg(when)', QML_SOURCE)
        self.assertIn('qsTr("%1, %2").arg(a).arg(b)', QML_SOURCE)
        # The failed-provider line lists vendor marks, and the separator around
        # them is as much the locale's as the sentence around the line is.
        self.assertIn(
            'qsTr("No reading for: %1").arg(joinLocalized(failed))', QML_SOURCE
        )
        self.assertIn("function joinNames(names)", QML_SOURCE)

    def test_currency_codes_go_through_the_locale_formatter(self) -> None:
        # "67.63 SGD" is a Latin number with a code pasted after it. The
        # locale decides symbol, side, spacing, and digits, so an amount
        # that names its currency is formatted as currency.
        self.assertIn("function moneyStr(amount, currency)", QML_SOURCE)
        self.assertIn(
            "return moneyFromCents(Number(amount) * 100, currency)", QML_SOURCE
        )
        self.assertIn('qsTr("Extra usage: %1")', QML_SOURCE)
        self.assertIn('qsTr("Extra usage credits: %1")', QML_SOURCE)

    def test_a_vendor_currency_code_is_checked_before_it_is_formatted(self) -> None:
        # A currency style raises a RangeError on anything that is not an
        # ISO 4217 code, and the label that asked for it renders blank. The
        # check sits at the one call site that reaches toLocaleString().
        self.assertIn("function isCurrencyCode(v)", QML_SOURCE)
        self.assertIn("currency: isCurrencyCode(currency)", QML_SOURCE)
        self.assertEqual(QML_SOURCE.count('style: "currency"'), 1)

    def test_spend_scale_is_bounded(self) -> None:
        # spend.used_minor is divided by 10^exponent, so an exponent outside
        # the decimal places an amount is counted in renders a real charge as
        # 0.00 (Math.pow(10, 1e308) is Infinity) or inflates it 10^5-fold.
        self.assertIn("function spendExponent(value)", QML_SOURCE)
        self.assertIn("const exp = root.spendExponent(spend.exponent)", QML_SOURCE)
        self.assertIn("readonly property int minSpendExponent: 0", QML_SOURCE)
        self.assertIn("readonly property int maxSpendExponent: 6", QML_SOURCE)
        self.assertNotIn("Math.pow(10, spend.exponent)", QML_SOURCE)
        # The bounds are the fetcher's, and the panel is what renders a payload
        # written under them: a wider window there clips an amount the fetcher
        # passed on, and a narrower one hides one it meant to show.
        self.assertEqual(fetcher_int("MIN_SPEND_EXPONENT"), 0)
        self.assertEqual(fetcher_int("MAX_SPEND_EXPONENT"), 6)

    def test_meter_fills_from_the_leading_edge(self) -> None:
        # Qt mirrors the left/right anchor lines in a right-to-left layout; a
        # physical edge would grow the meter from the wrong side.
        self.assertNotIn("anchors.leftToRight", QML_SOURCE)
        self.assertIn("anchors.left: parent.left", QML_SOURCE)


class MainQmlProviderRosterTest(unittest.TestCase):
    """One roster, walked wherever the panel has to look at every provider."""

    def setUp(self) -> None:
        declared = re.search(ROSTER_RE, QML_SOURCE)
        assert declared is not None, "the panel has no provider roster"
        self.roster = re.findall(r'"([^"]+)"', declared.group(1))
        self.assertTrue(self.roster, "the roster is empty")

    def test_every_provider_property_is_on_the_roster(self) -> None:
        # A `property var` the roster does not name is a provider the merge,
        # the error pick, and noData all skip: it never refreshes and never
        # ages out.
        self.assertEqual(
            sorted(re.findall(r"property var (\w+): null", QML_SOURCE)),
            sorted(self.roster),
        )

    def test_a_poll_merges_every_provider_off_the_roster(self) -> None:
        body = QML_SOURCE.split("onNewData: (sourceName, data) => {", 1)[1]
        body = body.split("} catch (e) {", 1)[0]
        self.assertIn("root.providerNames", body)
        for name in self.roster:
            self.assertNotIn(f"root.{name} =", body)
            self.assertNotIn(f"p.{name}", body)

    def test_the_roster_decides_whether_the_panel_has_data(self) -> None:
        # The whole body, not its first line: a noData() that returned true
        # outright would blank every card on a poll that answered.
        body = block_body(QML_SOURCE, "function noData(")
        self.assertIn("root.providerNames", body)
        self.assertIn("return false", body)
        self.assertIn("return true", body)
        for name in self.roster:
            self.assertNotIn(f"root.{name}", body)

    def test_roster_matches_fetcher_except_quickshell_only_go(self) -> None:
        # Go is consumed by the Quickshell frontend; the Plasma roster must
        # still account for every other provider the shared fetcher emits.
        tree = ast.parse(FETCHER_SOURCE)
        main = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        table = next(
            node.value
            for node in ast.walk(main)
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "providers"
        )
        assert isinstance(table, ast.Dict)
        emitted = [key.value for key in table.keys if isinstance(key, ast.Constant)]
        self.assertEqual(sorted(emitted), sorted([*self.roster, "opencode_go"]))


class MainQmlBlockScanTest(unittest.TestCase):
    """The scanner every block-scoped assertion above runs on.

    A scan that reads past the block it was given turns every one of those
    assertions into a search of the rest of the file, so it is checked against
    a source built to end where the block ends.
    """

    SOURCE = (
        "function first(a) {\n"
        '    if (a) { return "{ }" }\n'
        "    return a\n"
        "}\n"
        "function second() {\n"
        "    return 2\n"
        "}\n"
        "const later = 3\n"
    )

    def test_a_body_ends_at_its_own_brace(self) -> None:
        self.assertEqual(
            block_body(self.SOURCE, "function second(").strip(), "return 2"
        )

    def test_every_block_is_found(self) -> None:
        bodies = list(block_bodies(self.SOURCE, "function "))
        self.assertEqual(len(bodies), 2)
        self.assertTrue(all("return" in body for body in bodies))

    def test_a_missing_block_is_a_failure(self) -> None:
        with self.assertRaises(AssertionError):
            block_body(self.SOURCE, "function missing(")

    def test_the_label_scan_reads_the_shipped_source_to_their_ends(self) -> None:
        self.assertEqual(
            len(list(label_blocks(QML_SOURCE))), QML_SOURCE.count(LABEL_OPEN)
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
        self.assertEqual(dimmed_labels(QML_SOURCE), [])

    def test_the_dimmed_label_scan_reaches_a_label_with_a_block_body(self) -> None:
        # The scan balances braces, so a label whose text is a block is read to
        # its own end and not silently skipped the way a `[^}]` pattern skips it.
        blocks = list(label_blocks(QML_SOURCE))
        self.assertEqual(
            len(blocks), QML_SOURCE.count(LABEL_OPEN), "a label body ran to no end"
        )
        self.assertTrue(
            any("{" in block for block in blocks), "no label carries a block body"
        )
        dimmed = dimmed_labels(
            "PlasmaComponents3.Label {\n"
            "    text: {\n"
            '        if (x) { return "a" }\n'
            '        return "b"\n'
            "    }\n"
            "    opacity: 0.4\n"
            "}\n"
        )
        self.assertEqual(dimmed, ["opacity: 0.4"])

    def test_meters_expose_a_spoken_summary(self) -> None:
        self.assertIn("Accessible.role: Accessible.ProgressBar", QML_SOURCE)
        self.assertIn("Accessible.description: joinLocalized([", QML_SOURCE)

    def test_panel_widget_is_keyboard_operable(self) -> None:
        self.assertIn("Keys.onSpacePressed: root.expanded = !root.expanded", QML_SOURCE)
        self.assertIn(
            "Keys.onReturnPressed: root.expanded = !root.expanded", QML_SOURCE
        )
        self.assertIn("border.color: Kirigami.Theme.focusColor", QML_SOURCE)

    def test_panel_widget_can_be_activated_by_assistive_tech(self) -> None:
        # The key handlers above are for a sighted keyboard user. A screen
        # reader drives the item through its accessible action, so without one
        # the panel button is announced and then does nothing (WCAG 2.1.1).
        self.assertIn(
            "Accessible.onPressAction: root.expanded = !root.expanded", QML_SOURCE
        )

    def test_the_panel_reading_is_not_announced_twice(self) -> None:
        # The button's description is tooltipBody(), which already names every
        # provider and its percentage, so leaving the two labels in the tree
        # makes a reader say the same number once per line and once in the
        # description.
        compact = QML_SOURCE.split("compactRepresentation:", 1)[1]
        compact = compact.split("fullRepresentation:", 1)[0]
        self.assertEqual(compact.count("Accessible.ignored: true"), 4)

    def test_the_view_switch_exposes_the_state_it_is_in(self) -> None:
        # Gauge view and list view draw the same icon-only button, so the
        # state is the only thing a screen reader can read (WCAG 4.1.2).
        self.assertIn("Accessible.role: Accessible.CheckBox", QML_SOURCE)
        self.assertIn("Accessible.checked: root.gaugeView", QML_SOURCE)

    def test_the_popup_takes_focus_when_it_opens(self) -> None:
        # The popup opens on a click, which leaves focus on the panel, so Tab
        # would walk out of the widget and none of it would be reachable.
        toggle = QML_SOURCE.split("icon.name: root.gaugeView", 1)[1]
        block = toggle.split("PlasmaComponents3.BusyIndicator", 1)[0]
        self.assertIn("focus: true", block)

    def test_icon_only_buttons_meet_the_minimum_target_size(self) -> None:
        # A ToolButton is sized by its icon, and Plasma's small icon is under
        # 24 px, so the target a finger or a stylus has to hit is too small
        # (WCAG 2.5.8).
        self.assertIn("readonly property int minTargetPx: 24", QML_SOURCE)
        self.assertEqual(QML_SOURCE.count("Layout.minimumHeight: root.minTargetPx"), 2)

    def test_the_popup_closes_on_escape(self) -> None:
        # The popup takes the focus with it, so the panel button's Escape
        # handler is off the key path and a keyboard user who opened the popup
        # had no key that closed it (WCAG 2.1.2). The handler sits on the view
        # so it covers every control inside, and the focus goes back to the
        # panel button the keyboard came from (WCAG 2.4.3).
        full = QML_SOURCE.split("fullRepresentation:", 1)[1]
        escape = full.split("Keys.onEscapePressed:", 1)[1].split("}", 1)[0]
        self.assertIn("root.expanded = false", escape)
        self.assertIn("compact.forceActiveFocus()", escape)

    def test_meter_labels_are_not_elided(self) -> None:
        # Eliding the label, the countdown, or the reset time drops the part
        # that says which window the meter is. The popup's height follows its
        # content, so the text can wrap instead of disappearing when the user's
        # font is larger (WCAG 1.4.4).
        self.assertNotIn("elide: Text.ElideRight", QML_SOURCE)

    def test_the_first_reading_is_announced(self) -> None:
        # The first poll answers into a widget the reader is not looking at: the
        # loading label goes away and the cards appear in silence (WCAG 4.1.3).
        # Only the way out of loading announces, or the way into it would
        # announce a reading that is not there yet.
        first = QML_SOURCE.split("onFirstLoadChanged:", 1)[1].split("}", 1)[0]
        self.assertIn("if (!root.firstLoad)", first)
        self.assertIn("root.announceStatus()", first)

    def test_every_popup_control_joins_the_tab_chain(self) -> None:
        # The view switch and the refresh button are the whole keyboard reach of
        # the popup, so each says it is in the tab chain instead of leaning on
        # the control default (WCAG 2.1.1).
        popup = QML_SOURCE.split("fullRepresentation:", 1)[1]
        buttons = list(block_bodies(popup, "PlasmaComponents3.ToolButton {"))
        self.assertEqual(len(buttons), 2)
        for button in buttons:
            self.assertIn("activeFocusOnTab: true", button)

    def test_a_failure_is_announced_when_it_arrives(self) -> None:
        # A poll answers into a widget the reader is not looking at, so the
        # banner and the cards change in silence (WCAG 4.1.3). A repeated
        # message is dropped: a poll runs every pollSeconds, and a rate limit
        # outlasts several of them.
        self.assertIn("onErrorMsgChanged: root.announceStatus()", QML_SOURCE)
        self.assertIn("function announceStatus()", QML_SOURCE)
        self.assertIn("if (msg === root.announced)", QML_SOURCE)
        self.assertIn("Accessible.announce(msg)", QML_SOURCE)

    def test_a_cached_reading_explains_itself_without_a_hover(self) -> None:
        # "cached" is a word about the fetch, not about the number. The hover
        # tooltip is the only place it was explained, and a screen reader
        # never hovers and the subtitle is not focusable.
        self.assertIn("function staleNote()", QML_SOURCE)
        self.assertIn(
            "Accessible.description: card.stale ? root.staleNote()", QML_SOURCE
        )
        self.assertEqual(QML_SOURCE.count('qsTr("Last known reading'), 1)

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

    def test_a_run_that_produced_no_payload_is_not_called_offline(self) -> None:
        # "exec" covers a missing python3, an unreadable fetcher, and a crash
        # on the way in. None of those is a network condition, so saying
        # "Offline" points the reader at the wrong thing to check.
        self.assertNotIn("Offline", QML_SOURCE)
        self.assertIn('qsTr("Quota poll did not run")', QML_SOURCE)

    def test_the_panel_says_it_has_no_reading(self) -> None:
        # A bare "!" in a 40 pixel panel is punctuation, not a status, and no
        # translator can read it. "n/a" is what the meter already says when a
        # value is missing, so the panel and the popup use one word for it.
        self.assertNotIn('root.errorMsg ? "!"', QML_SOURCE)
        self.assertIn('root.errorMsg ? qsTr("n/a")', QML_SOURCE)

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

    def test_a_card_that_answered_is_not_labelled_loading(self) -> None:
        # A provider with no plan name (Cursor reads one off the credential)
        # rendered live meters under a subtitle saying "Loading".
        for provider in ("claude", "cursor", "codex"):
            self.assertIn(
                f"subtitle: (root.{provider} && root.{provider}.ok)\n"
                f"                        ? withStaleMark(root.{provider}.plan "
                '|| qsTr("Signed in"),',
                QML_SOURCE,
            )


class MainQmlPollingTest(unittest.TestCase):
    """A poll holds a child process; it must always be released."""

    def test_every_started_run_arms_the_watchdog(self) -> None:
        self.assertIn("pollWatchdog.restart()\n            connectSource", QML_SOURCE)

    def test_completed_run_releases_the_source(self) -> None:
        self.assertIn(
            "disconnectSource(sourceName)\n            pollWatchdog.stop()",
            QML_SOURCE,
        )

    def test_the_fetcher_runs_under_the_engine_main_declares(self) -> None:
        # The DataSource default engine is not the fetcher, so without this the
        # widget connects to nothing and every card stays empty in silence:
        # no error, no payload, no trace.
        source = block_body(QML_SOURCE, "P5Support.DataSource {")
        self.assertIn('engine: "executable"', source)

    def test_a_hung_run_is_dropped_so_polling_resumes(self) -> None:
        # Without this, one stalled fetcher holds the source and no later poll
        # ever starts.
        self.assertIn("id: pollWatchdog", QML_SOURCE)
        self.assertIn("interval: root.pollTimeoutMs", QML_SOURCE)
        self.assertIn("onTriggered: exec.dropStalled()", QML_SOURCE)
        self.assertIn("disconnectSource(connectedSources[0])", QML_SOURCE)

    def test_the_stall_deadline_is_not_measured_on_the_wall_clock(self) -> None:
        # Date.now() steps backwards on an NTP correction or a manual set, so
        # the difference went negative and the drop never fired: the source
        # stayed connected and polling was dead until the widget was reloaded.
        # A QML Timer runs on a monotonic clock, so it is immune to either.
        self.assertNotIn("pollStartedMs", QML_SOURCE)

    def test_a_run_in_flight_is_released_when_the_widget_goes_away(self) -> None:
        # Removing the widget destroys the QML while a run is still out, and
        # that run never reports back: the fetcher process has to be stopped
        # here or it outlives the object that started it.
        self.assertIn("Component.onDestruction:", QML_SOURCE)
        self.assertIn("exec.disconnectSource(held[i])", QML_SOURCE)

    def test_a_dropped_run_releases_the_indicators(self) -> None:
        # The dropped run reports nothing back, so the refresh button and its
        # spinner keep the state of a run that is already gone.
        block = block_body(QML_SOURCE, "function dropStalled(")
        self.assertIn("disconnectSource(connectedSources[0])", block)
        self.assertIn("root.fetching = false", block)
        self.assertIn("root.userRefreshing = false", block)

    def test_a_kept_reading_is_copied_before_it_is_marked_stale(self) -> None:
        # mergeProv returned the object the property already held, so the card
        # kept rendering it as live: assigning the same object to a var
        # property raises no change signal for the `stale` bindings to follow.
        self.assertIn("return Object.assign({}, oldv, { stale: true })", QML_SOURCE)
        self.assertNotIn("oldv.stale = true", QML_SOURCE)

    def test_only_the_same_accounts_reading_is_kept(self) -> None:
        # The fetcher scopes its cache by account digest and carries that digest
        # in every payload. The panel kept its own copy across an account switch
        # instead, so a failed first poll showed the previous account's plan and
        # usage for as long as the window allowed.
        merge = block_body(QML_SOURCE, "function mergeProv(")
        self.assertIn("oldv.account && oldv.account === newv.account", merge)

    def test_the_transient_call_is_the_fetchers(self) -> None:
        # The panel re-derived "may this failure keep the card" from the error
        # code, so a code added later read as final and blanked a card on a rate
        # limit. The fetcher classifies the failure and says so in the payload;
        # "exec" is the panel's own condition, where no payload arrived at all.
        merge = block_body(QML_SOURCE, "function mergeProv(")
        self.assertIn("newv.transient === true", merge)
        self.assertIn('newv.error === "exec"', merge)
        self.assertNotIn("http-5", merge)


class MainQmlDuplicateRunTest(unittest.TestCase):
    """A run the poll dropped for running long can still answer, and the
    numbers it brings are the ones from before."""

    def test_a_late_payload_is_not_merged(self) -> None:
        # Merging it rewinds every card and the age with it, so the payload is
        # dropped whole rather than reconciled field by field.
        self.assertIn(
            'if (typeof p.fetched_ms === "number" && p.fetched_ms < root.fetchedMs)\n'
            "                    return",
            QML_SOURCE,
        )

    def test_an_older_reading_keeps_the_card_it_loses_to(self) -> None:
        # mergeProv runs per provider, so one provider can answer from a later
        # poll than another and the guard has to hold there too.
        self.assertIn("newv.fetched_ms < oldv.fetched_ms", QML_SOURCE)

    def test_the_poll_clock_never_rewinds(self) -> None:
        self.assertIn("root.fetchedMs = Math.max(root.fetchedMs,", QML_SOURCE)


class MainQmlStaleWindowTest(unittest.TestCase):
    """The keep-while-failing window is the fetcher's setting, not a second
    copy of it: QUOTA_WIDGET_CACHE_MAX_AGE_S has to reach the panel."""

    def test_the_window_comes_from_the_poll_payload(self) -> None:
        self.assertIn('root.staleKeepMs = (typeof keepS === "number"', QML_SOURCE)
        self.assertIn("const keepS = p.cache_max_age_s", QML_SOURCE)

    def test_the_window_is_in_force_before_the_merge_it_governs(self) -> None:
        # mergeProv ages a kept reading against staleKeepMs, so taking the
        # window after the merge leaves a shortened
        # QUOTA_WIDGET_CACHE_MAX_AGE_S in force for one poll past the payload
        # that reported it. The merge walks providerNames, so one site covers
        # every provider and the window has to precede that walk.
        window = QML_SOURCE.index("root.staleKeepMs = (typeof keepS ===")
        # The roster walks every provider, so the call site is one loop rather
        # than a line per provider: the loop header is the thing that has to
        # sit between the window and the assignment it wraps.
        loop = QML_SOURCE.index(
            "for (let i = 0; i < root.providerNames.length; i++)", window
        )
        merge = QML_SOURCE.index("root[n] = mergeProv(root[n], p[n])", loop)
        self.assertLess(window, loop, "the roster is walked before the window")
        self.assertLess(loop, merge, "the merge is not the roster walk")
        self.assertLess(window, merge, "roster merged before the window")

    def test_the_fallback_window_is_not_writable_state(self) -> None:
        # staleKeepMs is overwritten by every poll, so the default it falls
        # back to has to be a separate readonly property.
        self.assertIn(
            "readonly property int defaultStaleKeepMs: 24 * 60 * 60 * 1000",
            QML_SOURCE,
        )
        self.assertIn("property int staleKeepMs: defaultStaleKeepMs", QML_SOURCE)
        # A payload that carries no window falls back to the fetcher's own
        # default, so the two copies have to be the same number of seconds.
        self.assertIn("DEFAULT_CACHE_MAX_AGE_S = SECONDS_PER_DAY", FETCHER_SOURCE)
        fallback = re.search(
            r"readonly property int defaultStaleKeepMs: ([0-9 *]+)$",
            QML_SOURCE,
            re.MULTILINE,
        )
        assert fallback is not None
        self.assertEqual(
            math.prod(int(f) for f in fallback.group(1).split("*")),
            fetcher_int("SECONDS_PER_DAY") * 1000,
        )

    def test_a_kept_reading_is_marked_with_a_new_object(self) -> None:
        # A property holding a JS object re-reads its bindings only when the
        # value changes identity, so setting stale on the stored reading would
        # leave the card unmarked until some later poll replaced the object.
        self.assertNotIn("oldv.stale = true", QML_SOURCE)
        # Spacing inside the object literal is the formatter's, not the rule's.
        self.assertRegex(
            QML_SOURCE,
            r"return Object\.assign\(\{\},\s*oldv,\s*\{\s*stale:\s*true\s*\}\s*\)",
        )


class MainQmlPollWatchdogTest(unittest.TestCase):
    """The poll watchdog has to outlast a poll the fetcher is entitled to
    take: QUOTA_WIDGET_HTTP_TIMEOUT goes to 300 s, more than the panel's own
    constant holds, and a shorter watchdog drops the run and calls it exec."""

    def test_the_watchdog_budget_comes_from_the_poll_payload(self) -> None:
        self.assertIn("const budgetS = p.poll_timeout_s", QML_SOURCE)
        self.assertIn(
            'root.pollTimeoutMs = (typeof budgetS === "number" && budgetS > 0)',
            QML_SOURCE,
        )

    def test_the_fallback_watchdog_is_not_writable_state(self) -> None:
        # pollTimeoutMs is overwritten by every poll, so the default it falls
        # back to has to be a separate readonly property, as staleKeepMs is.
        self.assertIn(
            "readonly property int defaultPollTimeoutMs: 10 * 60 * 1000", QML_SOURCE
        )
        self.assertIn("property int pollTimeoutMs: defaultPollTimeoutMs", QML_SOURCE)

    def test_a_reported_budget_only_ever_raises_the_watchdog(self) -> None:
        # The budget bounds the requests a poll makes, not the body a socket
        # trickles back under its per-read timeout, so a smaller number must
        # not shorten a deadline the default already covers.
        self.assertIn("Math.max(root.defaultPollTimeoutMs,", QML_SOURCE)

    def test_a_reported_budget_is_capped(self) -> None:
        # The number arrives in a payload the panel trusts, so a run that would
        # never end cannot buy itself an unbounded deadline.
        self.assertIn(
            "readonly property int maxPollTimeoutMs: 30 * 60 * 1000", QML_SOURCE
        )
        self.assertIn("Math.min(root.maxPollTimeoutMs,", QML_SOURCE)

    def test_the_watchdog_uses_it(self) -> None:
        self.assertIn("interval: root.pollTimeoutMs", QML_SOURCE)


class MainQmlStatusWordingTest(unittest.TestCase):
    """A failure has to name its condition; "Unavailable" names none."""

    @staticmethod
    def _err_text() -> str:
        return QML_SOURCE.split("function errText(", 1)[1].split("\n    }", 1)[0]

    def test_a_forbidden_response_asks_for_a_sign_in(self) -> None:
        # The fetcher calls a 403 final, the same as a 401, and both are
        # answered the same way. It used to fall through to "Unavailable",
        # which is not something a user can act on.
        self.assertIn('code === "http-403"', self._err_text())
        self.assertIn("return signIn", self._err_text())

    def test_a_server_error_is_stated_as_a_wait(self) -> None:
        # 429 was the only 5xx with a wording, so a 500 or a 503 that arrived
        # before a reading existed read as an unfixable fault. The fetcher
        # keeps the card through one and the next poll usually clears it.
        self.assertIn('code.indexOf("http-5") === 0', self._err_text())
        self.assertIn('qsTr("Provider unavailable, retrying")', self._err_text())

    def test_an_unreadable_body_is_not_a_vendor_status(self) -> None:
        # The fetcher answers 599 when the provider replied and the body was
        # not one it can read. It has to clear the http-5 branch, which would
        # otherwise blame the provider's server for a body the fetcher refused.
        self.assertIn('code === "bad-body"', self._err_text())
        self.assertIn('qsTr("Unreadable response, retrying")', self._err_text())

    def test_a_refused_request_is_not_reported_as_a_vendor_status(self) -> None:
        # The fetcher declines a redirect that would carry the credential to
        # another host. It raises an HTTPError, so without its own code the
        # payload said "http-302" and this read it as a provider down; the
        # refusal is the fetcher's own, it repeats, and no reading is served.
        self.assertIn('code === "refused"', self._err_text())

    def test_the_panel_names_the_providers_it_has_no_reading_for(self) -> None:
        # The tooltip listed only the providers that answered, so a failed one
        # disappeared from the panel summary without a trace while the popup
        # showed a card for it.
        self.assertIn("function failedNames()", QML_SOURCE)
        tooltip = QML_SOURCE.split("function tooltipBody(", 1)[1].split("\n    }", 1)[0]
        self.assertIn("failedNames()", tooltip)
        self.assertIn("joinLocalized(failed)", tooltip)
        self.assertIn('qsTr("No reading for: %1")', tooltip)

    def test_the_panel_marks_a_reading_it_is_only_caching(self) -> None:
        # The popup marks a kept reading "cached"; the panel showed the same
        # number with nothing on it, so a failed poll looked live there.
        tooltip = QML_SOURCE.split("function tooltipBody(", 1)[1].split("\n    }", 1)[0]
        for provider in ("claude", "cursor", "codex", "grok"):
            self.assertIn(f"staleSuffix({provider})", tooltip)

    def test_a_poll_in_flight_is_visible(self) -> None:
        # The refresh button is disabled while a run is out, and a disabled
        # item receives no hover, so the tooltip that explains the greyed
        # button never opened. The spinner has to cover the automatic polls.
        indicator = block_body(QML_SOURCE, "PlasmaComponents3.BusyIndicator {")
        self.assertIn("visible: root.fetching", indicator)

    def test_the_gauge_shows_the_detail_the_list_row_shows(self) -> None:
        # A money-backed meter states what was spent of what it was capped at
        # in its sub-detail. Gauge view carried it in a hover tooltip only, so
        # it had no visible place at all.
        label = QML_SOURCE.split("// gauge", 1)[1]
        self.assertIn(
            '&& (row.detail !== "" || row.subdetail !== "")',
            label,
        )
        self.assertIn(
            'text: [row.detail, row.subdetail].filter(s => s !== "").join("\\n")',
            label,
        )


class MainQmlTokenTest(unittest.TestCase):
    """The token block is the widget's type scale. A factor typed at a call
    site is a fourth scale nobody chose, so the steps are pinned here."""

    def test_no_type_size_is_a_literal_factor(self) -> None:
        # The panel reading, the card titles, and the gauge numbers each
        # carried their own factor (1.1, 1.15, 1.05). None of them related to
        # the next, which is what a hierarchy looks like when no one picked
        # one: the card title was nearly the size of the panel's only number.
        self.assertEqual(
            re.findall(r"pointSize:\s*[^,\n]*\*\s*[0-9.]+", QML_SOURCE), []
        )

    def test_the_scale_names_every_level_it_draws(self) -> None:
        for token in ("panelValueScale", "cardTitleScale", "gaugeValueScale"):
            self.assertIn(f"readonly property real {token}:", QML_SOURCE)
        # The panel reading leads: it is the only text a panel shows.
        self.assertIn("* root.panelValueScale", QML_SOURCE)
        self.assertIn("* root.cardTitleScale", QML_SOURCE)
        self.assertIn("* root.gaugeValueScale", QML_SOURCE)

    def test_the_panel_rule_has_no_width_of_its_own(self) -> None:
        # The rule under the panel reading is the same mark as a card's accent
        # bar, at a smaller weight; a literal 2 next to markThickness is two
        # places to change it.
        self.assertIn("readonly property int ruleThickness: 2", QML_SOURCE)
        self.assertIn("readonly property int tightSpacing: 2", QML_SOURCE)
        self.assertNotIn("Layout.preferredHeight: 2", QML_SOURCE)
        self.assertNotIn("spacing: 2", QML_SOURCE)


if __name__ == "__main__":
    unittest.main()
