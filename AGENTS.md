# Agent Rules

Inherits the universal rules in `~/.agents/AGENTS.md`. Local notes below; they do not weaken parent rules.

## What this is

KDE Plasma 6 plasmoid (`com.maci.quota-widget`). QML UI in `package/contents/ui/main.qml`. Data source in `package/contents/code/fetch_quota.py`, run by the `executable` engine `main.qml` declares, not by the manifest.

## Runtime Python

Plasmashell runs `python3 package/contents/code/fetch_quota.py`. That path is system Python on purpose: the widget has no venv at display time. Do not switch the QML command to `uv run`.

Every wall-clock read goes through `now_ms()` / `now_utc()`; `QUOTA_WIDGET_NOW_MS` pins the clock so a poll replays byte-for-byte. Call `time.time()` or `datetime.now()` directly anywhere else in the fetcher and the replay guarantee is gone. A wait that ends on a deadline (the refresh lock, currently) reads `monotonic()` and pauses through `sleep()` instead: a pinned wall clock never advances, so a deadline taken through `now_ms()` would expire on the first poll and a raw `time.sleep` would cost real seconds. Nothing in the fetcher calls `time.monotonic` or `time.sleep` directly, and the one `time.time()` outside `now_ms()` is `_poll_stamp`'s fallback, which fires only when the pinned clock itself raised and plasmashell would otherwise get no JSON.

A provider timestamp without an offset is UTC. Parse it with `iso_to_utc()`, never `datetime.fromisoformat(...).timestamp()`: that resolves a naive value in plasmashell's host zone, so the same reading lands hours off outside UTC and shifts again at every DST transition.

Every credential, cache, and state file is UTF-8 JSON, read through `fetch_quota._read_text()` and written by `_atomic_write_json()`. Never `Path.read_text()` bare: plasmashell can start under a C locale where `open()` defaults to ASCII, and the merge-write fallback would then rewrite a shared store without the fields it could not decode.

Dev and CI use `uv`. The gate is at the end of this file; run it after any edit.

## Polling

The QML owns the fetcher process. One run at a time: `exec.poll()` returns early while a source is connected, `onNewData` disconnects it, and the one-shot `pollWatchdog` timer disconnects a run still out after `pollTimeoutMs` (10 min, in the token block with the other timing values) so a stalled fetch cannot wedge polling. The deadline is a QML Timer, never a difference of two `Date.now()` readings: `nowMs` is a wall clock and steps backwards on an NTP correction, and a negative elapsed time never reaches the timeout, so polling stays dead. Keep the release on every path that starts a run.

## Threads

The four providers run in one `ThreadPoolExecutor`, so every provider is a
shared-state site. `config()` publishes `_CONFIG` behind `_CONFIG_LOCK`, so a
caller that reaches a provider without `main()`'s preload still loads it once.
`_refresh_lock()` waits only on `LOCK_BUSY_ERRNOS`: a filesystem that cannot
lock (`ENOLCK`, a network mount) refreshes unguarded at once instead of
spinning out the 20 s deadline, and a `flock` never taken is never released.
Grok's two billing calls are a second pool of two, nested in the provider one
(it waits on the network, not on CPU), so the token a 401 rotates is returned
from the call that took it instead of assigned through a shared name; both
refreshes then meet the same `flock` re-read and end up on one token.
Nothing else in the fetcher mutates module state; the rest is cross-process,
through `flock`, `_atomic_write_json`, and the re-read in `_merge_write_json`.

## Layout

- `package/`: plasmoid (`metadata.json` for Plasma, `metainfo.xml` for Discover and KNewStuff, QML, fetcher, `contents/config/main.xml` defaults, `contents/icons/com.maci.quota-widget.svg`)
- `docs/THREAT_MODEL.md`: entry points, trust boundaries, assets, and the threats per boundary
- `tests/`: pytest. One file per module under test (`test_fetch_quota.py`,
  `test_main_qml.py`, `test_package_metadata.py`, `test_print_smoke.py`,
  `test_release.py`, `test_install_script.py`); `project_paths.py` is the only
  shared helper and holds `project_root()`.
