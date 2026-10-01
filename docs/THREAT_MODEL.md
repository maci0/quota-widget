# Threat model

Last reviewed: 2026-09-28, against `package/contents/code/fetch_quota.py` at
`package/metadata.json` version 1.2.0.

Scope: the plasmoid as installed on a single user session. It runs as that
user, holds that user's vendor credentials, and talks to five vendor APIs.
It listens on nothing and serves nothing, so there is no remote attacker with
a socket; every hostile input in this model is either a local file on the same
account, the environment the fetcher inherits, or a response from a vendor
endpoint.

Owner and review cadence: not recorded in this repository.

## Risk-ranked summary

| # | Risk | Boundary | State |
| --- | --- | --- | --- |
| 1 | A read of the credential or cache files leaks live OAuth tokens for four accounts | local filesystem | Mitigated by file modes, not by encryption: `FILE_MODE_PRIVATE` (`fetch_quota.py:395`) is applied to every write (`_atomic_write_json`, `fetch_quota.py:821`), the cache directory is `CACHE_DIR_MODE` (`fetch_quota.py:394`) and is re-tightened on every poll (`_private_dir`, `fetch_quota.py:1021`), and the refresh lock is `0600` (`fetch_quota.py:1176`). The account digest key is `0600` and created `O_EXCL` (`_install_salt`, `fetch_quota.py:913`). Any process running as the same user can still read all of it. |
| 2 | The fetcher writes rotated OAuth tokens back into the vendor CLIs' own credential files | local filesystem to vendor CLI | Single-writer race is handled (`_merge_write_json`, `fetch_quota.py:850`; `_refresh_lock`, `fetch_quota.py:1156`); a lost update there signs the user out of Claude Code, Codex, or Grok, not just the widget. |
| 3 | The OIDC token endpoint for Grok is named by a remote discovery document | vendor HTTPS to widget | Confined: the endpoint is followed only when it is `https` on `GROK_OIDC_HOST` (`_is_grok_token_url`, `fetch_quota.py:1653`, host at `fetch_quota.py:432`); a document naming any other host is refused and the refresh token is not sent (`fetch_quota.py:1690`). The residual exposure is a compromise of `auth.x.ai` itself, which the allowlist cannot exclude. |
| 4 | A hostile local process sets `QUOTA_WIDGET_*` and repoints the fetcher at paths it controls | environment | Paths must be absolute and non-empty (`_env_path`, `fetch_quota.py:490`) and numbers are bounded (`_env_number`, `fetch_quota.py:500`; `_env_seconds`, `fetch_quota.py:527`), an unknown `QUOTA_WIDGET_*` name aborts the poll rather than being ignored (`_unknown_env`, `fetch_quota.py:107`), and a bad value is reported, not silently accepted. |
| 5 | `QUOTA_WIDGET_NOW_MS` pins the clock every freshness and expiry check reads | environment | Validated as an integer inside the representable datetime range (`_pinned_ms`, `fetch_quota.py:221`, bounds at `fetch_quota.py:134`), so a pin cannot abort a poll mid-refresh. It is still the one value that decides whether a cached entry counts as fresh (`_read_provider_cache`, `fetch_quota.py:1049`) and whether a token counts as expired (`_claude_expired`, `fetch_quota.py:1336`). Documented as tests-only (`README.md:88`), not enforced as such. |
| 6 | Vendor API responses are parsed with no schema validation | vendor HTTPS to widget | Every field is read defensively (`_as_dict`, `fetch_quota.py:141`; `_as_text`, `fetch_quota.py:145`), numbers are coerced rather than trusted (`_finite_number`, `fetch_quota.py:286`), text that cannot be encoded is dropped where it arrives (`_utf8_encodable`, `fetch_quota.py:160`), and a response body is capped at `MAX_RESPONSE_BYTES` (`fetch_quota.py:427`, read at `fetch_quota.py:1278`). A response that changes shape still silently changes what the panel shows. |
| 7 | Raw vendor-derived exception text and a full traceback reach the session journal | fetcher to journal | `warn` (`fetch_quota.py:701`) redacts the home directory through `_redact` (`fetch_quota.py:686`) before printing, and a config error is redacted the same way in the payload (`main`, `fetch_quota.py:2604`). Two gaps remain: redaction is a path rewrite, so vendor-controlled text that is not a path reaches the journal unchanged, and the crash traceback (`fetch_quota.py:2525`) is written with `traceback.print_exc()`, which never passes through `_redact`. |
| 8 | Failure reporting is thin: a successful token rotation and a dropped one look alike from outside | all | Failures are journaled (URL, status, reason). Successes are not, so "was this token written" and "which run rotated it" cannot be answered after the fact. |
| 9 | `QUOTA_WIDGET_ACCOUNT_SALT` names the key every account digest is taken under | environment | Validated as 64 hex characters (`_env_salt`, `fetch_quota.py:492`) and used without being written (`_load_or_create_salt`, `fetch_quota.py:921`). A process that sets it chooses the key, but a key already in the cache directory still wins, so it cannot re-scope entries another poll wrote there; it decides only what a run with no key on disk digests under. Documented as tests-only, not enforced as such. |

