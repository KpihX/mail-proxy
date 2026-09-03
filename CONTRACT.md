# mail-proxy — Architecture Contract

> **Status:** 🟢 **IMPLEMENTED — 24 actions.** This document is the authoritative architecture
> contract for `mail-proxy`, the non-MCP IMAP/SMTP CLI built on the exact ADN of `tick-proxy`
> (`$HOME/KpihX-Labs/tick_proxy`), itself built on the ADN of `tg-proxy`.

---

## Mission

Total refonte of the MCP `mail-mcp` (`$HOME/Work/AI/MCPs/mail_mcp`) into a non-MCP CLI proxy that
follows **exactly** the `tick-proxy` model (`$HOME/KpihX-Labs/tick_proxy`):

- **Single binary, two namespaces** — `mail-proxy do <action>` (RPC) + `mail-proxy admin <action>` (always JSON)
- **Flat kebab-case actions** — ONE level after `do`, pure JSON-RPC, payload inline or file
- **`meta` + `data` envelope** — every response, always
- **Docstring-driven `--help`** — the docstring IS the documentation (single source of truth);
  **≥3 real examples per action docstring** (KπX rule 2026-08-12), more for the escape hatch
- **HITL web UI** — destructive, content-writing and secret-touching operations require human approval
- **Autosave** — every `do` execution snapshots to `/tmp/mail-proxy-autosave/`
- **Python + uv + Typer + Pydantic + Rich** — same stack as `tick-proxy` (IMAP via `imapclient`,
  SMTP via stdlib `smtplib`, HTML→text via `html2text`)
- **NO Docker** — explicitly excluded (same decision as `tick-proxy`)

**Location:** `$HOME/KpihX-Labs/mail-proxy/` — sibling of `tick_proxy/`.

---

## Mantras

- **0 Hardcoding · 100% Flexibility** — no hardcoded endpoints in logic, no in-repo `.env`,
  every account host/port/e-mail/display-name and timeout overridable from `~/.config/mail-proxy/.env`.
- **0 Magic · 100% Transparency** — every IMAP/SMTP exchange is explicit; read-back verification is a
  structural decorator (`@require_verification`), never a hidden retry loop.
- **0 Trust · 100% Control** — secrets live only in `~/.config/mail-proxy/.env` (never in the repo,
  chmod 600); destructive actions preflight then pass through HITL with their identities locked;
  `raw` gives full escape-hatch access with approval.
- **Preflighted destructive review** — deletions read their declared targets before HITL and lock
  their identity fields. The approval payload cannot redirect the write to a different resource;
  absent targets fail without opening a review page.

---

## Design — Single Binary, Namespaced CLI

```
mail-proxy
   │
   ├── admin <action>                       # ALWAYS JSON — credential lifecycle
   │   ├── setup                            # HITL web form → writes ~/.config/mail-proxy/.env
   │   ├── status                           # masked credentials, live IMAP/SMTP probes
   │   ├── reset                            # clear all credentials (HITL)
   │   └── purge                            # delete the config directory (HITL)
   │
   └── do <action> [payload|file] [--output-file/-o] [--format/-f] [--help/-h]
                                            # RPC — 24 flat actions, JSON payload (inline or file)
```

### `mail-proxy admin` — Admin (ALWAYS JSON to stdout — hardcoded, no `--format`)

| Command | Role | Output | HITL | Backend |
|---------|------|--------|:----:|---------|
| `mail-proxy admin setup` | Credential setup via HITL web form — login + password for **ALL declared accounts** (poly, outlook, gmail, …) | JSON (final) | ✅ | local file write |
| `mail-proxy admin status` | Auth state: per-account masked credentials, live IMAP/SMTP probes for **EVERY account**, config path, permissions | JSON | ❌ | IMAP + SMTP probe |
| `mail-proxy admin reset` | Clear all credentials from the config file | JSON | ✅ | local file write |
| `mail-proxy admin purge` | Delete the config directory (uninstall hint printed) | JSON | ✅ | local deletion |

**`admin setup` replaces the mail-mcp admin surface** (CLI `mail-admin` + HTTP `/admin/*` +
Telegram bot + SSH exec). The old surface collapses into ONE HITL web form with **two persisted
fields per account**: `MAIL_<ID>_LOGIN` and `MAIL_<ID>_PASS`. Semantics are explicit, not magic:

| Form state | Effect on `.env` |
|------------|------------------|
| Field left **unchanged** | key preserved as-is |
| Field **filled** | key overwritten with the new value |
| Field **emptied + `clear` checkbox ticked** | key removed from `.env` |

