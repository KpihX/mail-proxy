# Changelog

## 0.3.10 — 2026-09-03

- **Fix:** `message-mark` verification for `flagged:false`/`seen:false`/etc. now correctly reports `ok: true` when the flag is removed. Previously, both `expected` and `observed` contained the flag, producing false negatives.

## 0.3.9 — 2026-09-03

- **Fix:** `label-set` now tries custom keywords FIRST and only falls back to `\Flagged` if verification shows they were silently dropped. This fixes Gmail (and Zimbra non-Junk folders) which support custom keywords but don't advertise `\*` in `PERMANENTFLAGS`.

## 0.3.8 — 2026-09-03

- **New action: `label-delete`** — removes keyword labels from ALL messages in a folder that carry them. Scans last 500 messages, finds matching UIDs, removes labels, verifies absence.
- **`label-list` now reports `custom_keywords_supported`** — detects whether the server supports custom IMAP keywords via PERMANENTFLAGS `\*` wildcard. Hotmail/Outlook returns `false`.
- **`\\Recent` excluded** from custom keyword filter in `label-list` (v0.3.7).
- **`label-list` scans message flags** (last 50 messages) in addition to PERMANENTFLAGS (fixes Zimbra tags not appearing).

## 0.3.7 — 2026-09-03

- **Fix:** `label-list` now scans actual message flags in addition to `PERMANENTFLAGS` to discover keywords in use (fixes Zimbra tags not appearing).

## 0.3.6 — 2026-09-03

- **Fix:** IMAP UIDs are strictly folder-scoped and change upon moving. `message-move`, `message-archive`, `message-trash`, and `message-spam` verifications now correctly verify absence from the source folder rather than incorrectly attempting to find the old UIDs in the destination folder.

## 0.3.5 — 2026-09-03

- **Fix:** Support localized folder names for Archive, Trash, and Spam moves (`Deleted`, `[Gmail]/Tous les messages`, `[Gmail]/Corbeille`).

## 0.3.4 — 2026-09-03

- **Reply attachments:** `message-reply` now accepts `attachments`, passes them
  through SMTP, and includes them in the Sent-folder copy. Added MIME coverage.

## 0.3.3 — 2026-09-03

- **Fix:** `message-list` correctly detects uppercase IMAP `BODYSTRUCTURE`
  attachment dispositions. The patch is released as a new local tool version so
  `uv tool install --force` cannot reuse the prior 0.3.2 build cache.

## 0.3.2 — 2026-09-03

- **Correction:** the account-scoped attachment default belongs under
  `~/Downloads/Mail-Proxy/<account-id>/`, never directly under `$HOME`.
- **Correction:** `message-list` now recognizes uppercase IMAP
  `BODYSTRUCTURE` `ATTACHMENT` dispositions.

## 0.3.1 — 2026-09-03

- **Attachment downloads are account-scoped by default:** omitting `save_path`
  now writes to `~/Mail-Proxy/<account-id>/<filename>` and creates that directory
  on demand. A trailing-slash `save_path` is an explicit destination directory;
  an explicit file path still supports deliberate renaming. Added three regression
  tests for default, directory, and file destinations.

## 0.3.0 — 2026-09-02

- **Reply/forward HITL now shows the original message and an adjustable
  default subject** — the previous form left "To" and "Subject" blank with
  no indication of what they would become, because `message-reply`/
  `message-forward` compute both fields AFTER approval and never carry
  them in their own payload. Fixed by hooking into the SAME centralized
  UID resolution `cli.py::_inject_uid_resolution` already uses for
  move/archive/trash/spam/delete/mark: `hitl.py::_render()` now reads the
  existing `_uid_resolution` entry to render a "↩ Replying to" / "➡
  Forwarding" card (sender, subject, date, folder) and computes a proper
  default subject ("Re: "/"Fwd: ", never double-prefixed).
- **New `subject_override` field on `MessageReplyPayload` and
  `MessageForwardPayload`** — the reviewer's edit to the (now pre-filled)
  Subject field is no longer silently dropped by Pydantic (subject isn't a
  real field on these payloads); it round-trips through
  `subject_override` and replaces the auto Re:/Fwd: computation end-to-end,
  including the Sent-copy save. `SMTPClient.reply()`/`.forward()` accept
  the same parameter.
- 12 new regression tests (SMTP subject_override behavior + HITL rendering
  for reply/forward/send). 201 tests green.

## 0.2.9 — 2026-09-02

- **SMTP bare line-feed fix:** Outlook/Hotmail deliveries to strict servers
  (Zimbra `mx-b.polytechnique.fr`) were rejected with
  `SMTPSEND.BareLinefeedsAreIllegal` because `MIMEMultipart.as_bytes()`
  leaked bare `\n` into HTML bodies. Added `_normalize_crlf()` before every
  `sendmail()` call — idempotent CRLF normalization that eliminates the
  rejection while keeping all other providers (Gmail, Outlook) unaffected.
  Verified live: 12-message cross-account matrix (Gmail↔Hotmail↔Zimbra,
  text-only + HTML+Text) — all 12 delivered.
