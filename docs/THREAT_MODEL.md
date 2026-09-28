# Threat model

Last reviewed: 2026-09-28, against `package/contents/code/fetch_quota.py` at
`package/metadata.json` version 1.1.0.

Scope: the plasmoid as installed on a single user session. It runs as that
user, holds that user's vendor credentials, and talks to four vendor APIs.
It listens on nothing and serves nothing, so there is no remote attacker with
a socket; every hostile input in this model is either a local file on the same
account, the environment the fetcher inherits, or a response from a vendor
endpoint.

Owner and review cadence: not recorded in this repository.

## Risk-ranked summary

| # | Risk | Boundary | State |
| --- | --- | --- | --- |
| 1 | A read of the credential or cache files leaks live OAuth tokens for four accounts | local filesystem | Mitigated by file modes, not by encryption: `FILE_MODE_PRIVATE` (`fetch_quota.py:209`) is applied to every write (`_atomic_write_json`, `fetch_quota.py:525`), the cache directory is `CACHE_DIR_MODE` (`fetch_quota.py:208`), and the refresh lock is `0600` (`fetch_quota.py:679`). Any process running as the same user can still read all of it. |
| 2 | The fetcher writes rotated OAuth tokens back into the vendor CLIs' own credential files | local filesystem to vendor CLI | Single-writer race is handled (`_merge_write_json`, `fetch_quota.py:536`; `_refresh_lock`, `fetch_quota.py:661`); a lost update there signs the user out of Claude Code, Codex, or Grok, not just the widget. |
| 3 | The OIDC token endpoint for Grok is taken from a remote discovery document | vendor HTTPS to widget | The URL is whatever `auth.x.ai` returns (`fetch_quota.py:1080`), and the refresh token is POSTed to it (`fetch_quota.py:1091`). A compromise of that host redirects a live refresh token. No allowlist. |
| 4 | A hostile local process sets `QUOTA_WIDGET_*` and repoints the fetcher at paths it controls | environment | Paths must be absolute and non-empty (`_env_path`, `fetch_quota.py:264`) and numbers are bounded (`_env_number`, `fetch_quota.py:277`; `_env_seconds`, `fetch_quota.py:307`), but nothing stops them pointing at a file the attacker wrote, and a bad value is reported, not refused mid-poll. |
| 5 | `QUOTA_WIDGET_NOW_MS` pins the clock every freshness and expiry check reads | environment | Validated only as an integer (`_pinned_ms`, `fetch_quota.py:104`). It is the one value that decides whether a cached entry counts as fresh (`_read_provider_cache`, `fetch_quota.py:617`) and whether a token counts as expired (`_claude_expired`, `fetch_quota.py:776`). Documented as tests-only, not enforced as such. |
| 6 | Vendor API responses are parsed with no schema validation | vendor HTTPS to widget | Every field is read defensively (`_as_dict`, `fetch_quota.py:77`), and numbers are coerced rather than trusted (`_finite_number`, `fetch_quota.py:144`), but a response that changes shape silently changes what the panel shows. |
| 7 | Raw vendor-derived exception text and a full traceback reach the session journal | fetcher to journal | `warn` (`fetch_quota.py:408`) writes to stderr, which Plasma captures. `_safe_fetch` also prints the traceback (`fetch_quota.py:1871`). Nothing filters vendor-controlled strings out of an exception message before it is journaled. |
| 8 | Failure reporting is thin: a successful token rotation and a dropped one look alike from outside | all | Failures are journaled (URL, status, reason). Successes are not, so "was this token written" and "which run rotated it" cannot be answered after the fact. |

Nothing here is rated critical: the process has no privilege of its own, holds
no signing key, and can only read what its own user can already read.

## Attack surface inventory

### Entry points

