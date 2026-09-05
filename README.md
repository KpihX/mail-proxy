# mail-proxy

Mail administrative proxy — RPC CLI for IMAP/SMTP accounts, messages, folders and labels.

> **Status:** 🟢 **IMPLEMENTED — 24 actions + OAuth2 + App Password.** See `CONTRACT.md` for the
> authoritative action, HITL, verification, and transport contracts.

Refonte of [`mail-mcp`](https://github.com/KpihX/mail-mcp) (MCP server, 25 tools) into a non-MCP
CLI built on the exact model of [`tick-proxy`](https://github.com/kpihx-labs/tick-proxy), itself built
on the ADN of [`tg-proxy`](https://github.com/kpihx-labs/tg-proxy).

---

## Architecture

Single binary with two namespaces:

```bash
mail-proxy admin doctor|status|auth login|status|logout|default|reset|purge   # Admin (always JSON)
mail-proxy do <action> [payload|file]                                  # 24 RPC actions
```

### `mail-proxy admin`

| Command | Description |
|---------|-------------|
| `doctor` | Scan config directory, auto-fix permission problems |
| `status` | Complete status: accounts, auth state, permissions, probes, issues |
| `auth login` | Add one account via smart HITL form (type selector → email → password/OAuth2) → writes `accounts.json` + `.env` atomically |
| `auth status` | Show auth state per account (configured / missing password) |
| `auth logout` | Remove password for one account (HITL-confirmed) |
| `auth default -a <account>` | Set the default account by ID, alias, or email prefix (HITL-confirmed) |
| `reset` | Clear all passwords (HITL-confirmed) |
| `purge` | Delete the config directory (HITL-confirmed) |

### `mail-proxy do` — 37 actions

| Domain | Actions |
|--------|---------|
| **Inbox** | `inbox-check` · `inbox-digest` |
| **Messages** | `message-list` · `message-info` · `message-search` · `message-thread` · `message-mark` · `message-move` · `message-archive` · `message-trash` · `message-spam` · `message-delete` |
| **Compose** | `message-send` · `message-reply` · `message-forward` · `message-draft` |
| **Folders** | `folder-list` · `folder-create` · `folder-rename` · `folder-delete` |
| **Attachments** | `attachment-download` |
| **Labels** | `label-list` · `label-set` |
| **Zimbra** | `zimbra-tag-list` · `zimbra-tag-items` · `zimbra-tag-create` · `zimbra-tag-delete` · `zimbra-tag-apply` · `zimbra-tag-remove` |
| **Escape hatch** | `raw` |

Full catalog with source-tool mapping and HITL requirement: **`CONTRACT.md`**.

---

## Setup

### 1. Install

```bash
uv tool install .              # production
uv tool install --editable .   # development
```

### 2. Doctor (fix permissions)

```bash
mail-proxy admin doctor
```

### 3. ⚠️ Enable IMAP on your account (CRITICAL — read this first!)

**Before** doing anything else, you MUST enable IMAP on your email account. Without this,
no authentication method will work for reading emails.

#### Outlook / Hotmail / Live

1. Go to: https://outlook.live.com/mail/0/options/mail/accounts
2. Scroll to **"POP and IMAP"** section
3. Set **"Let devices and apps use IMAP"** to **Enabled**
4. Save

Or try: Settings (⚙️) → Mail → Accounts → POP and IMAP → Enable IMAP.

> ⚠️ If you don't see the IMAP option, your account may be managed by an organization
> (Microsoft 365). In that case, the admin IT must enable IMAP in the admin center:
> https://admin.microsoft.com → Settings → Org settings → Modern authentication →
> ensure IMAP is allowed.

#### Gmail

IMAP is enabled by default on Gmail. No action needed.

#### Zimbra / custom server

IMAP is enabled by default on most Zimbra installations. No action needed.

### 4. Create App Password (if using MFA — recommended)

#### Gmail

1. **Enable 2-Step Verification** (required first):
   → https://myaccount.google.com/signinoptions/two-step-verification

2. **Generate an App Password**:
   → https://myaccount.google.com/apppasswords
   → Select app: **Mail** → Select device: **Other** → type a name → **Generate**
   → Copy the 16-character password (e.g. `abcd efgh ijkl mnop`)

3. **Use it in mail-proxy**:
   → `mail-proxy admin auth login` → Type: Gmail → Email: `you@gmail.com` → Password: the app password

> ⚠️ If the apppasswords page says "No app passwords available", make sure 2-Step Verification
> is **active** (not just set up). The app passwords option only appears after 2FA is on.

#### Microsoft 365 / Outlook / Hotmail

1. **Go to security proofs page**:
   → https://account.live.com/proofs/manage/additional?mkt=fr-FR&refd=account.microsoft.com&refp=security

2. **Create an app password**:
   → Scroll to "Mots de passe d'application" / "App passwords"
   → Click **"Créer un mot de passe d'application"** / **"Create a new app password"**
   → Copy the generated password

3. **Use it in mail-proxy**:
   → `mail-proxy admin auth login` → Type: Outlook → Email: `you@outlook.com` → Password: the app password

> ⚠️ 2-Step Verification must be **enabled** for app passwords to appear. If the section is
> missing, enable 2FA first at the same URL.

#### Zimbra (Polytechnique / custom)

No app password needed — use your **regular webmail password** directly.

### 5. Add your first account

```bash
mail-proxy admin auth login
```

Opens a **smart HITL form** in your browser:

1. **Select provider type** (Gmail / Outlook / Zimbra / Custom)
2. **Choose auth method**:
   - **App Password** — simple, works for Gmail + Zimbra + Outlook (if IMAP enabled)
   - **OAuth2** — modern, works for Gmail + Outlook (no app password needed, browser consent)
3. **Fill in your email** — IMAP/SMTP hosts are auto-detected from the domain
4. **Set an account ID** (auto-generated from email, or custom)
5. **Add aliases** (comma-separated, optional)
6. **Enter your password** (for App Password method) OR **authorize in browser** (for OAuth2)

The form writes **both** `accounts.json` + `.env` atomically — no partial state.

### 6. Verify

```bash
mail-proxy admin status        # see all accounts, permissions, IMAP/SMTP probes
mail-proxy admin doctor        # fix any permission issues
```

### 7. Use

```bash
mail-proxy do inbox-check                        # default account
mail-proxy do inbox-check -a work                # specific account by alias
mail-proxy do message-send '{...}' -a gmail      # specific account by ID
mail-proxy admin auth default -a work             # select the default account explicitly
```

---

## Authentication Methods

Two methods, both fully supported for Gmail and Outlook:

### App Password

- **How it works**: Generate a one-time password on the provider's website, use it like a regular password
- **Best for**: Quick setup, no browser consent needed
- **Works on**: Gmail ✅, Outlook ✅, Zimbra ✅
- **Requires**: 2FA enabled + IMAP enabled on the account
- **Secret storage**: `MAIL_<ID>_PASS` in `~/.config/mail-proxy/.env`

### OAuth2

- **How it works**: Browser-based consent flow, get an access token + refresh token
- **Best for**: Modern standard, no password exchange, scope control
- **Works on**: Gmail ✅, Outlook ✅
- **Requires**: IMAP enabled on the account (for Microsoft: well-known Thunderbird client ID)
- **Secret storage**: `~/.config/mail-proxy/tokens/<id>.json` (chmod 600)
- **Token refresh**: Automatic — when the access token expires, the refresh token gets a new one

#### Microsoft OAuth2 (Device Code Flow)

```
1. mail-proxy admin auth login → select Outlook → OAuth2
2. Terminal shows: "Go to https://microsoft.com/devicelogin"
3. Terminal shows: "Enter code: ABCD-EFGH"
4. Open browser → enter code → consent → done
5. Tokens stored automatically → IMAP + SMTP work
```

No Azure AD app registration needed — uses Thunderbird's well-known client ID.

#### Google OAuth2 (Authorization Code Flow)

```
1. mail-proxy admin auth login → select Gmail → OAuth2
2. Browser opens → Google consent screen → approve
3. Redirected to localhost → tokens captured automatically
4. IMAP + SMTP work with XOAUTH2
```

Requires a Google Cloud project with OAuth 2.0 credentials:
- Set `MAIL_OAUTH2_GOOGLE_CLIENT_ID` and `MAIL_OAUTH2_GOOGLE_CLIENT_SECRET` in `.env`

### Comparison

| | App Password | OAuth2 |
|---|---|---|
| **Gmail** | ✅ Simple | ✅ Modern |
| **Outlook** | ✅ Works (if IMAP enabled) | ✅ Works (device code flow) |
| **Zimbra** | ✅ Direct | ❌ Not supported |
| **Setup complexity** | Low — generate password | Medium — browser consent |
| **Token refresh** | Never (permanent password) | Automatic (refresh token) |
| **Secret storage** | `.env` (password) | `tokens/<id>.json` (OAuth tokens) |
| **Scope control** | Full access | Configurable |

---

## Config

Three files, one purpose each:

| File | Purpose |
|------|---------|
| `~/.config/mail-proxy/accounts.json` | Account definitions (email, aliases, hosts, auth_method) — NOT secrets |
| `~/.config/mail-proxy/.env` | Passwords ONLY (`MAIL_<ID>_PASS`) — chmod 600 (for App Password auth) |
| `~/.config/mail-proxy/tokens/<id>.json` | OAuth2 tokens (access_token, refresh_token, expires_at) — chmod 600 (for OAuth2 auth) |

The login **IS** the email address from `accounts.json` — no separate `MAIL_*_LOGIN` needed.

### Email domain auto-detection

When an account's email matches a known domain, IMAP/SMTP hosts are resolved automatically:

| Domain | IMAP | SMTP |
|--------|------|------|
| `gmail.com` | `imap.gmail.com:993` | `smtp.gmail.com:587` |
| `outlook.com` / `hotmail.com` / `live.com` | `outlook.office365.com:993` | `smtp.office365.com:587` |
| `polytechnique.edu` | `webmail.polytechnique.fr:993` | `webmail.polytechnique.fr:587` |

For custom servers, the `auth login` form lets you specify `imap_host` and `smtp_host` manually.

### Security

```bash
chmod 700 ~/.config/mail-proxy
chmod 600 ~/.config/mail-proxy/.env
chmod 600 ~/.config/mail-proxy/accounts.json
chmod 600 ~/.config/mail-proxy/tokens/
```

Run `mail-proxy admin doctor` to auto-fix any permission issues.

---

## Troubleshooting

### IMAP login rejected

**Most common cause**: IMAP is not enabled on your account.

1. Check your provider's settings (see "Enable IMAP" section above)
2. For Outlook: https://outlook.live.com/mail/0/options/mail/accounts → POP and IMAP → Enable
3. For Gmail: IMAP is enabled by default

### "Basic authentication is disabled" (Microsoft)

This error can be misleading. It often means **IMAP is disabled**, not that Basic Auth is
blocked. Enable IMAP first, then retry with app password.

### OAuth2 device code expired

The device code expires after ~15 minutes. Run `mail-proxy admin auth login` again to get a
new code.

### Token refresh failed (OAuth2)

If the refresh token is expired or revoked, run `mail-proxy admin auth login` again to
re-authorize.

### Gmail Web shows a yellow star, but `message-mark` says unstarred

Gmail maps its star to IMAP `\\Flagged`. `message-mark` verifies its write by reading back
`UID FETCH FLAGS`; `message-search` with `flagged_only:true` independently checks Gmail with
`SEARCH FLAGGED`. Both are normal `mail-proxy do` actions — no raw IMAP command is needed.

On 2026-09-03, Gmail Web retained a yellow star in `[Gmail]/Spam` after repeated page refreshes
while both checks reported the exact same message unstarred (`FLAGS: []`, absent from
`SEARCH FLAGGED`). Another visually starred message in that folder correctly returned
`\\Flagged`. This is a Gmail Web/IMAP state divergence, not a wrong account, UID, folder, or
generic flags-parser result.

When this happens:

1. Do **not** repeat a flag mutation blindly.
2. Use `message-info` for the target UID and `message-search` with `flagged_only:true` in the
   same folder. If both agree, `message-mark` has verified the IMAP state correctly.
3. Treat `data.verification.ok:true` as proof of **IMAP state only**, not proof that an already
   loaded Gmail Web row has rendered the same state.
4. If Gmail Web parity is required, a future provider-specific Gmail API path must read/write
   the Gmail `STARRED` label explicitly; generic IMAP must not silently claim that guarantee.

---

## HITL

Human-in-the-Loop via a local web UI. The `auth login` form has a dedicated template with a
provider type selector, auth method selector (App Password / OAuth2), auto-fill, and existing
accounts display. Other HITL operations (compose, drafts, irreversible deletes) use the
standard review form.

**HITL-required:** `auth login` · `auth logout` · `message-send` · `message-reply` ·
`message-forward` · `message-draft` · `message-delete` · `folder-delete` · `raw` ·
`admin reset` · `admin purge`.

---

## Usage

```bash
# Discover everything — the docstrings ARE the documentation (≥3 examples each)
mail-proxy do --help                 # compact overview of all 24 actions
mail-proxy do message-send --help    # full docstring + exact payload schema

# Payload: inline JSON or a file path
mail-proxy do inbox-check
mail-proxy do message-list '{"folder":"Archive","limit":5}'
mail-proxy do message-search ./filter.json
mail-proxy do folder-delete '{"names":["Projects/2025","Projects/2026"]}'
mail-proxy do attachment-download '{"uid":42,"filename":"diagram.png"}'
# → ~/Downloads/Mail-Proxy/<account-id>/diagram.png (created automatically)
mail-proxy do attachment-download '{"uid":42,"filename":"diagram.png","save_path":"~/Downloads/Mail-Proxy/"}'
# → ~/Downloads/Mail-Proxy/diagram.png
mail-proxy do message-reply '{"uid":42,"body_text":"See attached","attachments":["/absolute/path/diagram.png"]}'

# Compose actions always open the HITL review page before sending
mail-proxy do message-send '{"to":["x@y.fr"],"subject":"Rendez-vous","body_text":"Dispo demain ?"}'

# Meta options
mail-proxy do folder-list -f table            # table instead of JSON
mail-proxy do message-search ./f.json -o /tmp/result.json
```

### `raw`: complete protocol escape hatch

`raw` is always browser-HITL-approved. It can express every normal action through one or more
protocol primitives and more, but is not an unbounded code-execution surface:

| Protocol | Coverage |
|---|---|
| `imap` (default) | Any IMAP command, including flags, folders, searches, copies, moves and extensions; isolated connection |
| `smtp` | Any prebuilt RFC822/MIME message through the account SMTP transport |
| `gmail-api` | Any Gmail REST endpoint, including canonical `STARRED` label operations; Google OAuth2 only |
| `zimbra-soap` | Any authenticated Zimbra SOAP operation XML; provider-native tags and item actions |

```bash
mail-proxy do raw '{"protocol":"imap","method":"UID","args":["FETCH","42","(FLAGS)"],"select":"INBOX"}'
mail-proxy do raw '{"protocol":"smtp","method":"send-rfc822","params":{"recipients":["a@b.fr"],"rfc822_base64":"..."}}'
mail-proxy do raw '{"protocol":"gmail-api","method":"post","endpoint":"/users/me/messages/ID/modify","payload":{"addLabelIds":["STARRED"]}}'
mail-proxy do raw '{"protocol":"zimbra-soap","method":"GetTagRequest","payload":"<GetTagRequest xmlns=\"urn:zimbraMail\"/>","account_id":"poly"}'
```

Native Zimbra tags accept batches: `zimbra-tag-create` accepts `names`; delete accepts `tag_ids`;
apply/remove accept both `tag_ids` and `item_ids`. Delete, apply, and remove require HITL.

Raw returns the provider response and carries no automatic verification. It never silently changes
protocol, and never executes shell, filesystem, Python, or arbitrary runtime code.

### Meta options (`do` only)

| Option | Role |
|--------|------|
| `--output-file <path>` / `-o` | Write the full envelope to a file |
| `--format json\|table` / `-f` | Display format (default: `json`) |
| `--help` / `-h` | Full docstring + payload schema |

Attachment downloads default to `~/Downloads/Mail-Proxy/<account-id>/<filename>` and create
the account directory as needed. `save_path` accepts either an explicit file path or a directory
path ending in `/`; a directory retains the attachment filename.

---

## Output format

Every response carries a `meta` section:

```json
{
  "meta": {
    "status": "ok",
    "comment": "",
    "edited": false
  },
  "data": { }
}
```

`stdout` is **pure JSON** — logs and HITL prompts go to `stderr`, so `mail-proxy do … | jq` always
works. Every execution also autosaves to `/tmp/mail-proxy-autosave/{action}_{timestamp}.json`.

---

## Development

```bash
make check   # smoke + ruff + py_compile + pyright + pytest
make smoke   # CLI + registry integrity (24 actions, 0 duplicates)
make uv-link # editable install
```

See `Makefile` for the full target list, `AGENTS.md` for the agent working context, and
`CONTRACT.md` for the architecture contract.