**Auth & password policy — credentials are NEVER committed.** `.env` holds at most the per-account
login/password pairs (required for IMAP/SMTP — there is no token tier) plus optional endpoint
overrides. The file is chmod 600, created by `admin setup`, and never enters the repository.
Process environment always wins over the file (`os.environ.setdefault`), so bw-env / shell
injection remains possible without any magic.

**Admin never accepts `--format` or `--output-file`** — passing either exits **2** with an error envelope.

### `admin` walkthroughs — what really happens

**`mail-proxy admin setup`**
1. HITL web server starts on an OS-assigned free port; the browser opens the form.
2. The form shows login + password for every account declared in `config.py` (default: `poly`),
   pre-filled from the current `.env` when present.
3. On submit, `config.py` writes each filled field to `~/.config/mail-proxy/.env` (`chmod 600`);
   untouched fields keep their existing value; `clear`-checked fields are removed.
4. Exit 0 with `{"meta":{"status":"ok"},"data":{"config":"~/.config/mail-proxy/.env","fields":["MAIL_POLY_LOGIN","MAIL_POLY_PASS"]}}`.

**`mail-proxy admin status`** (read-only, no HITL, always JSON)
1. Reads `.env` and reports which keys are present per account — **masked** (`ivan…uokam`), never full values.
2. Probes IMAP: real `imapclient` connect + login → `imap.reachable` / `imap.auth_ok`.
3. Probes SMTP: real `smtplib` connect + ehlo → `smtp.reachable`.
4. Reports config path, directory/file permissions (with `chmod` fix hints) and the binary path.
   Exit 0. Never asks for anything.

### `mail-proxy do` — RPC Actions (JSON default, table via `--format/-f`)

**Meta options (ONLY for `do`, every `--` has its `-`):**

| Option | Role |
|--------|------|
| `--output-file <path>` / `-o <path>` | Write the full envelope to a file (path required) |
| `--format json\|table` / `-f json\|table` | Display format (default: `json`) |
| `--help` / `-h` | Full docstring + Pydantic payload schema for that action |
| *(positional)* `payload` | Inline JSON `'{"k":"v"}'` **or** a file path `./payload.json` |

> **No `--verify/-V` flag.** Verification is NOT a CLI option — it is a **structural decorator**
> (`@require_verification`) baked into the handler of the actions that need it. It cannot be
> forgotten or bypassed: `cli.py` has no way to skip it. See **Verification model**.

**Output envelope — EVERY response (verification appears only in `data` when required):**

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

| `meta` field | Values | Meaning |
|--------------|--------|---------|
| `status` | `ok` · `approved` · `rejected` · `error` | `approved`/`rejected` only when HITL was involved |
| `comment` | free text | the HITL reviewer's comment (empty if none) |
| `edited` | `true` · `false` | the HITL reviewer modified the payload before approving |
| `verification` | — | never present in `meta` |

> **No `verified` boolean and no `meta.verification`.** An action that declares
> `@require_verification(...)` adds one `data.verification` comparison block; actions without the
> decorator have no verification field at all.

**Pre-check (ALL `do` commands):** `~/.config/mail-proxy/.env` must exist and expose the login and
password of the (default) account. Checked **once** at the start of any `do` command; the error
names the missing keys and points to `mail-proxy admin setup`.

**Autosave:** every `do` execution writes `/tmp/mail-proxy-autosave/{action}_{YYYYmmdd_HHMMSS}.json`.
When `-o` is given, the file path is printed instead of the autosave path (both are always written).

---

## Actions — FLAT, ONE level after `do` (24 actions)

Naming convention (inherited from `tg-proxy`/`tick-proxy`):
**`<domain>-<verb>`, kebab-case, domain FIRST.** All `mail-mcp` `verb_noun` names are flipped.

### Inbox (2)

| Action | Source tool (`mail-mcp`) | HITL | Notes |
|--------|--------------------------|:----:|-------|
| `inbox-check` | `check_inbox` | ❌ | unread count + last N summaries — entry point |
| `inbox-digest` | `daily_digest` | ❌ | unread / flagged / received-today overview |

### Messages (10)

| Action | Source tool | HITL | Notes |
|--------|-------------|:----:|-------|
| `message-list` | `list_messages` + `find_unread` | ❌ | `unseen_only:true` replaces `find_unread` |
| `message-info` | `get_message` | ❌ | full body + attachments metadata by UID |
| `message-search` | `search_messages` | ❌ | IMAP filters + client-side regex (full engine) |
| `message-thread` | `get_thread` | ❌ | conversation view by Message-ID (oldest-first) |
| `message-mark` | `mark_messages` | ❌ | seen/flagged/answered/draft — reversible, **read-back verified** |
| `message-move` | `move_messages` | ✅ | reviewer-confirmed move, **read-back verified** |
| `message-archive` | `archive_messages` | ✅ | reviewer-confirmed move to auto-detected Archive folder, **verified** |
| `message-trash` | `trash_messages` | ✅ | reviewer-confirmed recoverable delete, **verified** |
| `message-spam` | `mark_as_spam` | ✅ | reviewer-confirmed move to auto-detected Spam/Junk folder, **verified** |
| `message-delete` | `delete_messages` | ✅ | irreversible — preflight + identity lock + absence poll |

