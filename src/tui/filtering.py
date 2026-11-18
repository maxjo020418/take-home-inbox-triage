"""Search/filter for the dashboard lists.

Query syntax (case-insensitive, all terms must match):
    invoice                 substring anywhere (id, sender, subject, label, action, status, domain, preview)
    label:billing           field match; fields: id from subject label action status domain to safety
    safety:warn             by rating (safe/check/warn/danger) or flagged check (injection/impersonation/domain)
    domain:imp              prefix-friendly: matches "impersonation_suspected"
    -spam  /  -label:spam   exclude
    "two words"             quoted phrase
"""

from __future__ import annotations

import shlex
from collections.abc import Callable

from src.tui.safety import assess

FIELDS = ("id", "from", "subject", "label", "action", "status", "domain", "to", "safety")


def searchable(row: dict) -> dict[str, str]:
    """Normalize a dashboard row (any tab) into the fields a query can address."""
    payload = row.get("payload") or {}
    actions = row.get("actions") or []
    fields = {
        "id": row.get("email_id") or "",
        "from": row.get("sender") or "",
        "subject": row.get("subject") or "",
        "label": row.get("label") or "",
        "action": " ".join(filter(None, [row.get("kind") or "", "extra" if row.get("extra") else "",
                                         *(a.get("kind", "") + (" extra" if a.get("extra") else "") for a in actions)])),
        "status": " ".join(filter(None, [row.get("status") or ("failed" if row.get("row_type") == "failed" else "pending"),
                                         *(a.get("status", "") for a in actions)])),
        "domain": row.get("domain_verdict") or "",
        "to": payload.get("to") or payload.get("channel") or payload.get("email") or "",
    }
    sf = assess(row)  # safety:warn, safety:injection, safety:danger, safety:impersonation ...
    fields["safety"] = " ".join([sf.word, *(c.name for c in sf.checks if c.rank > 0)])
    preview = payload.get("body") or payload.get("message") or payload.get("summary") or ""
    fields["_all"] = " ".join([*fields.values(), preview, row.get("reason") or row.get("rationale") or ""])
    return {k: v.lower() for k, v in fields.items()}


def compile_query(query: str) -> Callable[[dict], bool]:
    query = query.strip()
    if not query:
        return lambda row: True
    try:
        tokens = shlex.split(query)
    except ValueError:  # unbalanced quote while typing: treat literally
        tokens = query.split()
    terms: list[tuple[bool, str, str]] = []  # (negate, field, needle)
    for tok in tokens:
        negate = tok.startswith("-") and len(tok) > 1
        tok = tok[1:] if negate else tok
        field, sep, needle = tok.partition(":")
        if sep and field.lower() in FIELDS:
            terms.append((negate, field.lower(), needle.lower()))
        else:
            terms.append((negate, "_all", tok.lower()))

    def match(row: dict) -> bool:
        f = searchable(row)
        return all((needle in f[field]) != negate for negate, field, needle in terms)

    return match
