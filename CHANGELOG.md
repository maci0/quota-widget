# Changelog

All notable changes to the AI Quota plasmoid, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/spec/v2.0.0.html). `package/metadata.json` holds the
version Plasma displays and is the single source of truth.

The widget's own contract with a running desktop is the JSON its fetcher prints
and the `main.xml` config keys. Changes to either are listed as breaking, and a
plasmoid installed from an older package can only read what its own fetcher
prints, so a fetcher and UI shipped together never break each other.

The `[1.0.0]` and `[1.1.0]` sections below were written after those tags: the
1.1.0 tag (`release 1.1.0`) carries no changelog, so those two are a
reconstruction of the release history, not a record written at the time.

## [Unreleased]

Next release: 2.0.0. `CONTRIBUTING.md` classes a change to the fetcher JSON or
to a `main.xml` key that an older installed widget cannot read as a major, and
this window holds two of those, under Breaking below. `tests/test_release.py`
fails if a Breaking entry lands in this section without a matching next version.

### Breaking

- A `QUOTA_WIDGET_*` variable the fetcher does not read is now a configuration
  error instead of an ignored name. Before, a typo such as
  `QUOTA_WIDGET_CASH` left the poll succeeding with the intended setting
  quietly doing nothing; now the run aborts with `error: "config"` before any
  provider is called, naming the variable, and the panel shows the
  configuration failure. Every name this version reads is listed by
  `fetch_quota.py --help` and in the README table, and a test compares that
  table against the fetcher's own list of knobs, so a knob that lands in one
  and not the other fails the gate. To upgrade: remove or rename any
  `QUOTA_WIDGET_*` variable that is not in that list. A variable left over from
  a build older than the one that dropped it is the usual cause; the fetcher
  names it.
- A Cursor `403` is reported as `http-403`. Before, a rejected request was
  reported as `http-401`, which `errText()` renders as "Sign in to Cursor"; now
  it renders as "Unavailable", and a signed-out Cursor is still `http-401`. A
  consumer matching Cursor on `http-401` to mean a rejection matches
  `http-403` now.
- A Claude session whose refresh token the provider rejected is reported as
  `http-401`, not `http-429`. Before, any Claude `401` answered while a
  refresh token was present was reported as `http-429`, so a revoked session
  and a throttled refresh were one code; now the `429` label depends on the
  refresh actually being throttled, and a rejected refresh asks for a fresh
  sign-in. The card read "Rate-limited" on a revoked session until the user
  cleared a sign-in line that never came. A consumer matching Claude on
  `http-429` to mean "a refresh token was present" matches `http-401` now.
- A provider that answers with a body the fetcher cannot read (over the 4 MiB
  cap, empty, or not JSON) is reported as `bad-body`, not as the status the
  vendor sent. Before, a `200` carrying no reading behind it reached the panel
  as `http-200`, a code no HTTP client sends, and it read as final, so a card
  was blanked on a response the next poll would have read. It is `bad-body` now,
  it carries `transient` like a `5xx` or a dropped connection, and `errText()`
  renders it as "Unreadable response, retrying". A consumer matching the other
  codes (`no-token`, `net`, `http-<code>`) is unaffected.
- A redirect the fetcher refused to follow, because it would have carried the
  credential to another host, is reported as `refused`. Before, the refusal
  raised an `HTTPError`, so the transport returned the redirect status as
  though the vendor had judged the account, and a `3xx` is final: the panel
  replaced a good reading with a blank card and `errText()` read it as an
  unavailable provider. The account, the credential, and the vendor are all
  unchanged by a refusal, so it is now transient and the card is held. A
  consumer matching a failure on `http-<status>` matches `refused` instead; a
  `3xx` was never a verdict any vendor returned for an account.

### Added

- `package/metainfo.xml`, the AppStream component Plasma Discover and KNewStuff
  read. Without it the widget is a directory Plasma loads but nothing lists,
  updates, or describes: a packaged Plasma applet carries both the KPackage
  `metadata.json` and the component. The Id, name, license, homepage, and the
  released version are the values `metadata.json` already holds, and
  `tests/test_package_metadata.py` fails when the two drift.
- Every failed provider payload carries `transient`, the fetcher's own
  classification of whether a cached reading beats reporting the failure. The
  panel re-derived that rule from the error code, so a code added later read as
  final and blanked a card on a rate limit; it reads the flag now. `errText()`
  and the card subtitles are unchanged, so a consumer reading `error` alone
  sees the same codes as before.