### Compose (4)

| Action | Source tool | HITL | Notes |
|--------|-------------|:----:|-------|
| `message-send` | `send_message` | ✅ | mandatory approval — reaches other people; Sent copy + optional bounce probe |
| `message-reply` | `reply_message` | ✅ | Re:/In-Reply-To/References, `reply_all`, local attachments, Sent copy + optional bounce probe |
| `message-forward` | `forward_message` | ✅ | Fwd: prefix + quoted original; Sent copy + optional bounce probe |
| `message-draft` | `save_draft` | ✅ | IMAP APPEND to Drafts — content write to the mailbox |

### Folders (4)

| Action | Source tool | HITL | Notes |
|--------|-------------|:----:|-------|
| `folder-list` | `list_folders` | ❌ | |
| `folder-create` | `create_folder` | ❌ | |
| `folder-rename` | `rename_folder` | ❌ | |
| `folder-delete` | `delete_folder` | ✅ | preflight (must exist) + identity lock + absence poll |

### Attachments (1)

| Action | Source tool | HITL | Notes |
|--------|-------------|:----:|-------|
| `attachment-download` | `download_attachment` | ❌ | default `~/Downloads/Mail-Proxy/<account-id>/`; explicit file/directory path or `ingest_base64:true` |

### Labels (2)

| Action | Source tool | HITL | Notes |
|--------|-------------|:----:|-------|
| `label-list` | `list_labels` | ❌ | PERMANENTFLAGS — Zimbra tags appear here |
| `label-set` | `set_labels` | ❌ | add/remove keywords — **read-back verified** |

### Escape hatch (1)

| Action | Source tool | HITL | Notes |
|--------|-------------|:----:|-------|
| `raw` | *(new — multi-protocol escape hatch)* | ✅ | arbitrary `imap`, RFC822 `smtp`, or `gmail-api` operation |

`raw` is **always HITL**. It is unlimited only inside its explicit selected protocol:

```bash
mail-proxy do raw '{"protocol":"imap","method":"STATUS","args":["INBOX","(MESSAGES UNSEEN)"]}'
mail-proxy do raw '{"protocol":"smtp","method":"send-rfc822","params":{"recipients":["a@b.fr"],"rfc822_base64":"..."}}'
mail-proxy do raw '{"protocol":"gmail-api","method":"post","endpoint":"/users/me/messages/ID/modify","payload":{"addLabelIds":["STARRED"]}}'
```

No silent protocol fallback exists. IMAP is the default; SMTP uses the configured account's
authenticated transport; Gmail API requires a Google OAuth2 account with the `mail.google.com`
scope. `raw` never exposes shell, filesystem, Python/runtime execution, or automatic verification.

### Action count

| Group | Count |
|-------|------:|
| Inbox | 2 |
| Messages | 10 |
| Compose | 4 |
| Folders | 4 |
| Attachments | 1 |
| Labels | 2 |
| Escape hatch | 1 |
| **TOTAL `do` actions** | **24** |

**Coverage proof — all 25 `mail-mcp` tools accounted for:**

| Fate | Count | Detail |
|------|------:|--------|
| Renamed 1:1 → `do` action | 23 | domain-first kebab rename |
| **Merged into an existing action** | 1 | `find_unread` → `message-list` (`unseen_only:true`) |
| Folded into `do --help` | 1 | `mail_guide` — docstrings are the single source of truth |
| **Total consumed** | **25** | ✅ zero gaps |
| **New** | +1 | `raw` |
| **Result** | **24** | `23 + 1` |

---

## Verification model — `@require_verification` decorator

IMAP is reliable at the protocol level, but "200 OK, nothing persisted" has a mail equivalent:
a `UID MOVE`/`STORE`/`EXPUNGE` can silently affect only part of the requested UIDs (or none) —
e.g. a stale UID, a folder race, or a server-side policy. `mail-proxy` centralizes the read-back
exactly like `tick-proxy`:

**No `--verify/-V` flag.** Verification is NOT a CLI option — it is a **structural decorator**
(`@require_verification`) applied directly on the handler of the actions that need it. `cli.py` has
no code path to skip it; the decorator is the single source of truth — non-bypassable by construction.

