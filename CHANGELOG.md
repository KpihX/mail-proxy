# Changelog

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