- `QUOTA_WIDGET_HOME` relocates the home the other paths resolve against, and
  `QUOTA_WIDGET_CLAUDE_CREDENTIALS`, `QUOTA_WIDGET_CURSOR_AUTH`,
  `QUOTA_WIDGET_CURSOR_STATE_DB`, `QUOTA_WIDGET_CODEX_AUTH`, and
  `QUOTA_WIDGET_GROK_AUTH` name a credential file each. Every one is read
  through the same validated config as the rest and defaults to the path the
  vendor CLI writes, so an unset variable changes nothing. Documented in the
  README.
- `QUOTA_WIDGET_HTTP_TIMEOUT` sets the per-request timeout, 0 < s <= 300,
  default 12.
- Three `main.xml` config keys, all with a default, so a config file written by
  an older package reads them without a migration: `pollSeconds` (default 120,
  clamped to 30 to 3600), `utilWarnAt` (default 70, 1 to 99), and
  `utilCritAt` (default 90, 1 to 100). Documented in the README.
- Seeded randomized fuzzing for the parsers that read untrusted input: the
  Cursor usage-summary body, the Grok billing config, the Claude usage body and
  its `limits` array, the Codex rate-limit windows and reset credits, the Cursor
  `ItemTable` cells, and vendor JWTs (`tests/test_fuzz_parsers.py`). A failure
  prints the seed that reproduces it.
- `QUOTA_WIDGET_NOW_MS` pins the fetcher clock, so the same HTTP responses print
  byte-identical output on every run. Documented in the README, for tests and
  smoke runs.
- `QUOTA_WIDGET_ACCOUNT_SALT` names the account-salt key a run digests under, as
  64 hex characters. Without it a poll whose cache holds no key yet, which is a
  first run, a sandboxed `QUOTA_WIDGET_CACHE`, a run after `--clear-cache`, and
  a cache directory that cannot be written, drew a fresh random key and emitted
  a different `account` digest in every card, so that poll was not reproducible
  from its inputs. The named key is used and not written, an unset run still
  keeps 32 bytes of `os.urandom` beside the entries it scopes, and a key already
  in the cache directory still wins over a named one. Documented in the README,
  for tests and smoke runs.
- `docs/THREAT_MODEL.md`: entry points, trust boundaries, assets, and the
  threats that apply to each, with file references.
- `--help` on the fetcher and on `print_smoke.py`, listing each one's flags,
  the exit codes it can return, and (on the fetcher) the environment variables
  it reads. Help is answered before the environment is read, so it works on a
  broken config.
- `install.sh --uninstall` removes the installed widget and leaves the provider
  cache and the plasmoid settings in place.
- `fetch_quota.py --clear-cache` erases everything the widget keeps: the cached
  readings and the per-installation key the account digests are taken under.
  The key goes with the entries because it outlives them, and an entry restored
  from a backup is still readable under a key that stayed behind. It runs after
  the config check and before any provider, like `--print-config`.
- `install.sh --version` prints the plugin id and the released version, read from
  `package/metainfo.xml` at run time so the answer cannot drift from what
  Discover and KNewStuff list. `install.sh --help` gained a summary and now
  names every flag it accepts, like the other two scripts.

### Changed

- A cache directory that holds entries but no `account-salt` is reported in
  the journal. That is what a restore which left the key behind, or a lost key,
  looks like, and every entry in it reads as a miss, so the 429 fallback
  quietly stopped working with nothing to say why. The line fires once, on the
  poll that finds the directory in that state.
- The README "Local state" table now names every file the widget writes,
  including the ones it had left out: the per-installation account key, the two
  lock files, and the plasmoid settings file, which is the only one of them
  nothing in the widget can rebuild. A short recovery section says what a
  restore has to take with it: `account-salt` travels with the cache entries,
  since an entry whose key stayed behind is a reading the fetcher will not
  serve, and a lock file does not travel at all. Nothing in the fetcher changed.
  `StateInventoryTest` in `tests/test_fetch_quota.py` fails when a path the
  fetcher writes has no row in that table.
- The countdown under the panel reading takes its unit from the catalog
  instead of welding an English letter to a Latin digit. `2d 3h` read as
  English in every locale and left a translator nothing to change; the count
  now picks a unit word of its own (`day`/`days`, the same for hours and
  minutes) and the pattern around them owns their order, so a language that
  writes `2日 3時間` or reverses the pair can say so. English reads
  "2 days 3 hours" and "5 minutes".