```python
# actions/base.py — the decorator (twin of hitl.require_approval)
def require_verification(*checks: str):
    """Mandatory post-write read-back verification, baked into the handler."""
    def decorator(func):
        @wraps(func)
        def wrapper(client, payload):
            result = func(client, payload)
            result["data"]["verification"] = verify_write(client, result, checks)
            return result
        return wrapper
    return decorator

# actions/messages.py — the handler returns (data, verification), built by itself
@require_verification("uids", "destination_folder")
def message_move(client: MailClient, p: MessageMovePayload) -> tuple[dict, Verification]:
    ...
```

**Verification lands in `data`, never in `meta`:**

```json
{
  "meta": {"status": "ok", "comment": "", "edited": false},
  "data": {
    "moved": 2, "from": "INBOX", "to": "Archive", "account": "poly",
    "verification": {
      "method": "UID SEARCH INBOX+Archive",
      "checked": ["destination_folder", "uids"],
      "expected": {"uids": [311, 312], "destination_folder": "Archive"},
      "actual": {"uids": [311, 312], "destination_folder": "Archive"},
      "ok": true
    }
  }
}
```

**Always-on verification (NOT optional — enforced by the decorator):**

| Action | Decorator checks | Why always verified |
|--------|------------------|---------------------|
| `message-move` | `uids`, `destination_folder` | UIDs must be gone from the source **and** present in the destination |
| `message-archive` / `message-trash` / `message-spam` | `uids`, `folder` | the auto-detected target folder must really hold the UIDs |
| `message-mark` | `uids`, `flags` | a flag must be applied on **EVERY** target UID (partial failure detection) |
| `label-set` | `uids`, `labels` | same all-UID semantics for keywords |
| `message-delete` | `deleted` | absence poll until every UID is gone (see `verify_absence`) |
| `folder-delete` | `deleted` | absence poll until the folder name disappears from LIST |

### Gmail `\\Flagged` verification boundary

For Gmail, a visual yellow star is specified to map to standard IMAP `\\Flagged`.
`message-mark` therefore writes with `UID STORE` and verifies the resulting state with
`UID FETCH FLAGS`; `message-search` with `flagged_only:true` independently uses
`SEARCH FLAGGED`. These are independent **normal `do`-action** checks of Gmail's IMAP
state — `raw` is neither needed nor appropriate for this validation.

**Observed divergence (2026-09-03):** a message in `[Gmail]/Spam` retained a yellow star in
Gmail Web after several manual refreshes, while both normal checks reported it unstarred:
`FETCH FLAGS → []` and `SEARCH FLAGGED → absent`. A neighbouring Gmail Spam message with a
yellow Web star correctly returned `\\Flagged` through both paths. This rules out an account,
UID, folder, or generic flag-parser mix-up for the divergent message.

Consequently, `data.verification.ok:true` for `message-mark` means **the target IMAP server
accepted and returned the requested standard-flag state**. It does **not** prove that an
already-loaded Gmail Web row renders the same state. During such a Gmail Web/IMAP divergence:

1. Do not blindly repeat a mutation just because the Web row disagrees.
2. Read the target via both normal actions: `message-info` (`FETCH FLAGS`) and
   `message-search` with `flagged_only:true` (`SEARCH FLAGGED`).
3. If both agree, report the IMAP state transparently; the generic IMAP backend has done its
   contractually defined verification.
4. If Gmail Web parity must be a hard guarantee, use a future **Gmail API** provider path that
   reads/writes Gmail's `STARRED` label. It must be exposed as an explicit provider-specific
   action or verification mode, never silently substituted for generic IMAP.

**Anti-bypass guard:** `make smoke` (registry integrity) checks — at import time — that every
action declaring required verification carries the `@require_verification` decorator, and the test
suite asserts the exact `REQUIRE_VERIFICATION` set. A missing decorator is a **hard error**.

### `@require_verification` — detailed scenario (always-on case: `message-delete`)

```
1. Intent
   mail-proxy do message-delete '{"uids":[42]}'

2. Preflight (before HITL)  → UID 42 must exist in INBOX (else: error, exit 1)
3. HITL                     → the reviewer sees the payload, uids/folder are LOCKED
4. Write  → UID STORE \Deleted + EXPUNGE      (folder INBOX)
5. Re-read→ UID SEARCH INBOX (bounded poll, 10 s) until 42 is absent
6. Compare→ expected {"deleted": "42"} vs actual {"deleted": "42"}
7. Report → data.verification.ok=true, exit 0   (mismatch → ok=false, exit 1)
```

---

## Config — one `.env` at `~/.config/mail-proxy/.env`

**No `config.yaml`. No in-repo `.env`. No cache. No magic.**
`mail-mcp` stored its `.env` inside the package and its accounts in `config.yaml` — both are
dropped. Account definitions (hosts, ports, e-mail, display name, signature) live as documented
constants in `config.py`; **every one of their fields is overridable** from this single file.

### Multi-account support

