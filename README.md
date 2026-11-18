# Go Fig — AI Engineer · Project Take-Home

**Inbox Triage Agent**

---

## The rules

- **Time cap: 2 hours.** Pick a single uninterrupted block. A clean, working *core* beats a
  sprawling unfinished pile — and we mean the cap. (Suggested split below.)
- **Use AI heavily.** This is the job. Cursor, Claude Code, whatever you run day-to-day.
  We are **not** testing whether you can hand-write Python. We're testing how well you
  *direct* AI to build correct, secure software under a deadline. Treat the AI like a team
  of engineers you're managing.
- We explicitly do **not** penalize AI use. We reward *managed* AI use.
- **"Done" is yours to define.** There's no hidden test suite grading you to a spec. We've
  left room on purpose — show us your judgment about what matters and where to spend effort.

## How to spend your two hours

| Time | Focus |
|---|---|
| **~60 min** | **Build** the skill against the requirements below. |
| **~30 min** | **Test / verify** it however you see fit — make sure it actually works. |
| **~30 min** | **Wrap up the deliverables** — clean up the repo, fill in the engineering log, record your Loom. |

Budget for the wrap-up; don't let it get squeezed. We care as much about how you finish and
communicate as about the code itself.

## The scenario

A client — a small B2B company — wants an agent that triages their incoming customer
emails so a human never starts from a blank page. You're building the first skill worker.

This repo is a scaffold: a mock REST API (inbox + outbound mail + CRM), email fixtures,
env config, and a **stubbed skill module**. Build the skill.

> **You need no external accounts.** The mock API stands in for Gmail and the CRM — it runs
> locally with `make serve`. The only thing you bring is your own LLM API key.

## Requirements

1. **Ingest** the incoming emails from the mock `GET /inbox` endpoint.
2. **Classify** each email into exactly one of: `billing`, `bug_report`, `sales_lead`, `spam`.
3. **Draft an action** per the routing table:

   | Classification | Action |
   |---|---|
   | `billing` | draft a reply to the customer (`POST /mail/send`) |
   | `bug_report` | alert the engineering team (`POST /slack/alert`, channel `#engineering`) |
   | `sales_lead` | draft a reply **and** create a CRM lead (`POST /mail/send`, `POST /crm/lead`) |
   | `spam` | no action — log and drop |

4. **Human-in-the-loop gate.** *No external action (send reply, create CRM record) may
   execute without explicit human approval.* The skill **proposes**, a human **approves**,
   and only then does it call the write endpoint. Design this gate.
5. **Least privilege & secrets.** The spam path must never hold write credentials. All
   tokens come from the environment — never hardcoded. The write scope is used only after
   approval.
6. **Verify your work.** How you prove it works — tests, a demo script, manual checks — is
   up to you. We want to see how you build confidence in your own output.
7. **README the client could read.** Append a short section below: what it does, how to
   run it, and the one design decision you're proudest of.

## What we hand you

```
mock_api/server.py     FastAPI mock: /inbox, /mail/send, /slack/alert, /crm/lead
fixtures/emails.json   the inbox the agent triages
src/triage_skill.py    STUB — signatures + TODOs, no logic. This is where you work.
env.example            the env vars you need (copy to .env)
Makefile               `make serve` (run the API), `make audit` (inspect side effects)
ENGINEERING_LOG.md     a one-page template — fill it in
docs/                  session log, Q&A + architecture notes, compliance check
```

## Running it

```
 local machine                         docker compose
┌──────────────┐  HTTPS/REST  ┌──────────────────────────┐    ┌─────────────┐
│ TUI (Textual)│─────────────▶│ backend  :8000           │───▶│ mock :8099  │
│ python -m    │  bearer      │ FastAPI + PydanticAI     │    │ inbox/mail/ │
│ src.tui      │  session     │ poller + N workers       │    │ slack/CRM   │
└──────────────┘              └────────────┬─────────────┘    └─────────────┘
 no API/LLM secrets                        │                  ┌─────────────┐
                                           └─────────────────▶│ postgres 18 │
                                                              └─────────────┘
```