- The Claude extra-usage amount is formatted as currency. It read
  "67.63 SGD", a Latin number with the code pasted after it; the locale now
  decides the symbol, its side, its spacing, and its digits. A payload that
  names no currency keeps the bare number rather than claiming the default
  one is right.
- Text a translator has to be able to reorder is one catalog pattern rather
  than a phrase assembled in QML: the `·` between a meter's numbers and its
  reset time, the one between a plan name and its "cached" mark, and the
  separator in a meter's spoken summary.
- A vendor currency that is not an ISO 4217 code falls back to the default
  currency instead of raising a `RangeError` out of `toLocaleString`, which
  blanked the label that asked for it.
- The gate lives in `scripts/gate.sh` and the workflow runs that script, so the
  step list is written once instead of in the workflow, the README, and
  `AGENTS.md` separately. The script pins `TZ=UTC` and `LC_ALL=C.UTF-8` the way
  the job does, so a local run and a CI run answer the same questions.
- `.python-version` names a full patch version. `3.12` left the interpreter
  the gate type checks and tests under at whatever the latest 3.12.x was, so a
  new patch could change what the gate says with no commit to point at.

### Fixed

- The tooltip's "No reading for:" line joined the provider names on a literal
  `", "`, a comma in every locale, and failed the gate's own rule that no
  sentence is assembled from a translated phrase and a separator written in
  QML. The names go through the catalog separator now, the one the meter's
  spoken summary already used.
- A vendor number too large for a float took the whole provider down. The gate
  every reading passes through turned an integer literal into a Python `int` of
  whatever size the body spells, and `float()` raises `OverflowError` past
  `1e308` rather than returning an infinity, so a 309-digit value in a
  well-formed `200` was never a reading that read as absent: it was an
  exception, and the panel was told the connection dropped while it held the
  last good card. A number no float can hold now reads as no number. A JSON
  integer literal past Python's digit limit is the same shape one step
  earlier, where `json.loads` raises a plain `ValueError` the parse path did
  not catch; it is a non-JSON body now, like any other it cannot read.
- A plan name off the wire is drawn as it stands, so it no longer carries the
  characters a label never needs: a bidi override that reorders the text
  beside it, a control character that moves the cursor, or a length bounded
  only by the response cap, which put a megabyte of vendor text in the cache
  entry and on the panel every poll. Claude's plan and weekly-window labels,
  Cursor's membership type, and Codex's `plan_type` are filtered and capped;
  a value left empty by the filter falls back to the name the card would show
  with no value at all. The label itself is unchanged for every plan name
  there is.
- A `Location` header or an OIDC `token_endpoint` naming a port that is not a
  number, or one past 65535, raised out of the origin comparison instead of
  naming no origin. Both URLs come off the network, and `ValueError` is neither
  an `OSError` nor a `URLError`, so it left the redirect handler and blanked the
  whole card as a transport failure. `_origin` reads the port inside the parse
  and answers `None`, which is what the redirect guard and the Grok token
  endpoint check already treat as a URL with no usable origin.
- The traceback a crashing provider writes to the session journal spelled the
  home directory out. `traceback.print_exc` writes to stderr itself, past the
  redaction every `warn` line goes through, and each frame names a file under
  the checkout, which sits under the home directory on every install path, so
  the account name in that path's first component reached the journal on every
  crash. The traceback is printed through the same redaction now.
- A Claude `spend.used.exponent` outside the decimal places an amount is
  counted in (0 to 6) was passed to the panel as the scale for the minor
  amount. A wire value like `400` made the card divide the charge by
  `Math.pow(10, 400)`, which is Infinity, so a real charge read as `0.00`, and a
  negative one inflated it by a power of ten. The fetcher reads such a value as
  cents, and the panel bounds it the same way for a reading cached by an older
  fetcher.
- A `Retry-After` of `inf`, `Infinity`, or `nan` was read as a number of
  seconds to wait: an infinity is a wait that never ends, and a NaN compares
  false against every bound. A header that names neither is no wait at all now.
- A Claude 401 served the cached reading even when the session had been revoked.
  The entry is scoped by the token's `sub`, which a revoked session still
  carries, so the cache matched and the card kept showing the last good plan for
  the whole 24 h window without ever asking the user to log in again. The reading
  now stands in only when the 401 followed a refresh the provider throttled,
  which is the expired access token and not the session, and the other three
  providers already reported a 401 as one.
- A poll's `cache_max_age_s` reached the panel one poll after the merge it
  governs, so shortening `QUOTA_WIDGET_CACHE_MAX_AGE_S` kept a reading alive for
  one more cycle under the window it replaced. The window is now in force before
  the merge reads it.