Three accounts are declared by default: `poly` (Zimbra), `outlook` (Microsoft 365), `gmail`
(Google Workspace). Each account has:

- A unique **id** (used as env prefix: `MAIL_<ID_UPPER>_`)
- Optional **aliases** for `-a` flag resolution (e.g. `poly` has aliases `x`, `polytechnique`)
- Default IMAP/SMTP endpoints (overridable via `.env`)
- The same env-prefix pattern: `MAIL_<ID>_LOGIN`, `MAIL_<ID>_PASS`

**Resolution by `-a` flag** — the user can pass any of:
- `-a poly` → exact id match
- `-a x` → alias match (resolves to `poly`)
- `-a user.name` → email prefix match (resolves to `poly` if email starts with it)

### Adding a new account

1. Add one `AccountDef` to the `ACCOUNTS` list in `config.py` (hosts, ports, label, aliases).
2. Add the matching `MAIL_<ID>_LOGIN` / `MAIL_<ID>_PASS` to `.env.example`.
3. Configure via `mail-proxy admin setup` — the HITL form shows ALL accounts.
4. Every action payload accepts `"account_id"` — omit it to use the default account.

```env
# ── mail-proxy configuration ──────────────────────────────────────────────────
# Location : ~/.config/mail-proxy/.env      (chmod 600 — contains secrets)
# Created  : mail-proxy admin auth login
# Resolution: -a <id|alias|email_prefix> → account

# ── SECRETS ONLY ─────────────────────────────────────────────────────────────
# The login IS the email address from accounts.json — only passwords go here.
# Accounts are created via `mail-proxy admin auth login` which writes both
# accounts.json AND .env atomically.

MAIL_<ID>_PASS=correct-horse-battery-staple
MAIL_TIMEOUT=15
```

**Config directory layout:**

```
~/.config/mail-proxy/
└── .env               # the file above (chmod 600)
```

**No log file — like `tg-proxy`/`tick-proxy`, logs go to stderr only.**

### Adding an account

1. Add one `AccountDef` to the `ACCOUNTS` list in `config.py` (documented, non-secret).
2. Add the matching `MAIL_<ID>_LOGIN` / `MAIL_<ID>_PASS` (+ optional overrides) to `.env.example`
   and configure them via `mail-proxy admin setup`.
3. Every action payload accepts `"account_id"` — omit it to use the default account.

No config.yaml, no code generation, no magic.

**Env-var prefix:** `MAIL_*`, harmonizing with `TG_*` and `TICK_*`.

---

## Architecture

```
mail-proxy
   │
   ├── admin setup|status|reset|purge           # ALWAYS JSON
   └── do <action> [payload|file] [-o] [-f]     # 24 flat RPC actions
       │
       ▼
┌────────────────────────────────────────────────────────────────┐
│  src/mail_proxy/                                               │
│  ├── cli.py            ONE Typer app: `do` + `admin` sub-typers │
│  │                     (thin — parse, dispatch, envelope, exit) │
│  ├── client.py         MailClient — account + lazy IMAP + SMTP  │
│  ├── models.py         SHARED types only: Output, OutputMeta,   │
│  │                     Verification, Status                     │
│  ├── config.py         ~/.config/mail-proxy/.env loader +       │
│  │                     documented ACCOUNTS defaults + overrides │
│  ├── display.py        Rich helpers: print_json / print_table   │
│  ├── doc.py            dynamic --help injection from docstrings │
│  ├── logger.py         stderr logger — systemd/journald (no file)│
│  ├── exceptions.py     MailProxyError · MailAPIError            │
│  ├── hitl.py           HITL web UI (free port, browser auto-open)│
│  ├── templates/        hitl.html · hitl.css                     │
│  │                                                              │
│  ├── api/              LOW-LEVEL MAIL LAYER (from mail_mcp/core)│
│  │   ├── models.py       Address, Attachment, Flag, Folder,     │
│  │   │                   Message, MessageSummary, SearchCriteria│
│  │   ├── imap.py         IMAPClient (imapclient) — search,      │
│  │   │                   fetch, flags, move, delete, keywords   │
│  │   └── smtp.py         SMTPClient (smtplib) — send, reply,    │
│  │                       forward, build_draft_bytes + signature │
│  │                                                              │
│  ├── actions/          THE 24 ACTIONS (from mail_mcp/tools)     │
│  │   ├── base.py         ActionDef, AccountScoped, decorators,  │
│  │   │                   compare, verify_absence                │
│  │   ├── registry.py     name → ActionDef map, duplicate = error│
│  │   ├── inbox.py        inbox-check · inbox-digest             │
│  │   ├── messages.py     message-* (10)                         │
│  │   ├── compose.py      message-send/reply/forward/draft       │
│  │   ├── folders.py      folder-* (4)                           │
│  │   ├── attachments.py  attachment-download                    │
│  │   ├── labels.py       label-list · label-set                 │
│  │   └── raw.py          raw (dedicated imaplib connection)     │
│  │                                                              │
│  └── admin.py          setup · status · reset · purge —         │
│                        SINGLE SOURCE OF TRUTH for admin logic   │
└────────────────────────────────────────────────────────────────┘
       │
       ▼
┌────────────────────────────────────────────────────────────────┐
│  ~/.config/mail-proxy/.env                                      │
│  /tmp/mail-proxy-autosave/{action}_{timestamp}.json             │
└────────────────────────────────────────────────────────────────┘
```