| Entry point | Where | Untrusted input |
| --- | --- | --- |
| Widget configuration file | `package/contents/config/main.xml`, read via `Plasmoid.configuration` and clamped in `package/contents/ui/main.qml:28` (`intSetting`) | User-editable JSON under `~/.config/plasmoids/...` |
| Environment | `load_config`, `fetch_quota.py:347`; every `QUOTA_WIDGET_*` name listed in `README.md:135-144` | Inherited from the Plasma session, not from a shell rc file |
| Pinned clock | `now_ms`, `fetch_quota.py:118`; validated at `fetch_quota.py:360` | `QUOTA_WIDGET_NOW_MS`, an integer that overrides every `now_ms()` read |
| Claude credentials | `fetch_claude`, `fetch_quota.py:917`; path from `Config.claude_cred` | `~/.claude/.credentials.json`, written by Claude Code |
| Codex credentials | `fetch_codex`, `fetch_quota.py:1440` | `~/.codex/auth.json` |
| Grok credentials | `_load_grok_auth`, `fetch_quota.py:1016` | `~/.grok/auth.json` |
| Cursor credentials | `_load_cursor_auth`, `fetch_quota.py:1669` | `~/.config/cursor/auth.json`, or a SQLite database at `~/.config/Cursor/User/globalStorage/state.vscdb` opened read-only (`_read_cursor_state_db`, `fetch_quota.py:1640`) |
| Vendor usage APIs | `fetch_http`, `fetch_quota.py:702`; URLs at `fetch_quota.py:164,171,174,195` | JSON bodies and headers from four third parties |
| OAuth token endpoints | `fetch_quota.py:166` (two Claude hosts), `fetch_quota.py:175` (OpenAI), and the Grok endpoint discovered at `fetch_quota.py:1080` | JSON bodies, plus `Retry-After` headers parsed at `parse_retry_after`, `fetch_quota.py:475` |
| CLI arguments | `main`, `fetch_quota.py:1885` | Only `--print-config` is accepted; any other argument exits 2 |
| Poll interval | `package/contents/ui/main.qml:35`, clamped to 30..3600 s | Widget setting |
| Stale window in the UI | `package/contents/ui/main.qml:43` (`staleKeepMs`) | Fixed, mirrors `DEFAULT_CACHE_MAX_AGE_S` |

### Outbound requests

Seven distinct hosts can receive a request, not four:

| Host | Request | Where |
| --- | --- | --- |
| `api.anthropic.com` | `GET /api/oauth/usage` | `fetch_quota.py:164` |
| `platform.claude.com` | `POST /v1/oauth/token` | `fetch_quota.py:167` |
| `console.anthropic.com` | `POST /v1/oauth/token` (fallback) | `fetch_quota.py:168` |
| `cursor.com` | `GET /api/usage-summary` | `fetch_quota.py:195` |
| `chatgpt.com` | `GET /backend-api/wham/usage` | `fetch_quota.py:174` |
| `auth.openai.com` | `POST /oauth/token` | `fetch_quota.py:175` |
| `cli-chat-proxy.grok.com` | `GET /v1/billing` (twice: credits and monthly) | `fetch_quota.py:171` |
| `auth.x.ai` | `GET /.well-known/openid-configuration` | `fetch_quota.py:172` |
| whatever `token_endpoint` that document names | `POST` with the Grok refresh token | `fetch_quota.py:1080`, `fetch_quota.py:1091` |

### Outbound channels

| Channel | Where | Note |
| --- | --- | --- |
| stdout, one JSON payload per run | `emit`, `fetch_quota.py:398` | The panel's only data channel; fully trusted on the way back in |
| stderr, prefixed `fetch_quota:` | `warn`, `fetch_quota.py:408` | Lands in the Plasma journal and in `.scratch/smoke.err` (`install.sh`) |
| Traceback of a provider crash | `_safe_fetch`, `fetch_quota.py:1871` | Full frames and exception text, same destination as above |

## Trust boundaries

1. **Plasmashell to fetcher.** plasmashell spawns `python3 <path>/fetch_quota.py`
   on a timer (`package/contents/ui/main.qml:152,541`) and parses its stdout as
   JSON. The fetcher's output is fully trusted by the widget: every field is
   bound to a QML property without validation (`onNewData`,
   `package/contents/ui/main.qml:98`). A hijacked fetcher binary is a code
   execution win for whoever can write into the installed package. The command
   line is assembled as a string with the script path single-quote escaped
   (`package/contents/ui/main.qml:23`), so a package directory containing a
   quote cannot break out of the argument.
2. **Fetcher to local filesystem.** Reads five credential files and writes two
   caches plus a lock. It reads with the ambient user identity; there is no
   ownership or mode check on the files it opens.
3. **Fetcher to vendor APIs.** TLS to the hosts above, bearer tokens in
   `Authorization` (Claude, Codex, Grok) or a `Cookie` header
   (`WorkosCursorSessionToken`, `fetch_quota.py:1803`). Responses are parsed
   with `json.loads` and consumed structurally.
