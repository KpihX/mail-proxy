# mail-proxy — Agent Context

## Project

Non-MCP CLI proxy for IMAP/SMTP mail. The full `mail-mcp` catalog (25 MCP tools) refactored into
**24 flat `do` actions**, built on the exact ADN of `tick-proxy` (`$HOME/KpihX-Labs/tick_proxy`):
single binary, `do` + `admin` namespaces, `meta`+`data` envelope, docstring-driven `--help`,
HITL web UI, autosave. **No Docker, no MCP transport, no daemon.**

**Design reference: `CONTRACT.md` — read it before touching anything.**

> **Status:** 🟢 IMPLEMENTED. The registry contains 24 actions and `make check` is the mandatory
> quality gate. `CONTRACT.md` remains the architecture contract.

## Overview

```bash
mail-proxy do <action> [payload|file] [-o path] [-f json|table]   # 24 RPC actions
mail-proxy admin setup|status|reset|purge                        # ALWAYS JSON
```

## Key Files

| File | Role |
|------|------|
| `src/mail_proxy/cli.py` | ONE Typer app: `do` + `admin` sub-typers, built **from the registry** |
| `src/mail_proxy/client.py` | `MailClient` — resolved account + lazy IMAP connection + stateless SMTP |
| `src/mail_proxy/config.py` | `~/.config/mail-proxy/.env` loader + documented `ACCOUNTS` catalog + overrides |
| `src/mail_proxy/models.py` | SHARED types only: `Output`, `OutputMeta`, `Verification`, `Status` |
| `src/mail_proxy/doc.py` | Dynamic `--help` injection from docstrings |
| `src/mail_proxy/display.py` | Rich output helpers (`print_json`, `print_table`) |
| `src/mail_proxy/logger.py` | stderr logger — systemd/journald captures (tg-proxy ADN, no file) |
| `src/mail_proxy/exceptions.py` | `MailProxyError`, `MailAPIError` |
| `src/mail_proxy/hitl.py` | HITL web UI (free port, browser auto-open) |
| `src/mail_proxy/api/` | Low-level mail layer: `imap.py`, `smtp.py`, `models.py` (domain models) |
| `src/mail_proxy/actions/` | The 24 actions: `ActionDef` + colocated Pydantic payload + handler |
| `src/mail_proxy/actions/registry.py` | `name → ActionDef` map; duplicates raise at import |
| `src/mail_proxy/admin.py` | Single source of truth for admin logic (setup, status, reset, purge) |
| `CONTRACT.md` | Architecture contract + full 24-action catalog |

## Key Rules

- **stdout is pure JSON.** Logs, HITL prompts, progress → **stderr**. `mail-proxy do … | jq` must never break.
- **Never write secrets into the repo.** The only secret location is `~/.config/mail-proxy/.env`
  (chmod 600). `mail-mcp`'s in-package `src/mail_mcp/.env` is an anti-pattern that must not be reproduced.
- **Adding an action = adding ONE `ActionDef`.** Never register a command directly in `cli.py`.
- **The docstring IS the documentation.** Mandatory sections: description, `Parameters:`,
  `Examples:` with **≥3 real `→` outputs per action** (KπX rule 2026-08-12; ≥5 for `raw`).
  `doc.py` renders them into `--help`; there is no second doc surface.
- **Envelope always.** `{"meta":{"status","comment","edited"},"data":…}` — errors exit 1,
  admin misuse (`--format`/`-o`) exits 2.
- **Verification is not optional for the 8 declared writes** — `message-move`,
  `message-archive`/`trash`/`spam`, `message-mark`, `label-set`, `message-delete`,
  `folder-delete`. It is enforced by the **`@require_verification` decorator** on the handler —
  no flag, no bypass. There is no verification field in `meta`: only verified actions add a
  proof at `data.verification`.
- **No Docker, ever** in this repo (explicit KπX decision, same as `tick-proxy`).
- **Every HITL declaration is visible.** A handler requiring review must carry
  `@require_approval`; `action_def()` derives HITL policy from it. Never use `hitl=True` directly
  in a production action definition.
- **Irreversible HITL starts with a locked preflight.** Every delete carries
  `@require_preflight(check=..., identity_fields=...)`: it reads every destructive target before
  a review page can open, then rejects a reviewer-edited target identity. The approved write only
  acts on the preflighted resource; absent IDs never consume a HITL cycle.
- **Compose actions (send/reply/forward/draft) always require HITL** — they reach other people or
  write content to the mailbox. Reversible moves/flag/label changes run without HITL but their
  read-back verification is mandatory.

## Mail gotchas (silent failures — no error, data simply not applied)

| Operation | Gotcha | Correct approach |
|-----------|--------|------------------|
| Move/delete UIDs | a stale UID or folder race can leave part of the batch untouched (200-style silent partial failure) | `@require_verification` read-back on all target UIDs |
| Flag / keyword writes | partial application across UIDs is invisible in the response | verification requires the state on **EVERY** target UID |
| Raw IMAP commands | a raw command on the shared imapclient connection can corrupt its response parser | `raw` runs on a dedicated imaplib connection, closed right after |
| Sent copy / bounce probe | the Sent-copy append is best-effort — it must never fail the send | `except Exception` around the append; `saved_to_sent` reports honestly |
| Shell env vs file | `os.environ.setdefault` means an exported `MAIL_*` var wins over the file | document it; change the shell or the file, never both silently |

## Admin invariants

- `admin setup` writes ONLY `MAIL_<ID>_LOGIN` / `MAIL_<ID>_PASS` (+ optional overrides) to
  `~/.config/mail-proxy/.env` (chmod 600, dir 700). No password ever enters the repo.
- `admin status` probes IMAP (connect + login) and SMTP (connect + ehlo) with real connections;
  missing credentials skip the network and report the `admin setup` hint.
- `admin` never accepts `--format` or `--output-file` — exit 2 on misuse.
- `admin reset` clears the file; `admin purge` deletes the config dir and prints the
  `uv tool uninstall mail-proxy` hint (never uninstalls from within the running process).

## Commands

```bash
make check        # smoke + ruff check --fix + ruff format + py_compile + pyright + pytest
make smoke        # mail-proxy do --help + registry integrity (24 actions, 0 duplicates)
make uv-link      # editable install (dev)
make uv-install   # uv tool install . --force
make git-push     # push to github + gitlab
make release      # check → git-push → uv-publish
```

## Reference implementations

| Repo | Role |
|------|------|
| `$HOME/KpihX-Labs/tick_proxy/` | **ADN source** — CLI shape, `doc.py`, `hitl.py`, envelope, autosave, Makefile, registry |
| `$HOME/KpihX-Labs/tg_proxy/` | **ADN origin** — the tg-proxy model tick-proxy itself follows |
| `$HOME/Work/AI/MCPs/mail_mcp/` | **Content source** — 25 tools, IMAP/SMTP core, response shapes. Keep as reference until parity, then archive. |

## Evolution Rules

- New feature → update `TODO.md` first, propose before acting.
- Significant change → update `CONTRACT.md` + `AGENTS.md` + `README.md` + `CHANGELOG.md`.
- Breaking change → bump version in `pyproject.toml` + `CHANGELOG.md` entry.
- Destructive / architectural → **stop and confirm with KπX first**.
- `sudo` required → tmux ops pane, never a raw `sudo` in an agent shell.
- **Makefile is the standard task runner** — `make check`, `make push`, `make release`.
