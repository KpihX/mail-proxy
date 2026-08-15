# mail-proxy

Mail administrative proxy — RPC CLI for IMAP/SMTP accounts, messages, folders and labels.

> **Status:** 🟢 **IMPLEMENTED — 24 actions.** See `CONTRACT.md` for the authoritative action,
> HITL, verification, and transport contracts.

Refonte of [`mail-mcp`](https://github.com/KpihX/mail-mcp) (MCP server, 25 tools) into a non-MCP
CLI built on the exact model of [`tick-proxy`](https://github.com/KpihX/tick-proxy), itself built
on the ADN of [`tg-proxy`](https://github.com/KpihX/tg-proxy).

---

## Architecture

Single binary with two namespaces:

```bash
mail-proxy admin setup|status|reset|purge       # Admin operations (always JSON)
mail-proxy do <action> [payload|file]           # 24 RPC actions (JSON default)
```

### `mail-proxy admin`

| Command | Description |
|---------|-------------|
| `setup` | Credential setup via HITL web form (login + password per account) — writes `~/.config/mail-proxy/.env` |
| `status` | Masked credentials per account, live IMAP/SMTP probes, permissions |
| `reset` | Clear all credentials (HITL-confirmed) |
| `purge` | Delete the config directory (HITL-confirmed) |

### `mail-proxy do` — 24 actions

| Domain | Actions |
|--------|---------|
| **Inbox** | `inbox-check` · `inbox-digest` |
| **Messages** | `message-list` · `message-info` · `message-search` · `message-thread` · `message-mark` · `message-move` · `message-archive` · `message-trash` · `message-spam` · `message-delete` |
| **Compose** | `message-send` · `message-reply` · `message-forward` · `message-draft` |
| **Folders** | `folder-list` · `folder-create` · `folder-rename` · `folder-delete` |
| **Attachments** | `attachment-download` |
| **Labels** | `label-list` · `label-set` |
| **Escape hatch** | `raw` |

Full catalog with source-tool mapping and HITL requirement: **`CONTRACT.md`**.

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

# Compose actions always open the HITL review page before sending
mail-proxy do message-send '{"to":["x@y.fr"],"subject":"Rendez-vous","body_text":"Dispo demain ?"}'

# Meta options
mail-proxy do folder-list -f table            # table instead of JSON
mail-proxy do message-search ./f.json -o /tmp/result.json
# (verification is NOT a CLI option — it runs automatically via @require_verification)
```

### Meta options (`do` only)

| Option | Role |
|--------|------|
| `--output-file <path>` / `-o` | Write the full envelope to a file |
| `--format json\|table` / `-f` | Display format (default: `json`) |
| `--help` / `-h` | Full docstring + payload schema |

> **No `--verify/-V` flag.** Verification is structural — the `@require_verification` decorator on
> the handler runs it automatically. See `CONTRACT.md` → **Verification model**.

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

## Config

Single `.env` at `~/.config/mail-proxy/.env`, created by `mail-proxy admin setup`:

```env
MAIL_POLY_LOGIN=ivann.kamdem-pouokam          # IMAP/SMTP login (required)
MAIL_POLY_PASS=correct-horse-battery-staple   # IMAP/SMTP password (required)
```

- Accounts are declared in `src/mail_proxy/config.py` (hosts, ports, e-mail, signature) — the
  default account is `poly` (Polytechnique Zimbra).
- Every account field is overridable from the same `.env` (`MAIL_<ID>_IMAP_HOST`, …) — see
  `.env.example` for the fully commented template.
- All actions accept an optional `"account_id"` — omit it to use the default account.

**The credentials are never committed.** They live only in the chmod-600 `.env`, written by the
HITL form. Process environment always wins over the file (shell/bw-env injection works without
any magic).

### Security

```bash
chmod 700 ~/.config/mail-proxy
chmod 600 ~/.config/mail-proxy/.env
```

---

## HITL

Human-in-the-Loop via a local web UI. Compose, drafts, irreversible deletions and secret-touching
operations open a browser page for review, editing, and approval/rejection.

**HITL-required:** `message-send` · `message-reply` · `message-forward` · `message-draft` ·
`message-delete` · `folder-delete` · `raw` · `admin setup` · `admin reset` · `admin purge`.

Every irreversible delete pre-reads and locks its target identity before HITL: an absent,
duplicate, or reviewer-swapped target fails without opening a review page. The deletion is then
confirmed by polling the read until the resource is absent (`data.verification`).

Everything else (reads, views and non-listed writes) runs without prompting — but reversible
moves, flag and label changes still carry the mandatory read-back verification.

---

## Install

```bash
uv tool install .              # production
uv tool install --editable .   # development
```

**No Docker.** This project is a local CLI by design.

---

## Development

```bash
make check   # smoke + ruff + py_compile + pyright + pytest (95 tests)
make smoke   # CLI + registry integrity (24 actions, 0 duplicates)
make uv-link # editable install
```

See `Makefile` for the full target list, `AGENTS.md` for the agent working context, and
`CONTRACT.md` for the architecture contract.