4. **Fetcher to environment.** `QUOTA_WIDGET_*` is trusted after a value check.
5. **Fetcher to vendor CLIs.** The Claude, Codex, and Grok token stores are
   shared files that the widget writes and the CLIs also write. This is the one
   boundary where the widget has write authority over state another program owns.
6. **Fetcher to session journal.** stderr is a channel to the Plasma journal,
   which outlives the process. Anything written there is retained by the
   session, and its content is derived from vendor responses and from
   exception text built out of them.

## Assets

| Asset | Where | Impact if lost or exposed |
| --- | --- | --- |
| Claude, Codex, Grok OAuth access and refresh tokens | `~/.claude/.credentials.json`, `~/.codex/auth.json`, `~/.grok/auth.json` | Account takeover and billable spend on three vendor accounts; refresh-token reuse also signs the user out of the CLI |
| Cursor session token | Cursor `state.vscdb` and `~/.config/cursor/auth.json` | Read-only access to the Cursor account's usage; the token is not refreshed by the widget |
| Account identity | JWT `sub`, WorkOS user id, `ChatGPT-Account-Id` header | These are sent to the vendor as request context and hashed into the cache key; they never leave in the emitted JSON |
| Journal contents | Plasma journal, `.scratch/smoke.err` | Whatever a `warn` line or a traceback carried; the journal is not deleted by the widget and is readable after the session ends |
| Usage and spend figures | `~/.cache/quota-widget/*.json`, panel | Low value alone: plan name, utilization percentages, reset times, credit balances |
| Panel correctness | `package/contents/ui/main.qml` | A wrong or stale number is shown as live, which is a decision the user acts on |
| Vendor rate-limit budget | Poll interval, `Retry-After` handling | Aggressive polling gets the user rate-limited or banned by the vendor |

## Threats per boundary

### Plasmashell to fetcher

- **Tampering.** The package directory is a symlink into a checkout
  (`install.sh`, final `ln -sfn`). Anyone who can write the checkout owns the
  fetcher, and its stdout is trusted. Blast radius is the user's session.
- **Spoofing.** `onNewData` accepts any stdout that parses as JSON with a
  zero exit code; there is no signature on the output.
- **Denial of service.** A fetcher that hangs holds the widget at its previous
  reading; each request is bounded by `http_timeout_s`
  (`DEFAULT_HTTP_TIMEOUT_S`, `fetch_quota.py:206`, capped at
  `MAX_HTTP_TIMEOUT_S`, `fetch_quota.py:207`), and a run that outlives
  `pollTimeoutMs` is disconnected (`package/contents/ui/main.qml:135`).

### Fetcher to local filesystem

- **Information disclosure.** Any process running as the user can read the
  credential files and the cache. Modes are `0600`/`0700`; there is no
  encryption and no keyring.
- **Tampering.** A file that is replaced between the read and the refresh write
  is handled by the read-modify-write retry (`_merge_write_json`,
  `fetch_quota.py:536`), but the value written is whatever the fetcher holds in
  memory.
- **Repudiation.** A failed credential write is journaled (`fetch_quota.py:843`,
  `fetch_quota.py:1121`, `fetch_quota.py:1434`); a successful one is not, so a
  rotation cannot be traced to a run.
- **Denial of service.** `_merge_write_json` retries
  (`MERGE_WRITE_ATTEMPTS`, `fetch_quota.py:211`); a permanently unwritable
  cache directory degrades to no caching, not to a failed poll
  (`_write_provider_cache`, `fetch_quota.py:625`).

### Fetcher to environment

- **Spoofing / elevation of privilege.** `QUOTA_WIDGET_CLAUDE_CREDENTIALS` and
  its siblings let the caller's environment choose which file is read as the
  credential store. Validation is limited to non-empty and absolute
  (`_env_path`, `fetch_quota.py:264`); an environment-controlled path is
  trusted. Anything that can set the Plasma session environment already runs as
  the user, so this is a persistence and confusion surface rather than a
  privilege gain.
- **Tampering (clock).** `QUOTA_WIDGET_NOW_MS` (`now_ms`, `fetch_quota.py:118`)
  feeds the freshness test in `_read_provider_cache` (`fetch_quota.py:617`), the
  expiry tests in `_claude_expired` (`fetch_quota.py:776`), `_token_expired`
  (`fetch_quota.py:1047`), and `_codex_token_expired`
  (`fetch_quota.py:1373`). A value far in the future makes an expired cache
  entry read as fresh; a value far in the past makes a retired access token read
  as live. It is documented as tests-only (`README.md:144`) but nothing rejects
  it outside a test.