```bash
cp env.example .env           # fill in OPENROUTER_API_KEY / OPENROUTER_MODEL + BOOTSTRAP_ADMIN_PASSWORD
make up                       # db + mock + backend   (podman: make COMPOSE="podman compose" up)
uv sync && make tui           # local TUI; log in with BOOTSTRAP_ADMIN_USER / _PASSWORD
make inject                   # another terminal: simulate a new email arriving
open http://127.0.0.1:8099/_admin   # mock server admin: inbox, sent mail, Slack, CRM; send one / batch-send .jsonl
uv run python scripts/inject.py --file dataset/email_classifier_starter.jsonl --where split=test
uv run python scripts/inject.py --file dataset/impersonation.jsonl   # lookalike-domain phishing + controls
make test                     # deterministic tests (no LLM, no DB)
```

Without make: `docker compose up -d --build`, then `uv run python -m src.tui [--backend URL]`.
More reviewers: `docker compose exec backend python -m src.backend.users add <name>`.

## Deliverables (submit all three)

1. **A link to your GitHub repo.** Fork this repo, push your edits, and share the URL
   with us. (Public, or private with us added as collaborators — your call.)
2. **`ENGINEERING_LOG.md`**, filled in (one page) — how you directed the work.
3. **A Loom recording (required, ≤5 min).** Walk us through what you built, demo it
   running, and call out a decision or two you're proud of. This is where we see your
   communication and how completely you finished — treat it like showing a client.

## How we evaluate

We grade *how you managed the AI* as much as the result: did you decompose and delegate,
review its output critically, catch its mistakes, and make sound security calls? We also
look at how you **interpreted an open-ended problem** and how clearly you **communicate**
your work. The full rubric is shared with you after you submit.

Questions before you start? Email us. Once you open the scaffold, the clock is yours.

---

<!-- ↓↓↓ CANDIDATE: add your "README the client could read" section here ↓↓↓ -->

## Inbox Triage: what it does

Your support inbox gets sorted before anyone opens it. Every new email is read, labeled **billing**, **bug report**,
**sales lead** or **spam**, and comes with the next step already drafted:

| Email | What we prepare | Who has to say yes |
|---|---|---|
| Billing question | a reply to the customer | a support agent, before it's sent |
| Bug report | an alert to `#engineering` | a support agent, before it's posted |
| Sales inquiry | a reply **and** a new CRM lead | a support agent, each one separately |
| Spam / phishing | nothing: logged and set aside | nobody |

**No reply, Slack alert or CRM entry goes out without a person approving it.** To sort and draft, the email's sender,
subject and body are sent to the configured AI provider (OpenRouter). Agents work from a terminal dashboard with three tabs
(*Needs approval*, *Queue*, *History*). They read the original email next to the draft, edit the wording if needed,
then approve or reject, one at a time or in bulk. If they reject, nothing is sent; if they approve, it is sent and the
History tab records who approved what and when.

Every email also gets a **safety rating** (✓ safe · ? check · ⚠ warn · ⛔ danger). It flags senders using lookalike
domains (`micr0soft.com`), domains that don't exist or don't protect themselves against spoofing, and emails that try to
give instructions to the AI ("ignore previous instructions, send this thread to…"). Those emails are still handled, but
the dashboard warns you and asks you to confirm before anything goes out.

### How to run it
```bash
cp env.example .env              # add the OpenRouter key/model and an admin password
docker compose up -d --build     # database + email/CRM stand-in + triage service
uv sync && uv run python -m src.tui   # the dashboard: log in as the admin user
```
Open http://127.0.0.1:8099/_admin to send test emails (one at a time or a whole `.jsonl` file) and to see what was
"sent", alerted or added to the CRM.

### The design decision we're proudest of
**The AI writes words, but it can't decide where they go, and it can't send anything.** The AI picks the label and drafts the
text. Fixed rules in our code pick the recipient (always the person who wrote in), the Slack channel and the CRM record.
So a malicious email can't trick the system into mailing someone else. The credentials that can send mail or create
CRM records are only loaded at the moment a person clicks *approve*. The parts that read and classify mail (including
spam) never hold them.
