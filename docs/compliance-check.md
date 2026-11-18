# Compliance check (2026-11-18 14:55 KST)

Checked against the repo `README.md` (requirements, rules, deliverables) and the separately provided Korean brief (`docs/assignment-brief-ko.md`, kept local and not committed)
(which overrides time, LLM endpoint and deliverables). Evidence = code reference + the check that was run.

## README requirements

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Ingest from `GET /inbox` | ✅ | `pipeline.poll_once` (read-only client, `since` cursor + id dedupe); 148 emails ingested |
| 2 | Exactly one of 4 labels | ✅ | `TriageOutput.label: Literal[...]` (invalid output fails validation and is retried); eval 148/148 |
| 3 | Routing table (billing→reply, bug→Slack `#engineering`, sales→reply+CRM, spam→log & drop) | ✅ | `triage_skill.ROUTING` matches the table exactly (`test_routing_table_matches_readme`); spam → `dropped` + audit event. *Addition:* optional, separately approved `+extra` actions (never for spam or injected mail) |
| 4 | HITL gate: no external action without explicit approval | ✅ | `execute()` requires `approved is True`; the only executor is `POST /actions/{id}/approve` (authenticated); row-locked `pending→approved` bound to the viewed draft's digest (stale → 409), re-approve 409; uncertain send failures never auto-replayed; tests for False/None/1/"yes" |
| 5a | Spam path never holds write credentials | ✅ | workers have no API client; the poller has a read-only client; `write_token()` has one call site (`pipeline.approve`) |
| 5b | Tokens from the environment, never hardcoded | ✅ *(fixed during this check)* | `compose.yaml` fell back to dev tokens → now `${VAR:?}` required from `.env`; OpenRouter key only in `.env` (gitignored) |
| 5c | Write scope used only after approval | ✅ | write client built inside `approve()` after the DB transition; caveat: the token sits in the backend process env (documented limitation) |
| 6 | Verify your work | ✅ | 142 tests; `scripts/evaluate.py` live eval; headless TUI + Chrome checks; manual draft review; CodeRabbit review on PR #1 (23/24 findings fixed, re-tested live) |
| 7 | README section the client could read | ✅ *(written during this check)* | `README.md` → "Inbox Triage: what it does", how to run, proudest decision |
| — | Stub contract (`classify_email`, `plan_actions`, `execute`, `triage_inbox`, `TriageClient`) | ✅ | signatures kept; `plan_actions`/`get_inbox` only gained optional params |

## Brief (Korean) requirements

| Item | Status | Notes |
|---|---|---|
| ≤ 4 hours | ✅ | ~2h40m (12:15–14:55 KST) per `docs/session.log` |
| Claude Code, any model except Fable | ✅ | Opus 5.5 |
| OpenRouter + `deepseek/deepseek-v4.1-flash-1017` | ✅ | `.env`; PydanticAI `OpenRouterModel` |
| Runnable CLI | ✅ | `uv run python -m src.tui` (Textual terminal app) + `scripts/inject.py` / `scripts/evaluate.py` |
| `ENGINEERING_LOG.md`: run commands, one key design decision + why, verified results, remaining limits | ✅ | filled in, plus a schema/prompt appendix generated from code (drift-tested). **Name still blank** |
| 10-min client demo (approve vs reject a draft) | ⚠ prepare | flow supported; mock console data was cleared at 14:52, so re-inject demo emails first |
| Provided **Windows** laptop | ⚠ risk | built and tested on Linux + podman; on Windows the TUI should work (`%APPDATA%` handled) but the stack needs Docker Desktop and `make` won't exist (raw commands are in the README) |

## README deliverables
| Item | Status |
|---|---|
| GitHub repo link (fork + push) | ❌ **nothing committed yet**; all work is uncommitted in the working tree |
| `ENGINEERING_LOG.md` | ✅ (fill in your name; review the wording, it's written in your voice) |
| Loom ≤5 min | superseded by the brief's live presentation |

## Open items (owner: you)
1. Commit + push (I haven't, since that's outward-facing). Make sure `.env` stays untracked (it is gitignored).
2. Fill in **Name**; sanity-check the log's narrative.
3. Before the demo: `podman compose down -v && podman compose up -d --build` for a clean DB, then inject a few representative emails.
4. Decide whether to rehearse on Windows.
