# Change provenance

Append a dated entry for each delivered change, with its decision source,
evidence, and verification. Published history before this ledger remains in
the changelog and Git history.

## 2026-10-21: subscription credential file

- Request: integrate and release the existing local drafts while preserving
  published changes.
- Change: read the Go API key as UTF-8 text from `~/.secrets/opencode-go`, or
  the configured override, without writing it. Preserve the published cache
  fuzz isolation and session sandbox fixes.
- Evidence: `fetch_opencode_go`, `load_config`, and `ENV_DOCS` in
  `package/contents/code/fetch_quota.py`; compare the preserved draft commit
  `3c3e266a53d830ee456faf7c4b0ef3587095a536` with release `v1.3.0`.
- Verification: `OpenCodeGoTest`, the credential-path config test, `ReplayTest`,
  and `OpenCodeGoFuzz` cover credentials, usage parsing, caching, and replay.
  The release gate is `./scripts/gate.sh`.