- **Denial of service.** Every value is read once, and an invalid one aborts the
  whole poll with `error: "config"` (`main`, `fetch_quota.py:1885`), which blanks
  all four cards at once.

### Fetcher to vendor APIs

- **Spoofing.** A hostile response is accepted as data. JSON is parsed and
  coerced, never schema-checked.
- **Tampering.** The Claude request sends Claude Code's User-Agent
  (`CLAUDE_USER_AGENT`, `fetch_quota.py:193`) to stay inside that rate-limit
  bucket. That is deliberate, and it means the widget is identified to
  Anthropic as another product's client.
- **Information disclosure.** Every request carries a live bearer token. The
  response body of a failure is drained and discarded (`fetch_http`,
  `fetch_quota.py:743`), and the fetcher emits only the fields the panel renders.
  The journal is the narrower leak: a `warn` line carries the URL and, in the
  transport-failure case, the exception repr (`fetch_quota.py:754`).
- **Denial of service.** Polling every 30 s (the minimum setting) against four
  vendors is roughly 11,500 requests a day. `Retry-After` is honoured, bounded
  to 0.5-10 s (`RETRY_AFTER_MIN_S`/`MAX_S`, `fetch_quota.py:214`,`215`), and a
  failed GET is retried once after a fixed `NETWORK_RETRY_BACKOFF_S`
  (`fetch_quota.py:219`, `fetch_quota.py:752`). There is no exponential backoff
  and no global rate limit. A persistent `http-429` still costs one request per
  poll per provider.
- **Elevation of privilege.** The Grok refresh token is POSTed to the
  `token_endpoint` from a remote discovery document with no host allowlist
  (`fetch_quota.py:1080`). Claude has a fixed two-host list
  (`fetch_quota.py:166`) and Codex a fixed one (`fetch_quota.py:175`); Grok is
  the exception.

### Fetcher to session journal

- **Information disclosure.** `warn` (`fetch_quota.py:408`) emits the message
  it is given, unfiltered. `_safe_fetch` (`fetch_quota.py:1870`) passes
  `str(exc)` from any provider, and providers build exceptions out of values
  that came from a vendor body; `fetch_http` adds `reason!r` from the transport
  (`fetch_quota.py:754`). A vendor that echoes a field the parser feeds to a
  numeric conversion or a string operation can put that field's text into the
  journal. The failure body itself is not read, so the direct path is closed;
  this is the indirect one.
- **Information disclosure (frames).** `_safe_fetch` prints the full traceback
  (`fetch_quota.py:1871`), so the journal records local paths and the exact
  call chain of a crash.
- **Repudiation.** A journal line is attributable to a run by its proximity in
  the Plasma journal, not by anything the fetcher writes. The journal is
  user-owned and is not rotated by this project.

### Fetcher to vendor CLIs (shared credential files)

- **Tampering / availability.** Two runs refreshing at once could rotate a
  single-use refresh token twice. `_refresh_lock` (`fetch_quota.py:661`) and the
  re-read under that lock are the controls; the lock is best-effort and falls
  open when `fcntl` is missing (`fetch_quota.py:672`) or the cache directory is
  unwritable (`fetch_quota.py:683`), and it breaks out on a wait deadline
  rather than blocking (`REFRESH_LOCK_WAIT_S`, `fetch_quota.py:200`).
- **Repudiation.** The CLIs rewrite the same file; nothing distinguishes a write
  by the widget from a write by the CLI.

## Mitigations

