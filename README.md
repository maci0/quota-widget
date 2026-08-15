# AI Quota — KDE Plasma widget

Desktop / panel widget for **KDE Plasma 6** that shows your live **Claude**,
**Codex**, and **Grok** usage quotas.

### Claude (same numbers as claude.ai → Settings → Usage)

- Plan name (e.g. Max 20x)
- Current session (5-hour window) % + reset countdown
- Weekly limits (All models, Fable, …) % + reset times
- Extra usage credits when enabled

### Codex (same numbers as chatgpt.com/codex/settings/usage)

- Plan (Plus, Pro, …)
- Session / weekly usage toward the limit, with % and reset times
- Credits balance and available limit resets when present

### Grok

- Weekly limit % used (matches the `Weekly limit left` line in the Grok CLI)
- Monthly limit % used, with $ spent / remaining
- Reset time for each period

Whichever meters the billing API returns are shown; accounts with only one
period show one bar.

## Requirements

- KDE Plasma 6
- Python 3
- **Claude:** [Claude Code](https://claude.com/claude-code) logged in
  (`~/.claude/.credentials.json`)
- **Codex:** [Codex CLI](https://developers.openai.com/codex) logged in
  (`~/.codex/auth.json`, `codex login`)
- **Grok:** [Grok Build CLI](https://x.ai) logged in (`~/.grok/auth.json`,
  `grok login`)

## Install

```bash
./install.sh
```

Then: right-click the desktop or a panel → **Add Widgets** → search **AI Quota**.

To pick up QML edits after changing source:

```bash
rm -rf ~/.cache/plasmashell/qmlcache
systemctl --user restart plasma-plasmashell.service
```

## How data is fetched

A small Python helper ships inside the package and is polled every 60s:

| Provider | Endpoint | Credentials |
| --- | --- | --- |
| Claude | `GET https://api.anthropic.com/api/oauth/usage` | Claude Code OAuth token |
| Codex | `GET https://chatgpt.com/backend-api/wham/usage` | Codex ChatGPT OAuth token |
| Grok | `GET https://cli-chat-proxy.grok.com/v1/billing` | Grok OIDC token |

Tokens never leave your machine except to those HTTPS endpoints. Grok and Codex
tokens are refreshed in place when near expiry (and written back to the CLI auth
files so the CLIs keep working).

Smoke-test without Plasma:

```bash
python3 package/contents/code/fetch_quota.py | jq .
```

## Layout

```
package/
  metadata.json
  contents/
    ui/main.qml              # panel compact + full card
    code/fetch_quota.py      # data source
install.sh
```

## Disclaimer

Unofficial. Not affiliated with Anthropic, OpenAI, or xAI. The Claude and Codex
usage endpoints are reverse-engineered from their CLIs and may change. Use only
with your own accounts.

## License

MIT
