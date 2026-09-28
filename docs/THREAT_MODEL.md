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
| 1 | A read of the credential or cache files leaks live OAuth tokens for four accounts | local filesystem | Mitigated by file modes, not by encryption: `FILE_MODE_PRIVATE` (`fetch_quota.py:134`) is applied to every write (`fetch_quota.py:375`), the cache directory is `CACHE_DIR_MODE` (`fetch_quota.py:133`), and the refresh lock is `0600` (`fetch_quota.py:517`). Any process running as the same user can still read all of it. |
| 2 | The fetcher writes rotated OAuth tokens back into the vendor CLIs' own credential files | local filesystem to vendor CLI | Single-writer race is handled (`_merge_write_json`, `fetch_quota.py:386`; `_refresh_lock`, `fetch_quota.py:505`); a lost update there signs the user out of Claude Code, Codex, or Grok, not just the widget. |
| 3 | The OIDC token endpoint for Grok is taken from a remote discovery document | vendor HTTPS to widget | The URL is whatever `auth.x.ai` returns (`fetch_quota.py:886`), and the refresh token is POSTed to it. A compromise of that host redirects a live refresh token. No allowlist. |
| 4 | A hostile local process sets `QUOTA_WIDGET_*` and repoints the fetcher at paths it controls | environment | Paths must be absolute and non-empty (`_env_path`, `fetch_quota.py:190`), but nothing stops them pointing at a file the attacker wrote, and a bad value is reported, not refused mid-poll. |
| 5 | Vendor API responses are parsed with no schema validation | vendor HTTPS to widget | Every field is read defensively (`_as_dict`, `fetch_quota.py:59`), and numbers are coerced rather than trusted, but a response that changes shape silently changes what the panel shows. |
| 6 | No audit trail exists for any security-relevant event | all | Nothing is logged, by design. A stolen token, a rotated credential file, and a normal refresh leave the same absence of evidence. |

Nothing here is rated critical: the process has no privilege of its own, holds
no signing key, and can only read what its own user can already read.

## Attack surface inventory

### Entry points

| Entry point | Where | Untrusted input |
| --- | --- | --- |
| Widget configuration file | `package/contents/config/main.xml`, read via `Plasmoid.configuration` and clamped in `package/contents/ui/main.qml:27` (`intSetting`) | User-editable JSON under `~/.config/plasmoids/...` |
| Environment | `load_config`, `fetch_quota.py:244`; every `QUOTA_WIDGET_*` name listed in `README.md:120-130` | Inherited from the Plasma session, not from a shell rc file |
| Claude credentials | `fetch_claude`, `fetch_quota.py:731`; path from `Config.claude_cred` | `~/.claude/.credentials.json`, written by Claude Code |
| Codex credentials | `fetch_codex`, `fetch_quota.py:1230` | `~/.codex/auth.json` |
| Grok credentials | `_load_grok_auth`, `fetch_quota.py:828` | `~/.grok/auth.json` |
| Cursor credentials | `_load_cursor_auth`, `fetch_quota.py:1435` | `~/.config/cursor/auth.json`, or a SQLite database at `~/.config/Cursor/User/globalStorage/state.vscdb` opened read-only (`_read_cursor_state_db`, `fetch_quota.py:1406`) |
| Vendor usage APIs | `fetch_http`, `fetch_quota.py:543`; URLs at `fetch_quota.py:89,96,99,120` | JSON bodies and headers from four third parties |
| OAuth token endpoints | `fetch_quota.py:91` (two Claude hosts), `fetch_quota.py:100` (OpenAI), and the Grok endpoint discovered at `fetch_quota.py:886` | JSON bodies, plus `Retry-After` headers parsed at `parse_retry_after`, `fetch_quota.py:328` |
| CLI arguments | `main`, `fetch_quota.py:1614` | Only `--print-config` is accepted; any other argument exits 2 |
| Poll interval | `package/contents/ui/main.qml:34`, clamped to 30..3600 s | Widget setting |
| Stale window in the UI | `package/contents/ui/main.qml:42` (`staleKeepMs`) | Fixed, mirrors `DEFAULT_CACHE_MAX_AGE_S` |

### Outbound requests

Seven distinct hosts can receive a request, not four:

| Host | Request | Where |
| --- | --- | --- |
| `api.anthropic.com` | `GET /api/oauth/usage` | `fetch_quota.py:89` |
| `platform.claude.com` | `POST /v1/oauth/token` | `fetch_quota.py:92` |
| `console.anthropic.com` | `POST /v1/oauth/token` (fallback) | `fetch_quota.py:93` |
| `cursor.com` | `GET /api/usage-summary` | `fetch_quota.py:120` |
| `chatgpt.com` | `GET /backend-api/wham/usage` | `fetch_quota.py:99` |
| `auth.openai.com` | `POST /oauth/token` | `fetch_quota.py:100` |
| `cli-chat-proxy.grok.com` | `GET /v1/billing` (twice: credits and monthly) | `fetch_quota.py:96` |
| `auth.x.ai` | `GET /.well-known/openid-configuration` | `fetch_quota.py:97` |
| whatever `token_endpoint` that document names | `POST` with the Grok refresh token | `fetch_quota.py:886,896` |

