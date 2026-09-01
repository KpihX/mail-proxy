# Changelog

## 0.2.0 — 2026-09-01

- **Multi-account architecture:** accounts moved from hardcoded `AccountDef` list in `config.py`
  to dynamic `~/.config/mail-proxy/accounts.json`. Any number of accounts per provider, resolved
  by id → alias → email prefix. IMAP/SMTP endpoints auto-detected from email domain via
  `EMAIL_PROVIDER_DEFAULTS` (gmail.com, outlook.com, hotmail.com, live.com, polytechnique.edu).
- **OAuth2 support:** Microsoft (Device Code Flow — no redirect URI, Thunderbird well-known
  client ID) + Google (Authorization Code Flow). XOAUTH2 for IMAP (`_imap.authenticate`) and
  SMTP (`AUTH XOAUTH2`). Token store at `~/.config/mail-proxy/tokens/<id>.json` with auto-refresh.
  App Password remains as fallback for all providers.
- **Smart HITL form:** dedicated `auth_login.html` template with provider type selector (Gmail /
  Outlook / Zimbra / Custom), auth method selector (App Password / OAuth2), auto-fill from
  email domain, existing accounts display chips.
- **Admin commands reworked:** `doctor` (auto-fix permissions), `status` (unified: accounts +
  auth state + permissions + IMAP/SMTP probes + issues), `auth login|status|logout` (unified
  account management — writes both `accounts.json` + `.env` atomically). Removed `setup` (replaced
  by `auth login`).
- **Validation before write:** `auth login` validates email domain + endpoint resolution BEFORE
  any disk write — prevents corrupted `accounts.json` from bad entries.
- **Secrets-only .env:** login IS the email address from `accounts.json` — `.env` carries only
  `MAIL_<ID>_PASS` per account. No more `MAIL_*_LOGIN`.
- **Personal data purge:** zero personal emails/names in `src/` or `tests/`. All examples use
  generic placeholders.
- **IMAP XOAUTH2 fix:** `imaplib.authenticate()` expects raw bytes (not base64). Fixed to pass
  raw XOAUTH2 string (`user=<email>\x01auth=Bearer <token>\x01\x01`) instead of base64-encoded.
- **Error message cleanup:** removed stale `MAIL_*_LOGIN` references from error messages.
- **Quality gate:** `make check` green — 127 tests (was 102), ruff clean, pyright 0 errors.

## 0.1.1 — 2026-08-15

- **Repo move:** project directory relocated to `$HOME/KpihX-Labs/proxies/mail-proxy/`
  (renamed from `mail_proxy`) to follow the `xxx-yyy` root-dir naming convention. Package
  stays `mail_proxy` (underscores, importable); config stays at `~/.config/mail-proxy/`.
- **Transport hardening (`api/imap.py`):** `_guard_imap` decorator translates every raw
  imapclient/socket failure into a clean `MailAPIError` across the public `IMAPClient`
  methods (no stack traces, no credential leakage). `message_exists` now re-selects the
  folder before the UID probe so a moved UID is not reported absent from its new home.
- **HITL tests (`test_hitl.py`):** 5 live local HTTP round-trips against the real review
  server (approve, reject, unknown id 404, timeout) — no browser, no mocking of the server.
- **Quality gate:** `make check` green — now 95 tests (was 90), ruff clean, pyright 0 errors.

## 0.1.0 — 2026-08-12

- Full refonte of `mail-mcp` v0.2.4 (25 MCP tools) into a non-MCP CLI on the exact `tick-proxy`
  ADN: single binary, `do` + `admin` namespaces, `meta`+`data` envelope, docstring-driven
  `--help`, HITL web UI (free port, 600 s fail-closed), autosave to `/tmp/mail-proxy-autosave/`,
  registry-based actions (24 actions, duplicate names raise at import).
- **24 flat `do` actions** — domain-first kebab naming, Pydantic payloads colocated with their
  handlers. Coverage proof: all 25 mail-mcp tools consumed (23 renamed 1:1, `find_unread` merged
  into `message-list`, `mail_guide` folded into docstrings), +1 new `raw` escape hatch.
- **Docstring rule (KπX 2026-08-12):** every action carries ≥3 real `→` examples (raw ≥5);
  enforced by the registry test suite.
- **HITL policy:** `message-send`/`reply`/`forward`/`draft` (external side effects / content
  writes), `message-delete` + `folder-delete` (irreversible — preflight + locked identity +
  absence poll) and `raw` always require human approval.
- **Verification model:** `@require_verification` structural decorator on the 8 declared writes
  (move, archive, trash, spam, mark, label-set, message-delete, folder-delete) — proof lands in
  `data.verification`, never in `meta`. ALL-UID semantics: a silent partial failure fails the check.
- **`admin` surface:** `setup` (HITL form → `~/.config/mail-proxy/.env`, chmod 600),
  `status` (masked credentials + live IMAP/SMTP probes + permissions), `reset`, `purge`.
  The mail-mcp triple admin surface (CLI + HTTP routes + Telegram bot) and the daemon are dropped.
- **Config:** single `.env` at `~/.config/mail-proxy/.env`; accounts are documented `AccountDef`s
  in `config.py` with `MAIL_<ID>_*` secrets and endpoint overrides. `config.yaml` and the
  in-package `.env` are dropped. Env prefix `MAIL_*`.
- **Transport:** `api/imap.py` (imapclient wrapper — search, fetch, flags, move, delete,
  keywords, downloads) and `api/smtp.py` (smtplib — send, reply, forward, draft bytes with
  signature logo) ported from mail-mcp core with `MailAPIError` translation (no stack traces,
  no credential leakage).
- **`raw` escape hatch:** arbitrary IMAP commands (`STATUS`, `UID FETCH`, `NAMESPACE`, …) on a
  dedicated imaplib connection — the shared imapclient state machine can never be corrupted.
- **Quality gate:** `make check` green — 90 tests (registry integrity incl. ≥3-examples rule,
  models, config, admin, search criteria, MIME building, handler verification flows with fake
  IMAP), ruff clean, pyright 0 errors, CLI smoke. Docker explicitly excluded.
- **Ecosystem:** mail-mcp kept as reference at `~/Work/AI/MCPs/mail_mcp/` until parity;
  opencode `mcp.mail_fallback` and the `k-mail` skill rewrite are tracked in `TODO.md`.

## 0.0.1 — 2026-08-12 (design)

- Skeleton project: `pyproject.toml`, `Makefile`, `.gitignore`, `.env.example` — exact
  tick-proxy shape.