- A cached entry is stamped with `PAYLOAD_SCHEMA`, and one written under another
  value is deleted rather than replayed into a panel built for a different
  payload shape.
- The panel tooltip listed the providers with no reading joined them on a comma
  written in QML, so a language that lists with a semicolon or a full stop
  could not say so. The list goes through the same catalog separator as a
  meter's spoken summary.
- A mistyped or misplaced second argument to `install.sh` was dropped, so
  `install.sh --uninstall typo` removed the widget on a line nobody read to the
  end. A second argument is a usage error now, exit 2, and all three scripts
  answer the same line the same way. The comment describing the old behavior
  described behavior the script did not have.
- A `QUOTA_WIDGET_HTTP_TIMEOUT` near the top of its accepted range, 300 s, made
  every poll outlive the panel's ten-minute poll watchdog, so each run was
  dropped mid-flight and reported as `exec` with no payload behind it. A poll now
  reports the budget its timeout adds up to as `poll_timeout_s`, and the panel
  waits for the longer of that and its own default. The default watchdog is
  unchanged for every timeout that fitted inside it.
- A token refresh that failed for any reason left the journal with no line at
  all, so a throttled exchange and a revoked credential reached the operator as
  the same card ("no-token" or a `401`) and neither could be diagnosed without
  rerunning the fetcher by hand. The exchange now names the status it got, at
  the vendor token endpoint and for the Claude refresh that walks two of them.
- A cache directory the account key could not be written to was passed over in
  silence. The poll still worked, but a key redrawn on every run scopes every
  entry differently, so no cached reading was ever read back, and a widget that
  never kept a reading looked exactly like four providers failing every poll.
  The reason is now in the journal, as it already was for the entry write.
- `fetch_quota.py --clear-cache` raised out of `main()` on a cache directory it
  could not list, which left plasmashell with no payload at all and printed a
  traceback instead of the erasure the operator asked for. It reports the
  directory in the payload with `ok: false`, and names the reason in the
  journal.
- Giving up on a token store after three merge attempts reported "another
  writer replaced the value each time" whatever the reason was, sending the
  operator after a race that was not there when the store simply could not be
  read back. The line names the reason it stopped for.
- A Grok token refresh raised `OverflowError` when the new expiry fell past the
  last instant the calendar holds, which is what a `QUOTA_WIDGET_NOW_MS` at the
  ceiling the fetcher accepts, or a lifetime far longer than the calendar, both
  produce. On that path the exception came after the token POST had retired the
  old refresh token, so the rotated credential was never written back and the
  user was signed out of the Grok CLI with the widget showing a network error.
  The expiry sums saturate at the last representable instant now.
- The configuration error for a clock override past the representable range
  printed a range whose lower bound the next check rejected, naming the one
  value the operator must not pass as an acceptable one. It names the range it
  accepts.
- A response the fetcher reported as anything but `no-token`, `http-401`,
  `http-429`, or `net` read as "Unavailable" on both the panel and its cards: a
  `403` (a vendor decision about the account, the same one a `401` is) and a
  `500` (the vendor's own server, which the next poll usually clears) said the
  same thing, and neither said what a user could do. A `403` now asks for the
  provider's sign-in step, a `5xx` states that the provider is unavailable and
  a retry is running, and a remaining `4xx` reads as a rejected request.
- The refresh button is disabled while a poll is out, and a disabled item
  takes no hover, so the tooltip that explained it never opened and a poll
  running in the background greyed the button with nothing on screen saying
  why. The header spinner now runs for every poll, not only one a user asked
  for.
- The panel tooltip listed the providers that answered and dropped the ones
  that did not, so a failed provider disappeared from the summary without a
  trace while the popup still showed its card. The tooltip now names the
  providers it has no reading for, and marks a reading it is holding from
  cache the way its card does.
- The providers the tooltip names as unanswered were listed on a comma written
  in QML and spliced into the translated "No reading for: %1" line, so the
  catalog held half a sentence and a language that lists its separators
  differently could not be shown one. The list is joined on the same
  translated pattern a meter's spoken summary uses.
- `install.sh` stopped on its first `$HOME` expansion when the environment had
  none, which `set -u` reports in the shell's own wording. It now says which
  variable is missing, before it reads a manifest or a destination.
- Gauge view carried a meter's sub-detail (the spend of a cap, the absolute
  reset time) in a hover tooltip only, so it had no visible place at all. The
  gauge now states it under the dial, as the list row does.