Nothing here is rated critical: the process has no privilege of its own, holds
no signing key, and can only read what its own user can already read.

## Attack surface inventory

### Entry points

| Entry point | Where | Untrusted input |
| --- | --- | --- |
| Widget configuration file | `package/contents/config/main.xml`, read via `Plasmoid.configuration` and clamped in `package/contents/ui/main.qml:28` (`intSetting`) | User-editable JSON under `~/.config/plasmoids/...` |
| Environment | `load_config`, `fetch_quota.py:586`; every `QUOTA_WIDGET_*` name listed at `fetch_quota.py:93` (`ENV_DOCS`) and in `README.md:153` | Inherited from the Plasma session, not from a shell rc file |
| Pinned clock | `now_ms`, `fetch_quota.py:247`; validated in `load_config`, `fetch_quota.py:606` | `QUOTA_WIDGET_NOW_MS`, an integer that overrides every `now_ms()` read |
| Pinned account key | `_load_or_create_salt`, `fetch_quota.py:1025`; validated at `fetch_quota.py:584` | `QUOTA_WIDGET_ACCOUNT_SALT`, 64 hex characters that stand in for the `account-salt` key when the cache directory holds none |
| CLI arguments | `main`, `fetch_quota.py:2567` | `--help`/`-h`, `--print-config`, `--clear-cache`; anything else exits 2 before the config is read (`fetch_quota.py:2582`) |
| Claude credentials | `fetch_claude`, `fetch_quota.py:1478`; path from `Config.claude_cred` | `~/.claude/.credentials.json`, written by Claude Code |
| Codex credentials | `fetch_codex`, `fetch_quota.py:2042` | `~/.codex/auth.json` |
| Grok credentials | `_load_grok_auth`, `fetch_quota.py:1588` | `~/.grok/auth.json` |
| Cursor credentials | `_load_cursor_auth`, `fetch_quota.py:2312` | `~/.config/cursor/auth.json`, or a SQLite database at `~/.config/Cursor/User/globalStorage/state.vscdb` opened read-only (`_read_cursor_state_db`, `fetch_quota.py:2278`) |
| Go credentials | `fetch_opencode_go`, path from `Config.opencode_auth` | `$XDG_DATA_HOME/opencode/auth.json` (`~/.local/share` by default), read only; only the `opencode-go` API entry is used |
| Vendor usage APIs | `fetch_http`, fixed URL constants in `fetch_quota.py` | JSON bodies and headers from five third parties |
| OAuth token endpoints | `fetch_quota.py:341,342` (two Claude hosts), `fetch_quota.py:349` (OpenAI), and the Grok endpoint read out of the discovery document at `fetch_quota.py:346` | JSON bodies, plus `Retry-After` headers parsed at `parse_retry_after`, `fetch_quota.py:789` |
| Poll interval | `package/contents/ui/main.qml:35`, clamped to 30..3600 s | Widget setting |
| Stale window in the UI | `package/contents/ui/main.qml:47` (`defaultStaleKeepMs`), overridden from the payload at `package/contents/ui/main.qml:145` | Fixed fallback; the effective value arrives in `cache_max_age_s` |
| CI pipeline | `.github/workflows/test.yml`, the only workflow; it runs `scripts/gate.sh` on a `ubuntu-24.04` runner with `contents: read` and no persisted credentials | Each third-party action is pinned to the commit behind its version tag and bumped by Dependabot (`.github/dependabot.yml`); the gate installs the dev tools from `uv.lock` with `--locked`, so a dependency that is not the reviewed resolution fails the run. The residual is the runner image itself (`ubuntu-24.04` is a moving label, not a digest) and the two actions that execute third-party code, `actions/checkout` and `astral-sh/setup-uv` |