| Control | Where | Covers |
| --- | --- | --- |
| Credentials never accepted from the environment or argv | `load_config`, `fetch_quota.py:347` | Token disclosure through the process table or a session export |
| `--print-config` prints paths only | `Config.describe`, `fetch_quota.py:249` | Accidental token leak in a diagnostic |
| Atomic, fsynced, `0600` writes | `_atomic_write_json`, `fetch_quota.py:507` | Torn credential files, world-readable tokens |
| Script path single-quote escaped in the spawn command | `package/contents/ui/main.qml:23` | Argument breakout through a quote in the install path |
| Widget settings clamped before use | `intSetting`, `package/contents/ui/main.qml:28` | A malformed config file blanking the widget |
| Environment numbers bounded at load | `_env_number`, `fetch_quota.py:277`; `_env_seconds`, `fetch_quota.py:307` | A hostile or typo'd timeout or retention value |
| Refresh serialized per cache directory | `_refresh_lock`, `fetch_quota.py:661` | Double refresh-token rotation |
| Read-modify-write with re-read verification | `_merge_write_json`, `fetch_quota.py:536` | Clobbering a concurrent CLI refresh |
| Provider cache scoped to a hashed account id | `_account_id`, `fetch_quota.py:572`; `_read_provider_cache`, `fetch_quota.py:599` | One account's numbers being shown to another on a shared machine |
| Cache expiry deletes the file on read | `fetch_quota.py:619` | Retention beyond the stated window |
| Failed HTTP bodies drained and discarded | `fetch_http`, `fetch_quota.py:743` | Account identifiers echoed in an error reaching the panel or a file |
| One provider's failure cannot abort the others | `_safe_fetch`, `fetch_quota.py:1862` | Availability of the whole panel |
| Per-request timeout, bounded `Retry-After`, one GET retry | `fetch_quota.py:206`, `fetch_quota.py:214`, `fetch_quota.py:219` | A hung vendor stalling the panel indefinitely |
| Failures journaled with URL and reason, never the body | `warn`, `fetch_quota.py:408`; `fetch_http`, `fetch_quota.py:754` | An undiagnosed offline panel; a payload dumped into a log |
| Cursor state DB opened read-only via URI | `_read_cursor_state_db`, `fetch_quota.py:1643` | The fetcher mutating the IDE's own state |

### Threats with no mitigation

- **No filtering between vendor text and the journal.** Exception messages and
  tracebacks reach stderr unfiltered (`fetch_quota.py:1870`). Closing it means
  deciding what a provider may name in an error, which is a code change and not
  a documentation one.
- **No record of a successful token rotation.** Failures are journaled;
  successes are not, so the widget cannot prove which run rotated a credential.
- **No token encryption at rest.** Tokens are stored as the CLIs store them.
- **No host allowlist on the Grok token endpoint.**
- **No rate limiting** beyond the vendor's own throttling.
- **No signature or integrity check on the fetcher's stdout.**
- **No enforcement that `QUOTA_WIDGET_NOW_MS` is unset in production.** It is
  documented as tests-only and validated as an integer, nothing more.

## Abuse cases

There is no authenticated, hostile user of this software: it has one user, and
that user owns every file it touches. The abuse cases that remain are about a
second party on the same machine or on the network path:

- **Same-user process reads the cache or the token files.** Not a privilege
  boundary in Unix terms, but it is the realistic theft path: a browser
  extension, a malicious npm postinstall, or another agent run as the user
  obtains four live vendor sessions from one JSON file.
- **A shared machine with two accounts.** Mitigated by the account-scoped cache
  (`fetch_quota.py:572`); a credential that yields no account id caches
  nothing, so the failure mode is "no reading" rather than "wrong reading".
- **A poisoned environment.** A `QUOTA_WIDGET_CACHE` pointed at a directory the
  attacker controls lets them pre-seed a payload. The account id must still
  match, and the entry must be inside the freshness window, so a seeded entry
  shows for at most `DEFAULT_CACHE_MAX_AGE_S` and only to the account whose id
  it was built for. Pairing that with `QUOTA_WIDGET_NOW_MS` removes the window
  from the equation for as long as the variable stays set.
- **Vendor response shape change.** Not adversarial, but the same class: a
  renamed field degrades to a missing meter, and a hostile response with the
  same shape would be rendered the same way. A response that instead trips a
  parser lands its text in the journal via `_safe_fetch`.
- **Unbounded poll rate.** Setting `pollSeconds` to its 30 s minimum spends the
  vendor's rate-limit budget and invites an IP-level block, which affects the
  user's CLI sessions too, not just the widget.

## Security documentation state

- `README.md` "Data and privacy" is the user-facing security statement. Its
  claim that a stderr line carries the URL and the error, never a payload or an
  identifier, holds for the paths `fetch_http` writes itself. It does not cover
  the provider-crash path (`fetch_quota.py:1870`), where vendor-derived
  exception text and a traceback reach the same journal.
- There is no `SECURITY.md` in this repository: no disclosure contact, no
  supported-versions list, and no documented path from "a vulnerability is
  reported" to "a fix ships". None of that is invented here.
- `CHANGELOG.md` is the only record of what changed in a release.