### Why a registry instead of a monolithic `client.py`

Identical rationale to `tick-proxy` (see its contract): at 24 actions the single-`client.py`
shape would exceed 2500 lines and repeat each action name in three places. The registry is
**explicit and single-sourced** — adding an action means adding ONE `ActionDef` to its domain
module; `cli.py` builds its Typer commands from the registry, nothing else.

```python
# actions/inbox.py — payload model and handler colocated, docstring = documentation
class InboxCheckPayload(AccountScoped):
    limit: int = Field(10, description="Max summaries to return")

def inbox_check(client: MailClient, p: InboxCheckPayload) -> dict:
    """Quick inbox check: unread count + last N message summaries.
    ...
    Examples:
        - Default check:
            `mail-proxy do inbox-check`
            → {"account":"poly","unread_count":14,...}
    """

ACTIONS = [ActionDef("inbox-check", InboxCheckPayload, inbox_check, group="Inbox")]
```

### Doc system (`tick-proxy` `doc.py`, unchanged)

Every handler carries a structured docstring with the mandatory sections
`Description` / `Parameters:` / `Examples:` — **≥3 real `→` examples per action** (KπX rule
2026-08-12), more for `raw` (≥5, arbitrary surface). `doc.py` extracts it and injects it into
Typer `help` (`get_full_help`) and into the `do --help` overview (`get_compact_help`), wrapping
`→` outputs in the `meta`+`data` envelope. **Result:** `mail-proxy do message-send --help` shows
the full docstring **plus** the exact Pydantic payload schema — this replaces `mail_guide` entirely.

### Multi-account support — the one structural addition over tick-proxy

TickTick has a single account; IMAP/SMTP can have many. This is handled with the minimum surface:
- Every payload inherits `AccountScoped` (`account_id: str | None` — omit → default account).
- `config.ACCOUNTS` is the documented account catalog; secrets and endpoint overrides are
  keyed `MAIL_<ID>_*` in the `.env`.
- `MailClient` resolves the account once per invocation; handlers never mention accounts.

### What differs from `tick-proxy` (deliberate, all documented)

| tick-proxy | mail-proxy | Why |
|------------|------------|-----|
| V1/V2 dual API + `v2` flag + `ensure_env(require_v2=…)` | single credential tier; `v2` field kept for ADN parity, always `False` | IMAP/SMTP authenticate with one login/password pair — no token hierarchy |
| `admin session-refresh` (transient password → token) | `admin reset`/`purge` only; re-run `setup` to change credentials | mail passwords do not expire like tokens; nothing transient to refresh |
| `task` review mode + document operations (`title_ops`…) | `review_mode="default"` only — one editable full-JSON page | the tick document-patch machinery is TickTick-specific; compose payloads are already the full message |
| `raw` = arbitrary HTTP endpoint on the shared transport | `raw` = arbitrary IMAP command on a **dedicated** imaplib connection | a raw command can corrupt imapclient's response parser; isolation keeps the shared connection safe |
| `verify_absence` tolerates HTTP 404 | `verify_absence` treats `[]`/`{}`/`None` as absent (no HTTP status) | IMAP reads return lists, not status codes |

---

## HITL — 100% Web UI

Same mechanism as `tg-proxy`/`tick-proxy` (`hitl.py`): a local HTTP server on an OS-assigned free
port (bind `127.0.0.1:0` — a fixed port would collide across concurrent invocations), the browser
auto-opens, the payload is editable, the reviewer approves or rejects, and the outcome is reported
in `meta`. Timeout 600 s → automatic `rejected`. If no browser is available the URL is printed
with an `ssh -L` hint.

**HITL-required — 7 `do` actions + 2 `admin` commands:**

| Reason | Actions |
|--------|---------|
| External side effect (reaches other people) | `message-send` · `message-reply` · `message-forward` |
| Content write to the mailbox | `message-draft` |
| Irreversible deletion | `message-delete` · `folder-delete` |
| Arbitrary protocol access | `raw` |
| Secrets | `admin setup` · `admin reset` · `admin purge` |