### Outbound requests

Nine fixed hosts can receive a request:

| Host | Request | Where |
| --- | --- | --- |
| `api.anthropic.com` | `GET /api/oauth/usage` | `fetch_quota.py:338` |
| `platform.claude.com` | `POST /v1/oauth/token` | `fetch_quota.py:341` |
| `console.anthropic.com` | `POST /v1/oauth/token` (fallback) | `fetch_quota.py:342` |
| `cursor.com` | `GET /api/usage-summary` | `fetch_quota.py:369` |
| `chatgpt.com` | `GET /backend-api/wham/usage` | `fetch_quota.py:348` |
| `auth.openai.com` | `POST /oauth/token` | `fetch_quota.py:349` |
| `cli-chat-proxy.grok.com` | `GET /v1/billing` | `fetch_quota.py:345` |
| `auth.x.ai` | `GET /.well-known/openid-configuration` | `fetch_quota.py:346` |
| `opencode.ai` | `GET /zen/go/v1/usage`, Bearer API key | `OPENCODE_GO_USAGE_URL`, `fetch_opencode_go` |
| the `token_endpoint` that document names | `POST` with the Grok refresh token, only when it is `https` on `auth.x.ai` | `fetch_quota.py:1690`, `fetch_quota.py:1653` |

No other host is contacted. `api.openai.com` appears in a JWT claim path
(`fetch_quota.py:2066`), not as a request target.

Go usage returns no account id. Its cache is scoped to a keyed digest of the API key, so a key change invalidates that reading. The credential file is never written, and the key is sent only to the fixed HTTPS origin above. Invalid or absent percentages are omitted; a response with no usable windows is a transient `bad-body` failure.

### Outbound channels

| Channel | Where | Note |
| --- | --- | --- |
| stdout, one JSON payload per run | `emit`, `fetch_quota.py:660` | The panel's only data channel; fully trusted on the way back in |
| stderr, prefixed `fetch_quota:`, home directory spelled `~` | `warn`, `fetch_quota.py:701`; `_redact`, `fetch_quota.py:686` | Lands in the Plasma journal and in `.scratch/smoke.err` (`install.sh:184`) |
| Traceback of a provider crash | `_safe_fetch`, `fetch_quota.py:2525` | Full frames and exception text, same destination as above |

## Trust boundaries

1. **Plasmashell to fetcher.** plasmashell spawns `python3 <path>/fetch_quota.py`
   on a timer (`package/contents/ui/main.qml:22,190`) and parses its stdout as
   JSON. The fetcher's output is fully trusted by the widget: every field is
   bound to a QML property without validation (`onNewData`,
   `package/contents/ui/main.qml:120`). A hijacked fetcher binary is a code
   execution win for whoever can write into the installed package. The command
   line is assembled as a string with the script path single-quote escaped
   (`package/contents/ui/main.qml:22`), so a package directory containing a
   quote cannot break out of the argument.
2. **Fetcher to local filesystem.** Reads five credential files and writes two
   caches plus a lock and a digest key. It reads with the ambient user
   identity; there is no ownership or mode check on the files it opens.