- A rate-limit window whose `reset_at` or `reset_after_seconds` was a very
  large number raised `OverflowError`: the seconds-to-milliseconds product of
  any value past 1.8e305 overflows a double, and rounding the resulting
  infinity raises. Codex sends the reset as a plain number, so one response
  like that failed the whole Codex card for that poll even though its meters
  had parsed. A reset the widget cannot place on a date is now reported as an
  absent date, the same rule `iso_to_ms` already follows. The same overflow is
  fixed on the other wire-fed conversions, a Claude or Grok refresh
  `expires_in` and a JWT `exp` claim.
- The account id the cache entries are scoped by was an unsalted 16-character
  SHA-256 of a short, guessable vendor id, so a copy of the cache directory
  gave up the WorkOS user id behind every entry in it. The digest is now taken
  under a random per-installation key kept beside the entries, which changes
  who can compute the digest and nothing about which account reads which
  entry. Documented in the README.
- An expired provider cache entry was deleted only when the account that wrote
  it asked for it. Once another account was signed in, nothing read that file
  again under the digest scoping it, so a 24-hour reading could sit on disk
  indefinitely. The retention window is now checked first, so an expired entry
  goes whenever it is next read.
- Every line the fetcher wrote to stderr, and the `config_error` the panel
  shows, spelled the home directory out, so the account name in the path
  outlived the poll in the journal and on the card. They now spell it `~`.
  `--print-config` is unchanged: its whole output is the resolved paths.
- An unknown argument to the fetcher is now a usage error whatever the
  environment says. Before, the config was read first, so a typo run against a
  broken `QUOTA_WIDGET_*` printed the configuration payload and exited 0, and
  a script reading stdout never learned its argument was wrong. The argument is
  checked before the environment, exits 2, and names the offending name, so
  `--print-config extra` no longer reports `--print-config` as unknown.
- A Codex sign-out reported `http-401` with no account digest, the only failure
  of the four that did not name the account it was made for, so the panel could
  not tell this account's session ending from another account's blip. The digest
  is taken from the token in hand before the call and travels on every Codex
  failure, like the other three providers.
- A poll the panel dropped for running long can still answer after the poll
  that replaced it, and its numbers are the older ones. The provider cache and
  the panel now keep the newer reading instead of rewinding to the late one, so
  the cards and their age never go backwards.
- `install.sh` hardcoded the install directory instead of reading it from
  `package/metadata.json`, so a rename of `KPlugin.Id` would link the payload
  under a name Plasma never reads and then refuse to remove it again. The Id is
  read at run time, must be a name that can be a directory, and the check before
  `rm -rf` matches that Id field instead of the string anywhere in the file.
- `install.sh` marked `fetch_quota.py` executable on every run. Nothing execs it
  (plasmashell runs `python3 <path>`) and it is already executable in the
  checkout, so the call only dirtied the source tree and could fail the install
  on a read-only one.
- `install.sh` created the plasmoid directory and the smoke output at the
  process umask, so the fetcher's stderr (a URL, an error, a traceback) landed
  next to it world-readable while the fetcher's own files are `0600`. The
  installer now runs under `umask 077`.
- A `QUOTA_WIDGET_NOW_MS` past the year 9999 passed the config check and then
  raised `OverflowError` inside every provider, after the Codex refresh POST had
  already retired the stored refresh token. The pin is now bounded to the range
  `now_utc()` can represent, so an impossible clock is a config error like every
  other knob.
- `spend.exponent`, `spend.currency`, `extra_usage.currency`, Codex
  `rate_limit.allowed`, and the Codex credit balance reached the widget JSON with
  whatever type and value the wire held. A `NaN`, an `Infinity`, or a non-string
  currency there printed JSON plasmashell's parser rejects, blanking the view.
  Each is checked like every other number the fetcher passes on.
- A stalled fetcher run is dropped by a one-shot QML timer instead of by
  subtracting two `Date.now()` readings. The wall clock steps backwards on an
  NTP correction or a manual set, and a negative elapsed time never reached
  `pollTimeoutMs`, so the source stayed connected and no later poll ever
  started.
- A provider timestamp sent as a bare epoch number (`billingCycleEnd`,
  `resets_at`) is read in the unit it arrived in, seconds or milliseconds,
  instead of being dropped as unparsable and showing no reset at all.
- `install.sh --uninstall` named the widget settings file as
  `~/.config/plasmoids/...` whatever `XDG_CONFIG_HOME` said, so on a session
  with a custom base directory the path it told the user to keep was not the
  one plasmashell wrote.
