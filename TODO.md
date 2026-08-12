# TODO

## 🔴 GATE — validation required before live use

The decisions in `CONTRACT.md` → *Decisions requiring KπX validation* must be answered first.
Summary of what is being asked:

- [ ] **D1** — Action naming flipped to domain-first kebab (`message-send`, not `send-message`)
- [ ] **D2** — `mail_guide` dropped in favour of docstring-driven `do --help` (≥3 examples each)
- [ ] **D3** — 4 admin surfaces (CLI + HTTP + Telegram + SSH) folded into ONE
       `admin setup|status|reset|purge`
- [ ] **D4** — `config.yaml` + in-package `.env` dropped (account defaults in `config.py`,
       secrets in `~/.config/mail-proxy/.env`)
- [ ] **D5** — Env prefix `MAIL_*` (harmonizes with `TG_*`/`TICK_*`)
- [ ] **D6** — HTTP transport + Telegram bot + daemon + Docker dropped
- [ ] **D7** — HITL scope: compose + drafts + irreversible deletes + `raw` + admin secrets;
       reversible moves/marks run without HITL but with mandatory read-back verification
- [ ] **D8** — `raw` on a dedicated imaplib connection (isolation from imapclient state)
- [ ] **D9** — Account catalog as documented `AccountDef`s in `config.py` + `MAIL_<ID>_*` secrets
- [ ] **D10** — `~/Work/AI/MCPs/mail_mcp/` kept as reference until parity, then archived

## Done (design + implementation phase)

- [x] Exhaustive analysis of `tick-proxy` ADN — `cli.py` (Typer `do`/`admin`, `_make_rpc`,
      autosave, meta options), `actions/` (registry, `ActionDef`, decorators), `client.py`,
      `config.py` (`.env` loader), `doc.py`, `display.py`, `logger.py`, `exceptions.py`,
      `hitl.py`, `admin.py`, `Makefile`, `pyproject.toml`, tests
- [x] Exhaustive analysis of `mail-mcp` — 25 tools across `tools/{read,compose,manage,guide}.py`,
      `core/{imap_client,smtp_client,models}.py`, `admin/`, `config.py` + `config.yaml`,
      `http_app.py`, `daemon.py`, tests
- [x] Complete 25 → 24 action mapping with coverage proof (zero gaps)
- [x] `CONTRACT.md` — architecture contract (incl. "What differs from tick-proxy")
- [x] `AGENTS.md` — agent working context
- [x] `README.md` — user-facing documentation
- [x] `CHANGELOG.md` — 0.1.0 entry
- [x] `TODO.md` — this file
- [x] Implementation P0–P7 complete: core, transport, HITL + admin, 24 actions, verification
      engine, `raw`, 90 tests — `make check` green, smoke green

## Remaining (P8 — live use + ecosystem switch)

### P8 — Docs + ecosystem switch
- [ ] Live smoke against the real Polytechnique account (read-only first: `inbox-check`,
      `folder-list`, `message-info`) once credentials are configured via `admin setup`
- [ ] Remove `mcp.mail_fallback` from `~/.config/opencode/opencode.jsonc`
- [ ] Rewrite `k-mail` skill: `allowed-tools` → `Bash(mail-proxy *)`, re-point the account map
      and tool list to the 24 `do` actions
- [ ] Confirm nothing else consumes `https://mail.kpihx-labs.com/mcp` before dropping it
- [ ] Archive `~/Work/AI/MCPs/mail_mcp/` once parity is proven
- [ ] `make release` (after KπX validates D1–D10)
- [ ] Add the `work` account to `config.py` + `.env.example` when the second account is ready

## Open questions (non-blocking)

- [ ] Should `raw` autosave include the request as well as the response? *(useful for auditing)*
- [ ] Shell completions — `tg-proxy`/`tick-proxy` deliberately disable them
       (`add_completion=False`). Keep that stance for ADN fidelity, or enable Typer's
       `--install-completion` so the 24 action names become tab-completable?
- [ ] Bounce probe (`verify_bounce_window_seconds`) — keep as payload field or move to a
       dedicated `message-bounce-check` action?