3. **Fetcher to vendor APIs.** TLS to the hosts above, bearer tokens in
   `Authorization` (Claude, Codex, Grok) or a `Cookie` header
   (`fetch_quota.py:2458`). Responses are parsed with `json.loads` and consumed
   structurally.
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
| Account identity | JWT `sub`, WorkOS user id, `ChatGPT-Account-Id` header | These are sent to the vendor as request context and hashed into the cache key. Only the 16-character digest reaches the emitted JSON, as the `account` field the panel scopes a kept reading by; the value itself never does |
| Account digest key | `~/.cache/quota-widget/account-salt` | A copy of the cache directory is a copy of the key, so the key and the entries it scopes have the same exposure. Not a credential for anything: it only ever compares two digests taken under itself (`_digest`, `fetch_quota.py:982`) |
| Journal contents | Plasma journal, `.scratch/smoke.err` | Whatever a `warn` line or a traceback carried; the journal is not deleted by the widget and is readable after the session ends |
| Usage and spend figures | `~/.cache/quota-widget/*.json`, panel | Low value alone: plan name, utilization percentages, reset times, credit balances |
| Panel correctness | `package/contents/ui/main.qml` | A wrong or stale number is shown as live, which is a decision the user acts on |
| Vendor rate-limit budget | Poll interval, `Retry-After` handling | Aggressive polling gets the user rate-limited or banned by the vendor |

## Threats per boundary

### Plasmashell to fetcher

- **Tampering.** The package directory is a symlink into a checkout
  (`install.sh:198`). Anyone who can write the checkout owns the fetcher, and
  its stdout is trusted. Blast radius is the user's session.
- **Spoofing.** `onNewData` accepts any stdout that parses as JSON with a
  zero exit code; there is no signature on the output.
- **Denial of service.** A fetcher that hangs holds the widget at its previous
  reading; each request is bounded by `http_timeout_s`
  (`DEFAULT_HTTP_TIMEOUT_S`, capped at `MAX_HTTP_TIMEOUT_S`), and a run that
  outlives `pollTimeoutMs` is disconnected by the poll watchdog
  (`package/contents/ui/main.qml`). The watchdog takes the budget a poll reports
  as `poll_timeout_s` (`Config.poll_timeout_s`), so a `QUOTA_WIDGET_HTTP_TIMEOUT`
  the panel's own ten-minute default cannot hold does not drop a run that was
  still entitled to answer. That number arrives in a payload the panel trusts,
  so the panel caps it (`maxPollTimeoutMs`): a hostile or broken fetcher cannot
  buy itself an unbounded deadline, only half an hour.
- **Denial of service (memory).** The payload is read into plasmashell's heap
  every poll. The fetcher caps what it will read from a vendor at
  `MAX_RESPONSE_BYTES` (`fetch_quota.py:427`) and the number of meters it will
  keep from a `limits` array at `MAX_WEEKLY_LIMITS` (`fetch_quota.py:401`), so
  neither side grows without a bound.

### Fetcher to local filesystem

- **Information disclosure.** Any process running as the user can read the
  credential files and the cache. Modes are `0600`/`0700`; there is no
  encryption and no keyring.
- **Tampering.** A file that is replaced between the read and the refresh write
  is handled by the read-modify-write retry (`_merge_write_json`,
  `fetch_quota.py:850`), but the value written is whatever the fetcher holds in
  memory. A cache entry written by another release is deleted unread
  (`PAYLOAD_SCHEMA`, `fetch_quota.py`), so an upgrade never replays a payload
  shape the current panel was not built against; the tradeoff is that a downgrade
  and an upgrade in a row leave no reading to fall back on.
- **Repudiation.** A failed credential write is journaled
  (`_write_rotated_tokens`, `fetch_quota.py:886`, and its three callers); a
  successful one is not, so a rotation cannot be traced to a run.
- **Denial of service.** `_merge_write_json` retries
  (`MERGE_WRITE_ATTEMPTS`, `fetch_quota.py:397`); a permanently unwritable
  cache directory degrades to no caching, not to a failed poll
  (`_write_provider_cache`, `fetch_quota.py:1098`).