- `install.sh` walked up from the path it was called through, so a symlink to
  it from outside the checkout (a distro package, a link in `~/bin`) reported
  `package/metadata.json not found` and installed nothing. The root walk now
  follows the symlink chain to the script, as the Python entry points already
  do with `Path(__file__).resolve()`.
- The fetcher wrote its payload and its journal through whatever encoding the
  session locale gave the streams. A plasmashell started without `LANG` on a
  host with no `C.UTF-8` left them on ASCII, where a warning naming a
  non-ASCII path or vendor text raised `UnicodeEncodeError` mid-poll and the
  panel got no payload at all. Both streams are now pinned to UTF-8 at
  startup, as `scripts/print_smoke.py` already did for its own output.
- The gate was red on a clean tree: `ruff` reported an unparameterized table
  name and a needless comprehension in the Cursor `state.vscdb` fixture. The
  table name is now checked against the names the fixture builds, so `CI` is
  green again.
- `install.sh` fetched from every provider and wrote `.scratch/` before it
  decided whether the install could go ahead. A refused install now refuses
  first, leaving the checkout and the plasmoid directory untouched.
- `install.sh` ran the fetcher with whatever `python3` was on `PATH`. Below
  3.11 (`dt.UTC`, the floor in `pyproject.toml`) the widget showed a broken
  data source with no reason given; the install now stops with the version it
  found.
- The wait for the OAuth refresh lock reads its deadline and its poll interval
  through the same seams the retry backoff already used, instead of calling
  `time.monotonic` and `time.sleep` directly. A contended poll no longer spends
  real seconds in the suite, and a replay of one is driven by an injected
  clock. A deadline read through the pinned wall clock would have expired on
  the first contended poll, since a pinned clock never advances.
- `QUOTA_WIDGET_CACHE_MAX_AGE_S` shortened the fetcher's own cache but not the
  panel's, which kept a reading on screen for 24 h regardless. Each poll now
  reports the effective window as `cache_max_age_s` and the widget ages a kept
  reading against it.
- `QUOTA_WIDGET_NOW_MS` accepted a negative value, dating every reading before
  the epoch.
- A Grok `used`/`limit` pair whose percent overflows (`100 * 1.7e308`) printed
  a bare `Infinity` in the widget JSON, which plasmashell's parser rejects, and
  a negative Cursor `limit` flipped the meter negative instead of reading as no
  ratio.
- A Grok on-demand cap of `0` was dropped for the snake_case fallback key, so a
  plan forbidden from on-demand spend reported itself as uncapped.
- A Codex credits balance or reset-credit count, and a Claude currency or spend
  exponent, no longer reach the widget JSON with a non-finite number or an
  untyped value the UI cannot format.
- `uv run ruff check .` failed on the Cursor `ItemTable` fixture in
  `tests/test_fetch_quota.py` (`S608`, `C416`), so the gate was red for
  everyone before a first change.
- A Cursor response whose `membershipType` is not a string, or whose
  `billingCycleEnd` is not a date string, raised out of the parser and blanked
  the card instead of reading as unknown.
- A `used`/`limit` pair whose ratio overflows (`1e308` over `1e-308`) printed a
  bare `Infinity` in the widget JSON, which plasmashell's parser rejects.
- A Cursor `ItemTable` cell holding a lone surrogate (`"\ud800"`) reached the
  header quote and the cache write, where the encode fails. Such a cell is
  dropped, like any other undecodable one.
- `limitType` reached the widget JSON with whatever type the wire held; only a
  string is passed on now. `isUnlimited` likewise counts only a real `true`.
- The install stops instead of deleting whatever sits at the plasmoid directory
  when that is not this widget, so a copy from Plasma Discover, a distro
  package, or a hand-unpacked archive survives an upgrade.
- Every provider payload now carries `fetched_ms`, the instant the reading was
  taken. The plasmoid ages a kept reading by that stamp instead of by when it
  arrived, so replaying a cached payload no longer resets the 24 h stale window
  and stretches the oldest reading a widget can show to two windows.
- A malformed `QUOTA_WIDGET_NOW_MS` is reported as a configuration error before
  any provider runs, instead of raising out of the emit path and leaving
  plasmashell with no JSON for that poll.
- The Codex and Grok cards mark a reading served from the cache the way the
  Claude and Cursor cards already did.