- `tests/conftest.py`: the suite's sandbox. It sets `QUOTA_WIDGET_HOME` and
  `QUOTA_WIDGET_CACHE` at import, before pytest collects a test module, and
  drops them at session end, so no test module may set them itself: a
  `tearDownModule` that popped them left every module collected after it
  running against the real `~/.cache/quota-widget`, where `_account_id`
  installs the account salt. The autouse `_outside_the_real_home` check fails
  the test that leaves the fetcher's config pointing at the real home, naming
  the node id and the fields that escaped.
- `tests/test_install_script.py`: runs `install.sh` against a checkout copied
  into a temp dir, over the paths that decide a directory's fate, and through
  symlinks that name it from outside the checkout. It never runs the install
  itself, which polls four providers.
- `tests/test_fuzz_parsers.py`: seeded randomized fuzzing of the parsers fed
  untrusted input (the Cursor and Grok bodies, the Claude usage body and its
  `limits` array, the Codex rate-limit windows and reset credits, `ItemTable`
  cells, JWTs). Generators are seeded so a failure reproduces; raise
  `ITERATIONS` or move `BASE_SEED` to widen a run.
- `scripts/print_smoke.py`: prints a fetched JSON dump (`install.sh` writes
  `.scratch/smoke.json`). `install.sh` runs it as a standalone script, so it
  keeps its own `project_root()` walk instead of importing the test helper.
- `install.sh`: root symlink installer. Its root walk starts at the script
  behind whatever symlink named it, since a distro package or a link in
  `~/bin` runs it from outside the checkout.
- `.scratch/`: gitignored local scratch (never `/tmp`)

Project marker: `package/metadata.json`. Scripts walk up to that file.

`install.sh` deletes `~/.local/share/plasma/plasmoids/com.maci.quota-widget` before
symlinking, but only when that path is a symlink or holds a `metadata.json` naming
`com.maci.quota-widget`; anything else there is left alone and the run stops. The
install directory is the manifest's `KPlugin.Id`, read at run time, so a rename
takes the link, the guard, and the uninstall with it and a manifest without a
usable Id stops the run. It creates nothing group- or world-readable (`umask
077`) and does not write to the checkout except `.scratch/` and removing
`__pycache__` under `package/`.
`./install.sh --uninstall` removes the widget and keeps the cache and the plasmoid
config. Run the script only when the user asks to install, upgrade, or remove, never
as a build or test step.

## Look

`package/contents/ui/main.qml` holds the type scale, dimming steps, meter thickness, and provider marks in one token block at the top. Add a value there instead of an inline literal, so the panel, the popup, and the gauges stay on one scale.

Provider marks: Claude and Codex use their published brand color. Cursor and Grok are monochrome brands, so they use `Kirigami.Theme` neutrals. Do not invent a hue for a vendor without one.

`providerNames` in `main.qml` is the panel's roster, in panel order. Everything that has to look at every provider (the poll merge, the error pick, `noData()`) walks it instead of naming one, so a new provider is a name in that list, a `property var`, a mark, and a card.

## Accessibility

The panel reading is a `MouseArea` with an accessible role, name, description, and press action: a `MouseArea` alone is invisible to a screen reader and cannot be activated by one. Anything a hover tooltip carries has to reach the accessible tree too, since a reader never hovers and a `Label` is not focusable. The popup takes focus when it opens, and a control smaller than `minTargetPx` is a target a finger cannot land on.

A poll answers where a screen reader is not looking, so a change in `errorMsg` is announced with `Accessible.announce` (Qt 6.8; the call is guarded, as Kirigami guards it). Repeat the same wording only when it changes, since a poll runs every `pollSeconds`. Severity is a text channel (`utilSeverity`), never the meter color alone.

`tests/test_main_qml.py` holds the contract these rules name; a new control, meter, or status belongs in that class.

## Locale