### Fetcher to environment

- **Spoofing / elevation of privilege.** `QUOTA_WIDGET_CLAUDE_CREDENTIALS` and
  its siblings let the caller's environment choose which file is read as the
  credential store. Validation is limited to non-empty and absolute
  (`_env_path`, `fetch_quota.py:490`); an environment-controlled path is
  trusted. Anything that can set the Plasma session environment already runs as
  the user, so this is a persistence and confusion surface rather than a
  privilege gain. A name under the prefix that no knob reads is refused
  outright (`_unknown_env`, `fetch_quota.py:107`), so a silently ignored
  setting is not possible.
- **Tampering (clock).** `QUOTA_WIDGET_NOW_MS` (`now_ms`, `fetch_quota.py:247`)
  feeds the freshness test in `_read_provider_cache` (`fetch_quota.py:1049`),
  the expiry tests in `_claude_expired` (`fetch_quota.py:1336`),
  `_token_expired` (`fetch_quota.py:1619`), and `_codex_token_expired`
  (`fetch_quota.py:1996`). A value far in the future makes an expired cache
  entry read as fresh; a value far in the past makes a retired access token read
  as live. It is documented as tests-only (`README.md:88`, `README.md:153`) but
  nothing rejects it outside a test.
- **Denial of service.** Every value is read once, and an invalid one aborts
  the whole poll with `error: "config"` (`main`, `fetch_quota.py:2603`), which
  blanks all four cards at once.

### Fetcher to vendor APIs

- **Spoofing.** A hostile response is accepted as data. JSON is parsed and
  coerced, never schema-checked.
- **Information disclosure (redirect).** `urllib` copies the whole request
  header set onto a redirect target, so a 30x from a vendor could hand a live
  token to a host the widget never named. `_OriginBoundRedirect`
  (`fetch_quota.py:1217`, installed globally at `fetch_quota.py:1249`) refuses
  a cross-origin redirect whenever the request carried `Authorization` or
  `Cookie` (`CREDENTIAL_HEADERS`, `fetch_quota.py:436`) and reports it as the
  failure it is.
- **Tampering.** The Claude request sends Claude Code's User-Agent
  (`CLAUDE_USER_AGENT`, `fetch_quota.py:367`) to stay inside that rate-limit
  bucket. That is deliberate, and it means the widget is identified to
  Anthropic as another product's client.
- **Information disclosure.** Every request carries a live bearer token. The
  response body of a failure is drained and discarded (`fetch_http`,
  `fetch_quota.py:1303`), and the fetcher emits only the fields the panel
  renders. The journal is the narrower leak: a `warn` line carries the URL and,
  in the transport-failure case, the exception repr (`fetch_quota.py:1314`).
- **Denial of service.** Polling every 30 s (the minimum setting) against four
  vendors is roughly 11,500 requests a day. `Retry-After` is honoured, bounded
  to 0.5-10 s (`RETRY_AFTER_MIN_S`/`MAX_S`, `fetch_quota.py:407`,`408`), a
  failed GET is retried once after a fixed `NETWORK_RETRY_BACKOFF_S`
  (`fetch_quota.py:412`, `fetch_quota.py:1272`), and both success and error
  bodies are read at most `MAX_RESPONSE_BYTES` (`fetch_quota.py:1278`,
  `fetch_quota.py:1295`). There is no exponential backoff and no global rate
  limit. A persistent `http-429` still costs one request per poll per provider.
- **Elevation of privilege.** The Grok refresh token is POSTed to the
  `token_endpoint` named by a remote discovery document. The endpoint is
  constrained to `https` on `GROK_OIDC_HOST` (`_is_grok_token_url`,
  `fetch_quota.py:1653`) and anything else is refused with a journal line
  (`fetch_quota.py:1690`), so the exposure left is a compromise of
  `auth.x.ai` itself. Claude has a fixed two-host list (`fetch_quota.py:341`)
  and Codex a fixed one (`fetch_quota.py:349`); neither reads its endpoint from
  a document at all.

