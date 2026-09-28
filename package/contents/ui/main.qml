import QtQuick
import QtQuick.Layouts
import QtQuick.Shapes
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.plasma5support as P5Support
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root

    readonly property string scriptPath: {
        var s = Qt.resolvedUrl("../code/fetch_quota.py").toString()
        if (s.indexOf("file://") === 0)
            s = s.substring(7)
        try {
            return decodeURIComponent(s)
        } catch (e) {
            return s
        }
    }
    readonly property string cmd: "python3 '"
        + scriptPath.replace(/'/g, "'\\''") + "'"

    // kcfg values are user-editable, so every one is clamped to its range
    // before use; a bad config file must not blank the widget.
    function intSetting(value, fallback, min, max) {
        var n = parseInt(value)
        if (!isFinite(n))
            return fallback
        return Math.max(min, Math.min(max, n))
    }

    readonly property int pollSeconds: intSetting(
        Plasmoid.configuration.pollSeconds, 120, 30, 3600)
    readonly property int pollMs: pollSeconds * 1000
    readonly property int utilWarnAt: intSetting(
        Plasmoid.configuration.utilWarnAt, 70, 1, 99)
    readonly property int utilCritAt: Math.max(utilWarnAt, intSetting(
        Plasmoid.configuration.utilCritAt, 90, 1, 100))
    // Mirrors DEFAULT_CACHE_MAX_AGE_S in package/contents/code/fetch_quota.py.
    readonly property int staleKeepMs: 24 * 60 * 60 * 1000
    // Longest one fetcher run may hold the data source before the poll timer
    // drops it: four providers, each with a bounded HTTP timeout, a
    // Retry-After sleep, and a refresh-lock wait, plus process startup.
    readonly property int pollTimeoutMs: 10 * 60 * 1000

    // ── tokens ───────────────────────────────────────────────────────────
    // Type scale, dimming steps, and meter geometry. Every view reads these
    // so the panel, the popup, and the gauges stay on one scale.
    readonly property real scaleTitle: 1.15
    readonly property real scaleReading: 1.1
    readonly property real scaleGaugeRead: 1.05
    readonly property real dim: 0.8
    readonly property real dimMuted: 0.7
    readonly property real dimFaint: 0.55
    readonly property real trackOpacity: 0.2
    readonly property real inactiveMarkOpacity: 0.35
    readonly property int markThickness: 3
    readonly property int barThickness: 8

    // Minor-unit amounts reach the UI without a currency code (the Cursor
    // usage API sends bare cents). Every number they came from is a USD
    // amount; the locale still decides the symbol, its side, and the grouping.
    readonly property string defaultCurrency: "USD"

    // Provider marks. Claude and Codex ship a brand color; Cursor and Grok
    // are monochrome, so they get theme neutrals rather than invented hues.
    readonly property color claudeMark: "#D97757"
    readonly property color codexMark: "#10A37F"
    readonly property color cursorMark: Kirigami.Theme.textColor
    readonly property color grokMark: Kirigami.Theme.neutralTextColor

    property var claude: null
    property var cursor: null
    property var grok: null
    property var codex: null
    property string errorMsg: ""
    property string configError: ""
    property double nowMs: Date.now()
    property double fetchedMs: 0
    property double pollStartedMs: 0
    readonly property bool gaugeView: !!Plasmoid.configuration.gaugeView
    readonly property bool firstLoad: root.noData() && root.errorMsg === ""
    // A poll is in flight. exec.poll() drops a second one, so the header
    // disables refresh and the compact view shows a placeholder meanwhile.
    property bool fetching: false
    property bool userRefreshing: false

    Plasmoid.icon: "com.maci.quota-widget.svg"
    toolTipMainText: qsTr("AI Quota")
    toolTipSubText: tooltipBody()

    preferredRepresentation: fullRepresentation
    switchWidth: Kirigami.Units.gridUnit * 14
    switchHeight: Kirigami.Units.gridUnit * 12

    // ── data ──────────────────────────────────────────────────────────────
    P5Support.DataSource {
        id: exec
        engine: "executable"
        connectedSources: []
        onNewData: (sourceName, data) => {
            disconnectSource(sourceName)
            root.pollStartedMs = 0
            root.fetching = false
            root.userRefreshing = false
            if (data["exit code"] !== 0 && data["exit code"] !== "0") {
                // Keep last-known values on transient failures.
                if (root.noData())
                    root.errorMsg = "exec"
                return
            }
            try {
                const p = JSON.parse(data["stdout"])
                root.claude = mergeProv(root.claude, p.claude)
                root.cursor = mergeProv(root.cursor, p.cursor)
                root.grok = mergeProv(root.grok, p.grok)
                root.codex = mergeProv(root.codex, p.codex)
                root.fetchedMs = p.fetched_ms || Date.now()
                root.configError = p.config_error || ""
                const anyOk = (root.claude && root.claude.ok)
                    || (root.cursor && root.cursor.ok)
                    || (root.grok && root.grok.ok)
                    || (root.codex && root.codex.ok)
                root.errorMsg = anyOk ? "" : ((p.claude && p.claude.error)
                    || (p.cursor && p.cursor.error)
                    || (p.grok && p.grok.error)
                    || (p.codex && p.codex.error) || "empty")
            } catch (e) {
                if (root.noData())
                    root.errorMsg = "parse"
            }
        }
        function poll() {
            if (connectedSources.length) {
                // A run that outlived every timeout in the fetcher (a socket
                // trickling bytes, say) would hold the source and stall every
                // later poll; drop it and let the next tick start a fresh one.
                if (root.nowMs - root.pollStartedMs > root.pollTimeoutMs) {
                    disconnectSource(connectedSources[0])
                    root.pollStartedMs = 0
                }
                return
            }
            root.fetching = true
            root.pollStartedMs = root.nowMs
            connectSource(root.cmd)
        }
    }

    Timer {
        interval: root.pollMs
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: exec.poll()
    }
    Timer {
        interval: 15 * 1000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.nowMs = Date.now()
    }

    // ── helpers ───────────────────────────────────────────────────────────
    function noData() {
        return !root.claude && !root.cursor && !root.grok && !root.codex
    }
    // Keep the last good reading on transient failures (429/5xx/net/exec) so a
    // blip doesn't blank a card. Replace on success or on auth/no-token errors.
    // A kept reading is aged by its own fetched_ms, the instant the fetcher took
    // it, so replaying a cached payload cannot keep it alive past
    // DEFAULT_CACHE_MAX_AGE_S the way an arrival clock would.
    function mergeProv(oldv, newv) {
        if (!newv) return oldv
        if (newv.ok) return newv
        const e = newv.error || ""
        const transient = e === "net" || e === "exec"
            || e.indexOf("429") >= 0 || e.indexOf("http-5") === 0
        if (transient && oldv && oldv.ok && oldv.fetched_ms
                && root.nowMs - oldv.fetched_ms <= root.staleKeepMs) {
            oldv.stale = true
            return oldv
        }
        return newv
    }

    function remainStr(resetMs) {
        if (!resetMs) return qsTr("n/a")
        const ms = Math.max(0, resetMs - nowMs)
        const totalMin = Math.floor(ms / 60000)
        const d = Math.floor(totalMin / 1440)
        const h = Math.floor((totalMin % 1440) / 60)
        const m = totalMin % 60
        if (d > 0) return qsTr("%1d %2h").arg(d).arg(h)
        if (h > 0) return qsTr("%1h %2m").arg(h).arg(m)
        return qsTr("%1 min").arg(m)
    }

    // A fixed "ddd h:mm AP" pattern is English: it names the weekday in
    // English, puts the meridiem after the hour, and orders date and time the
    // American way. The locale's own short date-and-time does it correctly
    // everywhere, in its own digits and script.
    function localeDateTimeStr(ms) {
        return Qt.formatDateTime(new Date(ms), Qt.DefaultLocaleShortDate)
    }

    function resetAtStr(resetMs) {
        if (!resetMs) return ""
        return localeDateTimeStr(resetMs)
    }

    // Decimal separator, digit grouping, and the digits themselves follow the
    // user's locale, so a German reading gets "12,5" and a CJK or Arabic one
    // gets its own digits.
    function numStr(n, maxDigits, minDigits) {
        const opts = { maximumFractionDigits: maxDigits }
        if (minDigits)
            opts.minimumFractionDigits = minDigits
        return Number(n).toLocaleString(Qt.locale().name, opts)
    }

    // Vendor amounts arrive as strings of unknown precision. Group and
    // decimal-separate them through the locale without inventing decimals
    // the API never reported.
    function amountStr(v) {
        const n = Number(v)
        if (v === "" || isNaN(n)) return String(v)
        return numStr(n, 2)
    }

    function utilColor(u) {
        if (u === undefined || u === null) return Kirigami.Theme.textColor
        if (u >= root.utilCritAt) return Kirigami.Theme.negativeTextColor
        if (u >= root.utilWarnAt) return Kirigami.Theme.neutralTextColor
        return Kirigami.Theme.positiveTextColor
    }

    // Severity is a text channel, not only the meter color, so it survives
    // colorblindness and high-contrast themes.
    function utilSeverity(u) {
        if (u === undefined || u === null) return qsTr("unknown")
        const n = Number(u)
        if (isNaN(n)) return qsTr("unknown")
        if (n >= root.utilCritAt) return qsTr("critical")
        if (n >= root.utilWarnAt) return qsTr("high")
        return qsTr("normal")
    }

    function pct(u) {
        if (u === undefined || u === null) return qsTr("n/a")
        const n = Number(u)
        if (isNaN(n)) return qsTr("n/a")
        return qsTr("%1%").arg(numStr(Math.round(n * 10) / 10, 1))
    }

    function periodSubdetail(p) {
        if (!p) return ""
        const when = p.resets_ms ? resetAtStr(p.resets_ms) : ""
        if (p.unit === "cents" && (p.used != null || p.limit != null)) {
            const spent = moneyFromCents(p.used, p.currency)
            const cap = p.limit != null
                ? moneyFromCents(p.limit, p.currency) : qsTr("no cap")
            return spent + " / " + cap + (when ? (" · " + when) : "")
        }
        if (p.used != null && p.limit != null)
            return numStr(p.used, 0) + " / " + numStr(p.limit, 0)
                + (when ? (" · " + when) : "")
        return when
    }

    // A "$" glued to an English-grouped number reads wrong in most of the
    // world: German wants "12,50 $", a locale that uses a narrow space wants
    // one, and some scripts place the code before the digits. Letting the
    // locale format the currency keeps all of that in one place.
    function moneyFromCents(cents, currency) {
        if (cents === undefined || cents === null) return qsTr("n/a")
        // Round to whole cents before scaling: a fraction of a cent would
        // otherwise reach toLocaleString as a float artifact.
        const n = Math.round(Number(cents)) / 100
        if (isNaN(n)) return qsTr("n/a")
        return n.toLocaleString(Qt.locale().name, {
            style: "currency",
            currency: currency || root.defaultCurrency,
            minimumFractionDigits: n % 1 === 0 ? 0 : 2,
            maximumFractionDigits: 2
        })
    }

    function tooltipBody() {
        const lines = []
        if (claude && claude.ok && claude.session)
            lines.push(qsTr("Claude session %1 · weekly %2")
                .arg(pct(claude.session.util))
                .arg(claude.weekly && claude.weekly[0]
                    ? pct(claude.weekly[0].util) : qsTr("n/a")))
        if (cursor && cursor.ok && cursor.periods && cursor.periods.length)
            lines.push(qsTr("Cursor %1 %2")
                .arg(cursor.plan || qsTr("usage"))
                .arg(pct(cursor.periods[0].util)))
        if (codex && codex.ok && codex.windows && codex.windows.length)
            lines.push(qsTr("Codex %1 %2")
                .arg(codex.windows[0].label || qsTr("usage"))
                .arg(pct(codex.windows[0].util)))
        if (grok && grok.ok && grok.periods) {
            // The period label is vendor data in the vendor's casing, and the
            // fallback below is a translated string: case-folding either one
            // with toLowerCase() would mangle it under a Turkish locale.
            for (let i = 0; i < grok.periods.length; i++)
                lines.push(qsTr("Grok %1 %2")
                    .arg(grok.periods[i].label || qsTr("usage"))
                    .arg(pct(grok.periods[i].util)))
        }
        if (lines.length === 0)
            return errorMsg ? statusText() : qsTr("Loading")
        return lines.join("\n")
    }

    // One vocabulary for a failure, so the banner and the provider cards never
    // label the same condition differently ("Error" vs "Rate-limited").
    function errText(code, signIn) {
        if (code === "no-token" || code === "http-401")
            return signIn
        if (code === "net") return "Network error"
        if (code === "exec") return "Offline"
        if (code === "http-429") return "Rate-limited"
        if (code === "empty") return "No provider data returned"
        if (code === "config" && root.configError !== "")
            return "Check fetcher config: " + root.configError
        return "Unavailable"
    }

    function statusText() {
        return errText(errorMsg,
            qsTr("Sign in to Claude / Cursor / Codex / Grok"))
    }

    function compactPct(u) {
        if (u === undefined || u === null) return ""
        const n = Number(u)
        if (isNaN(n)) return ""
        return qsTr("%1%").arg(numStr(Math.round(n), 0))
    }

    function topByUtil(rows) {
        if (!rows || !rows.length) return null
        let top = rows[0]
        for (let i = 1; i < rows.length; i++)
            if ((Number(rows[i].util) || 0) > (Number(top.util) || 0))
                top = rows[i]
        return top
    }

    function cursorTopPeriod() {
        if (!(cursor && cursor.ok)) return null
        return topByUtil(cursor.periods)
    }

    function grokTopPeriod() {
        if (!(grok && grok.ok)) return null
        return topByUtil(grok.periods)
    }

    function codexTopWindow() {
        if (!(codex && codex.ok)) return null
        return topByUtil(codex.windows)
    }

    function maxUtil() {
        let m = 0
        if (claude && claude.ok && claude.session && claude.session.util != null)
            m = Math.max(m, Number(claude.session.util) || 0)
        if (claude && claude.ok && claude.weekly) {
            for (let i = 0; i < claude.weekly.length; i++) {
                const u = Number(claude.weekly[i].util)
                if (isFinite(u)) m = Math.max(m, u)
            }
        }
        if (cursor && cursor.ok && cursor.periods) {
            for (let i = 0; i < cursor.periods.length; i++) {
                const u = Number(cursor.periods[i].util)
                if (isFinite(u)) m = Math.max(m, u)
            }
        }
        if (codex && codex.ok && codex.windows) {
            for (let i = 0; i < codex.windows.length; i++) {
                const u = Number(codex.windows[i].util)
                if (isFinite(u)) m = Math.max(m, u)
            }
        }
        if (grok && grok.ok && grok.periods) {
            for (let i = 0; i < grok.periods.length; i++) {
                const u = Number(grok.periods[i].util)
                if (isFinite(u)) m = Math.max(m, u)
            }
        }
        return m
    }

    // The provider the compact reading is showing, in priority order.
    function primaryMarkColor() {
        if (claude && claude.ok && claude.session) return claudeMark
        if (cursorTopPeriod()) return cursorMark
        if (codexTopWindow()) return codexMark
        if (grokTopPeriod()) return grokMark
        return Kirigami.Theme.disabledTextColor
    }

    // ── compact (panel) ───────────────────────────────────────────────────
    compactRepresentation: MouseArea {
        id: compact
        Layout.minimumWidth: compactCol.implicitWidth + Kirigami.Units.smallSpacing * 2
        Layout.preferredWidth: compactCol.implicitWidth + Kirigami.Units.smallSpacing * 2
        Layout.minimumHeight: compactCol.implicitHeight
        hoverEnabled: true
        focus: true
        activeFocusOnTab: true
        Accessible.role: Accessible.Button
        Accessible.name: qsTr("AI Quota")
        Accessible.description: root.tooltipBody()
        Keys.onSpacePressed: root.expanded = !root.expanded
        Keys.onReturnPressed: root.expanded = !root.expanded
        Keys.onEscapePressed: root.expanded = false
        onClicked: root.expanded = !root.expanded

        Rectangle {
            anchors.fill: parent
            radius: Kirigami.Units.smallRadius
            color: "transparent"
            border.width: 2
            border.color: Kirigami.Theme.focusColor
            visible: compact.activeFocus
        }

        ColumnLayout {
            id: compactCol
            anchors.centerIn: parent
            spacing: 2

            PlasmaComponents3.Label {
                Layout.alignment: Qt.AlignHCenter
                text: {
                    if (root.claude && root.claude.ok && root.claude.session) {
                        var p = compactPct(root.claude.session.util)
                        if (p) return p
                    }
                    var cp = root.cursorTopPeriod()
                    if (cp) {
                        var c = compactPct(cp.util)
                        if (c) return c
                    }
                    var xp = root.codexTopWindow()
                    if (xp) {
                        var x = compactPct(xp.util)
                        if (x) return x
                    }
                    var gp = root.grokTopPeriod()
                    if (gp) {
                        var g = compactPct(gp.util)
                        if (g) return g
                    }
                    return root.errorMsg ? "!" : "…"
                }
                color: root.firstLoad
                    ? Kirigami.Theme.neutralTextColor
                    : (root.errorMsg
                        ? Kirigami.Theme.negativeTextColor
                        : utilColor(maxUtil()))
                font.bold: true
                font.pointSize: Kirigami.Theme.defaultFont.pointSize * 1.1
                font.features: { "tnum": 1 }
                horizontalAlignment: Text.AlignHCenter
            }
            PlasmaComponents3.Label {
                Layout.alignment: Qt.AlignHCenter
                text: {
                    if (root.claude && root.claude.ok && root.claude.session)
                        return remainStr(root.claude.session.resets_ms)
                    var cp = root.cursorTopPeriod()
                    if (cp)
                        return remainStr(cp.resets_ms)
                    var xp = root.codexTopWindow()
                    if (xp)
                        return remainStr(xp.resets_ms)
                    var gp = root.grokTopPeriod()
                    if (gp)
                        return remainStr(gp.resets_ms)
                    return qsTr("quota")
                }
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                font.features: { "tnum": 1 }
                horizontalAlignment: Text.AlignHCenter
            }
            Rectangle {
                Layout.fillWidth: true
                Layout.topMargin: 1
                Layout.preferredHeight: 2
                color: root.primaryMarkColor()
            }
        }
    }

    // ── full (desktop / popup) ────────────────────────────────────────────
    fullRepresentation: Item {
        Layout.minimumWidth: Kirigami.Units.gridUnit * 18
        Layout.minimumHeight: Kirigami.Units.gridUnit * 16
        Layout.preferredWidth: Kirigami.Units.gridUnit * 20
        Layout.preferredHeight: scroll.contentHeight
            + Kirigami.Units.largeSpacing * 2

        PlasmaComponents3.ScrollView {
            id: scroll
            anchors.fill: parent
            anchors.margins: Kirigami.Units.largeSpacing
            contentWidth: availableWidth

            ColumnLayout {
                width: scroll.availableWidth
                spacing: Kirigami.Units.largeSpacing

                RowLayout {
                    Layout.fillWidth: true
                    Kirigami.Heading {
                        level: 2
                        text: qsTr("AI Quota")
                        Layout.fillWidth: true
                    }
                    PlasmaComponents3.ToolButton {
                        icon.name: root.gaugeView ? "view-list-details" : "speedometer"
                        text: root.gaugeView ? qsTr("List view") : qsTr("Gauge view")
                        display: PlasmaComponents3.AbstractButton.IconOnly
                        onClicked: Plasmoid.configuration.gaugeView = !root.gaugeView
                        PlasmaComponents3.ToolTip.text: root.gaugeView
                            ? qsTr("List view") : qsTr("Gauge view")
                        PlasmaComponents3.ToolTip.visible: hovered || visualFocus
                        PlasmaComponents3.ToolTip.delay: Kirigami.Units.toolTipDelay
                    }
                    PlasmaComponents3.BusyIndicator {
                        visible: root.userRefreshing
                        implicitWidth: Kirigami.Units.gridUnit
                        implicitHeight: Kirigami.Units.gridUnit
                    }
                    PlasmaComponents3.ToolButton {
                        icon.name: "view-refresh"
                        text: qsTr("Refresh")
                        display: PlasmaComponents3.AbstractButton.IconOnly
                        // A poll started while one is running is dropped by
                        // exec.poll(); disable rather than swallow the click.
                        enabled: !root.fetching
                        // A disabled button drops hover, and the tooltip is the
                        // only thing that says why the click did nothing.
                        hoverEnabled: true
                        onClicked: {
                            root.userRefreshing = true
                            exec.poll()
                        }
                        PlasmaComponents3.ToolTip.text: root.fetching
                            ? qsTr("Refreshing…") : qsTr("Refresh now")
                        PlasmaComponents3.ToolTip.visible: hovered || visualFocus
                        PlasmaComponents3.ToolTip.delay: Kirigami.Units.toolTipDelay
                    }
                }

                // First poll has not answered yet: the cards are still hidden,
                // so the view would otherwise be blank under the heading.
                PlasmaComponents3.Label {
                    visible: root.firstLoad
                    text: qsTr("Loading quota…")
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    Accessible.role: Accessible.StatusBar
                }

                // Error / empty
                PlasmaComponents3.Label {
                    visible: root.errorMsg !== ""
                        && !(root.claude && root.claude.ok)
                        && !(root.cursor && root.cursor.ok)
                        && !(root.grok && root.grok.ok)
                        && !(root.codex && root.codex.ok)
                    text: statusText()
                    color: Kirigami.Theme.negativeTextColor
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                    Accessible.role: Accessible.AlertMessage
                    Accessible.name: statusText()
                }

                // ═══════════════ Claude ═══════════════
                ProviderCard {
                    Layout.fillWidth: true
                    visible: root.claude !== null
                    title: "Claude"
                    subtitle: (root.claude && root.claude.ok && root.claude.plan)
                        ? (root.claude.plan + staleSuffix(root.claude))
                        : ((root.claude && root.claude.error)
                            ? errText(root.claude.error, qsTr("Sign in with Claude Code"))
                            : qsTr("Loading"))
                    accent: root.claudeMark
                    ok: root.claude && root.claude.ok
                    stale: !!(root.claude && root.claude.stale)

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing
                        visible: root.claude && root.claude.ok

                        PlasmaComponents3.Label {
                            visible: !root.gaugeView
                            text: qsTr("Plan usage limits")
                            font.bold: true
                            Layout.fillWidth: true
                        }

                        Flow {
                            Layout.fillWidth: true
                            Layout.preferredHeight: implicitHeight
                            spacing: root.gaugeView
                                ? Kirigami.Units.largeSpacing
                                : Kirigami.Units.smallSpacing

                            UsageRow {
                                label: qsTr("Current session")
                                util: root.claude && root.claude.session
                                    ? root.claude.session.util : null
                                detail: root.claude && root.claude.session
                                    ? qsTr("Resets in %1")
                                        .arg(remainStr(root.claude.session.resets_ms))
                                    : ""
                                subdetail: root.claude && root.claude.session
                                    ? resetAtStr(root.claude.session.resets_ms) : ""
                            }

                            Kirigami.Separator {
                                visible: !root.gaugeView
                                width: parent.width
                            }

                            PlasmaComponents3.Label {
                                visible: !root.gaugeView
                                width: parent.width
                                text: qsTr("Weekly limits")
                                font.bold: true
                            }

                            Repeater {
                                model: (root.claude && root.claude.weekly)
                                    ? root.claude.weekly : []
                                delegate: UsageRow {
                                    required property var modelData
                                    label: modelData.label || qsTr("Weekly")
                                    util: modelData.util
                                    detail: modelData.resets_ms
                                        ? qsTr("Resets in %1")
                                            .arg(remainStr(modelData.resets_ms))
                                        : ""
                                    subdetail: modelData.resets_ms
                                        ? resetAtStr(modelData.resets_ms) : ""
                                }
                            }
                        }

                        PlasmaComponents3.Label {
                            visible: !!(root.claude
                                && root.claude.extra_usage
                                && root.claude.extra_usage.enabled
                                && root.claude.extra_usage.used_credits != null)
                            text: {
                                const e = root.claude && root.claude.extra_usage
                                if (!e) return ""
                                const cur = e.currency || ""
                                const used = Number(e.used_credits)
                                // used_credits appears to already be major units
                                // (e.g. 67.63 SGD would be 6763 minor via spend)
                                const spend = root.claude.spend
                                if (spend && spend.used_minor != null) {
                                    // exponent 0 means whole units, so only a
                                    // missing one falls back to cents.
                                    const exp = spend.exponent == null ? 2 : spend.exponent
                                    const major = spend.used_minor / Math.pow(10, exp)
                                    return qsTr("Extra usage: %1 %2")
                                        .arg(numStr(major, 2, 2))
                                        .arg(spend.currency || cur)
                                }
                                return qsTr("Extra usage credits: %1 %2")
                                    .arg(amountStr(used)).arg(cur)
                            }
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                        }
                    }
                }

                // ═══════════════ Cursor ═══════════════
                ProviderCard {
                    Layout.fillWidth: true
                    visible: root.cursor !== null
                    title: "Cursor"
                    subtitle: (root.cursor && root.cursor.ok && root.cursor.plan)
                        ? (root.cursor.plan + staleSuffix(root.cursor))
                        : ((root.cursor && root.cursor.error)
                            ? errText(root.cursor.error, qsTr("Sign in to Cursor"))
                            : qsTr("Loading"))
                    accent: root.cursorMark
                    ok: root.cursor && root.cursor.ok
                    stale: !!(root.cursor && root.cursor.stale)

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing
                        visible: root.cursor && root.cursor.ok

                        PlasmaComponents3.Label {
                            visible: !!(root.cursor && root.cursor.unlimited)
                            text: qsTr("Unlimited included usage")
                            Layout.fillWidth: true
                        }

                        Flow {
                            Layout.fillWidth: true
                            Layout.preferredHeight: implicitHeight
                            visible: !!(root.cursor && root.cursor.periods
                                && root.cursor.periods.length)
                            spacing: root.gaugeView
                                ? Kirigami.Units.largeSpacing
                                : Kirigami.Units.smallSpacing

                            Repeater {
                                model: (root.cursor && root.cursor.periods)
                                    ? root.cursor.periods : []
                                delegate: UsageRow {
                                    required property var modelData
                                    label: modelData.unit === "cents"
                                        ? qsTr("%1 spend").arg(modelData.label || qsTr("usage"))
                                        : (modelData.label || qsTr("usage"))
                                    util: modelData.util
                                    detail: modelData.resets_ms
                                        ? qsTr("Resets in %1")
                                            .arg(remainStr(modelData.resets_ms))
                                        : ""
                                    subdetail: periodSubdetail(modelData)
                                }
                            }
                        }

                        PlasmaComponents3.Label {
                            visible: root.cursor && root.cursor.ok
                                && !(root.cursor.unlimited)
                                && !(root.cursor.periods && root.cursor.periods.length)
                            text: qsTr("No usage meters reported")
                            Layout.fillWidth: true
                        }
                    }
                }

                // ═══════════════ Codex ═══════════════
                ProviderCard {
                    Layout.fillWidth: true
                    visible: root.codex !== null
                    title: "Codex"
                    subtitle: (root.codex && root.codex.ok && root.codex.plan)
                        ? (root.codex.plan + staleSuffix(root.codex))
                        : ((root.codex && root.codex.error)
                            ? errText(root.codex.error, qsTr("Sign in with `codex login`"))
                            : qsTr("Loading"))
                    accent: root.codexMark
                    ok: root.codex && root.codex.ok
                    stale: !!(root.codex && root.codex.stale)

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing
                        visible: root.codex && root.codex.ok

                        PlasmaComponents3.Label {
                            visible: !root.gaugeView
                            text: qsTr("Usage limits")
                            font.bold: true
                            Layout.fillWidth: true
                        }

                        PlasmaComponents3.Label {
                            visible: root.codex && root.codex.limit_reached
                            text: qsTr("Limit reached")
                            color: Kirigami.Theme.negativeTextColor
                            font.bold: true
                            Layout.fillWidth: true
                        }

                        Flow {
                            Layout.fillWidth: true
                            Layout.preferredHeight: implicitHeight
                            visible: !!(root.codex && root.codex.windows
                                && root.codex.windows.length)
                            spacing: root.gaugeView
                                ? Kirigami.Units.largeSpacing
                                : Kirigami.Units.smallSpacing

                            Repeater {
                                model: (root.codex && root.codex.windows)
                                    ? root.codex.windows : []
                                delegate: UsageRow {
                                    required property var modelData
                                    label: modelData.label || qsTr("usage")
                                    util: modelData.util
                                    detail: modelData.resets_ms
                                        ? qsTr("Resets in %1")
                                            .arg(remainStr(modelData.resets_ms))
                                        : ""
                                    subdetail: modelData.resets_ms
                                        ? resetAtStr(modelData.resets_ms) : ""
                                }
                            }
                        }

                        PlasmaComponents3.Label {
                            visible: root.codex && root.codex.windows
                                && root.codex.windows.length === 0
                            text: qsTr("No usage meters reported")
                            Layout.fillWidth: true
                        }

                        PlasmaComponents3.Label {
                            visible: {
                                const c = root.codex && root.codex.credits
                                return c && (c.has_credits || (c.balance && c.balance !== "0"))
                            }
                            text: {
                                const c = root.codex && root.codex.credits
                                if (!c) return ""
                                if (c.unlimited) return qsTr("Credits: unlimited")
                                return qsTr("Credits balance: %1")
                                    .arg(amountStr(c.balance || 0))
                            }
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                            Layout.fillWidth: true
                        }

                        PlasmaComponents3.Label {
                            visible: {
                                const r = root.codex && root.codex.reset_credits
                                return r && r.reported
                            }
                            text: {
                                const r = root.codex && root.codex.reset_credits
                                if (!r) return ""
                                if (r.applicable > 0)
                                    return qsTr("Limit resets available: %1 (%2 usable now)")
                                        .arg(amountStr(r.available))
                                        .arg(amountStr(r.applicable))
                                return qsTr("Limit resets available: %1")
                                    .arg(amountStr(r.available))
                            }
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                            Layout.fillWidth: true
                        }
                    }
                }

                // ═══════════════ Grok ═══════════════
                ProviderCard {
                    Layout.fillWidth: true
                    visible: root.grok !== null
                    title: "Grok"
                    subtitle: (root.grok && root.grok.ok)
                        ? (qsTr("Credit limits") + staleSuffix(root.grok))
                        : ((root.grok && root.grok.error)
                            ? errText(root.grok.error, qsTr("Sign in with `grok login`"))
                            : qsTr("Loading"))
                    accent: root.grokMark
                    ok: root.grok && root.grok.ok
                    stale: !!(root.grok && root.grok.stale)

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing
                        visible: root.grok && root.grok.ok && root.grok.periods

                        Flow {
                            Layout.fillWidth: true
                            Layout.preferredHeight: implicitHeight
                            visible: !!(root.grok && root.grok.periods
                                && root.grok.periods.length)
                            spacing: root.gaugeView
                                ? Kirigami.Units.largeSpacing
                                : Kirigami.Units.smallSpacing

                            Repeater {
                                model: (root.grok && root.grok.periods)
                                    ? root.grok.periods : []
                                delegate: UsageRow {
                                    required property var modelData
                                    label: qsTr("%1 limit")
                                        .arg(modelData.label || qsTr("usage"))
                                    util: modelData.util
                                    detail: modelData.resets_ms
                                        ? qsTr("Resets in %1")
                                            .arg(remainStr(modelData.resets_ms))
                                        : ""
                                    subdetail: periodSubdetail(modelData)
                                }
                            }
                        }

                        PlasmaComponents3.Label {
                            visible: !!(root.grok && root.grok.periods
                                && root.grok.periods.length === 0)
                            text: qsTr("No usage meters reported")
                            Layout.fillWidth: true
                        }

                        PlasmaComponents3.Label {
                            visible: root.grok && root.grok.periods
                                && root.grok.periods.length > 0
                                && root.grok.periods[0].on_demand_cap > 0
                            text: {
                                const periods = root.grok && root.grok.periods
                                return periods && periods.length > 0
                                    ? qsTr("On-demand cap: %1")
                                        .arg(moneyFromCents(
                                            periods[0].on_demand_cap,
                                            periods[0].currency))
                                    : ""
                            }
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                            Layout.fillWidth: true
                        }
                    }
                }

                PlasmaComponents3.Label {
                    text: root.fetchedMs
                        ? qsTr("Updated %1").arg(localeDateTimeStr(root.fetchedMs))
                        : ""
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignRight
                    Accessible.role: Accessible.StatusBar
                }
            }
        }
    }

    // Shown on every card that kept an old reading through a transient failure.
    function staleSuffix(p) {
        return (p && p.ok && p.stale) ? " · " + qsTr("cached") : ""
    }

    // ── reusable bits ─────────────────────────────────────────────────────
    component ProviderCard: ColumnLayout {
        id: card
        property string title
        property string subtitle
        property color accent: Kirigami.Theme.highlightColor
        property bool ok: true
        property bool stale: false
        default property alias content: body.data

        spacing: Kirigami.Units.smallSpacing

        Rectangle {
            Layout.fillWidth: true
            radius: root.markThickness / 2
            height: root.markThickness
            color: card.accent
            opacity: card.ok ? 1 : root.inactiveMarkOpacity
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing
            PlasmaComponents3.Label {
                text: card.title
                font.bold: true
                font.pointSize: Kirigami.Theme.defaultFont.pointSize * 1.15
                Accessible.role: Accessible.Heading
                Accessible.name: card.title
            }
            PlasmaComponents3.Label {
                text: card.subtitle
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                // "cached" is opaque on its own: say what it means on hover.
                HoverHandler { id: subtitleHover }
                PlasmaComponents3.ToolTip.text: card.stale
                    ? "Last known reading; the latest poll failed"
                    : ""
                PlasmaComponents3.ToolTip.visible: card.stale
                    && subtitleHover.hovered
                PlasmaComponents3.ToolTip.delay: Kirigami.Units.toolTipDelay
            }
        }

        ColumnLayout {
            id: body
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing
        }
    }

    component UsageRow: ColumnLayout {
        id: row
        property string label
        property var util
        property string detail: ""
        property string subdetail: ""

        // One spoken summary per meter: children below are ignored so the
        // label, value, severity and reset time are not read twice.
        Accessible.role: Accessible.ProgressBar
        Accessible.name: row.label
        Accessible.description: [
            qsTr("%1, %2 usage").arg(pct(row.util)).arg(utilSeverity(row.util)),
            row.detail,
            row.subdetail
        ].filter(s => s !== "").join(", ")

        readonly property real frac: {
            if (row.util === undefined || row.util === null)
                return 0
            const n = Number(row.util)
            if (!isFinite(n))
                return 0
            return Math.min(1, Math.max(0, n / 100))
        }
        property real shownFrac: frac
        Behavior on shownFrac {
            NumberAnimation { duration: 280; easing.type: Easing.OutCubic }
        }

        readonly property int gaugeSize: Kirigami.Units.gridUnit * 5
        readonly property real ring: Math.max(5, gaugeSize * 0.1)
        readonly property real arcRadius: gaugeSize / 2 - ring
        readonly property real startDeg: 135
        readonly property real maxSweep: 270

        spacing: root.gaugeView ? Kirigami.Units.smallSpacing : 2
        width: root.gaugeView ? gaugeSize : (parent ? parent.width : gaugeSize)
        implicitWidth: root.gaugeView ? gaugeSize : (parent ? parent.width : gaugeSize)
        Layout.alignment: root.gaugeView ? Qt.AlignHCenter : Qt.AlignLeft

        // list (bars)
        RowLayout {
            visible: !root.gaugeView
            Layout.fillWidth: true
            PlasmaComponents3.Label {
                text: row.label
                Layout.fillWidth: true
                elide: Text.ElideRight
                Accessible.ignored: true
            }
            PlasmaComponents3.Label {
                text: pct(row.util)
                font.bold: true
                font.features: { "tnum": 1 }
                color: utilColor(row.util)
                Accessible.ignored: true
            }
        }

        Item {
            visible: !root.gaugeView
            Layout.fillWidth: true
            Layout.preferredHeight: root.barThickness

            Rectangle {
                anchors.fill: parent
                radius: root.barThickness / 2
                color: Kirigami.Theme.disabledTextColor
                opacity: root.trackOpacity
                Accessible.ignored: true
            }
            Rectangle {
                // anchors.left is a logical edge: Qt mirrors the left, right
                // and horizontalCenter anchor lines in a right-to-left
                // layout, so the meter fills from the leading edge there too.
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: parent.width * row.shownFrac
                radius: root.barThickness / 2
                color: utilColor(row.util)
            }
        }

        RowLayout {
            visible: !root.gaugeView && (row.detail !== "" || row.subdetail !== "")
            Layout.fillWidth: true
            PlasmaComponents3.Label {
                text: row.detail
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                Layout.fillWidth: true
                elide: Text.ElideRight
                Accessible.ignored: true
            }
            PlasmaComponents3.Label {
                text: row.subdetail
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                font.features: { "tnum": 1 }
                elide: Text.ElideRight
                horizontalAlignment: Text.AlignRight
                Accessible.ignored: true
            }
        }

        // gauge
        Item {
            visible: root.gaugeView
            Layout.alignment: Qt.AlignHCenter
            Layout.preferredWidth: row.gaugeSize
            Layout.preferredHeight: row.gaugeSize
            implicitWidth: row.gaugeSize
            implicitHeight: row.gaugeSize

            HoverHandler { id: gaugeHover }

            PlasmaComponents3.ToolTip.visible: gaugeHover.hovered
                && (row.detail !== "" || row.subdetail !== "")
            PlasmaComponents3.ToolTip.text: row.detail
                + (row.detail !== "" && row.subdetail !== "" ? "\n" : "")
                + row.subdetail
            PlasmaComponents3.ToolTip.delay: Kirigami.Units.toolTipDelay
            Accessible.ignored: true

            Shape {
                anchors.fill: parent
                opacity: root.trackOpacity
                ShapePath {
                    strokeWidth: row.ring
                    strokeColor: Kirigami.Theme.disabledTextColor
                    fillColor: "transparent"
                    capStyle: ShapePath.RoundCap
                    startX: row.gaugeSize / 2
                        + row.arcRadius * Math.cos(row.startDeg * Math.PI / 180)
                    startY: row.gaugeSize / 2
                        + row.arcRadius * Math.sin(row.startDeg * Math.PI / 180)
                    PathAngleArc {
                        centerX: row.gaugeSize / 2
                        centerY: row.gaugeSize / 2
                        radiusX: row.arcRadius
                        radiusY: row.arcRadius
                        startAngle: row.startDeg
                        sweepAngle: row.maxSweep
                    }
                }
            }

            Shape {
                anchors.fill: parent
                visible: row.util !== undefined && row.util !== null
                    && isFinite(Number(row.util))
                ShapePath {
                    strokeWidth: row.ring
                    strokeColor: utilColor(row.util)
                    fillColor: "transparent"
                    capStyle: ShapePath.RoundCap
                    startX: row.gaugeSize / 2
                        + row.arcRadius * Math.cos(row.startDeg * Math.PI / 180)
                    startY: row.gaugeSize / 2
                        + row.arcRadius * Math.sin(row.startDeg * Math.PI / 180)
                    PathAngleArc {
                        centerX: row.gaugeSize / 2
                        centerY: row.gaugeSize / 2
                        radiusX: row.arcRadius
                        radiusY: row.arcRadius
                        startAngle: row.startDeg
                        sweepAngle: row.maxSweep * row.shownFrac
                    }
                }
            }

            PlasmaComponents3.Label {
                anchors.centerIn: parent
                anchors.verticalCenterOffset: row.ring * 0.15
                text: pct(row.util)
                font.bold: true
                font.pointSize: Kirigami.Theme.defaultFont.pointSize * 1.05
                font.features: { "tnum": 1 }
                color: utilColor(row.util)
                Accessible.ignored: true
            }
        }

        PlasmaComponents3.Label {
            visible: root.gaugeView
            text: row.label
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
            Layout.preferredWidth: row.gaugeSize
            Accessible.ignored: true
        }

        PlasmaComponents3.Label {
            visible: root.gaugeView && row.detail !== ""
            text: row.detail
            font.pointSize: Kirigami.Theme.smallFont.pointSize
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
            Layout.preferredWidth: row.gaugeSize
            Accessible.ignored: true
        }
    }
}