- Non-finite readings (`NaN`, `1e400`) from a provider no longer reach the JSON
  the plasmoid parses, and no longer read as a clamped 0% or a full 100%.
- Seconds-to-milliseconds conversion rounds instead of truncating, so a reset
  timestamp in whole milliseconds is not reported one millisecond early.
- Claude and Grok token expiry is read through the pinnable clock, so a replay
  run sees the same expiry decision.
- Money arriving as a floating-point cent amount rounds to the nearest cent
  rather than losing one to truncation, and a zero or negative spend limit no
  longer feeds the percent division.
- A minor-unit amount with `exponent` 0 displays in whole units instead of
  cents.
- The documented dev gate and smoke path run as written: `uv run pytest` finds
  `scripts/` on the path, and `print_smoke.py` reports a missing or malformed
  dump instead of a traceback.
- A dropped connection is retried once on a usage read, and the URL with the
  underlying error reaches the journal. A token refresh is never retried.
- A provider that raises is reported with its traceback on stderr instead of
  being passed off as a network error.
- An OAuth token that rotates but cannot be written back to disk is reported.
  Left silent, the next poll refreshed again and could sign the user out of the
  vendor CLI.
- `QUOTA_WIDGET_NOW_MS` is validated at startup: a malformed value aborts with
  `error: "config"` instead of raising mid-poll, and the Codex refresh and
  expiry checks read the pinned clock, so a replayed poll makes the same
  refresh decision on every run.
- The refresh-race, replay, and error-body tests point the fetcher's
  configuration at their temp files instead of module constants that no longer
  exist.
- The README no longer claims four network calls (there are also the OAuth token
  refreshes), a two-hour cache (the window is 24 hours), or that the account id
  is absent from the cache (it is stored as a digest).
- A fetcher run that never returns no longer blocks every later poll: the
  widget drops a run older than `pollTimeoutMs` and starts a fresh one.
- A token or cache write that fails for any reason other than an OS error
  (unserializable value, encoding error) no longer leaves a temp file in the
  token store, and the write is UTF-8 rather than the locale encoding.
- An HTTP error response is closed after its body is discarded, instead of
  relying on the process exit to release the socket.
- `ruff` passes again: a dead `point_credential` helper in the test module
  shadowed the one above it, so the gate and CI were red on a clean tree.
- An unknown argument now prints the usage line next to the error, and a
  `print_smoke.py` config error goes to stderr so the provider lines on stdout
  stay clean when piped.
- The fetcher tests bind credential paths through the environment again, so
  `uv run pytest` is green.
- A token merge-write that loses every attempt to a concurrent writer is
  reported on stderr. The write is dropped either way, so the journal is the
  only place that can say the store is being contested.
- A provider cache entry that cannot be written (a full disk, a read-only cache
  directory) is reported on stderr. It is the only fallback a later poll has
  when the vendor API fails.
- A Cursor `state.vscdb` that cannot be read for any reason other than the IDE
  holding its write lock is reported on stderr. It otherwise read as a signed
  out Cursor, and the fix was to sign in again over a db that was never
  readable.
- An unresolvable home directory is a configuration error, so the run prints
  `error: "config"` and leaves the panel a payload to read instead of raising
  out of the config load.
- `print_smoke.py` reports a dump it cannot read as a message rather than a
  traceback.
- A redirect answered by a vendor endpoint no longer carries the request's
  `Authorization` or `Cookie` header to another host. `urllib` copies the
  whole header set onto the redirected request, so a `3xx` from any of the four
  usage endpoints handed the user's access token (Claude, Codex, Grok) or
  Cursor's session cookie to whichever host it named. A redirect that stays on
  the origin is still followed; one that leaves it is reported as a failure.
- The Grok refresh token is no longer POSTed to whatever URL the OIDC discovery
  document names. The endpoint has to be `https` on `auth.x.ai`; a document
  naming another host, or the same host over plain `http`, fails the refresh
  and says so on stderr instead of forwarding the token.
- A vendor response body is read up to `MAX_RESPONSE_BYTES` (4 MB) and no
  further. The read was unbounded, so a peer that names no length and answers
  with an endless body was read into plasmashell's heap once a poll, every
  poll. An over-long body is refused and reported like any other unreadable
  one.
- The cache directory is closed up to `0700` when it already exists with a wider
  mode. `mkdir`'s mode only applies to the directory it creates, so a cache
  folder left behind by an earlier run kept whatever mode it had and left the
  readings and account digests in it readable by every local account.
