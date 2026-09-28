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

    // The kcfg integers are user-editable, so each is clamped to its range
    // before use; a bad config file must not blank the widget. gaugeView is a
    // Bool and needs no clamp.
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
    // Mirrors DEFAULT_CACHE_MAX_AGE_S in package/contents/code/fetch_quota.py,
    // the value a poll reports before the fetcher has said otherwise. The
    // effective window arrives as cache_max_age_s, so setting
    // QUOTA_WIDGET_CACHE_MAX_AGE_S shortens this too instead of leaving the
    // panel holding a reading the fetcher has already dropped.
    readonly property int defaultStaleKeepMs: 24 * 60 * 60 * 1000
    property int staleKeepMs: defaultStaleKeepMs
    // How long a run may hold the data source before the poll watchdog drops
    // it. A poll reports the budget its own QUOTA_WIDGET_HTTP_TIMEOUT adds up
    // to, and the higher of that and this stands, so a timeout the default
    // cannot hold does not drop a run the fetcher was still entitled to answer.
    // maxPollTimeoutMs caps the reported number, which arrives in a payload the
    // panel trusts; a poll past it is a hung one, not a slow one.
    readonly property int defaultPollTimeoutMs: 10 * 60 * 1000
    readonly property int maxPollTimeoutMs: 30 * 60 * 1000
    property int pollTimeoutMs: defaultPollTimeoutMs

    // ── tokens ───────────────────────────────────────────────────────────
    // Meter geometry and opacity. Every view reads these so the panel, the
    // popup, and the gauges stay on one scale.
    readonly property real trackOpacity: 0.2
    readonly property real inactiveMarkOpacity: 0.35
    readonly property int markThickness: 3
    readonly property int barThickness: 8

    // The smallest edge a pointer or a finger has to land on. An icon-only
    // toolbar button is sized by its icon, which is under this (WCAG 2.5.8).
    readonly property int minTargetPx: 24

    // Type scale, as multiples of the theme body size. The panel reading is
    // the largest text the widget draws (it is the only text a panel shows),
    // a card title sits one step below it, and the number inside a gauge
    // matches the label under it. Three unrelated factors typed at three call
    // sites are what a flat hierarchy looks like, so the steps are named here.
    readonly property real panelValueScale: 1.3
    readonly property real cardTitleScale: 1.15
    readonly property real gaugeValueScale: 1.05

    // The panel's rule under the reading, and the tightest gap inside a
    // meter row.
    readonly property int ruleThickness: 2
    readonly property int tightSpacing: 2

    // Minor-unit amounts reach the UI without a currency code (the Cursor
    // usage API sends bare cents). Every number they came from is a USD
    // amount; the locale still decides the symbol, its side, and the grouping.
    readonly property string defaultCurrency: "USD"

    // How many decimal places spend.used_minor is counted in, mirroring
    // DEFAULT_SPEND_EXPONENT and its bounds in
    // package/contents/code/fetch_quota.py. A reading cached by an older
    // fetcher can still carry a wire exponent, and Math.pow(10, 1e308) is
    // Infinity: the charge would render as 0.00. Anything outside the range
    // is read as cents.
    readonly property int defaultSpendExponent: 2
    readonly property int minSpendExponent: 0
    readonly property int maxSpendExponent: 6

    // Provider marks. Claude and Codex ship a brand color; Cursor and Grok
    // are monochrome, so they get theme neutrals rather than invented hues.
    readonly property color claudeMark: "#D97757"
    readonly property color codexMark: "#10A37F"
    readonly property color cursorMark: Kirigami.Theme.textColor
    readonly property color grokMark: Kirigami.Theme.neutralTextColor

    // The roster, in panel order. A provider is one name here plus its
    // `<name>Mark` color and its `property var <name>` above; every consumer
    // that has to look at all of them (the merge, the error pick, noData)
    // walks this list, so a fifth provider is a name and a card, not a
    // rewrite of each.
    readonly property var providerNames: ["claude", "cursor", "grok", "codex"]

    property var claude: null
    property var cursor: null
    property var grok: null
    property var codex: null
    property string errorMsg: ""
    property string configError: ""
    property double nowMs: Date.now()
    property double fetchedMs: 0
    readonly property bool gaugeView: !!Plasmoid.configuration.gaugeView
    readonly property bool firstLoad: root.noData() && root.errorMsg === ""
    // A poll is in flight. exec.poll() drops a second one, so the header
    // disables refresh while any run is out; the spinner is narrower than
    // that, and marks only a run the reader started.
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
            pollWatchdog.stop()
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
                // A run dropped for outliving pollTimeoutMs can still answer
                // after the poll that replaced it. Its numbers are the older
                // ones, so merging them would rewind the cards and the age.
                if (typeof p.fetched_ms === "number" && p.fetched_ms < root.fetchedMs)
                    return
                // The window this poll reports is in force for this poll. It is
                // read before the merge, not after: mergeProv ages a kept
                // reading against staleKeepMs, so taking the new value first is
                // what makes a shortened QUOTA_WIDGET_CACHE_MAX_AGE_S drop a
                // reading on the poll that carries it instead of one poll
                // later, still holding the window it replaced.
                const keepS = p.cache_max_age_s
                root.staleKeepMs = (typeof keepS === "number" && keepS > 0)
                    ? keepS * 1000 : root.defaultStaleKeepMs
                for (let i = 0; i < root.providerNames.length; i++) {
                    const n = root.providerNames[i]
                    root[n] = mergeProv(root[n], p[n])
                }
                root.fetchedMs = Math.max(root.fetchedMs,
                    p.fetched_ms || Date.now())
                const budgetS = p.poll_timeout_s
                root.pollTimeoutMs = (typeof budgetS === "number" && budgetS > 0)
                    ? Math.min(root.maxPollTimeoutMs,
                        Math.max(root.defaultPollTimeoutMs,
                            Math.ceil(budgetS * 1000)))
                    : root.defaultPollTimeoutMs
                root.configError = p.config_error || ""
                let anyOk = false
                let firstError = ""
                for (let i = 0; i < root.providerNames.length; i++) {
                    const n = root.providerNames[i]
                    if (root[n] && root[n].ok)
                        anyOk = true
                    // First failure in roster order, the same one the
                    // hand-written chain picked.
                    else if (!firstError && p[n] && p[n].error)
                        firstError = p[n].error
                }
                root.errorMsg = anyOk ? "" : (firstError || "empty")
            } catch (e) {
                if (root.noData())
                    root.errorMsg = "parse"
            }
        }
        // A run that outlived every timeout in the fetcher (a socket
        // trickling bytes, say) would hold the source and stall every later
        // poll, so the watchdog drops it and the next tick starts a fresh one.
        function dropStalled() {
            if (!connectedSources.length)
                return
            disconnectSource(connectedSources[0])
            // The dropped run reports nothing back, so its flags would stay
            // set: the spinner turning for good and refresh disabled until
            // some later poll happened to answer.
            root.fetching = false
            root.userRefreshing = false
        }
        function poll() {
            // exec.poll() drops a second run while one is connected; the
            // watchdog is what ends that one.
            if (connectedSources.length)
                return
            root.fetching = true
            pollWatchdog.restart()
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
    // A one-shot deadline for the run in flight, on the monotonic clock a
    // QML Timer runs on. Subtracting two Date.now() readings instead let a
    // backward wall-clock step (an NTP correction, a manual set) make the
    // elapsed time negative: the drop then never fires, the source stays
    // connected, and no later poll ever starts.
    Timer {
        id: pollWatchdog
        interval: root.pollTimeoutMs
        repeat: false
        onTriggered: exec.dropStalled()
    }

    // A poll is a child process, and the other two exits (onNewData, the
    // poll-timeout drop) are the only ones that ran. Removing the widget
    // destroys this object while a run is still out: nothing else ever sees
    // that run finish, so the fetcher would outlive the QML that started it.
    Component.onDestruction: {
        const held = exec.connectedSources
        for (let i = 0; i < held.length; i++)
            exec.disconnectSource(held[i])
    }

    // ── helpers ───────────────────────────────────────────────────────────
    function noData() {
        for (let i = 0; i < root.providerNames.length; i++) {
            if (root[root.providerNames[i]])
                return false
        }
        return true
    }
    // Keep the last good reading on transient failures (429/5xx/net/exec) so a
    // blip doesn't blank a card. Replace on success or on auth/no-token errors,
    // but never with an older one: a run the poll dropped for running long can
    // still answer, and merging it would rewind a card to a past reading.
    // A kept reading is aged by its own fetched_ms, the instant the fetcher took
    // it, so replaying a cached payload cannot keep it alive past staleKeepMs,
    // the panel's copy of the fetcher's DEFAULT_CACHE_MAX_AGE_S or of the
    // cache_max_age_s a poll reported, the way an arrival clock would.
    // Only the same account's reading is kept: `account` is the digest the
    // fetcher scopes its own cache by, and a poll that names a different one
    // (or none, because the credential went away) is another account's failure,
    // not this account's. Keeping it there would show one account's plan and
    // usage to the next one signed in on the same machine.
    function mergeProv(oldv, newv) {
        if (!newv) return oldv
        if (newv.ok) {
            // A late or replayed run carries a reading older than the one held,
            // so it keeps the card instead of rewinding it to a past poll.
            if (oldv && oldv.ok && typeof oldv.fetched_ms === "number"
                    && typeof newv.fetched_ms === "number"
                    && newv.fetched_ms < oldv.fetched_ms)
                return oldv
            return newv
        }
        // The fetcher classifies the failure and says so in the payload, so the
        // rule lives in one place. Reading it back out of the error code here
        // is how a code added later comes out final and blanks a card on a rate
        // limit. "exec" is the panel's own condition: the run produced no
        // payload at all, so the fetcher never got to classify it.
        const transient = newv.transient === true || newv.error === "exec"
        if (transient && oldv && oldv.ok && oldv.fetched_ms
                && oldv.account && oldv.account === newv.account
                && root.nowMs - oldv.fetched_ms <= root.staleKeepMs) {
            // A copy, not the kept reading itself: a property holding a JS
            // object is only re-read by a binding when the value it holds
            // changes identity, so assigning the same object back raises no
            // change signal and marking the reading in place leaves the card
            // without its "cached" badge until a later poll replaces it.
            return Object.assign({}, oldv, { stale: true })
        }
        return newv
    }

    // Counts go through numStr: .arg() would splice a JS number, so an Arabic
    // or Devanagari locale would read Latin digits next to a localized
    // percentage on the same row.
    //
    // The unit is a catalog entry of its own, not a letter welded to the
    // number. "2d 3h" is English to every reader who does not speak it, and a
    // translator who spells the unit out, writes it behind the count, or uses
    // a different script has nothing left to change. The pattern below owns
    // the order and the spacing of the four parts, so a language that writes
    // "2日 3時間" can drop the spaces and move the unit to the front.
    function remainStr(resetMs) {
        if (!resetMs) return qsTr("n/a")
        const ms = Math.max(0, resetMs - nowMs)
        const totalMin = Math.floor(ms / 60000)
        const d = Math.floor(totalMin / 1440)
        const h = Math.floor((totalMin % 1440) / 60)
        const m = totalMin % 60
        if (d > 0)
            return qsTr("%1 %2 %3 %4")
                .arg(numStr(d, 0)).arg(dayUnit(d))
                .arg(numStr(h, 0)).arg(hourUnit(h))
        if (h > 0)
            return qsTr("%1 %2 %3 %4")
                .arg(numStr(h, 0)).arg(hourUnit(h))
                .arg(numStr(m, 0)).arg(minuteUnit(m))
        return qsTr("%1 %2").arg(numStr(m, 0)).arg(minuteUnit(m))
    }

    // QML's qsTr() carries no plural argument, so the count picks the entry.
    // A language with more than two forms (Polish has four, Russian three,
    // Arabic six) gets one entry per form here, and the pattern above keeps
    // them in that language's own order.
    function dayUnit(n) { return n === 1 ? qsTr("day") : qsTr("days") }
    function hourUnit(n) { return n === 1 ? qsTr("hour") : qsTr("hours") }
    function minuteUnit(n) { return n === 1 ? qsTr("minute") : qsTr("minutes") }

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

    // Every meter states its countdown the same way, so one phrasing lives
    // here rather than in each row's delegate.
    function resetsIn(resetMs) {
        if (!resetMs) return ""
        return qsTr("Resets in %1").arg(remainStr(resetMs))
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

    // "%1%" is the English spelling: French puts a space before the sign
    // ("12 %") and a few locales lead with it. style: "percent" places the
    // sign, the space, and the digits the way the locale writes them.
    function percentStr(fraction, maxDigits) {
        return Number(fraction).toLocaleString(Qt.locale().name, {
            style: "percent",
            maximumFractionDigits: maxDigits
        })
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
        return percentStr(Math.round(n * 10) / 1000, 1)
    }

    function periodSubdetail(p) {
        if (!p) return ""
        const when = p.resets_ms ? resetAtStr(p.resets_ms) : ""
        if (p.unit === "cents" && (p.used != null || p.limit != null)) {
            const spent = moneyFromCents(p.used, p.currency)
            const cap = p.limit != null
                ? moneyFromCents(p.limit, p.currency) : qsTr("no cap")
            return appendReset(
                qsTr("%1 / %2").arg(spent).arg(cap), when)
        }
        if (p.used != null && p.limit != null)
            return appendReset(
                qsTr("%1 / %2")
                    .arg(numStr(p.used, 0)).arg(numStr(p.limit, 0)),
                when)
        return when
    }

    // The reset time rides along with a meter's numbers, and the mark between
    // them is part of a sentence the catalog has to own: a language that
    // reverses the two, drops the mark, or writes it the other way round
    // cannot do so behind a string glued together in QML.
    function appendReset(value, when) {
        if (!when) return value
        return qsTr("%1 · %2").arg(value).arg(when)
    }

    // An ISO 4217 code is three ASCII letters, and that is all a currency
    // style accepts: anything else from a vendor payload raises a RangeError
    // out of toLocaleString and leaves the label that asked for it blank.
    function isCurrencyCode(v) {
        return typeof v === "string" && /^[A-Za-z]{3}$/.test(v)
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
            currency: isCurrencyCode(currency)
                ? currency.toUpperCase() : root.defaultCurrency,
            minimumFractionDigits: n % 1 === 0 ? 0 : 2,
            maximumFractionDigits: 2
        })
    }

    // A "major unit" exponent for a minor-unit amount: how many decimal places
    // the amount is counted in. A wire value that is not a whole number in
    // range is not a scale, it is a number the card would divide by to
    // nothing, so it reads as cents.
    function spendExponent(value) {
        if (value === undefined || value === null || value === "")
            return root.defaultSpendExponent
        const n = Number(value)
        if (!isFinite(n) || n !== Math.floor(n)
                || n < root.minSpendExponent || n > root.maxSpendExponent)
            return root.defaultSpendExponent
        return n
    }

    // The providers a poll answered for but that have no reading to show. The
    // tooltip below names the ones that did answer, so without this a failed
    // provider simply vanished from the panel summary and the number on the
    // panel read as the whole of what is there.
    function failedNames() {
        const names = []
        if (claude && !claude.ok) names.push("Claude")
        if (cursor && !cursor.ok) names.push("Cursor")
        if (codex && !codex.ok) names.push("Codex")
        if (grok && !grok.ok) names.push("Grok")
        return names
    }

    // An amount in major units and the currency it is in. The code is not
    // pasted after the number here: it goes through the locale's own
    // currency format, so the symbol, its side, its spacing, and the digits
    // all follow the user. A payload that names no currency keeps the bare
    // number rather than claiming the default one is right.
    function moneyStr(amount, currency) {
        if (!isCurrencyCode(currency)) return amountStr(amount)
        return moneyFromCents(Number(amount) * 100, currency)
    }

    function tooltipBody() {
        const lines = []
        if (claude && claude.ok && claude.session)
            lines.push(qsTr("Claude session %1 · weekly %2")
                .arg(pct(claude.session.util))
                .arg(claude.weekly && claude.weekly[0]
                    ? pct(claude.weekly[0].util) : qsTr("n/a"))
                + staleSuffix(claude))
        if (cursor && cursor.ok && cursor.periods && cursor.periods.length)
            lines.push(qsTr("Cursor %1 %2")
                .arg(cursor.plan || qsTr("usage"))
                .arg(pct(cursor.periods[0].util))
                + staleSuffix(cursor))
        if (codex && codex.ok && codex.windows && codex.windows.length)
            lines.push(qsTr("Codex %1 %2")
                .arg(codex.windows[0].label || qsTr("usage"))
                .arg(pct(codex.windows[0].util))
                + staleSuffix(codex))
        if (grok && grok.ok && grok.periods) {
            // The period label is vendor data in the vendor's casing, and the
            // fallback below is a translated string: case-folding either one
            // with toLowerCase() would mangle it under a Turkish locale.
            for (let i = 0; i < grok.periods.length; i++)
                lines.push(qsTr("Grok %1 %2")
                    .arg(grok.periods[i].label || qsTr("usage"))
                    .arg(pct(grok.periods[i].util))
                    + staleSuffix(grok))
        }
        if (lines.length === 0)
            return errorMsg ? statusText() : qsTr("Loading")
        const failed = failedNames()
        if (failed.length)
            lines.push(qsTr("No reading for: %1").arg(joinNames(failed)))
        return lines.join("\n")
    }

    // One vocabulary for a failure, so the banner and the provider cards never
    // label the same condition differently ("Error" vs "Rate-limited").
    function errText(code, signIn) {
        // A 403 is a vendor decision about this account, the same one a 401
        // is, and both are answered by signing in again.
        if (code === "no-token" || code === "http-401" || code === "http-403")
            return signIn
        if (code === "net") return qsTr("Network error")
        // "exec" is a run that never produced a payload: python3 missing, the
        // fetcher unreadable, a crash on the way in. The network is not what
        // failed, so the panel must not say it did.
        if (code === "exec") return qsTr("Quota poll did not run")
        if (code === "http-429") return qsTr("Rate-limited")
        // The provider answered and the fetcher could not read the body (over
        // its size cap, empty, or not JSON). Nothing about the account is
        // wrong and nothing is known about the reading, so it is neither the
        // vendor's fault to report nor "no data" to show: the next poll is the
        // fix, the same as a dropped connection.
        if (code === "bad-body") return qsTr("Unreadable response, retrying")
        // The fetcher passes every status through as "http-<code>", so the two
        // families left get their own wording rather than one "Unavailable"
        // that reads the same whichever vendor answered and leaves the user
        // with nothing to act on. A 5xx is the vendor's server and clears on
        // the next poll, so it is stated as a wait, not a fault.
        if (code && code.indexOf("http-5") === 0)
            return qsTr("Provider unavailable, retrying")
        if (code && code.indexOf("http-4") === 0)
            return qsTr("Request rejected by the provider")
        if (code === "empty") return qsTr("No provider data returned")
        if (code === "config" && root.configError !== "")
            return qsTr("Check fetcher config: %1").arg(root.configError)
        return qsTr("Unavailable")
    }

    function statusText() {
        return errText(errorMsg,
            qsTr("Sign in to Claude / Cursor / Codex / Grok"))
    }

    function compactPct(u) {
        if (u === undefined || u === null) return ""
        const n = Number(u)
        if (isNaN(n)) return ""
        return percentStr(n / 100, 0)
    }

    // The parts of a meter's spoken summary, and the providers a panel reading
    // has no number for, joined by a catalog separator. A comma written in QML
    // is a comma in every locale, and a language that lists with a semicolon,
    // a middle dot, or a full stop cannot say so.
    function joinList(parts) {
        return parts.filter(s => s !== "").reduce(
            (a, b) => qsTr("%1, %2").arg(a).arg(b))
    }

    // The providers a poll has no reading for, as one list. The names are
    // vendor marks, but the separator between them and their order in the
    // sentence are the locale's, not a comma written here.
    function joinNames(names) {
        return names.reduce((a, b) => qsTr("%1, %2").arg(a).arg(b))
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
        const take = (u) => {
            const n = Number(u)
            if (isFinite(n)) m = Math.max(m, n)
        }
        if (claude && claude.ok && claude.session && claude.session.util != null)
            take(claude.session.util)
        const groups = []
        if (claude && claude.ok) groups.push(claude.weekly)
        if (cursor && cursor.ok) groups.push(cursor.periods)
        if (codex && codex.ok) groups.push(codex.windows)
        if (grok && grok.ok) groups.push(grok.periods)
        for (const rows of groups) {
            if (!rows) continue
            for (let i = 0; i < rows.length; i++) take(rows[i].util)
        }
        return m
    }

    // The provider the compact reading describes, in priority order: the
    // first one that has a percentage to show. The number, the countdown, and
    // the rule under them all answer this one walk, so a meter that carries a
    // reset time but no percentage (a Cursor block reporting a spend and no
    // cap) can never put one provider's number next to another's countdown.
    function primaryReading() {
        if (claude && claude.ok && claude.session
                && compactPct(claude.session.util) !== "")
            return { util: claude.session.util, resets_ms: claude.session.resets_ms, mark: claudeMark }
        const cp = cursorTopPeriod()
        if (cp && compactPct(cp.util) !== "")
            return { util: cp.util, resets_ms: cp.resets_ms, mark: cursorMark }
        const xp = codexTopWindow()
        if (xp && compactPct(xp.util) !== "")
            return { util: xp.util, resets_ms: xp.resets_ms, mark: codexMark }
        const gp = grokTopPeriod()
        if (gp && compactPct(gp.util) !== "")
            return { util: gp.util, resets_ms: gp.resets_ms, mark: grokMark }
        return null
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
        // The key handlers below open the widget for a sighted keyboard user.
        // An assistive technology activates the item through its accessible
        // action instead, and without one the panel button is announced and
        // then does nothing.
        Accessible.onPressAction: root.expanded = !root.expanded
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
            // The focus ring is the visual echo of a state the item above
            // already carries; it has nothing to say of its own.
            Accessible.ignored: true
        }

        ColumnLayout {
            id: compactCol
            anchors.centerIn: parent
            spacing: root.tightSpacing

            PlasmaComponents3.Label {
                Layout.alignment: Qt.AlignHCenter
                text: {
                    const p = root.primaryReading()
                    if (p)
                        return compactPct(p.util)
                    return root.errorMsg ? qsTr("n/a") : "…"
                }
                color: root.firstLoad
                    ? Kirigami.Theme.neutralTextColor
                    : (root.errorMsg
                        ? Kirigami.Theme.negativeTextColor
                        : utilColor(maxUtil()))
                font.bold: true
                font.pointSize: Kirigami.Theme.defaultFont.pointSize * root.panelValueScale
                font.features: { "tnum": 1 }
                horizontalAlignment: Text.AlignHCenter
                // The button's description is tooltipBody(), which already
                // carries these numbers, so a reader that also walked the
                // labels would hear every provider twice.
                Accessible.ignored: true
            }
            PlasmaComponents3.Label {
                Layout.alignment: Qt.AlignHCenter
                // With no provider answered there is no countdown to show, and
                // the generic "quota" label under an "n/a" would claim a
                // reading the panel does not have. The tooltip names the
                // failure; the panel says it has no number.
                visible: !(root.errorMsg !== "" && root.noData())
                text: {
                    const p = root.primaryReading()
                    if (p)
                        return remainStr(p.resets_ms)
                    return qsTr("quota")
                }
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                font.features: { "tnum": 1 }
                horizontalAlignment: Text.AlignHCenter
                Accessible.ignored: true
            }
            Rectangle {
                Layout.fillWidth: true
                Layout.topMargin: root.tightSpacing
                Layout.preferredHeight: root.ruleThickness
                color: {
                    const p = root.primaryReading()
                    return p ? p.mark : Kirigami.Theme.disabledTextColor
                }
                Accessible.ignored: true
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

        // The popup takes the focus with it (the view switch below claims it),
        // so the panel button's Escape handler is no longer on the key path:
        // a keyboard user who opened the popup had no key that closed it
        // (WCAG 2.1.2). Key events reach here from whichever control inside
        // the popup holds the focus, so one handler covers the whole view.
        Keys.onEscapePressed: {
            root.expanded = false
            // Focus left the panel button to open the popup, so hand it back to
            // where the keyboard user came from (WCAG 2.4.3).
            compact.forceActiveFocus()
        }

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
                        // The popup opens on a click, which leaves focus on
                        // the panel, so nothing inside it would be reachable
                        // by Tab. This is the first control in reading order
                        // and takes focus with the popup.
                        focus: true
                        // The two controls below are the whole of the popup's
                        // keyboard reach, so each one says it joins the tab
                        // chain rather than leaning on a control default
                        // (WCAG 2.1.1).
                        activeFocusOnTab: true
                        Layout.minimumWidth: root.minTargetPx
                        Layout.minimumHeight: root.minTargetPx
                        // The two views look identical from the button, so the
                        // state it is in is the only thing a screen reader can
                        // read (WCAG 4.1.2).
                        Accessible.role: Accessible.CheckBox
                        Accessible.checked: root.gaugeView
                        PlasmaComponents3.ToolTip.text: root.gaugeView
                            ? qsTr("List view") : qsTr("Gauge view")
                        PlasmaComponents3.ToolTip.visible: hovered || visualFocus
                        PlasmaComponents3.ToolTip.delay: Kirigami.Units.toolTipDelay
                    }
                    PlasmaComponents3.BusyIndicator {
                        // Every poll, not only the one a user asked for: the
                        // refresh button below is disabled while a run is out
                        // and a disabled item gets no hover, so the tooltip
                        // that explains the greyed button never opens. The
                        // spinner is the only thing saying a poll is running.
                        visible: root.fetching
                        implicitWidth: Kirigami.Units.gridUnit
                        implicitHeight: Kirigami.Units.gridUnit
                        // A spinner on its own says nothing over speech.
                        Accessible.role: Accessible.ProgressBar
                        Accessible.name: root.userRefreshing
                            ? qsTr("Refreshing") : qsTr("Updating")
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
                        activeFocusOnTab: true
                        Layout.minimumWidth: root.minTargetPx
                        Layout.minimumHeight: root.minTargetPx
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
                    subtitle: (root.claude && root.claude.ok)
                        ? withStaleMark(root.claude.plan || qsTr("Signed in"),
                            root.claude)
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
                                    ? resetsIn(root.claude.session.resets_ms)
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
                                    detail: resetsIn(modelData.resets_ms)
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
                                    // A missing or out-of-range exponent
                                    // reads as cents; 0 is whole units and is
                                    // kept.
                                    const exp = root.spendExponent(spend.exponent)
                                    const major = spend.used_minor / Math.pow(10, exp)
                                    return qsTr("Extra usage: %1")
                                        .arg(moneyStr(major,
                                            spend.currency || cur))
                                }
                                return qsTr("Extra usage credits: %1")
                                    .arg(moneyStr(used, cur))
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
                    subtitle: (root.cursor && root.cursor.ok)
                        ? withStaleMark(root.cursor.plan || qsTr("Signed in"),
                            root.cursor)
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
                                    detail: resetsIn(modelData.resets_ms)
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
                    subtitle: (root.codex && root.codex.ok)
                        ? withStaleMark(root.codex.plan || qsTr("Signed in"),
                            root.codex)
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
                                    detail: resetsIn(modelData.resets_ms)
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
                        ? withStaleMark(qsTr("Credit limits"), root.grok)
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
                                    detail: resetsIn(modelData.resets_ms)
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

    // The "cached" mark on a card that kept an old reading through a
    // transient failure. It and the plan name are one pattern, not a word
    // appended to a finished string: a language that leads with the mark,
    // reverses the two, or spells it out has to be able to.
    function withStaleMark(text, p) {
        if (!(p && p.ok && p.stale)) return text
        return qsTr("%1 · %2").arg(text).arg(qsTr("cached"))
    }

    // What the "cached" mark means. "cached" on its own is a word about the
    // fetch, not about the number, and the subtitle is the only place it lands.
    function staleNote() {
        return qsTr("Last known reading; the latest poll failed")
    }

    // A poll ends where a screen reader is not looking: the banner and the
    // cards change in place, so a failure arrives silently (WCAG 4.1.3).
    // The same wording twice in a row is not repeated, since a poll runs
    // every pollSeconds and a rate limit outlasts several of them.
    property string announced: ""
    onErrorMsgChanged: root.announceStatus()
    onFirstLoadChanged: {
        // The first reading lands where the reader is not looking too: the
        // loading label goes away and the cards appear in silence (WCAG
        // 4.1.3). Only the way out of loading announces; the way into it is
        // the label itself, and announceStatus() would name a reading that is
        // not there yet.
        if (!root.firstLoad)
            root.announceStatus()
    }
    function announceStatus() {
        const msg = root.errorMsg === "" ? qsTr("Quota updated") : statusText()
        if (msg === root.announced)
            return
        root.announced = msg
        // Accessible.announce landed in Qt 6.8.
        if (Accessible.announce)
            Accessible.announce(msg)
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
            // A provider's colour, drawn rather than read: the title below
            // names the provider and the meters carry the numbers.
            Accessible.ignored: true
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing
            PlasmaComponents3.Label {
                text: card.title
                font.bold: true
                font.pointSize: Kirigami.Theme.defaultFont.pointSize * root.cardTitleScale
                Accessible.role: Accessible.Heading
                Accessible.name: card.title
            }
            PlasmaComponents3.Label {
                text: card.subtitle
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                // "cached" is opaque on its own: say what it means on hover,
                // and again in the description, since a screen reader never
                // hovers and the label is not focusable.
                HoverHandler { id: subtitleHover }
                Accessible.description: card.stale ? root.staleNote() : ""
                PlasmaComponents3.ToolTip.text: card.stale ? root.staleNote() : ""
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
        Accessible.description: joinList([
            qsTr("%1, %2 usage").arg(pct(row.util)).arg(utilSeverity(row.util)),
            row.detail,
            row.subdetail
        ])

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

        spacing: root.gaugeView ? Kirigami.Units.smallSpacing : root.tightSpacing
        width: root.gaugeView ? gaugeSize : (parent ? parent.width : gaugeSize)
        implicitWidth: root.gaugeView ? gaugeSize : (parent ? parent.width : gaugeSize)
        Layout.alignment: root.gaugeView ? Qt.AlignHCenter : Qt.AlignLeft

        // bars
        RowLayout {
            visible: !root.gaugeView
            Layout.fillWidth: true
            PlasmaComponents3.Label {
                text: row.label
                Layout.fillWidth: true
                // A period label is a sentence ("Weekly limits · Opus 4.5
                // (2026-09-08 to 2026-09-15)"), and eliding it drops the part
                // that says which window the meter is. The row grows instead:
                // the popup's height follows its content, so a larger font
                // makes it taller rather than clipping the name (WCAG 1.4.4).
                wrapMode: Text.WordWrap
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
                Accessible.ignored: true
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
                wrapMode: Text.WordWrap
                Accessible.ignored: true
            }
            PlasmaComponents3.Label {
                text: row.subdetail
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                font.features: { "tnum": 1 }
                horizontalAlignment: Text.AlignRight
                Layout.preferredWidth: implicitWidth
                Layout.maximumWidth: implicitWidth
                wrapMode: Text.WordWrap
                Accessible.ignored: true
            }
        }

        // gauges
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

            GaugeArc {
                opacity: root.trackOpacity
                arcColor: Kirigami.Theme.disabledTextColor
                arcSweep: row.maxSweep
            }

            GaugeArc {
                visible: row.util !== undefined && row.util !== null
                    && isFinite(Number(row.util))
                arcColor: utilColor(row.util)
                arcSweep: row.maxSweep * row.shownFrac
            }

            PlasmaComponents3.Label {
                anchors.centerIn: parent
                anchors.verticalCenterOffset: row.ring * 0.15
                text: pct(row.util)
                font.bold: true
                font.pointSize: Kirigami.Theme.defaultFont.pointSize * root.gaugeValueScale
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
            // The countdown and the sub-detail it sits beside in list view.
            // Gauge view left the sub-detail to a hover tooltip, so the amount
            // of a money-backed meter had no visible place at all.
            visible: root.gaugeView
                && (row.detail !== "" || row.subdetail !== "")
            text: [row.detail, row.subdetail].filter(s => s !== "").join("\n")
            font.pointSize: Kirigami.Theme.smallFont.pointSize
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
            Layout.preferredWidth: row.gaugeSize
            Accessible.ignored: true
        }
    }

    // The dial is one arc drawn twice, the track at the full sweep and the
    // value at the animated fraction of it. The geometry is named in UsageRow
    // and read from there, so a change to the dial reaches both arcs at once.
    component GaugeArc: Shape {
        property color arcColor
        property real arcSweep
        anchors.fill: parent
        ShapePath {
            strokeWidth: row.ring
            strokeColor: arcColor
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
                sweepAngle: arcSweep
            }
        }
    }
}
