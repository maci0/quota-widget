import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.plasma5support as P5Support
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root

    readonly property string scriptPath: Qt.resolvedUrl("../code/fetch_quota.py")
        .toString().replace(/^file:\/\//, "")
    readonly property string cmd: "python3 '" + scriptPath + "'"
    // 2 min: the Claude usage endpoint rate-limits (429) on tighter polling.
    readonly property int pollMs: 120 * 1000

    property var claude: null
    property var grok: null
    property var codex: null
    property string errorMsg: ""
    property double nowMs: Date.now()
    property double fetchedMs: 0

    Plasmoid.icon: "office-chart-pie"
    toolTipMainText: "AI Quota"
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
            if (data["exit code"] !== 0 && data["exit code"] !== "0") {
                // Keep last-known values on transient failures.
                if (!root.claude && !root.grok && !root.codex)
                    root.errorMsg = "exec"
                return
            }
            try {
                const p = JSON.parse(data["stdout"])
                root.claude = mergeProv(root.claude, p.claude)
                root.grok = mergeProv(root.grok, p.grok)
                root.codex = mergeProv(root.codex, p.codex)
                root.fetchedMs = p.fetched_ms || Date.now()
                const anyOk = (root.claude && root.claude.ok)
                    || (root.grok && root.grok.ok)
                    || (root.codex && root.codex.ok)
                root.errorMsg = anyOk ? "" : ((p.claude && p.claude.error)
                    || (p.grok && p.grok.error)
                    || (p.codex && p.codex.error) || "empty")
            } catch (e) {
                if (!root.claude && !root.grok && !root.codex)
                    root.errorMsg = "parse"
            }
        }
        function poll() { connectSource(root.cmd) }
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
    // Keep the last good reading on transient failures (429/5xx/net/exec) so a
    // blip doesn't blank a card. Replace on success or on auth/no-token errors.
    function mergeProv(oldv, newv) {
        if (!newv) return oldv
        if (newv.ok) return newv
        const e = newv.error || ""
        const transient = e === "net" || e === "exec"
            || e.indexOf("429") >= 0 || e.indexOf("http-5") === 0
        if (transient && oldv && oldv.ok) return oldv
        return newv
    }

    function remainStr(resetMs) {
        if (!resetMs) return "—"
        const ms = Math.max(0, resetMs - nowMs)
        const totalMin = Math.floor(ms / 60000)
        const d = Math.floor(totalMin / 1440)
        const h = Math.floor((totalMin % 1440) / 60)
        const m = totalMin % 60
        if (d > 0) return d + "d " + h + "h"
        if (h > 0) return h + " hr " + m + " min"
        return m + " min"
    }

    function resetAtStr(resetMs) {
        if (!resetMs) return ""
        return Qt.formatDateTime(new Date(resetMs), "ddd h:mm AP")
    }

    function utilColor(u) {
        if (u === undefined || u === null) return Kirigami.Theme.textColor
        if (u >= 90) return Kirigami.Theme.negativeTextColor
        if (u >= 70) return Kirigami.Theme.neutralTextColor
        return Kirigami.Theme.positiveTextColor
    }

    function pct(u) {
        if (u === undefined || u === null) return "—"
        const n = Number(u)
        if (isNaN(n)) return "—"
        return (Math.round(n * 10) / 10) + "%"
    }

    function moneyFromCents(cents) {
        if (cents === undefined || cents === null) return "—"
        const n = Number(cents) / 100
        if (isNaN(n)) return "—"
        return "$" + n.toLocaleString(undefined, {
            minimumFractionDigits: n % 1 === 0 ? 0 : 2,
            maximumFractionDigits: 2
        })
    }

    function tooltipBody() {
        const lines = []
        if (claude && claude.ok && claude.session)
            lines.push("Claude session " + pct(claude.session.util)
                + " · weekly " + (claude.weekly && claude.weekly[0]
                    ? pct(claude.weekly[0].util) : "—"))
        if (codex && codex.ok && codex.windows && codex.windows.length)
            lines.push("Codex " + (codex.windows[0].label || "usage")
                + " " + pct(codex.windows[0].util))
        if (grok && grok.ok && grok.periods) {
            for (let i = 0; i < grok.periods.length; i++)
                lines.push("Grok " + (grok.periods[i].label || "usage").toLowerCase()
                    + " " + pct(grok.periods[i].util))
        }
        if (lines.length === 0)
            return errorMsg ? statusText() : "Loading…"
        return lines.join("\n")
    }

    function statusText() {
        if (errorMsg === "no-token" || errorMsg === "http-401")
            return "Sign in to Claude / Codex / Grok"
        if (errorMsg === "net" || errorMsg === "exec")
            return "Offline"
        return "Error"
    }

    // Grok period with the highest utilization (for the compact readout).
    function grokTopPeriod() {
        if (!(grok && grok.ok && grok.periods && grok.periods.length)) return null
        let top = grok.periods[0]
        for (let i = 1; i < grok.periods.length; i++)
            if ((Number(grok.periods[i].util) || 0) > (Number(top.util) || 0))
                top = grok.periods[i]
        return top
    }

    function maxUtil() {
        let m = 0
        if (claude && claude.ok && claude.session && claude.session.util != null)
            m = Math.max(m, Number(claude.session.util) || 0)
        if (claude && claude.ok && claude.weekly) {
            for (let i = 0; i < claude.weekly.length; i++) {
                const u = Number(claude.weekly[i].util)
                if (!isNaN(u)) m = Math.max(m, u)
            }
        }
        if (codex && codex.ok && codex.windows) {
            for (let i = 0; i < codex.windows.length; i++) {
                const u = Number(codex.windows[i].util)
                if (!isNaN(u)) m = Math.max(m, u)
            }
        }
        if (grok && grok.ok && grok.periods) {
            for (let i = 0; i < grok.periods.length; i++) {
                const u = Number(grok.periods[i].util)
                if (!isNaN(u)) m = Math.max(m, u)
            }
        }
        return m
    }

    // ── compact (panel) ───────────────────────────────────────────────────
    compactRepresentation: MouseArea {
        id: compact
        Layout.minimumWidth: compactCol.implicitWidth + Kirigami.Units.smallSpacing * 2
        Layout.preferredWidth: compactCol.implicitWidth + Kirigami.Units.smallSpacing * 2
        Layout.minimumHeight: compactCol.implicitHeight
        hoverEnabled: true
        onClicked: root.expanded = !root.expanded

        ColumnLayout {
            id: compactCol
            anchors.centerIn: parent
            spacing: 0

            PlasmaComponents3.Label {
                Layout.alignment: Qt.AlignHCenter
                text: {
                    if (root.claude && root.claude.ok && root.claude.session)
                        return Math.round(root.claude.session.util) + "%"
                    var gp = root.grokTopPeriod()
                    if (gp)
                        return Math.round(gp.util) + "%"
                    return root.errorMsg ? "!" : "…"
                }
                color: utilColor(maxUtil())
                font.bold: true
                font.pixelSize: Math.round(Kirigami.Theme.defaultFont.pixelSize * 1.1)
                font.features: { "tnum": 1 }
                horizontalAlignment: Text.AlignHCenter
            }
            PlasmaComponents3.Label {
                Layout.alignment: Qt.AlignHCenter
                text: {
                    if (root.claude && root.claude.ok && root.claude.session)
                        return remainStr(root.claude.session.resets_ms)
                    var gp = root.grokTopPeriod()
                    if (gp)
                        return remainStr(gp.resets_ms)
                    return "quota"
                }
                opacity: 0.8
                font.pixelSize: Math.round(Kirigami.Theme.smallFont.pixelSize)
                font.features: { "tnum": 1 }
                horizontalAlignment: Text.AlignHCenter
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
                        text: "AI Quota"
                        Layout.fillWidth: true
                    }
                    PlasmaComponents3.ToolButton {
                        icon.name: "view-refresh"
                        text: "Refresh"
                        display: PlasmaComponents3.AbstractButton.IconOnly
                        onClicked: exec.poll()
                        PlasmaComponents3.ToolTip.text: "Refresh now"
                        PlasmaComponents3.ToolTip.visible: hovered
                        PlasmaComponents3.ToolTip.delay: Kirigami.Units.toolTipDelay
                    }
                }

                // Error / empty
                PlasmaComponents3.Label {
                    visible: root.errorMsg !== ""
                        && !(root.claude && root.claude.ok)
                        && !(root.grok && root.grok.ok)
                        && !(root.codex && root.codex.ok)
                    text: statusText()
                    color: Kirigami.Theme.negativeTextColor
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }

                // ═══════════════ Claude ═══════════════
                ProviderCard {
                    Layout.fillWidth: true
                    visible: root.claude !== null
                    title: "Claude"
                    subtitle: (root.claude && root.claude.ok && root.claude.plan)
                        ? root.claude.plan
                        : ((root.claude && root.claude.error)
                            ? claudeErr(root.claude.error) : "…")
                    accent: "#D97757"
                    ok: root.claude && root.claude.ok

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing
                        visible: root.claude && root.claude.ok

                        // Plan usage limits header
                        PlasmaComponents3.Label {
                            text: "Plan usage limits"
                            font.bold: true
                            Layout.fillWidth: true
                        }

                        UsageRow {
                            Layout.fillWidth: true
                            label: "Current session"
                            util: root.claude && root.claude.session
                                ? root.claude.session.util : null
                            detail: root.claude && root.claude.session
                                ? ("Resets in " + remainStr(root.claude.session.resets_ms))
                                : ""
                            subdetail: root.claude && root.claude.session
                                ? resetAtStr(root.claude.session.resets_ms) : ""
                        }

                        Kirigami.Separator { Layout.fillWidth: true }

                        PlasmaComponents3.Label {
                            text: "Weekly limits"
                            font.bold: true
                            Layout.fillWidth: true
                        }

                        Repeater {
                            model: (root.claude && root.claude.weekly)
                                ? root.claude.weekly : []
                            delegate: UsageRow {
                                required property var modelData
                                Layout.fillWidth: true
                                label: modelData.label || "Weekly"
                                util: modelData.util
                                detail: modelData.resets_ms
                                    ? ("Resets " + resetAtStr(modelData.resets_ms))
                                    : ""
                                subdetail: modelData.resets_ms
                                    ? ("in " + remainStr(modelData.resets_ms)) : ""
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
                                    const exp = spend.exponent || 2
                                    const major = spend.used_minor / Math.pow(10, exp)
                                    return "Extra usage: "
                                        + major.toLocaleString(undefined, {
                                            minimumFractionDigits: 2,
                                            maximumFractionDigits: 2
                                        })
                                        + " " + (spend.currency || cur)
                                }
                                return "Extra usage credits: " + used + " " + cur
                            }
                            opacity: 0.75
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                        }
                    }
                }

                // ═══════════════ Codex ═══════════════
                ProviderCard {
                    Layout.fillWidth: true
                    visible: root.codex !== null
                    title: "Codex"
                    subtitle: (root.codex && root.codex.ok && root.codex.plan)
                        ? root.codex.plan
                        : ((root.codex && root.codex.error)
                            ? codexErr(root.codex.error) : "…")
                    accent: "#10A37F"
                    ok: root.codex && root.codex.ok

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing
                        visible: root.codex && root.codex.ok

                        PlasmaComponents3.Label {
                            text: "Usage limits"
                            font.bold: true
                            Layout.fillWidth: true
                        }

                        PlasmaComponents3.Label {
                            visible: root.codex && root.codex.limit_reached
                            text: "Limit reached"
                            color: Kirigami.Theme.negativeTextColor
                            font.bold: true
                            Layout.fillWidth: true
                        }

                        Repeater {
                            model: (root.codex && root.codex.windows)
                                ? root.codex.windows : []
                            delegate: UsageRow {
                                required property var modelData
                                Layout.fillWidth: true
                                label: modelData.label || "Usage"
                                util: modelData.util
                                detail: modelData.resets_ms
                                    ? ("Resets in " + remainStr(modelData.resets_ms))
                                    : ""
                                subdetail: modelData.resets_ms
                                    ? resetAtStr(modelData.resets_ms) : ""
                            }
                        }

                        PlasmaComponents3.Label {
                            visible: root.codex && root.codex.windows
                                && root.codex.windows.length === 0
                            text: "No active usage windows reported"
                            opacity: 0.7
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
                                if (c.unlimited) return "Credits: unlimited"
                                return "Credits balance: " + (c.balance || "0")
                            }
                            opacity: 0.75
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
                                return "Limit resets available: " + r.available
                                    + (r.applicable > 0
                                        ? (" (" + r.applicable + " usable now)")
                                        : "")
                            }
                            opacity: 0.75
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
                        ? "Credit limits"
                        : ((root.grok && root.grok.error)
                            ? grokErr(root.grok.error) : "…")
                    accent: "#1DA1F2"
                    ok: root.grok && root.grok.ok

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing
                        visible: root.grok && root.grok.ok && root.grok.periods

                        Repeater {
                            model: (root.grok && root.grok.periods)
                                ? root.grok.periods : []
                            delegate: UsageRow {
                                required property var modelData
                                Layout.fillWidth: true
                                label: (modelData.label || "Usage") + " limit"
                                util: modelData.util
                                detail: modelData.resets_ms
                                    ? ("Resets in " + remainStr(modelData.resets_ms))
                                    : ""
                                subdetail: {
                                    const when = modelData.resets_ms
                                        ? resetAtStr(modelData.resets_ms) : ""
                                    // Unified credits report percent only, no $.
                                    if (modelData.used == null && modelData.limit == null)
                                        return when
                                    return moneyFromCents(modelData.used) + " / "
                                        + moneyFromCents(modelData.limit)
                                        + (when ? (" · " + when) : "")
                                }
                            }
                        }

                        PlasmaComponents3.Label {
                            visible: root.grok && root.grok.periods
                                && root.grok.periods.length > 0
                                && root.grok.periods[0].on_demand_cap > 0
                            text: {
                                const periods = root.grok && root.grok.periods
                                return periods && periods.length > 0
                                    ? ("On-demand cap: "
                                        + moneyFromCents(periods[0].on_demand_cap))
                                    : ""
                            }
                            opacity: 0.75
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                            Layout.fillWidth: true
                        }
                    }
                }

                PlasmaComponents3.Label {
                    text: root.fetchedMs
                        ? ("Updated "
                            + Qt.formatTime(new Date(root.fetchedMs), "h:mm AP"))
                        : ""
                    opacity: 0.5
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignRight
                }
            }
        }
    }

    function claudeErr(code) {
        if (code === "no-token" || code === "http-401")
            return "Sign in with Claude Code"
        if (code === "net") return "Network error"
        if (code === "http-429") return "Rate-limited, retrying…"
        return "Unavailable"
    }
    function codexErr(code) {
        if (code === "no-token" || code === "http-401")
            return "Sign in with `codex login`"
        if (code === "net") return "Network error"
        return "Unavailable"
    }
    function grokErr(code) {
        if (code === "no-token" || code === "http-401")
            return "Sign in with `grok login`"
        if (code === "net") return "Network error"
        return "Unavailable"
    }

    // ── reusable bits ─────────────────────────────────────────────────────
    component ProviderCard: ColumnLayout {
        id: card
        property string title
        property string subtitle
        property color accent: Kirigami.Theme.highlightColor
        property bool ok: true
        default property alias content: body.data

        spacing: Kirigami.Units.smallSpacing

        Rectangle {
            Layout.fillWidth: true
            radius: 3
            height: 3
            color: card.accent
            opacity: card.ok ? 1 : 0.35
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing
            PlasmaComponents3.Label {
                text: card.title
                font.bold: true
                font.pixelSize: Math.round(Kirigami.Theme.defaultFont.pixelSize * 1.15)
            }
            PlasmaComponents3.Label {
                text: card.subtitle
                opacity: 0.7
                Layout.fillWidth: true
                elide: Text.ElideRight
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

        spacing: 2

        RowLayout {
            Layout.fillWidth: true
            PlasmaComponents3.Label {
                text: row.label
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
            PlasmaComponents3.Label {
                text: pct(row.util)
                font.bold: true
                font.features: { "tnum": 1 }
                color: utilColor(row.util)
            }
        }

        // Custom bar for clearer colouring than ProgressBar
        Item {
            Layout.fillWidth: true
            height: 8

            Rectangle {
                anchors.fill: parent
                radius: 4
                color: Kirigami.Theme.disabledTextColor
                opacity: 0.2
            }
            Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: parent.width * Math.min(1, Math.max(0, (Number(row.util) || 0) / 100))
                radius: 4
                color: utilColor(row.util)
                Behavior on width { NumberAnimation { duration: 250; easing.type: Easing.OutCubic } }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            visible: row.detail !== "" || row.subdetail !== ""
            PlasmaComponents3.Label {
                text: row.detail
                opacity: 0.7
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
            PlasmaComponents3.Label {
                text: row.subdetail
                opacity: 0.6
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                font.features: { "tnum": 1 }
                elide: Text.ElideRight
                horizontalAlignment: Text.AlignRight
            }
        }
    }
}