- `install.sh` took its mode from the first argument and dropped the rest, so
  `--uninstall typo` removed the widget with the mistyped flag silently
  ignored. A second argument is a usage error now, naming it and exiting 2, as
  the fetcher already did for its own flags.
- The gate was red on a clean tree: `black` wanted the fetcher's argument check
  on one line.

### Changed

- The lint gate covers more defect classes: the pylint convention group, a
  repeated-equality comparison, a `subprocess.run` without an explicit `check`,
  and a debugger statement left in the source. The first two were at zero
  across the tree, so nothing was rewritten to satisfy them; the last two each
  had one site, now fixed. The `status` test in `_transient_failure` reads
  `status in {0, 429} or status >= 500`, and the installer test states its
  `check=False` rather than leaving it to the default.
- The panel keeps its providers in one roster, `providerNames`, and the poll
  merge, the error pick, and `noData()` walk it. A fifth provider is a name, a
  mark, and a card; the wording, the codes, and the payloads are unchanged.
- The panel widget can be opened by an assistive technology, not only by a
  click or a key: the compact reading carries an accessible press action, so a
  screen reader that lands on it has something to activate. The two readings it
  draws are left out of the accessible tree, since the button's description
  already carries them and they were read a second time.
- A poll that fails, and the poll that clears the failure, are announced
  (`Accessible.announce`, Qt 6.8). The banner and the cards change in place
  where a screen reader is not looking, so in the panel a rate limit or an
  expired sign-in used to arrive in silence. The same wording twice in a row
  is not repeated.
- The gauge/list switch says which view it is in (`Accessible.checked`) and
  takes focus with the popup, so the widget is reachable by keyboard once it
  opens. The two icon-only toolbar buttons meet a 24 px target.
- A card marked "cached" explains itself in its accessible description as well
  as in its hover tooltip, and the meter fill is left out of the accessible
  tree like the track beside it.
- The three text sizes the widget draws (the panel reading, a card title, the
  number inside a gauge) are named steps in the `main.qml` token block instead
  of three unrelated factors typed at their call sites, and the panel reading is
  now the largest of the three. The panel rule and the tightest gap in a meter
  row are named there too, beside `markThickness`.
- A poll that produced no payload reads "Quota poll did not run" rather than
  "Offline": a missing `python3`, an unreadable fetcher, and a crash are none of
  them a network condition.
- The panel reads "n/a" (the word a meter already uses for a missing value)
  instead of a bare "!", and drops the label under it when no provider answered,
  so it no longer claims a reading it does not have.
- `pyproject.toml` declares `[tool.uv] required-version = ">=0.12.13"`, the
  release CI pins, so a uv too old for the lockfile's revision 3 stops with a
  version message on the first command.
- The `error: "config"` payload is stamped from the real clock instead of the
  pinned one, since the clock override can be the value that failed: a pin the
  config check rejects would otherwise raise again on the emit path and leave
  plasmashell with no JSON at all. The panel sees an `int` either way. No
  release carried the field on that payload, so this narrows a shape added in
  this same window rather than changing one a user depends on.
- CI pins `actions/checkout` and `astral-sh/setup-uv` to the commit behind
  their version tag, and Dependabot (`.github/dependabot.yml`) opens the bump,
  so a moved tag can no longer change what the gate runs. The uv cache is keyed
  on `uv.lock` explicitly rather than by the action's default glob.
- The dev tools are floored at the release the tree was verified against
  (`black>=26.5,<27`, `mypy>=2.3,<3`, `pytest>=9.1,<10`, `ruff>=0.16,<1`). The
  old ranges admitted black 24 and 25, mypy 1.x, and pytest 8, so a re-lock
  could swap the formatter, type checker, or test runner under a green gate.
  Dependabot now watches the `uv` ecosystem too, and a test fails when a
  requirement loses its floor or its cap.

## [1.1.0] - 2026-09-28

### Added

- Cursor usage from the Cursor IDE session or the cursor-agent token, shown
  beside the other providers.
- A header toggle switching between bars and wrapping circular gauges.
- A payload cache in `~/.cache/quota-widget` (override with
  `QUOTA_WIDGET_CACHE`), so a provider that rate-limits keeps the last good
  reading instead of an error.

### Fixed

- The README named a 60s poll; the timer has run every 2 minutes since 1.0.0.

## [1.0.0] - 2026-09-28

### Added

- Claude, Codex, and Grok usage on the desktop and in a panel, read from each
  vendor's own endpoint with credentials already on disk.
- Per-provider sign-in line when a token is missing; the other providers keep
  updating.