## Trust boundaries

1. **Plasmashell to fetcher.** plasmashell spawns `python3 <path>/fetch_quota.py`
   on a timer (`package/contents/ui/main.qml:85,118`) and parses its stdout as
   JSON. The fetcher's output is fully trusted by the widget: every field is
   bound to a QML property without validation (`onNewData`,
   `package/contents/ui/main.qml:87`). A hijacked fetcher binary is a code
   execution win for whoever can write into the installed package.
2. **Fetcher to local filesystem.** Reads five credential files and writes two
   caches plus a lock. It reads with the ambient user identity; there is no
   ownership or mode check on the files it opens.
3. **Fetcher to vendor APIs.** TLS to the hosts above, bearer tokens in
   `Authorization` (Claude, Codex, Grok) or a `Cookie` header
   (`WorkosCursorSessionToken`, `fetch_quota.py:1576`). Responses are parsed
   with `json.loads` and consumed structurally.
4. **Fetcher to environment.** `QUOTA_WIDGET_*` is trusted after a value check.
5. **Fetcher to vendor CLIs.** The Claude, Codex, and Grok token stores are
   shared files that the widget writes and the CLIs also write. This is the one
   boundary where the widget has write authority over state another program owns.

## Assets

| Asset | Where | Impact if lost or exposed |
| --- | --- | --- |
| Claude, Codex, Grok OAuth access and refresh tokens | `~/.claude/.credentials.json`, `~/.codex/auth.json`, `~/.grok/auth.json` | Account takeover and billable spend on three vendor accounts; refresh-token reuse also signs the user out of the CLI |
| Cursor session token | Cursor `state.vscdb` and `~/.config/cursor/auth.json` | Read-only access to the Cursor account's usage; the token is not refreshed by the widget |
| Account identity | JWT `sub`, WorkOS user id, `ChatGPT-Account-Id` header | These are sent to the vendor as request context and hashed into the cache key; they never leave in the emitted JSON |
| Usage and spend figures | `~/.cache/quota-widget/*.json`, panel | Low value alone: plan name, utilization percentages, reset times, credit balances |
| Panel correctness | `package/contents/ui/main.qml` | A wrong or stale number is shown as live, which is a decision the user acts on |
| Vendor rate-limit budget | Poll interval, `Retry-After` handling | Aggressive polling gets the user rate-limited or banned by the vendor |

## Threats per boundary

### Plasmashell to fetcher

- **Tampering.** The package directory is a symlink into a checkout
  (`install.sh`, final `ln -sfn`). Anyone who can write the checkout owns the
  fetcher, and its stdout is trusted. Blast radius is the user's session.
- **Spoofing.** `onNewData` accepts any stdout that parses as JSON with a
  non-zero-tolerant exit code; there is no signature on the output.
- **Denial of service.** A fetcher that hangs holds the widget at its previous
  reading; each request is bounded by `http_timeout_s`
  (`DEFAULT_HTTP_TIMEOUT_S`, `fetch_quota.py:131`).

### Fetcher to local filesystem

- **Information disclosure.** Any process running as the user can read the
  credential files and the cache. Modes are `0600`/`0700`; there is no
  encryption and no keyring.
- **Tampering.** A file that is replaced between the read and the refresh write
  is handled by the read-modify-write retry (`_merge_write_json`,
  `fetch_quota.py:386`), but the value written is whatever the fetcher holds in
  memory.
- **Repudiation.** No write is logged, so a rotated token cannot be traced to a
  run.
- **Denial of service.** `_merge_write_json` retries three times
  (`MERGE_WRITE_ATTEMPTS`, `fetch_quota.py:136`); a permanently unwritable
  cache directory degrades to no caching, not to a failed poll
  (`_write_provider_cache`, `fetch_quota.py:483`).

### Fetcher to environment

- **Spoofing / elevation of privilege.** `QUOTA_WIDGET_CLAUDE_CREDENTIALS` and
  its siblings let the caller's environment choose which file is read as the
  credential store. Validation is limited to non-empty and absolute
  (`_env_path`, `fetch_quota.py:190`); an environment-controlled path is
  trusted. Anything that can set the Plasma session environment already runs as
  the user, so this is a persistence and confusion surface rather than a
  privilege gain.
- **Denial of service.** Every value is read once, and an invalid one aborts the
  whole poll with `error: "config"` (`main`, `fetch_quota.py:1614`), which blanks
  all four cards at once.

### Fetcher to vendor APIs

- **Spoofing.** A hostile response is accepted as data. JSON is parsed and
  coerced, never schema-checked.
- **Tampering.** The Claude request sends Claude Code's User-Agent
  (`CLAUDE_USER_AGENT`, `fetch_quota.py:118`) to stay inside that rate-limit
  bucket. That is deliberate, and it means the widget is identified to
  Anthropic as another product's client.