### Fetcher to session journal

- **Information disclosure.** `warn` (`fetch_quota.py:701`) emits the message
  it is given with the home directory spelled `~` by `_redact`
  (`fetch_quota.py:686`), which is what keeps the account name a path carries
  out of the journal. Redaction is a path rewrite, not a filter: a vendor field
  that is not a path is printed as it arrived. `_safe_fetch`
  (`fetch_quota.py:2524`) passes `str(exc)` from any provider, and providers
  build exceptions out of values that came from a vendor body; `fetch_http`
  adds `reason!r` from the transport (`fetch_quota.py:1314`). The failure body
  itself is not read, so the direct path is closed; this is the indirect one.
- **Information disclosure (frames).** `_safe_fetch` prints the full traceback
  (`fetch_quota.py:2525`). That line goes straight to stderr through
  `traceback.print_exc()`, not through `warn`, so it is the one channel the
  fetcher writes that `_redact` never sees: the journal keeps the local paths,
  and the account name in any path among them, for as long as the journal
  keeps the entry.
- **Repudiation.** A journal line is attributable to a run by its proximity in
  the Plasma journal, not by anything the fetcher writes. The journal is
  user-owned and is not rotated by this project.

### Fetcher to vendor CLIs (shared credential files)

- **Tampering / availability.** Two runs refreshing at once could rotate a
  single-use refresh token twice. `_refresh_lock` (`fetch_quota.py:1156`) and
  the re-read under that lock are the controls; the lock is best-effort and
  falls open when `fcntl` is missing (`fetch_quota.py:1165`) or the cache
  directory is unwritable (`fetch_quota.py:1176`), and it breaks out on a wait
  deadline rather than blocking (`REFRESH_LOCK_WAIT_S`, `fetch_quota.py:384`).
  A filesystem that cannot lock at all is not waited on
  (`LOCK_BUSY_ERRNOS`, `fetch_quota.py:379`), so a network mount refreshes
  unguarded once instead of burning the deadline on every poll.
- **Repudiation.** The CLIs rewrite the same file; nothing distinguishes a write
  by the widget from a write by the CLI.

## Mitigations