Every user-facing string in `main.qml` goes through `qsTr()` with `%1`-style
placeholders, never concatenation, so a translator can reorder the sentence.
A composition is a second pattern, not a separator welded to a phrase: the
`·` between a meter's numbers and its reset time (`appendReset`), between a
plan name and its "cached" mark (`withStaleMark`), and the separator in a
meter's spoken summary (`joinSpoken`) are all `qsTr()` entries a translator
owns. A unit is a word of its own, not a letter glued to a digit: `remainStr`
takes it from `dayUnit` / `hourUnit` / `minuteUnit`, which pick a singular or
a plural entry by count, because QML's `qsTr()` carries no plural argument and
a language with four or six forms needs a string of its own for each.
Dates and times render through `Qt.DefaultLocaleShortDate`, amounts through
`Number.toLocaleString(Qt.locale().name, { style: "currency" })`, percentages
through `percentStr()` (`style: "percent"`, so the sign and its spacing are the
locale's), and counts through `numStr()`. Every value spliced with `.arg()` goes
through one of those, since a raw number keeps Latin digits. A currency code
never follows a number as plain text: `moneyStr()` formats the amount, and a
vendor value that is not an ISO 4217 code fails `isCurrencyCode()` and leaves
the bare number, since a currency style raises a `RangeError` on anything else
and a blanked label is worse than an unadorned amount. A hardcoded `"$"`, a
currency code spliced in beside a formatted number, a `"ddd h:mm AP"` format, a
`qsTr("%1%")` suffix, or a bare `toLocaleString()` with no locale argument all
render English numbering in every locale; `tests/test_main_qml.py` fails on
each of them.

`anchors.left`, `anchors.right`, and `anchors.horizontalCenter` are logical
edges in QML and mirror themselves in a right-to-left layout, so the meter fill
uses them as-is. Physical edge math does not.

The applet icon is `package/contents/icons/com.maci.quota-widget.svg`, a 270 degree gauge arc matching the in-app meter. It is a dark rim under a light fill so it reads on both panel themes; keep that pairing if it is redrawn.

## Providers

Fetcher talks to each vendor's own usage endpoint with credentials already on disk (Claude Code, Cursor IDE / cursor-agent, Codex CLI, Grok CLI). Tokens stay on the machine except those HTTPS calls.

## Caches

Two layers hold a last good reading: the fetcher writes `~/.cache/quota-widget/<provider>.json` and reads it back only through `_read_provider_cache`, and the plasmoid keeps its own copy in `mergeProv` for the same window.

- Age a value by `fetched_ms`, the instant the reading was taken, never by when the payload arrived. Replaying a cached payload must not buy a second window.
- Both layers expire at 24 h. `DEFAULT_CACHE_MAX_AGE_S` in `fetch_quota.py` (overridable through `QUOTA_WIDGET_CACHE_MAX_AGE_S`) is the one to change: the fetcher emits the effective window as `cache_max_age_s` and `main.qml` takes `staleKeepMs` from it, so an override reaches the panel. `defaultStaleKeepMs` in `main.qml` is the fallback for a payload that carries no value.
- An expired fetcher entry is deleted when it is read.
- Both layers only move forward. A reading taken earlier never replaces a newer
  one, in the fetcher (`_cache_holds_newer`) or in the panel (`mergeProv` and
  the `fetched_ms` guard in `onNewData`), so the run the poll dropped for
  outliving `pollTimeoutMs` cannot rewind the last good value when it answers
  late. In the fetcher that comparison and the write that follows it are one
  critical section (`_entry_lock`, a `flock` on `<name>.json.lock`): two runs
  that both read the older stamp before either renames, then write in the order
  they reach the rename, leave the older reading on disk. The lock lives in
  its own file because the entry is replaced by a rename, and a `flock` on the
  old inode guards nothing. `_read_provider_cache` takes it for the same
  reason: it computes the retention verdict from the entry it read and unlinks
  on it, so a poll that renamed a fresh entry in between must not lose it. A
  sidecar `.lock` file is not a reading, so `--clear-cache` leaves it, as it
  leaves `refresh.lock`.
- Entries are scoped to one account id (`_account_id`, hashed), so a second account signing in on the same machine never reads the first one's numbers. That id is text off the wire, so `_digest` normalizes it to `NORMALIZATION_FORM` (NFC) before hashing: an NFD spelling of the same account and its NFC twin are one scope, not two. The hash is keyed by the per-installation salt in `~/.cache/quota-widget/account-salt` (`_account_salt`), because an unsalted digest of a short, guessable vendor id is a lookup, not a hash. Scoping only ever compares two digests taken under the same key, so the salt never changes who reads what. The key is installed once and never replaced (`_install_salt` creates it with `O_EXCL`, so the create is the claim): two polls that reach it together cannot both write one, and the loser adopts the winner's key, which is the only key the entries already on disk were taken under. A run that mints its own instead strands every entry written by the run it raced with. The panel is scoped the same way: every provider payload carries that digest as `account`, success and failure alike, and `mergeProv` keeps a card only while the failing poll names the same digest. Without it the panel outlives the fetcher's rule and shows the previous account's plan to the next one signed in.
- The salt is the one source of entropy in a poll, and it lands in the emitted `account` of every card, so a run that has to be reproduced names it: `QUOTA_WIDGET_ACCOUNT_SALT` (64 hex characters) is the key for that run and is written nowhere. Unset, the run draws `os.urandom` and persists it beside the entries it scopes, so the digest is stable from the second poll on and different on each first one. A key already in the cache directory wins over a named one. `ReplayTest` in `tests/test_fetch_quota.py` polls with a fresh cache, which is the first-run case, and pins the clock and the key.
- The retention window is checked before the account match in `_read_provider_cache`, so an expired entry is deleted whoever asks. An entry whose account changed is otherwise never read again under the digest that scopes it, and would sit on disk past its window.
- `--clear-cache` erases the entries and the salt: the key outlives what it scopes, and an entry restored from a backup is still readable under a key that stayed behind. It runs after `load_config` and before any provider, like `--print-config`.
- The fetcher serves an entry on a rate limit, a 5xx, and a transport failure alike (`_transient_failure`); the panel treats the same three as transient. A 401 or 403 is a vendor decision and is reported as one. That classification travels in the payload as `transient` (`_failure` builds every failed provider with it), so the panel reads the rule instead of re-deriving it from the error code; adding a code without the flag would read as final and blank a card on a blip. `exec` is the panel's own condition, the one failure the fetcher never got to classify.

Nothing personal reaches a log, a cache, or the emitted JSON: no email or session token leaves the function that reads it, and a failed HTTP body is discarded rather than kept. Every line `warn()` prints, the traceback `warn_traceback()` writes, and the `config_error` the panel renders pass through `_redact`, which spells the home directory as `~` so the account name a path carries does not outlive the poll in the journal. A crash frame names a file under the checkout, so `traceback.print_exc()` is never the printer: it writes to stderr past the redaction. `--print-config` is exempt: its whole output is the resolved paths, and the operator asked for it. The provider cache holds only what the UI renders, plus the hashed account id used to scope it. README's "Data and privacy" section is the user-facing statement of this; change it with the code.

## Gate

```bash
uv sync --extra dev --locked
uv run black --check .
uv run ruff check .
uv run mypy
uv run pytest
shellcheck install.sh
```

`ruff` selects its groups in `[tool.ruff.lint]`, defect groups (bugbear, blind
except, builtin shadowing, bandit, comprehensions, datetime, type-checking
imports, raise correctness, return statements, pathlib, pylint convention and
errors, plus `PLR1714` and `PLW1510` named on their own) alongside the style
ones, and every gate step is blocking in CI. `mypy` is strict over the
fetcher, `tests/`, and `scripts/`, with `warn_unreachable` on. A
`noqa` carries its rule and a reason (`PGH` fails a bare one); the per-file
ignores in `pyproject.toml` are scoped to `tests/` and say why.

`.github/workflows/test.yml` pins each third-party action to the commit behind
its version tag; `.github/dependabot.yml` opens the bump. Do not repin one to a
floating tag. The `uv` version there is pinned, so raise it there and nowhere
else.