- **Information disclosure.** Every request carries a live bearer token. The
  response body of a failure is read and dropped, not stored
  (`fetch_http`, `fetch_quota.py:564`), and the fetcher emits only the fields
  the panel renders.
- **Denial of service.** Polling every 30 s (the minimum setting) against four
  vendors is roughly 11,500 requests a day. The only backoff is
  `Retry-After` bounded to 0.5-10 s (`RETRY_AFTER_MIN_S`/`MAX_S`,
  `fetch_quota.py:139`) plus the stale cache; there is no exponential backoff
  and no global rate limit. A persistent `http-429` still costs one request per
  poll per provider.
- **Elevation of privilege.** The Grok refresh token is POSTed to the
  `token_endpoint` from a remote discovery document with no host allowlist
  (`fetch_quota.py:886`). Claude has a fixed two-host list (`fetch_quota.py:91`)
  and Codex a fixed one (`fetch_quota.py:100`); Grok is the exception.

### Fetcher to vendor CLIs (shared credential files)

- **Tampering / availability.** Two runs refreshing at once could rotate a
  single-use refresh token twice. `_refresh_lock` (`fetch_quota.py:505`) and the
  re-read under that lock are the controls; the lock is best-effort and falls
  open when the cache directory is unwritable (`fetch_quota.py:523`).
- **Repudiation.** The CLIs rewrite the same file; nothing distinguishes a write
  by the widget from a write by the CLI.

## Mitigations

| Control | Where | Covers |
| --- | --- | --- |
| Credentials never accepted from the environment or argv | `load_config`, `fetch_quota.py:244` | Token disclosure through the process table or a session export |
| `--print-config` prints paths only | `Config.describe`, `fetch_quota.py:175` | Accidental token leak in a diagnostic |
| Atomic, fsynced, `0600` writes | `_atomic_write_json`, `fetch_quota.py:360` | Torn credential files, world-readable tokens |
| Refresh serialized per cache directory | `_refresh_lock`, `fetch_quota.py:505` | Double refresh-token rotation |
| Read-modify-write with re-read verification | `_merge_write_json`, `fetch_quota.py:386` | Clobbering a concurrent CLI refresh |
| Provider cache scoped to a hashed account id | `_account_id`, `fetch_quota.py:422`; `_read_provider_cache`, `fetch_quota.py:441` | One account's numbers being shown to another on a shared machine |
| Cache expiry deletes the file on read | `fetch_quota.py:462` | Retention beyond the stated window |
| Failed HTTP bodies discarded | `fetch_http`, `fetch_quota.py:564` | Account identifiers echoed in an error reaching the panel or a file |
| One provider's failure cannot abort the others | `_safe_fetch`, `fetch_quota.py:1606` | Availability of the whole panel |
| Per-request timeout, bounded `Retry-After` | `fetch_quota.py:131`, `fetch_quota.py:139` | A hung vendor stalling the panel indefinitely |
| Widget settings clamped before use | `intSetting`, `package/contents/ui/main.qml:27` | A malformed config file blanking the widget |

### Threats with no mitigation

- **No audit trail.** A refresh, a rotation, and a failed auth are
  indistinguishable after the fact. Adding logging is a privacy trade the
  project has deliberately declined; the gap is recorded, not closed.
- **No token encryption at rest.** Tokens are stored as the CLIs store them.
- **No host allowlist on the Grok token endpoint.**
- **No rate limiting** beyond the vendor's own throttling.
- **No signature or integrity check on the fetcher's stdout.**

## Abuse cases

There is no authenticated, hostile user of this software: it has one user, and
that user owns every file it touches. The abuse cases that remain are about a
second party on the same machine or on the network path:

- **Same-user process reads the cache or the token files.** Not a privilege
  boundary in Unix terms, but it is the realistic theft path: a browser
  extension, a malicious npm postinstall, or another agent run as the user
  obtains four live vendor sessions from one JSON file.
- **A shared machine with two accounts.** Mitigated by the account-scoped cache
  (`fetch_quota.py:459`); a credential that yields no account id caches
  nothing, so the failure mode is "no reading" rather than "wrong reading".
- **A poisoned environment.** A `QUOTA_WIDGET_CACHE` pointed at a directory the
  attacker controls lets them pre-seed a payload. The account id must still
  match, and the entry must be inside the freshness window, so a seeded entry
  shows for at most `DEFAULT_CACHE_MAX_AGE_S` and only to the account whose id
  it was built for.
- **Vendor response shape change.** Not adversarial, but the same class: a
  renamed field degrades to a missing meter, and a hostile response with the
  same shape would be rendered the same way.
- **Unbounded poll rate.** Setting `pollSeconds` to its 30 s minimum spends the
  vendor's rate-limit budget and invites an IP-level block, which affects the
  user's CLI sessions too, not just the widget.

## Security documentation state

- `README.md` "Data and privacy" is the user-facing security statement and is
  kept in step with the code.
- There is no `SECURITY.md` in this repository: no disclosure contact, no
  supported-versions list, and no documented path from "a vulnerability is
  reported" to "a fix ships". None of that is invented here.
- `CHANGELOG.md` is the only record of what changed in a release.