| Control | Where | Covers |
| --- | --- | --- |
| Credentials never accepted from the environment or argv | `load_config`, `fetch_quota.py:586` | Token disclosure through the process table or a session export |
| `--print-config` prints paths only | `Config.describe`, `fetch_quota.py:459` | Accidental token leak in a diagnostic |
| `--clear-cache` erases the readings and the digest key together | `_clear_cache`, `fetch_quota.py:2539` | An entry restored from a backup staying readable under a key that stayed behind |
| Atomic, fsynced, `0600` writes, and a `0700` cache directory re-tightened each poll | `_atomic_write_json`, `fetch_quota.py:821`; `_private_dir`, `fetch_quota.py:1021` | Torn credential files, world-readable tokens or readings |
| Digest key created `O_EXCL` and never replaced, losers adopt the winner's | `_install_salt`, `fetch_quota.py:913`; `_load_or_create_salt`, `fetch_quota.py:947` | Two racing polls stranding every entry by minting different keys |
| Account digest taken under that key, NFC-normalized, over a keyed construction | `_account_salt`, `fetch_quota.py:970`; `_digest`, `fetch_quota.py:982` | A short, guessable vendor id being recoverable from a copied cache directory |
| Script path single-quote escaped in the spawn command | `package/contents/ui/main.qml:22` | Argument breakout through a quote in the install path |
| Widget settings clamped before use | `intSetting`, `package/contents/ui/main.qml:28` | A malformed config file blanking the widget |
| Environment numbers bounded at load, unknown `QUOTA_WIDGET_*` refused | `_env_number`, `fetch_quota.py:500`; `_env_seconds`, `fetch_quota.py:527`; `_unknown_env`, `fetch_quota.py:107` | A hostile or typo'd timeout, retention value, or knob name |
| Pinned clock bounded to the representable date range | `_pinned_ms`, `fetch_quota.py:221` | A pin that would raise mid-refresh, after the old refresh token was already retired |
| Usage errors reported before the environment is read | `main`, `fetch_quota.py:2582` | A typo'd flag on a broken machine exiting 0 with a config payload |
| Cross-origin redirect refused for a credential-carrying request | `_OriginBoundRedirect`, `fetch_quota.py:1217` | A 30x handing a live token to a host the widget never named |
| Grok token endpoint confined to `https` on `auth.x.ai` | `_is_grok_token_url`, `fetch_quota.py:1653` | A discovery document redirecting a refresh token |
| Response bodies read at most `MAX_RESPONSE_BYTES` on success and failure | `fetch_quota.py:1278`, `fetch_quota.py:1295` | A peer answering at length, once a poll, every poll, into plasmashell's heap |
| Meters kept from a `limits` array capped | `MAX_WEEKLY_LIMITS`, `fetch_quota.py:401` | A growing array becoming session-long memory in the panel |
| Refresh serialized per cache directory | `_refresh_lock`, `fetch_quota.py:1156` | Double refresh-token rotation |
| Read-modify-write with re-read verification | `_merge_write_json`, `fetch_quota.py:850` | Clobbering a concurrent CLI refresh |
| Provider cache scoped to a keyed account id, retention checked before the account match | `_account_id`, `fetch_quota.py:1010`; `_read_provider_cache`, `fetch_quota.py:1049` | One account's numbers shown to another, and an expired entry outliving its window |
| Panel reading kept only for the same account | `mergeProv`, `package/contents/ui/main.qml:237` | The panel keeping a card across an account switch, which the fetcher's scoped cache refuses to do |
| Failure classification carried in the payload | `_transient_failure`, `fetch_quota.py:176`; `_failure`, `fetch_quota.py:188` | A new status code blanking a card on a blip because the panel re-derived the rule |
| Cache expiry deletes the file on read | `_discard_provider_cache`, `fetch_quota.py:1035` | Retention beyond the stated window |
| Failed HTTP bodies drained and discarded | `fetch_http`, `fetch_quota.py:1303` | Account identifiers echoed in an error reaching the panel or a file |
| One provider's failure cannot abort the others | `_safe_fetch`, `fetch_quota.py:2515` | Availability of the whole panel |
| Per-request timeout, bounded `Retry-After`, one GET retry, token POST never retried | `fetch_quota.py:392`, `fetch_quota.py:407`, `fetch_quota.py:412`, `fetch_quota.py:1272` | A hung vendor stalling the panel, and a repeated refresh retiring a token |
| Home directory spelled `~` on every `warn` line and in the panel's config error | `_redact`, `fetch_quota.py:686`; `main`, `fetch_quota.py:2604` | The account name in a path outliving the poll in the journal or on the card |
| Streams forced to UTF-8 whatever the locale says | `_use_utf8_streams`, `fetch_quota.py:641` | A warning raising `UnicodeEncodeError` and leaving the panel with no payload at all |
| Cursor state DB opened read-only via URI | `_read_cursor_state_db`, `fetch_quota.py:2281` | The fetcher mutating the IDE's own state |

### Threats with no mitigation

- **No filtering between vendor text and the journal.** `_redact` rewrites
  paths; it does not decide what a provider may name in an error. Exception
  messages reach stderr otherwise unfiltered (`fetch_quota.py:2524`), and the
  crash traceback bypasses `warn` entirely (`fetch_quota.py:2525`). Closing
  either means classifying provider error text and routing the traceback
  through the same redaction, which is a code change and not a documentation
  one.
- **No record of a successful token rotation.** Failures are journaled;
  successes are not, so the widget cannot prove which run rotated a credential.
- **No token encryption at rest.** Tokens are stored as the CLIs store them.
- **No rate limiting** beyond the vendor's own throttling.
- **No signature or integrity check on the fetcher's stdout.**
- **No enforcement that `QUOTA_WIDGET_NOW_MS` is unset in production.** It is
  documented as tests-only and validated as a bounded integer, nothing more.
