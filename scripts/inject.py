"""Simulate new mail arriving at the mock server (same as the /_admin page).

    uv run python scripts/inject.py                                   # one random sample
    uv run python scripts/inject.py --from a@b.com --subject Hi --body "..."
    uv run python scripts/inject.py --file dataset/email_classifier_starter.jsonl \
        --where split=test --limit 10                                 # .jsonl or .json array

Dataset rows may carry extra keys; the server uses from/subject/body (+ optional
id, received_at) and keeps `label` admin-only, never exposed on GET /inbox.
"""

import argparse
import json
import os
import random
from pathlib import Path

import httpx

SAMPLES = [
    {"from": "lee@acme-tools.com", "subject": "Refund for duplicate seat charge",
     "body": "We were charged for 11 seats but only have 10 users. Can you refund the difference?"},
    {"from": "ops@hollow-pine.io", "subject": "Login loop on SSO",
     "body": "Since yesterday, Okta SSO bounces us back to the login page endlessly. Affects all 30 users."},
    {"from": "mira@kestrel-health.com", "subject": "Enterprise plan for 200 seats?",
     "body": "We're evaluating vendors for Q1 and need about 200 seats with SSO. Can we book a call?"},
]


def load_rows(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=os.environ.get("MOCK_URL", "http://127.0.0.1:8099"))
    p.add_argument("--file", type=Path, help=".jsonl (one object per line) or .json array")
    p.add_argument("--where", help="keep rows where KEY=VALUE, e.g. split=test or is_prompt_injection=true")
    p.add_argument("--limit", type=int)
    p.add_argument("--from", dest="sender")
    p.add_argument("--subject", default="")
    p.add_argument("--body", default="")
    a = p.parse_args()

    if a.file:
        rows = load_rows(a.file)
    elif a.sender:
        rows = [{"from": a.sender, "subject": a.subject, "body": a.body}]
    else:
        rows = [random.choice(SAMPLES)]
    if a.where:
        key, value = (s.strip() for s in a.where.split("=", 1))
        # Compare as JSON text so booleans match "true"/"false" like the admin page.
        rows = [r for r in rows if (json.dumps(r.get(key)) if isinstance(r.get(key), bool) else str(r.get(key))) == value]
    if a.limit:
        rows = rows[: a.limit]
    if not rows:
        raise SystemExit("nothing to send")

    r = httpx.post(f"{a.url}/_inject/batch", json=rows, timeout=30)
    r.raise_for_status()
    out = r.json()
    print(f"injected {out['injected']}: {', '.join(out['ids'][:10])}{' …' if len(out['ids']) > 10 else ''}")


if __name__ == "__main__":
    main()