**Preflight + locked identity on every irreversible delete:** `message-delete` pre-reads every
target UID (absent UIDs fail before HITL — no review page wasted) and locks `uids`+`folder` in the
approval payload; `folder-delete` pre-reads the folder list and locks `name`. A reviewer-edited
identity rejects the command before any write. After approval, the delete is confirmed by polling
the read until the resource is absent (`verify_absence`, 10 s bounded) — the proof lands in
`data.verification`.

**Everything else (reads, reversible moves, flag changes, label changes) runs without prompting**
but reversible writes still carry the structural read-back verification.

---

## Error model & exit codes

| Case | Behavior | Exit |
|------|----------|-----:|
| Success | `{"meta":{"status":"ok",…},"data":…}` | 0 |
| HITL approved | `meta.status = "approved"` | 0 |
| HITL rejected / timeout | `meta.status = "rejected"`, `data = null` | 1 |
| Missing `.env` / missing credentials | error envelope naming the keys + hint `mail-proxy admin setup` | 1 |
| IMAP/SMTP login rejected | error envelope + hint `mail-proxy admin setup` | 1 |
| IMAP/SMTP unreachable | error envelope with host:port and the underlying cause | 1 |
| Invalid JSON / file not found | `MailProxyError` envelope | 1 |
| Pydantic validation error | error envelope listing offending fields | 1 |
| Verification failed (`@require_verification`) | `data.verification.ok = false` (block present) | 1 |
| `raw` command rejected by the server | `{"typ":"NO","data":[],"error":…}` envelope | 0 |
| `admin` + `--format`/`-o` | misuse error envelope | 2 |

**stdout is pure JSON.** All logs, HITL prompts and progress go to **stderr** — a piped
`mail-proxy do … \| jq` must never break.

---

## What is dropped from `mail-mcp` (and why)

| Dropped | Rationale |
|---------|-----------|
| `server.py` + `tools/*` FastMCP decorators | MCP plumbing — no MCP transport any more, the CLI *is* the interface |
| `http_app.py` (Starlette `/mcp` + `/admin/*` routes) | existed only to serve the remote Docker container |
| `admin/telegram.py` (Telegram admin bot) | remote-operation bridge for the container; a local CLI needs none |
| `admin/cli.py` + `admin/service.py` | folded into ONE `admin.py` (setup/status/reset/purge) |
| `tools/guide.py` (`mail_guide`) | replaced by docstring-driven `do --help` — single source of truth |
| `config.yaml` | folded into documented `config.py` account defaults + `.env` overrides |
| `daemon.py` (PID file lifecycle) | IMAP/SMTP are stateless per call — no persistent process to own |
| `deploy/` + `Dockerfile` + `.dockerignore` + `.gitlab-ci.yml` deploy stage | **Docker explicitly excluded** (same decision as `tick-proxy`) |
| `src/mail_mcp/.env` (in-repo secrets) | moved to `~/.config/mail-proxy/.env` |
| bw-env login-shell secret resolution (`zsh -l -c` probe) | 0 Magic — the `.env` is the single source; process-env override covers shell injection |
| `core/models.py` `SearchCriteria` client-side fields live in the payload model | `message-search` payload IS the criteria (flat, no double model) |

The ~2500 lines of real mail domain logic (imap_client, smtp_client, models, compose/read/manage
tools) are **ported, not rewritten** — the response shapes of every action are byte-compatible
with the mail-mcp tool outputs.

---

## Ecosystem impact — must be handled at implementation time

| Item | Action required |
|------|-----------------|
| `~/.config/opencode/opencode.jsonc` → `mcp.mail_fallback` | remove the MCP entry; agents call the CLI through `bash` (exactly as `tg-proxy` replaced the `tg` MCP) |
| `k-mail` skill (`allowed-tools: mcp__mail-fallback__*`) | rewrite to `Bash(mail-proxy *)`; the account map, gotchas and *What's up* slice stay valid (transport-agnostic) |
| `https://mail.kpihx-labs.com/mcp` deployment | goes away with the Docker/HTTP layer — confirm nothing else consumes it |
| `~/Work/AI/MCPs/mail_mcp/` | keep untouched as the reference implementation until `mail-proxy` reaches parity, then archive |
| PyPI `k-mail-mcp` | untouched; `mail-proxy` publishes as a new package |

---

## Infrastructure files (Docker excluded)