- **No enforcement that `QUOTA_WIDGET_ACCOUNT_SALT` is unset in production.**
  Same shape: documented as tests-only, validated as 64 hex characters, and
  never persisted, so it is scoped to the runs that set it.
- **No ownership or mode check on the credential files the fetcher opens.** It
  reads whatever path it is given with the ambient user identity.

## Abuse cases

There is no authenticated, hostile user of this software: it has one user, and
that user owns every file it touches. The abuse cases that remain are about a
second party on the same machine or on the network path:

- **Same-user process reads the cache or the token files.** Not a privilege
  boundary in Unix terms, but it is the realistic theft path: a browser
  extension, a malicious npm postinstall, or another agent run as the user
  obtains four live vendor sessions from one JSON file.
- **A shared machine with two accounts.** Mitigated by the account-scoped cache
  (`fetch_quota.py:1010`) and by the panel, which keeps its own copy of a
  reading only while the failing poll names the same account digest
  (`mergeProv`, `package/contents/ui/main.qml:237`); a credential that yields
  no account id caches nothing, so the failure mode is "no reading" rather than
  "wrong reading".
- **A poisoned environment.** A `QUOTA_WIDGET_CACHE` pointed at a directory the
  attacker controls lets them pre-seed a payload. The account digest is taken
  under a key in that same directory, so a seeded entry also needs the key, and
  the entry must be inside the freshness window, so it shows for at most
  `DEFAULT_CACHE_MAX_AGE_S`. Pairing it with `QUOTA_WIDGET_NOW_MS` removes the
  window from the equation for as long as the variable stays set. Choosing the
  key with `QUOTA_WIDGET_ACCOUNT_SALT` does not widen that: the seeded digest
  still has to be built from the account id, so the check is as hard to pass as
  it was.
- **Vendor response shape change.** Not adversarial, but the same class: a
  renamed field degrades to a missing meter, and a hostile response with the
  same shape would be rendered the same way. A response that instead trips a
  parser lands its text in the journal via `_safe_fetch`.
- **Unbounded poll rate.** Setting `pollSeconds` to its 30 s minimum spends the
  vendor's rate-limit budget and invites an IP-level block, which affects the
  user's CLI sessions too, not just the widget.

## Security documentation state

- `README.md` "Data and privacy" (line 197) is the user-facing security
  statement. Its claims were checked against the code in this pass: the body of
  a failed response is drained and not logged, the digest of an account id
  never the value reaches the cache or the payload, `transient` travels with
  every failure, the home directory is spelled `~` on every warning line, and
  `--clear-cache` removes both the entries and the key. The one caveat the
  README already states is the real one: a provider crash writes exception text
  and a traceback to the same stream, and that text is built from what the
  vendor sent. The redaction claim there was narrowed in this pass to match the
  code: the crash traceback (`fetch_quota.py:2525`) is written with
  `traceback.print_exc()` and never reaches `_redact`.
- There is no `SECURITY.md` in this repository: no disclosure contact, no
  supported-versions list, and no documented path from "a vulnerability is
  reported" to "a fix ships". None of that is invented here.
- `CHANGELOG.md` is the only record of what changed in a release.

## Response readiness

Noted, not built. What an investigation would have to work from today:

- **What exists.** `warn` lines in the Plasma journal carry the URL, the
  status, and the transport reason for a failed call; stderr is captured per
  run by `install.sh` into `.scratch/smoke.err` (line 184). Neither carries a
  timestamp written by the fetcher: attribution rests on journal ordering.
- **What is missing.** A successful token rotation leaves no record, so
  "which run replaced my refresh token" cannot be answered from the machine
  after the fact. `--clear-cache` reports what it removed on stdout
  (`fetch_quota.py:2539`) but writes nothing to the journal.
- **Fix path.** `CHANGELOG.md` records what changed per release and the
  repository history carries the commits. There is no documented route from a
  report to a shipped fix, and none is stated here.