- **SMTP custom-account prompt parity:** `MailClient.smtp()` now calls
  `_prompt_and_cache()` for custom accounts with no cached password, matching
  the existing `imap()` behavior. Previously, a Zimbra send after keyring
  expiry silently sent an empty password (rejected by the server); now it
  prompts interactively like every other IMAP operation.
- **HITL account badge:** both review templates (`hitl.html` and
  `message-review.html`) display the `account_id` prominently at the top,
  so reviewers always know which account they are approving.
- **HITL HTML body:** `message-review.html` now renders the `body_html`
  field with a live preview. Previously, `body_html` was silently ignored
  in the review UI and lost on approval when the reviewer cleared the
  (invisible) field.
- **`message-info` body_html exposed:** the action now returns `body_html`
  in its JSON output — previously only `body_text` was serialized, hiding
  the HTML part from CLI consumers.

## 0.2.8 — 2026-09-02

- **Non-ASCII search now works on every provider (Gmail, Outlook, Zimbra).**
  Any accented / non-Latin search term (`query`, `sender`, `subject_filter`,
  `to_filter`, `cc_filter`, `keyword`) previously either crashed with
  `UnicodeEncodeError` or was rejected by the server
  (`BAD [Could not parse command]` on Gmail, `parse error: expected ')'` on
  Zimbra). Two root causes, both fixed in mail-proxy — **no monkeypatching of
  imapclient**:

  1. **Terms are pre-encoded to UTF-8 bytes** (`_search_term`). `imapclient`'s
     `to_bytes()` defaults to `us-ascii` and the charset is not always
     forwarded (it is dropped when recursing into nested criteria, and the
     Outlook `BADCHARSET` retry passes no charset at all), so a `str` term
     raised `UnicodeEncodeError`. Bytes pass through untouched, so the charset
     no longer matters. This is also what makes `IMAPClient._raw_command`
     send the value as an RFC 3501 **literal** (`{n}` / `{n+}`) — the only
     wire form that reliably carries UTF-8: Gmail rejects non-ASCII
     quoted-strings and advertises neither `ENABLE` (RFC 6855 `UTF8=ACCEPT`)
     nor `LITERAL+`, and imapclient transparently handles both the `LITERAL+`
     fast path and the plain-`{n}` continuation handshake.
  2. **Non-ASCII free-text queries never use nested criteria.** imapclient
     builds the nested `OR` form by appending the closing paren onto the last
     element (`inner[-1] + b")"`), which returns plain `bytes` and drops the
     `_quoted` wrapper — so the quotes *and* the paren end up **inside** the
     literal payload, corrupting the command. `search()` now splits such a
     query into two FLAT searches (`SUBJECT`, then `BODY`) and unions the
     UIDs. ASCII queries keep the single-round-trip nested `OR`.

  ASCII behavior is provably unchanged (all pre-existing `test_search.py`
  assertions pass untouched). 10 new regression tests cover flat/nested
  encoding, the split, UID union + de-duplication, limit handling,
  combination with other filters, and the Outlook `BADCHARSET` retry path.

## 0.2.7 — 2026-09-02

- **Unicode mailbox negotiation:** after IMAP authentication, activate
  `UTF8=ACCEPT` only when the server advertises and accepts it; otherwise keep
  RFC modified UTF-7. All mailbox selection paths now use one negotiated
  transport helper, so UTF-8 folder names behave consistently for reads and
  writes.
- **Semantic Sent resolution:** sent-copy operations resolve the server's
  special-use `\\Sent` folder instead of guessing localized names.

## 0.2.6 — 2026-09-02

- **Outlook IMAP search compatibility:** on a server-declared `BADCHARSET`
  response to `SEARCH CHARSET UTF-8`, retry once without an explicit charset.
  All other IMAP errors retain their normal failure path; this restores Hotmail
  `ALL` and `UNSEEN` listing without weakening error visibility.

## 0.2.5 — 2026-09-02

- **Message-move HITL:** `message-move`, `message-archive`, `message-trash`, and
  `message-spam` now require centralized browser approval before their already-mandatory
  read-back verification. `message-mark` and `label-set` remain direct reversible writes.

## 0.2.4 — 2026-09-02

- **IMAP transport hardening:** `IMAPClient.connect()` now translates socket-open and
  authentication-stage failures independently: timeout, DNS, TLS, refused connection, generic
  network I/O, server abort, protocol error, and rejected credentials all become actionable
  `MailAPIError`s. `admin auth status` therefore returns a per-account probe error rather than
  leaking a traceback when a server times out during `LOGIN`.
- **Validation runner:** `make check` now runs through `uv run --all-groups`, making the declared
  `dev` dependencies (ruff, pyright, pytest) consistently available to every quality subcommand.

## 0.2.3 — 2026-09-02

- **`admin auth default` UX:** the default account is now selected explicitly with required
  `-a` / `--account <id|alias|email-prefix>`. The HITL confirmation is bound to that selection;
  the reviewed payload cannot redirect the change. Omitting `-a`/`--account` now returns Typer's
  actionable usage error instead of a dead JSON instruction for `account_id`.

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