| File | Source | Note |
|------|--------|------|
| `pyproject.toml` | `tick-proxy` | single entry point `mail-proxy = "mail_proxy.cli:app"`, `uv_build` backend |
| `Makefile` | `tick-proxy` | **minus** all `docker-*` targets; smoke asserts 24 actions |
| `.gitignore` | `tick-proxy` | `.env` ignored, `.env.example` kept |
| `.env.example` | this document | the fully-commented block above |
| `templates/hitl.html` + `hitl.css` | `tick-proxy` | rebranded Mail-Proxy |
| `assets/signature_logo.png` | `mail-mcp` | the account signature logo (package asset) |
| `tests/` | `tick-proxy` + `mail-mcp` | registry integrity, models, config, admin, search criteria, MIME building, preflight/verification flows (90 tests) |

### Makefile targets

| Target | Action |
|--------|--------|
| `check` | `smoke` → ruff check --fix → ruff format → py_compile → pyright → pytest |
| `smoke` | `mail-proxy do --help` + registry integrity (24 actions, zero duplicates) |
| `uv-install` / `uv-link` / `uv-uninstall` / `uv-purge` | `uv tool` lifecycle |
| `uv-build` / `uv-publish` | sdist + wheel → PyPI |
| `git-push` / `push` | push to `github` **and** `gitlab` |
| `git-install-hooks` | pre-commit → `make check` |
| `release` | `check` → `git-push` → `uv-publish` |

**Remotes:** `github: git@github.com:KpihX/mail-proxy.git` · `gitlab: git@gitlab.com:kpihx/mail-proxy.git`

---

## Decisions requiring KπX validation

| # | Decision | Proposal | Impact if refused |
|---|----------|----------|-------------------|
| **D1** | Action naming | flip to **domain-first kebab** (`message-send`, not `send-message`) — matches `tg-proxy`/`tick-proxy` | keep `send_message` style; ADN broken |
| **D2** | `mail_guide` | drop — `do --help` / `do <action> --help` generated from docstrings (≥3 examples each) is the guide | keep a `guide` action duplicating docstrings |
| **D3** | 4 admin surfaces (CLI + HTTP + Telegram + SSH) | fold into ONE `admin setup|status|reset|purge` (HITL form, ALWAYS JSON) | keep the container-era admin stack |
| **D4** | `config.yaml` + in-package `.env` | drop — documented account defaults in `config.py`, secrets + overrides via `~/.config/mail-proxy/.env` | keep a second config file |
| **D5** | Env prefix | **`MAIL_*`** (harmonizes with `TG_*`/`TICK_*`) | any other prefix |
| **D6** | HTTP transport + Telegram bot + daemon + Docker | **drop** (they served the Docker deployment only) | must keep FastAPI + bot + PID lifecycle |
| **D7** | HITL scope | compose (external side effect) + drafts + irreversible deletes + `raw` + admin secrets; reversible moves/marks run without HITL but with mandatory read-back verification | narrower/wider policy |
| **D8** | `raw` implementation | arbitrary IMAP command on a **dedicated** imaplib connection (isolation from imapclient state) | raw on the shared connection (state corruption risk) |
| **D9** | Account catalog | accounts are documented `AccountDef`s in `config.py`, secrets `MAIL_<ID>_*` in `.env`, `account_id` payload field | external account registry |
| **D10** | Old repo | keep `~/Work/AI/MCPs/mail_mcp/` as reference until parity, then archive | delete immediately |

---

## Implementation plan (after validation)

| Phase | Content | Gate |
|-------|---------|------|
| **P0** | Skeleton: `pyproject.toml`, `Makefile`, `.gitignore`, `.env.example`, package dirs | `make check` runs (empty) |
| **P1** | Core: `config.py`, `exceptions.py`, `logger.py`, `display.py`, `doc.py`, `models.py`, `api/models.py` | import OK |
| **P2** | Transport: `api/imap.py` + `api/smtp.py` (port of mail_mcp core) | unit tests green |
| **P3** | `hitl.py` + templates + `admin.py` (setup, status, reset, purge) | admin tests green |
| **P4** | `actions/base.py` + `registry.py` + `cli.py` + the 24 actions | `do --help` + registry = 24, `make smoke` green |
| **P5** | Verification engine (`@require_verification` on 8 declared writes) + preflight/absence polls | handler verification tests green |
| **P6** | `raw` gateway (dedicated imaplib connection) | raw unit coverage |
| **P7** | Tests: registry (24, ≥3 examples), models, config, admin, search, MIME, preflight/verification | `make check` fully green |
| **P8** | Docs + ecosystem switch (`opencode.jsonc`, `k-mail` skill, mail-mcp archive) | agents use `mail-proxy` end-to-end |

---

## Status

- See `AGENTS.md` for the agent working context.
- See `TODO.md` for the live task list.
- See `CHANGELOG.md` for version history.
- See `README.md` for user-facing documentation.

*Architecture contract drafted 2026-08-12 — refonte of `mail-mcp` v0.2.4 (25 MCP tools) into
`mail-proxy` (24 RPC actions), modelled on `tick-proxy` v2.1.1.*
